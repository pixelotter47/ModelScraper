"""Generic schema-v2 run store: immutable artifacts, manifests, checkpoints.

Platform-neutral safety kernel. Platform policy (URL grammar, filenames,
config digests) is injected by the platform store wrapper; this module must
not import ``ctb_*`` or ``stripchat_*``.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from platform_contracts import (
    PlatformContractError,
    RunIdentity,
    validate_session_id,
)
from storage_utils import (
    AtomicWriter,
    canonical_json_bytes,
    read_validated_json,
    sha256_bytes,
    sha256_file,
)
from workflow_types import (
    ArtifactRecord,
    OutcomeStatus,
    StepName,
    StepOutcome,
    utc_now_iso,
)

SCHEMA_VERSION = 2

RUN_STATUSES = (
    "running",
    "waiting_for_user",
    "incomplete",
    "cancelled",
    "failed",
    "succeeded",
)

PUBLICATION_STATES = (
    "none",
    "preparing",
    "prepared",
    "committed",
    "projecting",
    "projected",
)

RESTORATION_STATES = (
    "not_required",
    "pending",
    "in_progress",
    "restored",
    "failed",
)

_STEP_FOLDERS = {
    StepName.VPN_SNAPSHOT: "step1",
    StepName.LOCAL_SNAPSHOT: "step2",
    StepName.COMPARE: "step3",
    StepName.VERIFY: "step4",
    StepName.RESTORE: "step4",
}


class RunStoreError(ValueError):
    """A run-store contract violation with a stable snake_case code."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class RunContext:
    session_path: str
    session_id: str
    run_id: str
    generation_id: str
    platform_key: str
    mode: str

    @property
    def identity(self) -> RunIdentity:
        return RunIdentity(
            platform_key=self.platform_key,
            session_id=self.session_id,
            run_id=self.run_id,
            generation_id=self.generation_id,
        )

    @property
    def run_dir(self) -> str:
        return os.path.join(self.session_path, "runs", self.run_id)

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.run_dir, "manifest.json")

    @property
    def checkpoint_path(self) -> str:
        return os.path.join(self.run_dir, "verification_checkpoint.json")

    @property
    def events_path(self) -> str:
        return os.path.join(self.run_dir, "events.jsonl")

    @property
    def artifacts_dir(self) -> str:
        return os.path.join(self.run_dir, "artifacts")

    @property
    def publication_dir(self) -> str:
        return os.path.join(self.artifacts_dir, "publication")

    @property
    def commit_path(self) -> str:
        return os.path.join(
            self.session_path, "generations", self.generation_id, "commit.json"
        )


