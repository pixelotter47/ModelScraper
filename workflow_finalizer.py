"""Two-phase final/master publication with roll-forward recovery.

Platform-neutral safety kernel. Phase A stages and validates everything
inside the run directory; Phase B writes a durable commit witness and then
projects the live master and root compatibility views idempotently. A crash
can leave the old root view or a committed-but-not-fully-projected
generation - never a partial new file mistaken for success.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Callable, Mapping

from master_repository import (
    GenericMasterRepository,
    MasterProvenance,
    PreparedMasterRevision,
)
from storage_utils import canonical_json_bytes, sha256_bytes, sha256_file
from workflow_store import (
    PUBLICATION_STATES,
    GenericRunStore,
    RunContext,
    RunStoreError,
)
from workflow_types import (
    OutcomeStatus,
    StepName,
    StepOutcome,
    VerificationVerdict,
    utc_now_iso,
)


class WorkflowFinalizer:
    """Owns the publication state machine for one run generation."""

    def __init__(
        self,
        store: GenericRunStore,
        context: RunContext,
        repository: GenericMasterRepository,
        *,
        master_entry_builder: Callable[..., list[dict]],
        projections: Mapping[str, tuple[str, str]],
        shadow: bool = False,
        stop_token=None,
        logger=None,
        backup_callback: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.context = context
        self.repository = repository
        self.master_entry_builder = master_entry_builder
        # root filename -> (artifact logical name, mode: lines|bytes|joined)
        self.projections = dict(projections)
        self.shadow = shadow
        self.stop_token = stop_token
        self.logger = logger or (lambda _message: None)
        self.backup_callback = backup_callback

    # ------------------------------------------------------------------

    def _cancelled(self) -> bool:
        return bool(
            self.stop_token
            and callable(getattr(self.stop_token, "is_cancelled", None))
            and self.stop_token.is_cancelled()
        )

    def finalize(self, checkpoint_payload: Mapping) -> StepOutcome:
        """Called by the verifier once no transient unknown remains."""
        started_at = utc_now_iso()
        try:
            prepared = self._phase_a(checkpoint_payload)
        except RunStoreError as exc:
            return self._failed(started_at, exc.code, str(exc))
        except _PrepareCancelled:
            return self._cancelled_outcome(started_at)
        except Exception as exc:
            return self._failed(started_at, "prepare_failed", str(exc)[:200])
        if self.shadow:
            outcome = self._success(
                started_at,
                warnings=(
                    "shadow_mode_no_publication: staged only; no root or "
                    "master file changed",
                ),
            )
            self.store.record_step_attempt(self.context, outcome)
            return outcome
        if self._cancelled():
            return self._cancelled_outcome(started_at)
        try:
            return self._phase_b(started_at, prepared)
        except Exception as exc:
            if os.path.exists(self.context.commit_path):
                # The commit decision is durable: recovery is roll-forward
                # only and the committed generation stays authoritative.
                try:
                    self.store.set_recovery_required(self.context, True)
                except Exception:
                    pass
                return self._failed(
                    started_at,
                    "projection_failed",
                    str(exc)[:200],
                    resumable=True,
                )
            return self._failed(
                started_at,
                "commit_failed",
                str(exc)[:200],
                resumable=True,
            )

    # ------------------------------------------------------------------
    # Phase A

    def _phase_a(self, checkpoint_payload) -> PreparedMasterRevision:
        manifest = self.store.load_manifest(self.context)
        checkpoint = self.store.load_checkpoint(self.context)
        if checkpoint is None:
            raise RunStoreError("checkpoint_mismatch", "no checkpoint on disk")
        summary = checkpoint.get("summary", {})
        if summary.get("unattempted") or summary.get("unknown_transient"):
            raise RunStoreError(
                "transient_unknowns_remaining",
                "publication requires zero transient/unattempted candidates",
            )
        state = manifest.get("publication", {}).get("state", "none")
        witness_exists = os.path.exists(self.context.commit_path)
        if state in ("prepared", "committed", "projecting", "projected"):
            # Resume path: reuse the durable prepared journal; a new
            # transaction is only allowed before any commit witness.
            try:
                prepared = self._load_prepared()
                self.repository.validate_prepared(prepared)
                return prepared
            except (RunStoreError, OSError, ValueError, KeyError) as exc:
                if witness_exists or state != "prepared":
                    raise RunStoreError(
                        "projection_pending",
                        f"committed staging is unreadable: {exc}",
                    )
        if self._cancelled():
            raise _PrepareCancelled()
        if state == "none":
            self.store.set_publication_state(self.context, "preparing")
        records = dict(checkpoint.get("records") or {})
        blocked = sorted(
            record["canonical_url"]
            for record in records.values()
            if record.get("classification")
            == VerificationVerdict.BLOCKED.value
        )
        results_record = self._artifact_json_once(
            "step4.verification_results",
            StepName.VERIFY,
            "verification_results.json",
            {
                "summary": dict(summary),
                "records": records,
            },
            record_count=len(records),
        )
        final_record = self._artifact_lines_once(
            "step4.final_blocked",
            StepName.VERIFY,
            "final_blocked.staged.txt",
            blocked,
        )
        entries = self.master_entry_builder(
            existing_entries=self.repository.load(),
            blocked_urls=blocked,
            manual_urls=sorted(
                self.repository.read_url_set(self.repository.manual_path)
            ),
            blacklist=self.repository.read_url_set(
                self.repository.blacklist_path
            ),
            session_id=self.context.session_id,
            now_iso=utc_now_iso(),
        )
        transaction_id = str(uuid.uuid4())
        provenance = MasterProvenance(
            platform_key=self.context.platform_key,
            session_id=self.context.session_id,
            run_id=self.context.run_id,
            generation_id=self.context.generation_id,
            transaction_id=transaction_id,
            final_sha256=final_record["sha256"],
        )
        prepared = self.repository.prepare(
            entries,
            provenance=provenance,
            staging_dir=self.context.publication_dir,
        )
        journal = {
            "transaction_id": transaction_id,
            "prepared": prepared.to_dict(),
            "final_artifact": {
                "logical_name": "step4.final_blocked",
                "sha256": final_record["sha256"],
                "record_count": final_record["record_count"],
            },
            "results_artifact": {
                "logical_name": "step4.verification_results",
                "sha256": results_record["sha256"],
            },
            "created_at": utc_now_iso(),
        }
        journal_path = Path(self.context.publication_dir, "journal.json")
        self.store.writer.write_bytes(
            journal_path, canonical_json_bytes(journal)
        )
        reloaded = json.loads(journal_path.read_text(encoding="utf-8"))
        if reloaded.get("transaction_id") != transaction_id:
            raise RunStoreError(
                "prepared_validation_failed", "journal reload mismatch"
            )
        self.repository.validate_prepared(prepared)
        self.store.set_publication_state(
            self.context,
            "prepared",
            transaction_id=transaction_id,
            generation_id=self.context.generation_id,
            prepared_hashes={
                "master_json": prepared.json_sha256,
                "master_txt": prepared.txt_sha256,
                "master_meta": prepared.meta_sha256,
                "final": final_record["sha256"],
            },
        )
        return prepared

    def _artifact_lines_once(self, logical, step, filename, lines):
        try:
            document, _ = self.store.load_artifact(self.context, logical)
            return document
        except RunStoreError:
            pass
        record = self.store.artifact_lines(
            self.context, step, filename, lines,
            logical_name=logical, schema="canonical_url_set/v1",
        )
        self.store.update_manifest(
            self.context,
            lambda manifest: manifest["artifacts"].__setitem__(
                logical, self.store._artifact_document(record)
            ),
        )
        document, _ = self.store.load_artifact(self.context, logical)
        return document

    def _artifact_json_once(
        self, logical, step, filename, value, record_count
    ):
        try:
            document, _ = self.store.load_artifact(self.context, logical)
            return document
        except RunStoreError:
            pass
        record = self.store.artifact_json(
            self.context, step, filename, value,
            logical_name=logical, schema="verification_results/v1",
            record_count=record_count,
        )
        self.store.update_manifest(
            self.context,
            lambda manifest: manifest["artifacts"].__setitem__(
                logical, self.store._artifact_document(record)
            ),
        )
        document, _ = self.store.load_artifact(self.context, logical)
        return document

    # ------------------------------------------------------------------
    # Phase B

    def _phase_b(self, started_at, prepared: PreparedMasterRevision):
        # The commit witness is the point of no return, so every check that
        # can still fail safely has to happen before it. The live master is
        # shared across sessions: if another run published since this
        # revision was prepared, committing now would wedge this generation
        # forever, because roll-forward can only ever replay the stale
        # revision it already committed to.
        conflict = self._detect_base_revision_conflict(prepared)
        if conflict is not None:
            return self._failed(
                started_at,
                "master_revision_conflict",
                conflict,
                resumable=True,
                new_generation_required=True,
            )
        # Commit witness: after this write, recovery is roll-forward only.
        if self.backup_callback is not None:
            self.backup_callback(prepared.transaction_id)
        final_document, final_path = self.store.load_artifact(
            self.context, "step4.final_blocked"
        )
        witness = {
            "schema_version": 1,
            "transaction_id": prepared.transaction_id,
            "identity": self.context.identity.to_dict(),
            "prepared_hashes": {
                "master_json": prepared.json_sha256,
                "master_txt": prepared.txt_sha256,
                "master_meta": prepared.meta_sha256,
                "final": final_document["sha256"],
            },
            "base_revision_id": prepared.base_revision_id,
            "base_json_sha256": prepared.base_json_sha256,
            "committed_at": utc_now_iso(),
        }
        self.store.writer.write_bytes(
            self.context.commit_path, canonical_json_bytes(witness)
        )
        self.store.set_publication_state(
            self.context,
            "committed",
            commit_path=os.path.relpath(
                self.context.commit_path, self.context.session_path
            ).replace("\\", "/"),
        )
        self.store.set_committed_generation(
            {
                "transaction_id": prepared.transaction_id,
                "run_id": self.context.run_id,
                "generation_id": self.context.generation_id,
                "commit_relative_path": os.path.relpath(
                    self.context.commit_path, self.context.session_path
                ).replace("\\", "/"),
                "final_artifact_relative_path": final_document[
                    "relative_path"
                ],
                "final_sha256": final_document["sha256"],
                "final_record_count": final_document["record_count"],
                "committed_at": witness["committed_at"],
            }
        )
        return self._roll_forward(started_at, prepared)

    def _detect_base_revision_conflict(self, prepared) -> str | None:
        """Has the live master moved since this revision was prepared?

        Returns a message when committing would be unsafe, else None. An
        already-committed identical revision is not a conflict.
        """
        if prepared.base_json_sha256 is None:
            return None
        if not os.path.exists(self.repository.json_path):
            return None
        live = sha256_file(self.repository.json_path)
        if live in (prepared.base_json_sha256, prepared.json_sha256):
            return None
        return (
            "The master list changed after this publication was staged, so "
            "this generation can no longer be committed. Start a new run."
        )

    def _advance_publication_state(self, state: str, **fields) -> None:
        """Move the publication state forward, never backward.

        Recovery re-enters roll-forward on an already-``projected``
        generation; writing ``projecting`` again would be a backward move
        and the store rejects it, which used to dead-end every repair.
        """
        manifest = self.store.load_manifest(self.context)
        current = manifest.get("publication", {}).get("state", "none")
        if PUBLICATION_STATES.index(state) < PUBLICATION_STATES.index(current):
            if fields:
                self.store.set_publication_state(self.context, current, **fields)
            return
        self.store.set_publication_state(self.context, state, **fields)

    def _roll_forward(self, started_at, prepared: PreparedMasterRevision):
        try:
            self._reconcile_committed_pointer(prepared)
            result = self.repository.commit(prepared)
            if result.error_code:
                self.store.set_recovery_required(self.context, True)
                return self._failed(
                    started_at,
                    result.error_code,
                    result.message,
                    resumable=True,
                )
            self._advance_publication_state("projecting")
            projection_status = {}
            for root_name, (logical, mode) in self.projections.items():
                projection_status[root_name] = self._project(
                    root_name, logical, mode
                )
            self._seal_checkpoint(prepared)
            self._advance_publication_state(
                "projected",
                projection_status=projection_status,
            )
        except Exception as exc:
            try:
                self.store.set_recovery_required(self.context, True)
            except Exception:
                pass
            return self._failed(
                started_at,
                "projection_failed",
                str(exc)[:200],
                resumable=True,
            )
        verification = self._verify_projafter()
        if verification is not None:
            self.store.set_recovery_required(self.context, True)
            return self._failed(
                started_at, "projection_failed", verification, resumable=True
            )
        self.store.set_recovery_required(self.context, False)
        outcome = self._success(started_at)
        self.store.record_step_attempt(self.context, outcome)
        return outcome

    def _reconcile_committed_pointer(
        self, prepared: PreparedMasterRevision
    ) -> None:
        """Make the session pointer agree with the durable commit witness.

        Either durable record counts as the commit decision, so recovery
        reconciles whichever one a crash left behind. Idempotent.
        """
        if not os.path.exists(self.context.commit_path):
            return
        state = self.store.load_run_state() or {}
        pointer = state.get("committed_generation") or {}
        if pointer.get("generation_id") == self.context.generation_id:
            return
        witness = json.loads(
            Path(self.context.commit_path).read_text(encoding="utf-8")
        )
        final_document, _ = self.store.load_artifact(
            self.context, "step4.final_blocked"
        )
        self.store.set_committed_generation(
            {
                "transaction_id": witness["transaction_id"],
                "run_id": self.context.run_id,
                "generation_id": self.context.generation_id,
                "commit_relative_path": os.path.relpath(
                    self.context.commit_path, self.context.session_path
                ).replace("\\", "/"),
                "final_artifact_relative_path": final_document[
                    "relative_path"
                ],
                "final_sha256": final_document["sha256"],
                "final_record_count": final_document["record_count"],
                "committed_at": witness["committed_at"],
            }
        )

    def _project(self, root_name, logical, mode) -> dict:
        document, source_path = self.store.load_artifact(
            self.context, logical
        )
        target = Path(self.context.session_path, root_name)
        payload = Path(source_path).read_bytes()
        if mode == "joined":
            lines = [
                line
                for line in payload.decode("utf-8").splitlines()
                if line.strip()
            ]
            payload = "\n".join(lines).encode("utf-8")
        self.store.writer.write_bytes(target, payload)
        projected_sha = sha256_file(target)
        expected = (
            document["sha256"] if mode != "joined" else sha256_bytes(payload)
        )
        if projected_sha != expected:
            raise RunStoreError(
                "projection_failed", f"{root_name} hash mismatch"
            )
        return {
            "source": logical,
            "sha256": projected_sha,
            "projected_at": utc_now_iso(),
        }

    def _seal_checkpoint(self, prepared: PreparedMasterRevision) -> None:
        checkpoint = self.store.load_checkpoint(self.context)
        if checkpoint is None:
            return
        payload = {
            key: value
            for key, value in checkpoint.items()
            if key
            not in (
                "schema_version",
                "identity",
                "config_digest_sha256",
                "classifier_version",
                "updated_at",
            )
        }
        payload["sealed"] = True
        payload["sealed_transaction_id"] = prepared.transaction_id
        payload["sealed_master_json_sha256"] = prepared.json_sha256
        self.store.write_checkpoint(self.context, payload)

    def _verify_projafter(self) -> str | None:
        """Fresh disk reload before declaring success."""
        state = self.store.load_run_state()
        pointer = (state or {}).get("committed_generation") or {}
        if pointer.get("generation_id") != self.context.generation_id:
            return "committed pointer does not reference this generation"
        final_path = Path(
            self.context.session_path,
            pointer.get("final_artifact_relative_path", ""),
        )
        if not final_path.is_file():
            return "committed final artifact missing"
        if sha256_file(final_path) != pointer.get("final_sha256"):
            return "committed final artifact hash mismatch"
        for root_name, (logical, mode) in self.projections.items():
            target = Path(self.context.session_path, root_name)
            if not target.is_file():
                return f"projection {root_name} missing"
        return None

    # ------------------------------------------------------------------
    # Recovery

    def recover(self) -> StepOutcome | None:
        """Roll a committed-but-unprojected generation forward.

        Returns None when there is nothing to recover. Never reopens a
        browser and never deletes anything.
        """
        started_at = utc_now_iso()
        manifest = self.store.load_manifest(self.context)
        publication = manifest.get("publication", {})
        state = publication.get("state", "none")
        witness_exists = os.path.exists(self.context.commit_path)
        if state in ("none", "preparing") and not witness_exists:
            return None
        if state == "prepared" and not witness_exists:
            # Nothing is committed yet, so unreadable or drifted staging is
            # recoverable: hand back to the caller and let _phase_a stage a
            # fresh transaction rather than escaping with an untyped error.
            try:
                prepared = self._load_prepared()
                self.repository.validate_prepared(prepared)
            except (RunStoreError, OSError, ValueError, KeyError) as exc:
                self.logger(
                    "[WARN] Staged publication is unusable and will be "
                    f"rebuilt: {str(exc)[:120]}"
                )
            return None
        if state == "projected" and not witness_exists:
            return None
        try:
            prepared = self._load_prepared()
        except (RunStoreError, OSError, ValueError, KeyError) as exc:
            # Past the commit decision the staged revision is the only
            # thing that can be replayed, so an unreadable journal is a
            # typed recovery failure, never an untyped crash.
            self.store.set_recovery_required(self.context, True)
            return self._failed(
                started_at,
                "projection_pending",
                f"committed staging is unreadable: {str(exc)[:120]}",
                resumable=True,
            )
        if state == "projected":
            # Verify hashes; success is idempotent.
            verification = self._verify_projafter()
            if verification is None:
                return None
        return self._roll_forward(started_at, prepared)

    def _load_prepared(self) -> PreparedMasterRevision:
        journal_path = Path(self.context.publication_dir, "journal.json")
        if not journal_path.is_file():
            raise RunStoreError(
                "projection_pending", "publication journal missing"
            )
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        value = journal.get("prepared") or {}
        provenance = value.get("provenance") or {}
        return PreparedMasterRevision(
            transaction_id=value["transaction_id"],
            revision_id=value["revision_id"],
            staging_dir=str(Path(self.context.publication_dir)),
            base_revision_id=value.get("base_revision_id"),
            base_json_sha256=value.get("base_json_sha256"),
            item_count=int(value["item_count"]),
            json_sha256=value["json_sha256"],
            txt_sha256=value["txt_sha256"],
            meta_sha256=value["meta_sha256"],
            provenance=MasterProvenance(
                platform_key=provenance["platform_key"],
                session_id=provenance["session_id"],
                run_id=provenance["run_id"],
                generation_id=provenance["generation_id"],
                transaction_id=provenance["transaction_id"],
                final_sha256=provenance["final_sha256"],
            ),
        )

    # ------------------------------------------------------------------
    # Outcomes

    def _success(self, started_at, warnings=()) -> StepOutcome:
        checkpoint = self.store.load_checkpoint(self.context) or {}
        summary = dict(checkpoint.get("summary") or {})
        total = int(checkpoint.get("candidate_count", 0))
        combined_warnings = tuple(warnings)
        if summary.get("unknown_terminal"):
            combined_warnings += (
                f"{summary['unknown_terminal']} candidates ended as "
                "terminal non-target unknown; they were NOT added to the "
                "blocked list",
            )
        return StepOutcome.succeeded(
            run_id=self.context.run_id,
            generation_id=self.context.generation_id,
            platform=self.context.platform_key,
            step=StepName.VERIFY,
            started_at=started_at,
            input_count=total,
            output_count=int(summary.get("blocked", 0)),
            processed_count=total,
            remaining_count=0,
            warnings=combined_warnings,
            summary_counts=summary,
            session_id=self.context.session_id,
        )

    def _cancelled_outcome(self, started_at) -> StepOutcome:
        outcome = StepOutcome.cancelled(
            run_id=self.context.run_id,
            generation_id=self.context.generation_id,
            platform=self.context.platform_key,
            step=StepName.VERIFY,
            started_at=started_at,
            error_code="cancelled_by_user",
            error_message=(
                "Stopped at a safe publication boundary; the previous "
                "final and master are unchanged."
            ),
            resumable=True,
            retryable=True,
            session_id=self.context.session_id,
        )
        self.store.record_step_attempt(self.context, outcome)
        return outcome

    def _failed(
        self,
        started_at,
        code,
        message,
        *,
        resumable=False,
        new_generation_required=False,
    ) -> StepOutcome:
        outcome = StepOutcome.failed(
            run_id=self.context.run_id,
            generation_id=self.context.generation_id,
            platform=self.context.platform_key,
            step=StepName.VERIFY,
            started_at=started_at,
            error_code=code,
            error_message=str(message)[:200],
            resumable=resumable,
            new_generation_required=new_generation_required,
            session_id=self.context.session_id,
        )
        try:
            self.store.record_step_attempt(self.context, outcome)
        except (OSError, ValueError):
            pass
        return outcome


class _PrepareCancelled(Exception):
    pass
