"""Generic checkpointed tri-state verifier with bounded retry epochs.

Platform-neutral safety kernel. The page verdict provider, identity keys,
retryability policy, and finalizer are injected; this module must not import
``ctb_*`` or ``stripchat_*``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from workflow_store import GenericRunStore, RunContext, RunStoreError
from workflow_types import (
    OutcomeStatus,
    ProgressEvent,
    ProgressStatus,
    StepName,
    StepOutcome,
    VerificationRecord,
    VerificationVerdict,
    utc_now_iso,
)


class VerifierConfigError(ValueError):
    pass


class GenericCheckpointedVerifier:
    """Resumable candidate verification bound to one immutable generation."""

    def __init__(
        self,
        store: GenericRunStore,
        context: RunContext,
        verdict_provider: Callable[[str, int], VerificationRecord],
        *,
        identity_key: Callable[[str], str],
        canonicalize_url: Callable[[str], str],
        terminal_nontarget_reasons: Iterable[str],
        finalizer: Callable[[dict], StepOutcome] | None = None,
        stop_token=None,
        progress_callback=None,
        max_retry_epochs: int = 3,
        attempt_budget_per_epoch: int = 2,
        escalation_exceptions: tuple[type, ...] = (),
    ):
        self.store = store
        self.context = context
        self.verdict_provider = verdict_provider
        self.identity_key = identity_key
        self.canonicalize_url = canonicalize_url
        # Any UNKNOWN whose reason is not terminal non-target is transient.
        self.terminal_nontarget_reasons = frozenset(terminal_nontarget_reasons)
        self.finalizer = finalizer
        self.stop_token = stop_token
        self.progress_callback = progress_callback
        self.max_retry_epochs = max(1, int(max_retry_epochs))
        self.attempt_budget_per_epoch = max(1, int(attempt_budget_per_epoch))
        # Exceptions that must abort verification at a safe checkpoint and
        # propagate to the step runner (e.g. a challenge deadline).
        self.escalation_exceptions = tuple(escalation_exceptions)

    # ------------------------------------------------------------------

    def verify(
        self, candidate_artifact_id: str, *, resume: bool = True
    ) -> StepOutcome:
        started_at = utc_now_iso()
        try:
            document, _ = self.store.load_artifact(
                self.context, candidate_artifact_id
            )
            candidates = self.store.read_artifact_lines(
                self.context, candidate_artifact_id
            )
        except RunStoreError as exc:
            return self._failed(started_at, exc.code, str(exc))
        try:
            canonical = self._canonical_candidates(candidates)
        except ValueError:
            return self._failed(
                started_at,
                "invalid_candidate_url",
                "Candidate artifact contains an invalid URL.",
            )
        try:
            checkpoint = (
                self.store.load_checkpoint(self.context) if resume else None
            )
        except (OSError, RunStoreError, ValueError):
            return self._failed(
                started_at,
                "checkpoint_mismatch",
                "Saved verification state is incompatible with this run.",
            )
        if checkpoint is not None:
            if (
                checkpoint.get("candidate_artifact_id") != candidate_artifact_id
                or checkpoint.get("candidate_sha256") != document["sha256"]
                or int(checkpoint.get("candidate_count", -1)) != len(canonical)
            ):
                return self._failed(
                    started_at,
                    "checkpoint_mismatch",
                    "Candidate artifact changed after the checkpoint was made.",
                )
            records = dict(checkpoint.get("records") or {})
            epoch = int(checkpoint.get("retry_epoch", 0))
            epoch = self._maybe_advance_epoch(canonical, records, epoch)
            if epoch >= self.max_retry_epochs:
                self._checkpoint(
                    candidate_artifact_id, document, canonical, records,
                    self.max_retry_epochs - 1,
                )
                return self._epochs_exhausted(started_at, canonical, records)
        else:
            records = {}
            epoch = 0
            self._checkpoint(
                candidate_artifact_id, document, canonical, records, epoch
            )

        self.store.set_retry_epoch(
            self.context, epoch, "resume" if checkpoint is not None else None
        )

        for url in canonical:
            key = self.identity_key(url)
            while self._needs_attempt(records.get(key)):
                if self._is_cancelled():
                    self._checkpoint(
                        candidate_artifact_id, document, canonical, records,
                        epoch,
                    )
                    return self._cancelled(started_at, canonical, records, epoch)
                record = records.get(key) or self._new_record(url)
                attempt_epoch = int(record.get("attempts_this_epoch", 0)) + 1
                lifetime = int(record.get("lifetime_attempts", 0)) + 1
                try:
                    verdict = self._provide_verdict(
                        url, lifetime, epoch, attempt_epoch
                    )
                except self.escalation_exceptions:
                    self._checkpoint(
                        candidate_artifact_id, document, canonical, records,
                        epoch,
                    )
                    raise
                record.update(
                    {
                        "canonical_url": url,
                        "classification": verdict.verdict.value,
                        "reason_code": verdict.reason_code,
                        "retryable": self._is_transient(verdict),
                        "terminal": self._is_terminal(verdict),
                        "attempts_this_epoch": attempt_epoch,
                        "lifetime_attempts": lifetime,
                        "updated_at": verdict.timestamp,
                        "observed_url": verdict.observed_url[:300],
                        "evidence": verdict.diagnostic_summary[:240],
                    }
                )
                records[key] = record
                self._checkpoint(
                    candidate_artifact_id, document, canonical, records, epoch
                )
                self._emit_progress(canonical, records)
        summary = self._summary(canonical, records)
        if summary["unknown_transient"]:
            outcome = StepOutcome.incomplete(
                run_id=self.context.run_id,
                generation_id=self.context.generation_id,
                platform=self.context.platform_key,
                step=StepName.VERIFY,
                started_at=started_at,
                input_count=len(canonical),
                processed_count=len(canonical) - summary["unknown_transient"],
                remaining_count=summary["unknown_transient"],
                error_code="transient_unknowns_remaining",
                error_message=(
                    "Some pages could not be classified. Resume is available."
                ),
                resumable=epoch + 1 < self.max_retry_epochs,
                retryable=True,
                summary_counts=summary,
                session_id=self.context.session_id,
                new_generation_required=epoch + 1 >= self.max_retry_epochs,
            )
            self.store.record_step_attempt(
                self.context, outcome, retry_epoch=epoch
            )
            return outcome
        if self.finalizer is None:
            outcome = StepOutcome.succeeded(
                run_id=self.context.run_id,
                generation_id=self.context.generation_id,
                platform=self.context.platform_key,
                step=StepName.VERIFY,
                started_at=started_at,
                input_count=len(canonical),
                output_count=summary["blocked"],
                processed_count=len(canonical),
                remaining_count=0,
                summary_counts=summary,
                session_id=self.context.session_id,
                warnings=self._warnings(summary),
            )
            self.store.record_step_attempt(
                self.context, outcome, retry_epoch=epoch
            )
            return outcome
        checkpoint_payload = self._checkpoint_payload(
            candidate_artifact_id, document, canonical, records, epoch
        )
        outcome = self.finalizer(checkpoint_payload)
        if not isinstance(outcome, StepOutcome):
            return self._failed(
                started_at,
                "invalid_step_outcome",
                "Finalizer did not return a StepOutcome.",
            )
        return outcome

    # ------------------------------------------------------------------

    def blocked_urls(self, records: Mapping[str, dict]) -> list[str]:
        return sorted(
            record["canonical_url"]
            for record in records.values()
            if record.get("classification") == VerificationVerdict.BLOCKED.value
        )

    def _canonical_candidates(self, candidates):
        canonical = []
        seen = {}
        for item in candidates:
            url = self.canonicalize_url(item)
            key = self.identity_key(url)
            if key in seen:
                if seen[key] != url:
                    raise ValueError("identity collision in candidates")
                continue
            seen[key] = url
            canonical.append(url)
        return tuple(canonical)

    def _provide_verdict(self, url, lifetime, epoch, epoch_attempt):
        try:
            record = self.verdict_provider(url, lifetime)
            if self.canonicalize_url(record.original_url) != url:
                raise ValueError("verdict identity mismatch")
            return record
        except self.escalation_exceptions:
            raise
        except Exception as exc:
            return VerificationRecord(
                original_url=url,
                verdict=VerificationVerdict.UNKNOWN,
                reason_code="selector_error",
                attempt=lifetime,
                timestamp=utc_now_iso(),
                observed_url="",
                diagnostic_summary=str(exc)[:160],
                run_id=self.context.run_id,
                generation_id=self.context.generation_id,
                epoch=epoch,
                epoch_attempt=epoch_attempt,
                retryable=True,
                terminal=False,
            )

    def _is_transient(self, verdict: VerificationRecord) -> bool:
        if verdict.verdict is not VerificationVerdict.UNKNOWN:
            return False
        return not self._is_terminal(verdict)

    def _is_terminal(self, verdict: VerificationRecord) -> bool:
        if verdict.verdict in (
            VerificationVerdict.BLOCKED,
            VerificationVerdict.ACCESSIBLE,
        ):
            return True
        if verdict.terminal:
            return True
        return verdict.reason_code in self.terminal_nontarget_reasons

    def _needs_attempt(self, record) -> bool:
        if record is None:
            return True
        if record.get("terminal"):
            return False
        if record.get("classification") != VerificationVerdict.UNKNOWN.value:
            return False
        return (
            int(record.get("attempts_this_epoch", 0))
            < self.attempt_budget_per_epoch
        )

    def _maybe_advance_epoch(self, canonical, records, epoch) -> int:
        pending_budget = False
        transient_exists = False
        for url in canonical:
            record = records.get(self.identity_key(url))
            if record is None:
                pending_budget = True
                continue
            if record.get("terminal"):
                continue
            transient_exists = True
            if (
                int(record.get("attempts_this_epoch", 0))
                < self.attempt_budget_per_epoch
            ):
                pending_budget = True
        if pending_budget or not transient_exists:
            return epoch
        next_epoch = epoch + 1
        if next_epoch >= self.max_retry_epochs:
            return self.max_retry_epochs
        for record in records.values():
            if not record.get("terminal"):
                record["attempts_this_epoch"] = 0
        return next_epoch

    @staticmethod
    def _new_record(url):
        return {
            "canonical_url": url,
            "classification": None,
            "reason_code": None,
            "retryable": True,
            "terminal": False,
            "attempts_this_epoch": 0,
            "lifetime_attempts": 0,
            "first_seen_at": utc_now_iso(),
        }

    def _summary(self, canonical, records) -> dict[str, int]:
        summary = {
            "blocked": 0,
            "accessible": 0,
            "unknown_transient": 0,
            "unknown_terminal": 0,
            "unattempted": 0,
        }
        for url in canonical:
            record = records.get(self.identity_key(url))
            if record is None or record.get("classification") is None:
                summary["unattempted"] += 1
            elif record["classification"] == VerificationVerdict.BLOCKED.value:
                summary["blocked"] += 1
            elif (
                record["classification"]
                == VerificationVerdict.ACCESSIBLE.value
            ):
                summary["accessible"] += 1
            elif record.get("terminal"):
                summary["unknown_terminal"] += 1
            else:
                summary["unknown_transient"] += 1
        return summary

    @staticmethod
    def _warnings(summary) -> tuple[str, ...]:
        if summary.get("unknown_terminal"):
            return (
                f"{summary['unknown_terminal']} candidates ended as terminal "
                "non-target unknown; they were NOT added to the blocked list",
            )
        return ()

    def _checkpoint_payload(
        self, candidate_artifact_id, document, canonical, records, epoch
    ):
        return {
            "candidate_artifact_id": candidate_artifact_id,
            "candidate_sha256": document["sha256"],
            "candidate_count": len(canonical),
            "retry_epoch": int(epoch),
            "max_retry_epochs": self.max_retry_epochs,
            "attempt_budget_per_epoch": self.attempt_budget_per_epoch,
            "records": dict(records),
            "summary": self._summary(canonical, records),
        }

    def _checkpoint(
        self, candidate_artifact_id, document, canonical, records, epoch
    ):
        self.store.write_checkpoint(
            self.context,
            self._checkpoint_payload(
                candidate_artifact_id, document, canonical, records, epoch
            ),
        )

    def _is_cancelled(self):
        return bool(
            self.stop_token
            and callable(getattr(self.stop_token, "is_cancelled", None))
            and self.stop_token.is_cancelled()
        )

    def _emit_progress(self, canonical, records):
        if not self.progress_callback:
            return
        summary = self._summary(canonical, records)
        completed = (
            summary["blocked"]
            + summary["accessible"]
            + summary["unknown_terminal"]
        )
        self.progress_callback(
            ProgressEvent(
                run_id=self.context.run_id,
                generation_id=self.context.generation_id,
                platform=self.context.platform_key,
                step=StepName.VERIFY,
                phase="verify",
                completed=completed,
                total=len(canonical),
                message=f"Verified {completed} of {len(canonical)}",
                status=ProgressStatus.RUNNING,
                summary_counts=summary,
                session_id=self.context.session_id,
            )
        )

    def _cancelled(self, started_at, canonical, records, epoch):
        summary = self._summary(canonical, records)
        processed = (
            summary["blocked"]
            + summary["accessible"]
            + summary["unknown_terminal"]
        )
        outcome = StepOutcome.cancelled(
            run_id=self.context.run_id,
            generation_id=self.context.generation_id,
            platform=self.context.platform_key,
            step=StepName.VERIFY,
            started_at=started_at,
            input_count=len(canonical),
            processed_count=processed,
            remaining_count=len(canonical) - processed,
            error_code="cancelled_by_user",
            error_message="Verification stopped safely. Resume is available.",
            resumable=True,
            retryable=True,
            summary_counts=summary,
            session_id=self.context.session_id,
        )
        self.store.record_step_attempt(self.context, outcome, retry_epoch=epoch)
        return outcome

    def _epochs_exhausted(self, started_at, canonical, records):
        summary = self._summary(canonical, records)
        outcome = StepOutcome.incomplete(
            run_id=self.context.run_id,
            generation_id=self.context.generation_id,
            platform=self.context.platform_key,
            step=StepName.VERIFY,
            started_at=started_at,
            input_count=len(canonical),
            processed_count=len(canonical) - summary["unknown_transient"],
            remaining_count=summary["unknown_transient"],
            error_code="retry_epochs_exhausted",
            error_message=(
                "Retry epochs are exhausted. Start a new run to retry the "
                "remaining unknowns."
            ),
            resumable=False,
            retryable=False,
            summary_counts=summary,
            session_id=self.context.session_id,
            new_generation_required=True,
        )
        self.store.record_step_attempt(
            self.context, outcome, retry_epoch=self.max_retry_epochs - 1
        )
        return outcome

    def _failed(self, started_at, code, message):
        outcome = StepOutcome.failed(
            run_id=self.context.run_id,
            generation_id=self.context.generation_id,
            platform=self.context.platform_key,
            step=StepName.VERIFY,
            started_at=started_at,
            error_code=code,
            error_message=message,
            session_id=self.context.session_id,
        )
        try:
            self.store.record_step_attempt(self.context, outcome)
        except (OSError, ValueError):
            pass
        return outcome
