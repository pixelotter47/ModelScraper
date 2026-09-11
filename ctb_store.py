"""Manifest, checkpoint, compatibility, and legacy-adoption storage."""

from __future__ import annotations

import datetime as _datetime
import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from storage_utils import (
    AtomicWriter,
    read_validated_json,
    sha256_file,
)
from ctb_classifier import canonical_model_url
from master_repository import (
    GenericMasterRepository,
    MasterConsistency,
    MasterProtectionPolicy,
)
from workflow_types import (
    ArtifactRecord,
    OutcomeStatus,
    StepName,
    StepOutcome,
    utc_now_iso,
)


SCHEMA_VERSION = 1
VERIFIER_SCHEMA_VERSION = 1
PLATFORM = "Chaturbate"

CHATURBATE_PROTECTION_POLICY = MasterProtectionPolicy(
    protected_flags=("manual", "location_profile_match"),
    removal_confirmations=2,
    allow_accessible_removal=True,
)


class ChaturbateMasterRepository(GenericMasterRepository):
    """Import-compatible CTB wrapper around the generic master repository."""

    SCHEMA_VERSION = 1
    META_SCHEMA_VERSION = 1

    def __init__(self, base_dir, writer=None):
        super().__init__(
            base_dir,
            platform_key="chaturbate",
            canonicalize_url=canonical_model_url,
            identity_key=lambda value: value,
            writer=writer,
            protection_policy=CHATURBATE_PROTECTION_POLICY,
        )

    def validate_consistency(self, repair_txt=True):
        # CTB v1 default repaired the TXT view automatically; preserve it.
        return super().validate_consistency(repair_txt=repair_txt)


MasterRepository = ChaturbateMasterRepository


@dataclass(frozen=True)
class RunContext:
    session_path: str
    run_id: str
    generation_id: str
    platform: str
    mode: str

    @property
    def run_dir(self) -> str:
        return os.path.join(self.session_path, "runs", self.run_id)

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.run_dir, "manifest.json")

    @property
    def checkpoint_path(self) -> str:
        return os.path.join(
            self.run_dir, "verification_checkpoint.json"
        )