class GenericRunStore:
    """Authoritative v2 run persistence for one platform session."""

    def __init__(
        self,
        session_path,
        *,
        platform_key: str,
        config: Mapping[str, Any],
        writer: AtomicWriter | None = None,
        session_root=None,
    ):
        self.session_path = str(Path(session_path).resolve())
        self.session_id = os.path.basename(os.path.normpath(self.session_path))
        validate_session_id(self.session_id)
        if session_root is not None:
            root = Path(session_root).resolve()
            resolved = Path(self.session_path)
            if root == resolved or root not in resolved.parents:
                raise PlatformContractError(
                    "path_escape", "session escapes platform session root"
                )
        self.platform_key = str(platform_key)
        self.writer = writer or AtomicWriter()
        self.config = dict(config)
        for key in (
            "digest_sha256",
            "canonicalizer_version",
            "classifier_version",
            "api_contract_version",
        ):
            if not self.config.get(key):
                raise RunStoreError(
                    "invalid_request", f"config missing {key}"
                )
        self._seen_revisions: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Run lifecycle

    @property
    def run_state_path(self) -> str:
        return os.path.join(self.session_path, "run_state.json")

    def create_run(self, mode: str = "manual", source: str = "gui") -> RunContext:
        if mode not in ("manual", "full_auto", "resume", "shadow"):
            raise RunStoreError("invalid_request", f"invalid mode {mode!r}")
        if source not in ("gui", "web", "cli", "test"):
            raise RunStoreError("invalid_request", f"invalid source {source!r}")
        run_id = str(uuid.uuid4())
        generation_id = str(uuid.uuid4())
        context = RunContext(
            session_path=self.session_path,
            session_id=self.session_id,
            run_id=run_id,
            generation_id=generation_id,
            platform_key=self.platform_key,
            mode=mode,
        )
        context.identity.validate()
        Path(context.run_dir).mkdir(parents=True, exist_ok=False)
        for name in ("step1", "step2", "step3", "step4", "publication"):
            Path(context.artifacts_dir, name).mkdir(parents=True)
        now = utc_now_iso()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "manifest_revision": 1,
            "platform_key": self.platform_key,
            "session_id": self.session_id,
            "run_id": run_id,
            "generation_id": generation_id,
            "mode": mode,
            "source": source,
            "status": "running",
            "active_step": None,
            "created_at": now,
            "updated_at": now,
            "config": {
                "digest_sha256": self.config["digest_sha256"],
                "canonicalizer_version": self.config["canonicalizer_version"],
                "classifier_version": self.config["classifier_version"],
                "api_contract_version": self.config["api_contract_version"],
                "relevant_values": dict(
                    self.config.get("relevant_values") or {}
                ),
            },
            "steps": {},
            "artifacts": {},
            "retry": {
                "epoch": 0,
                "max_epochs": int(self.config.get("max_retry_epochs", 3)),
                "reason": None,
                "started_at": None,
            },
            "waiting": {
                "active": False,
                "reason": None,
                "started_at": None,
                "deadline_at": None,
            },
            "publication": {
                "state": "none",
                "generation_id": None,
                "transaction_id": None,
                "prepared_hashes": {},
                "commit_path": None,
                "projection_status": {},
                "recovery_required": False,
            },
            "restoration": {
                "required": False,
                "state": "pending",
                "original_policy": None,
                "desired_final_policy": None,
                "last_error_code": None,
                "updated_at": None,
            },
        }
        self._write_manifest_document(context, manifest, first_write=True)
        self._write_run_state(
            {
                "schema_version": SCHEMA_VERSION,
                "platform_key": self.platform_key,
                "session_id": self.session_id,
                "active_run_id": run_id,
                "active_generation_id": generation_id,
                "status": "running",
                "last_resumable_step": None,
                "committed_generation": self._load_committed_pointer(),
                "updated_at": now,
            }
        )
        return context

    def load_active(self) -> RunContext | None:
        if not os.path.exists(self.run_state_path):
            return None
        state = read_validated_json(self.run_state_path, self._validate_state)
        if not state.get("active_run_id"):
            return None
        context = RunContext(
            session_path=self.session_path,
            session_id=self.session_id,
            run_id=state["active_run_id"],
            generation_id=state["active_generation_id"],
            platform_key=self.platform_key,
            mode="manual",
        )
        manifest = self.load_manifest(context)
        return RunContext(
            session_path=self.session_path,
            session_id=self.session_id,
            run_id=manifest["run_id"],
            generation_id=manifest["generation_id"],
            platform_key=manifest["platform_key"],
            mode=manifest["mode"],
        )

    # ------------------------------------------------------------------
    # Manifest

    def load_manifest(self, context: RunContext) -> dict[str, Any]:
        manifest = read_validated_json(
            context.manifest_path, self._validate_manifest
        )
        if (
            manifest["run_id"] != context.run_id
            or manifest["generation_id"] != context.generation_id
            or manifest["platform_key"] != context.platform_key
            or manifest["session_id"] != context.session_id
        ):
            raise RunStoreError("identity_mismatch", "manifest identity mismatch")
        seen = self._seen_revisions.get(context.manifest_path, 0)
        revision = int(manifest["manifest_revision"])
        if revision < seen:
            raise RunStoreError(
                "identity_mismatch",
                "manifest revision regressed in-process",
            )
        self._seen_revisions[context.manifest_path] = revision
        return manifest

    def update_manifest(
        self,
        context: RunContext,
        mutate: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        manifest = self.load_manifest(context)
        mutate(manifest)
        self._write_manifest_document(context, manifest)
        return manifest

    def _write_manifest_document(
        self, context: RunContext, manifest: dict[str, Any], first_write=False
    ) -> None:
        if not first_write:
            manifest["manifest_revision"] = int(manifest["manifest_revision"]) + 1
        previous = manifest.get("updated_at") or ""
        now = utc_now_iso()
        manifest["updated_at"] = max(previous, now)
        self._validate_manifest(manifest)
        self.writer.write_bytes(
            context.manifest_path, canonical_json_bytes(manifest)
        )
        self._seen_revisions[context.manifest_path] = int(
            manifest["manifest_revision"]
        )

    # ------------------------------------------------------------------
    # Step attempts

    def record_step_attempt(
        self,
        context: RunContext,
        outcome: StepOutcome,
        *,
        retry_epoch: int = 0,
        consumed: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self._validate_outcome_identity(context, outcome)
        if outcome.status is OutcomeStatus.WAITING_FOR_USER:
            raise RunStoreError(
                "invalid_waiting_outcome",
                "waiting is a progress state, not a terminal step outcome",
            )
        attempt_id = str(uuid.uuid4())
        attempt = {
            "attempt_id": attempt_id,
            "retry_epoch": int(retry_epoch),
            "started_at": outcome.started_at,
            "finished_at": outcome.finished_at,
            "outcome": outcome.to_dict(),
            "consumed": dict(consumed or {}),
            "produced": [item.path for item in outcome.artifacts],
        }

        def mutate(manifest: dict[str, Any]) -> None:
            step_entry = manifest["steps"].setdefault(
                outcome.step.value,
                {"current_attempt_id": None, "current": None, "attempts": []},
            )
            step_entry["attempts"].append(attempt)
            step_entry["current_attempt_id"] = attempt_id
            step_entry["current"] = outcome.to_dict()
            manifest["active_step"] = outcome.step.value
            for record in outcome.artifacts:
                manifest["artifacts"][
                    record.logical_name or record.path
                ] = self._artifact_document(record)
            if outcome.status is OutcomeStatus.SUCCEEDED:
                if outcome.step is StepName.VERIFY:
                    manifest["status"] = "succeeded"
            else:
                manifest["status"] = outcome.status.value

        manifest = self.update_manifest(context, mutate)
        self.update_run_state(
            status=manifest["status"],
            active_run_id=context.run_id,
            active_generation_id=context.generation_id,
            last_resumable_step=(
                outcome.step.value
                if outcome.status
                in (OutcomeStatus.CANCELLED, OutcomeStatus.INCOMPLETE)
                and outcome.resumable
                else None
            ),
        )
        return manifest

    def _validate_outcome_identity(
        self, context: RunContext, outcome: StepOutcome
    ) -> None:
        if not isinstance(outcome, StepOutcome):
            raise RunStoreError("invalid_step_outcome")
        if outcome.platform != context.platform_key:
            raise RunStoreError(
                "identity_mismatch", "outcome platform mismatch"
            )
        if (
            outcome.run_id != context.run_id
            or outcome.generation_id != context.generation_id
        ):
            raise RunStoreError("identity_mismatch", "outcome run mismatch")
        if outcome.session_id != context.session_id:
            raise RunStoreError(
                "identity_mismatch", "outcome session mismatch"
            )

    # ------------------------------------------------------------------
    # Artifacts

    def artifact_lines(
        self,
        context: RunContext,
        step: StepName,
        filename: str,
        lines: Iterable[str],
        *,
        logical_name: str,
        schema: str,
        upstream: Mapping[str, str] | None = None,
    ) -> ArtifactRecord:
        values = [str(item).rstrip("\r\n") for item in lines]
        payload = "".join(f"{item}\n" for item in values).encode("utf-8")
        return self._write_artifact(
            context, step, filename, payload, len(values), logical_name,
            schema, upstream,
        )

    def artifact_json(
        self,
        context: RunContext,
        step: StepName,
        filename: str,
        value: Any,
        *,
        logical_name: str,
        schema: str,
        record_count: int | None = None,
        upstream: Mapping[str, str] | None = None,
    ) -> ArtifactRecord:
        payload = canonical_json_bytes(value)
        if record_count is None:
            record_count = len(value) if isinstance(value, (list, dict)) else 1
        return self._write_artifact(
            context, step, filename, payload, record_count, logical_name,
            schema, upstream,
        )

    def _write_artifact(
        self,
        context,
        step,
        filename,
        payload: bytes,
        record_count: int,
        logical_name: str,
        schema: str,
        upstream: Mapping[str, str] | None,
    ) -> ArtifactRecord:
        folder = _STEP_FOLDERS[step]
        path = os.path.join(context.artifacts_dir, folder, filename)
        target = Path(path).resolve()
        if Path(context.run_dir).resolve() not in target.parents:
            raise RunStoreError("path_escape", "artifact escapes run directory")
        existing = self._load_artifact_record(context, logical_name)
        if existing is not None:
            raise RunStoreError(
                "stale_generation",
                f"artifact {logical_name} already exists in this generation",
            )
        self.writer.write_bytes(path, payload)
        record = ArtifactRecord(
            path=os.path.relpath(path, self.session_path),
            record_count=max(0, int(record_count)),
            byte_size=len(payload),
            sha256=sha256_bytes(payload),
            generation_id=context.generation_id,
            created_at=utc_now_iso(),
            artifact_type=schema,
            logical_name=logical_name,
            schema=schema,
            platform_key=context.platform_key,
            session_id=context.session_id,
            run_id=context.run_id,
            producer_step=step.value,
            upstream_sha256=dict(upstream or {}),
        )
        return record

    def _artifact_document(self, record: ArtifactRecord) -> dict[str, Any]:
        return {
            "artifact_id": record.logical_name,
            "relative_path": record.path.replace("\\", "/"),
            "producer_step": record.producer_step,
            "schema": record.schema,
            "sha256": record.sha256,
            "byte_size": record.byte_size,
            "record_count": record.record_count,
            "created_at": record.created_at,
            "identity": {
                "platform_key": record.platform_key,
                "session_id": record.session_id,
                "run_id": record.run_id,
                "generation_id": record.generation_id,
            },
            "upstream_sha256": dict(record.upstream_sha256),
        }

    def _load_artifact_record(self, context, logical_name):
        try:
            manifest = self.load_manifest(context)
        except (OSError, ValueError):
            return None
        return manifest.get("artifacts", {}).get(logical_name)

    def load_artifact(self, context: RunContext, logical_name: str) -> tuple[dict, str]:
        """Validate and return (manifest artifact document, absolute path)."""
        manifest = self.load_manifest(context)
        document = manifest.get("artifacts", {}).get(logical_name)
        if not isinstance(document, dict):
            raise RunStoreError(
                "artifact_missing", f"artifact {logical_name} not in manifest"
            )
        identity = document.get("identity", {})
        if (
            identity.get("platform_key") != context.platform_key
            or identity.get("session_id") != context.session_id
            or identity.get("run_id") != context.run_id
            or identity.get("generation_id") != context.generation_id
        ):
            raise RunStoreError("stale_generation", "artifact identity mismatch")
        relative = str(document.get("relative_path") or "")
        if not relative or relative.startswith(("/", "\\")) or ":" in relative:
            raise RunStoreError("path_escape", "artifact path must be relative")
        path = Path(self.session_path, relative).resolve()
        if Path(self.session_path) not in path.parents:
            raise RunStoreError("path_escape", "artifact escapes session")
        if not path.is_file():
            raise RunStoreError("artifact_missing", relative)
        if path.stat().st_size != int(document.get("byte_size", -1)):
            raise RunStoreError("artifact_hash_mismatch", "size mismatch")
        if sha256_file(path) != document.get("sha256"):
            raise RunStoreError("artifact_hash_mismatch", relative)
        return document, str(path)

    def read_artifact_lines(
        self, context: RunContext, logical_name: str
    ) -> list[str]:
        _, path = self.load_artifact(context, logical_name)
        with open(path, "r", encoding="utf-8") as handle:
            return [line.rstrip("\n") for line in handle if line.strip()]

    def require_step_success(
        self, context: RunContext, step: StepName
    ) -> dict[str, Any]:
        manifest = self.load_manifest(context)
        current = (
            manifest.get("steps", {}).get(step.value, {}).get("current")
        )
        if not current or current.get("status") != "succeeded":
            raise RunStoreError(
                "stale_generation",
                f"{step.value} has no successful outcome in this generation",
            )
        return current

    # ------------------------------------------------------------------
    # Waiting, retry, publication, restoration fields

    def set_waiting(
        self, context: RunContext, reason: str, deadline_at: str | None
    ) -> None:
        def mutate(manifest):
            manifest["waiting"] = {
                "active": True,
                "reason": str(reason),
                "started_at": utc_now_iso(),
                "deadline_at": deadline_at,
            }
            manifest["status"] = "waiting_for_user"

        self.update_manifest(context, mutate)
        self.update_run_state(
            status="waiting_for_user",
            active_run_id=context.run_id,
            active_generation_id=context.generation_id,
        )

    def clear_waiting(self, context: RunContext) -> None:
        def mutate(manifest):
            manifest["waiting"] = {
                "active": False,
                "reason": None,
                "started_at": None,
                "deadline_at": None,
            }
            manifest["status"] = "running"

        self.update_manifest(context, mutate)
        self.update_run_state(
            status="running",
            active_run_id=context.run_id,
            active_generation_id=context.generation_id,
        )

    def set_retry_epoch(
        self, context: RunContext, epoch: int, reason: str | None
    ) -> None:
        def mutate(manifest):
            retry = manifest["retry"]
            if int(epoch) < int(retry.get("epoch", 0)):
                raise RunStoreError(
                    "invalid_request", "retry epoch may not regress"
                )
            retry["epoch"] = int(epoch)
            retry["reason"] = reason
            retry["started_at"] = utc_now_iso()

        self.update_manifest(context, mutate)

    def set_publication_state(
        self, context: RunContext, state: str, **fields: Any
    ) -> dict[str, Any]:
        if state not in PUBLICATION_STATES:
            raise RunStoreError("invalid_request", f"invalid state {state!r}")

        def mutate(manifest):
            publication = manifest["publication"]
            current = publication.get("state", "none")
            if PUBLICATION_STATES.index(state) < PUBLICATION_STATES.index(
                current
            ):
                raise RunStoreError(
                    "invalid_request",
                    f"publication state may not move backward "
                    f"({current} -> {state})",
                )
            publication["state"] = state
            for key, value in fields.items():
                publication[key] = value

        return self.update_manifest(context, mutate)

    def set_recovery_required(self, context: RunContext, value: bool) -> None:
        self.update_manifest(
            context,
            lambda manifest: manifest["publication"].__setitem__(
                "recovery_required", bool(value)
            ),
        )

    def record_restoration(
        self, context: RunContext, evidence: Mapping[str, Any]
    ) -> None:
        def mutate(manifest):
            restoration = dict(manifest.get("restoration") or {})
            restoration.update(dict(evidence))
            state = restoration.get("state")
            if state not in RESTORATION_STATES:
                raise RunStoreError(
                    "invalid_request", f"invalid restoration state {state!r}"
                )
            restoration["updated_at"] = utc_now_iso()
            manifest["restoration"] = restoration

        self.update_manifest(context, mutate)

    # ------------------------------------------------------------------
    # Checkpoint

    def write_checkpoint(
        self, context: RunContext, checkpoint: dict[str, Any]
    ) -> None:
        value = dict(checkpoint)
        value.update(
            {
                "schema_version": SCHEMA_VERSION,
                "identity": context.identity.to_dict(),
                "config_digest_sha256": self.config["digest_sha256"],
                "classifier_version": self.config["classifier_version"],
                "updated_at": utc_now_iso(),
            }
        )
        self._validate_checkpoint(value)
        self.writer.write_bytes(
            context.checkpoint_path, canonical_json_bytes(value)
        )

    def load_checkpoint(self, context: RunContext) -> dict[str, Any] | None:
        if not os.path.exists(context.checkpoint_path):
            return None
        value = read_validated_json(
            context.checkpoint_path, self._validate_checkpoint
        )
        identity = value.get("identity", {})
        if identity != context.identity.to_dict():
            raise RunStoreError("checkpoint_mismatch", "identity mismatch")
        if value.get("config_digest_sha256") != self.config["digest_sha256"]:
            raise RunStoreError("checkpoint_mismatch", "config digest mismatch")
        if value.get("classifier_version") != self.config["classifier_version"]:
            raise RunStoreError(
                "checkpoint_mismatch", "classifier version mismatch"
            )
        return value

    # ------------------------------------------------------------------
    # Session run-state pointer

    def load_run_state(self) -> dict[str, Any] | None:
        if not os.path.exists(self.run_state_path):
            return None
        return read_validated_json(self.run_state_path, self._validate_state)

    def update_run_state(
        self,
        *,
        status: str,
        active_run_id: str | None,
        active_generation_id: str | None,
        last_resumable_step: str | None = None,
    ) -> None:
        if status not in RUN_STATUSES:
            raise RunStoreError("invalid_request", f"invalid status {status!r}")
        state = self.load_run_state() or {}
        state.update(
            {
                "schema_version": SCHEMA_VERSION,
                "platform_key": self.platform_key,
                "session_id": self.session_id,
                "active_run_id": active_run_id,
                "active_generation_id": active_generation_id,
                "status": status,
                "last_resumable_step": last_resumable_step,
                "committed_generation": state.get("committed_generation"),
                "updated_at": utc_now_iso(),
            }
        )
        self._write_run_state(state)

    def set_committed_generation(self, pointer: Mapping[str, Any]) -> None:
        required = (
            "transaction_id",
            "run_id",
            "generation_id",
            "commit_relative_path",
            "final_artifact_relative_path",
            "final_sha256",
            "final_record_count",
            "committed_at",
        )
        for key in required:
            if key not in pointer:
                raise RunStoreError(
                    "invalid_request", f"committed pointer missing {key}"
                )
        state = self.load_run_state()
        if state is None:
            raise RunStoreError("invalid_request", "run state missing")
        state["committed_generation"] = dict(pointer)
        state["updated_at"] = utc_now_iso()
        self._write_run_state(state)

    def _load_committed_pointer(self):
        state = self.load_run_state() if os.path.exists(self.run_state_path) else None
        if state:
            return state.get("committed_generation")
        return None

    def _write_run_state(self, state: dict[str, Any]) -> None:
        self._validate_state(state)
        self.writer.write_bytes(self.run_state_path, canonical_json_bytes(state))

    # ------------------------------------------------------------------
    # Events journal (diagnostic mirror; never the API)

    def append_event(self, context: RunContext, event: Mapping[str, Any]) -> None:
        try:
            with open(context.events_path, "a", encoding="utf-8") as handle:
                import json as _json

                handle.write(_json.dumps(dict(event), sort_keys=True) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Validators

    def _validate_state(self, value):
        if not isinstance(value, dict):
            raise ValueError("run state must be an object")
        for key in (
            "schema_version",
            "platform_key",
            "session_id",
            "active_run_id",
            "active_generation_id",
            "status",
        ):
            if key not in value:
                raise ValueError(f"run state missing {key}")
        if value["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported run state schema")
        if value["platform_key"] != self.platform_key:
            raise ValueError("run state platform mismatch")
        if value["session_id"] != self.session_id:
            raise ValueError("run state session mismatch")
        if value["status"] not in RUN_STATUSES:
            raise ValueError("invalid run state status")

    def _validate_manifest(self, value):
        if not isinstance(value, dict):
            raise ValueError("manifest must be an object")
        for key in (
            "schema_version",
            "manifest_revision",
            "platform_key",
            "session_id",
            "run_id",
            "generation_id",
            "mode",
            "source",
            "status",
            "created_at",
            "updated_at",
            "config",
            "steps",
            "artifacts",
            "retry",
            "waiting",
            "publication",
            "restoration",
        ):
            if key not in value:
                raise ValueError(f"manifest missing {key}")
        if value["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported manifest schema")
        if value["platform_key"] != self.platform_key:
            raise ValueError("manifest platform mismatch")
        if value["status"] not in RUN_STATUSES:
            raise ValueError("invalid manifest status")
        if not isinstance(value["steps"], dict) or not isinstance(
            value["artifacts"], dict
        ):
            raise ValueError("invalid manifest collections")
        config = value.get("config")
        if not isinstance(config, dict) or not config.get("digest_sha256"):
            raise ValueError("manifest missing config digest")
        publication = value.get("publication")
        if (
            not isinstance(publication, dict)
            or publication.get("state") not in PUBLICATION_STATES
        ):
            raise ValueError("invalid publication state")

    def _validate_checkpoint(self, value):
        if not isinstance(value, dict):
            raise ValueError("checkpoint must be an object")
        for key in (
            "schema_version",
            "identity",
            "config_digest_sha256",
            "classifier_version",
            "candidate_artifact_id",
            "candidate_sha256",
            "candidate_count",
            "retry_epoch",
            "max_retry_epochs",
            "attempt_budget_per_epoch",
            "records",
            "summary",
        ):
            if key not in value:
                raise ValueError(f"checkpoint missing {key}")
        if value["schema_version"] != SCHEMA_VERSION:
            raise ValueError("checkpoint_mismatch")
        if not isinstance(value["records"], dict) or not isinstance(
            value["summary"], dict
        ):
            raise ValueError("invalid checkpoint collections")