class ChaturbateRunStore:
    def __init__(self, session_path, writer=None):
        self.session_path = str(Path(session_path).resolve())
        self.writer = writer or AtomicWriter()

    @property
    def run_state_path(self):
        return os.path.join(self.session_path, "run_state.json")

    def create_run(self, mode="manual", source="new") -> RunContext:
        run_id = str(uuid.uuid4())
        generation_id = str(uuid.uuid4())
        context = RunContext(
            self.session_path, run_id, generation_id, PLATFORM, mode
        )
        Path(context.run_dir).mkdir(parents=True, exist_ok=False)
        for name in ("step1", "step2", "step3", "step4"):
            Path(context.run_dir, name).mkdir()
        now = utc_now_iso()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "generation_id": generation_id,
            "platform": PLATFORM,
            "mode": mode,
            "source": source,
            "status": OutcomeStatus.INCOMPLETE.value,
            "created_at": now,
            "updated_at": now,
            "active_step": None,
            "steps": {},
            "artifacts": [],
            "restoration": None,
        }
        self.writer.write_json(context.manifest_path, manifest)
        self.writer.write_json(
            self.run_state_path,
            {
                "schema_version": SCHEMA_VERSION,
                "active_run_id": run_id,
                "generation_id": generation_id,
                "platform": PLATFORM,
                "status": OutcomeStatus.INCOMPLETE.value,
                "updated_at": now,
            },
        )
        return context

    def load_active(self) -> RunContext | None:
        if not os.path.exists(self.run_state_path):
            return None
        state = read_validated_json(self.run_state_path, self._validate_state)
        context = RunContext(
            self.session_path,
            state["active_run_id"],
            state["generation_id"],
            state["platform"],
            "manual",
        )
        manifest = self.load_manifest(context)
        return RunContext(
            self.session_path,
            manifest["run_id"],
            manifest["generation_id"],
            manifest["platform"],
            manifest["mode"],
        )

    def load_manifest(self, context: RunContext) -> dict[str, Any]:
        manifest = read_validated_json(
            context.manifest_path, self._validate_manifest
        )
        if (
            manifest["run_id"] != context.run_id
            or manifest["generation_id"] != context.generation_id
            or manifest["platform"] != context.platform
        ):
            raise ValueError("run identity mismatch")
        return manifest

    def record_step(
        self, context: RunContext, outcome: StepOutcome
    ) -> dict[str, Any]:
        if (
            outcome.run_id != context.run_id
            or outcome.generation_id != context.generation_id
            or outcome.platform != context.platform
        ):
            raise ValueError("step outcome identity mismatch")
        manifest = self.load_manifest(context)
        manifest["active_step"] = outcome.step.value
        manifest["steps"][outcome.step.value] = outcome.to_dict()
        manifest["updated_at"] = utc_now_iso()
        manifest["artifacts"] = self._merge_artifacts(
            manifest.get("artifacts", ()), outcome.artifacts
        )
        if outcome.status is OutcomeStatus.SUCCEEDED:
            if outcome.step is StepName.VERIFY:
                manifest["status"] = OutcomeStatus.SUCCEEDED.value
        else:
            manifest["status"] = outcome.status.value
        self.writer.write_json(context.manifest_path, manifest)
        self.writer.write_json(
            self.run_state_path,
            {
                "schema_version": SCHEMA_VERSION,
                "active_run_id": context.run_id,
                "generation_id": context.generation_id,
                "platform": context.platform,
                "status": manifest["status"],
                "updated_at": manifest["updated_at"],
            },
        )
        return manifest

    def artifact_lines(
        self,
        context: RunContext,
        step: StepName,
        filename: str,
        lines: Iterable[str],
        artifact_type: str,
    ) -> ArtifactRecord:
        path = self._artifact_path(context, step, filename)
        values = [str(item).rstrip("\r\n") for item in lines]
        self.writer.write_lines(path, values)
        return self._artifact_record(
            context, path, len(values), artifact_type
        )

    def artifact_json(
        self,
        context: RunContext,
        step: StepName,
        filename: str,
        value: Any,
        artifact_type: str,
        record_count: int | None = None,
    ) -> ArtifactRecord:
        path = self._artifact_path(context, step, filename)
        self.writer.write_json(path, value)
        if record_count is None:
            record_count = len(value) if isinstance(value, (list, dict)) else 1
        return self._artifact_record(
            context, path, record_count, artifact_type
        )

    def publish_lines(
        self,
        context: RunContext,
        record: ArtifactRecord,
        compatibility_name: str,
    ) -> None:
        self.validate_artifact(context, record)
        source = os.path.join(self.session_path, record.path)
        with open(source, "r", encoding="utf-8") as handle:
            lines = [line.rstrip("\r\n") for line in handle]
        self.writer.write_lines(
            os.path.join(self.session_path, compatibility_name), lines
        )

    def write_checkpoint(
        self, context: RunContext, checkpoint: dict[str, Any]
    ) -> None:
        value = dict(checkpoint)
        value.update(
            {
                "schema_version": VERIFIER_SCHEMA_VERSION,
                "run_id": context.run_id,
                "generation_id": context.generation_id,
                "platform": context.platform,
                "updated_at": utc_now_iso(),
            }
        )
        self._validate_checkpoint(value)
        self.writer.write_json(context.checkpoint_path, value)

    def load_checkpoint(self, context: RunContext) -> dict[str, Any] | None:
        if not os.path.exists(context.checkpoint_path):
            return None
        value = read_validated_json(
            context.checkpoint_path, self._validate_checkpoint
        )
        if (
            value["run_id"] != context.run_id
            or value["generation_id"] != context.generation_id
            or value["platform"] != context.platform
        ):
            raise ValueError("resume_checkpoint_mismatch")
        return value

    def prune_checkpoint(
        self, context: RunContext, keep_urls: Iterable[str]
    ) -> bool:
        """Drop checkpoint entries for URLs no longer being verified.

        Filtering the candidate list (for example skipping models already
        confirmed in the master list) would otherwise change the candidate
        digest and make an existing checkpoint fail as a mismatch. Pruning
        keeps the verdicts already earned for the URLs that remain.
        """
        try:
            checkpoint = self.load_checkpoint(context)
        except (OSError, ValueError):
            return False
        if not checkpoint:
            return False
        keep = list(dict.fromkeys(keep_urls))
        keep_set = set(keep)
        if set(checkpoint.get("candidate_urls", [])) == keep_set:
            return False
        if not keep_set.issubset(set(checkpoint.get("candidate_urls", []))):
            return False
        payload = dict(checkpoint)
        payload["candidate_urls"] = keep
        payload["candidate_sha256"] = hashlib.sha256(
            "".join(f"{item}\n" for item in keep).encode("utf-8")
        ).hexdigest()
        payload["verdicts"] = {
            url: value
            for url, value in checkpoint.get("verdicts", {}).items()
            if url in keep_set
        }
        payload["attempts"] = {
            url: value
            for url, value in checkpoint.get("attempts", {}).items()
            if url in keep_set
        }
        self.write_checkpoint(context, payload)
        return True

    def validate_artifact(
        self, context: RunContext, record: ArtifactRecord
    ) -> None:
        if record.generation_id != context.generation_id:
            raise ValueError("artifact generation mismatch")
        path = Path(self.session_path, record.path).resolve()
        if not path.is_relative_to(Path(self.session_path)):
            raise ValueError("artifact escapes session")
        if not path.is_file():
            raise ValueError("artifact missing")
        if path.stat().st_size != record.byte_size:
            raise ValueError("artifact size mismatch")
        if sha256_file(path) != record.sha256:
            raise ValueError("artifact hash mismatch")

    def inspect_legacy(self) -> dict[str, Any]:
        names = {
            "vpn": "cb_vpn_list.txt",
            "local": "cb_local_list.txt",
            "candidates": "cb_candidates.txt",
            "final": "FINAL_BLOCKED.txt",
        }
        present = {
            key: os.path.exists(os.path.join(self.session_path, value))
            for key, value in names.items()
        }
        warnings = []
        if present["final"]:
            classification, next_step, resumable = (
                "legacy_completed",
                None,
                False,
            )
        elif present["vpn"] and present["local"] and present["candidates"]:
            classification, next_step, resumable = (
                "legacy_incomplete",
                StepName.VERIFY.value,
                True,
            )
        elif present["vpn"] and present["local"]:
            classification, next_step, resumable = (
                "legacy_incomplete",
                StepName.COMPARE.value,
                True,
            )
        elif present["vpn"]:
            classification, next_step, resumable = (
                "legacy_incomplete",
                StepName.LOCAL_SNAPSHOT.value,
                True,
            )
        else:
            classification, next_step, resumable = (
                "legacy_incomplete",
                None,
                False,
            )
        return {
            "classification": classification,
            "next_step": next_step,
            "resumable": resumable,
            "present": present,
            "warnings": warnings,
        }

    def adopt_legacy(self, dry_run=True) -> dict[str, Any]:
        inspection = self.inspect_legacy()
        if dry_run or not inspection["resumable"]:
            return inspection
        hashes = []
        for key, present in sorted(inspection["present"].items()):
            if present:
                filename = {
                    "vpn": "cb_vpn_list.txt",
                    "local": "cb_local_list.txt",
                    "candidates": "cb_candidates.txt",
                    "final": "FINAL_BLOCKED.txt",
                }[key]
                hashes.append(f"{key}:{sha256_file(Path(self.session_path, filename))}")
        relative = Path(self.session_path).name
        generation_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{PLATFORM}|{relative}|{'|'.join(hashes)}",
            )
        )
        run_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"run|{generation_id}")
        )
        context = RunContext(
            self.session_path,
            run_id,
            generation_id,
            PLATFORM,
            "manual",
        )
        if os.path.exists(context.manifest_path):
            return {**inspection, "context": context, "idempotent": True}
        Path(context.run_dir).mkdir(parents=True, exist_ok=False)
        for name in ("step1", "step2", "step3", "step4"):
            Path(context.run_dir, name).mkdir()
        records = []
        mapping = {
            "vpn": (StepName.VPN_SNAPSHOT, "cb_vpn_list.txt"),
            "local": (StepName.LOCAL_SNAPSHOT, "cb_local_list.txt"),
            "candidates": (StepName.COMPARE, "cb_candidates.txt"),
            "final": (StepName.VERIFY, "FINAL_BLOCKED.txt"),
        }
        steps = {}
        for key, present in inspection["present"].items():
            if not present:
                continue
            step, filename = mapping[key]
            target = self._artifact_path(context, step, filename)
            shutil.copy2(Path(self.session_path, filename), target)
            count = len(
                Path(target).read_text(encoding="utf-8").splitlines()
            )
            record = self._artifact_record(
                context, target, count, key
            )
            records.append(record.to_dict())
            steps[step.value] = {"status": OutcomeStatus.SUCCEEDED.value}
        now = utc_now_iso()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "generation_id": generation_id,
            "platform": PLATFORM,
            "mode": "manual",
            "source": "legacy_adoption",
            "status": OutcomeStatus.INCOMPLETE.value,
            "created_at": now,
            "updated_at": now,
            "active_step": inspection["next_step"],
            "steps": steps,
            "artifacts": records,
            "restoration": None,
        }
        self.writer.write_json(context.manifest_path, manifest)
        self.writer.write_json(
            self.run_state_path,
            {
                "schema_version": SCHEMA_VERSION,
                "active_run_id": run_id,
                "generation_id": generation_id,
                "platform": PLATFORM,
                "status": OutcomeStatus.INCOMPLETE.value,
                "updated_at": now,
            },
        )
        return {**inspection, "context": context, "idempotent": False}

    def _artifact_path(self, context, step, filename):
        folder = {
            StepName.VPN_SNAPSHOT: "step1",
            StepName.LOCAL_SNAPSHOT: "step2",
            StepName.COMPARE: "step3",
            StepName.VERIFY: "step4",
            StepName.RESTORE: "step4",
        }[step]
        return os.path.join(context.run_dir, folder, filename)

    def _artifact_record(
        self, context, path, record_count, artifact_type
    ):
        relative = os.path.relpath(path, self.session_path)
        return ArtifactRecord(
            path=relative,
            record_count=max(0, int(record_count)),
            byte_size=os.path.getsize(path),
            sha256=sha256_file(path),
            generation_id=context.generation_id,
            created_at=utc_now_iso(),
            artifact_type=artifact_type,
        )

    @staticmethod
    def _merge_artifacts(existing, additions):
        records = {
            item["path"]: item
            for item in existing
            if isinstance(item, dict) and item.get("path")
        }
        for item in additions:
            records[item.path] = item.to_dict()
        return list(records.values())

    @staticmethod
    def _validate_state(value):
        if not isinstance(value, dict):
            raise ValueError("run state must be an object")
        for key in (
            "schema_version",
            "active_run_id",
            "generation_id",
            "platform",
            "status",
        ):
            if key not in value:
                raise ValueError(f"run state missing {key}")
        if value["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported run state schema")
        if value["platform"] != PLATFORM:
            raise ValueError("run state platform mismatch")

    @staticmethod
    def _validate_manifest(value):
        if not isinstance(value, dict):
            raise ValueError("manifest must be an object")
        for key in (
            "schema_version",
            "run_id",
            "generation_id",
            "platform",
            "mode",
            "status",
            "steps",
            "artifacts",
        ):
            if key not in value:
                raise ValueError(f"manifest missing {key}")
        if value["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported manifest schema")
        if value["platform"] != PLATFORM:
            raise ValueError("manifest platform mismatch")
        if not isinstance(value["steps"], dict) or not isinstance(
            value["artifacts"], list
        ):
            raise ValueError("invalid manifest collections")

    @staticmethod
    def _validate_checkpoint(value):
        if not isinstance(value, dict):
            raise ValueError("checkpoint must be an object")
        for key in (
            "schema_version",
            "run_id",
            "generation_id",
            "platform",
            "candidate_sha256",
            "candidate_urls",
            "verdicts",
        ):
            if key not in value:
                raise ValueError(f"checkpoint missing {key}")
        if value["schema_version"] != VERIFIER_SCHEMA_VERSION:
            raise ValueError("resume_checkpoint_mismatch")
        if not isinstance(value["candidate_urls"], list) or not isinstance(
            value["verdicts"], dict
        ):
            raise ValueError("invalid checkpoint collections")
