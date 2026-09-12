"""Generic canonical master repository with injected platform policy.

Platform-neutral safety kernel: URL grammar, identity keys, and protection
policy are injected. This module must not import ``ctb_*`` or
``stripchat_*``.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from storage_utils import (
    AtomicWriter,
    canonical_json_bytes,
    read_validated_json,
    sha256_bytes as _sha256_bytes,
    sha256_file,
)
from workflow_types import (
    VerificationRecord,
    VerificationVerdict,
    utc_now_iso,
)

CanonicalizeUrl = Callable[[str], str]
IdentityKey = Callable[[str], str]


@dataclass(frozen=True)
class MasterConsistency:
    valid: bool
    repaired_txt: bool
    json_count: int
    txt_count: int
    json_duplicates: int
    txt_duplicates: int
    error_code: str | None = None
    message: str = ""

    def to_dict(self):
        return {
            "valid": self.valid,
            "repaired_txt": self.repaired_txt,
            "json_count": self.json_count,
            "txt_count": self.txt_count,
            "json_duplicates": self.json_duplicates,
            "txt_duplicates": self.txt_duplicates,
            "error_code": self.error_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class MasterProtectionPolicy:
    """Which entries a verification verdict may never remove."""

    protected_flags: tuple[str, ...] = ("manual",)
    removal_confirmations: int = 2
    allow_accessible_removal: bool = False


@dataclass(frozen=True)
class MasterProvenance:
    platform_key: str
    session_id: str
    run_id: str
    generation_id: str
    transaction_id: str
    final_sha256: str

    def to_dict(self) -> dict:
        return {
            "platform_key": self.platform_key,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "transaction_id": self.transaction_id,
            "final_sha256": self.final_sha256,
        }


@dataclass(frozen=True)
class PreparedMasterRevision:
    transaction_id: str
    revision_id: str
    staging_dir: str
    base_revision_id: str | None
    base_json_sha256: str | None
    item_count: int
    json_sha256: str
    txt_sha256: str
    meta_sha256: str
    provenance: MasterProvenance

    @property
    def staged_json_path(self) -> str:
        return os.path.join(self.staging_dir, "master_data.staged.json")

    @property
    def staged_txt_path(self) -> str:
        return os.path.join(self.staging_dir, "master_text.staged.txt")

    @property
    def staged_meta_path(self) -> str:
        return os.path.join(self.staging_dir, "master_meta.staged.json")

    def to_dict(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "revision_id": self.revision_id,
            "staging_dir": self.staging_dir,
            "base_revision_id": self.base_revision_id,
            "base_json_sha256": self.base_json_sha256,
            "item_count": self.item_count,
            "json_sha256": self.json_sha256,
            "txt_sha256": self.txt_sha256,
            "meta_sha256": self.meta_sha256,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class MasterMutationResult:
    changed: bool
    revision_id: str | None
    item_count: int
    error_code: str | None = None
    message: str = ""
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "changed": self.changed,
            "revision_id": self.revision_id,
            "item_count": self.item_count,
            "error_code": self.error_code,
            "message": self.message,
            "details": dict(self.details),
        }


class GenericMasterRepository:
    """Canonical JSON master with atomic compatibility publication."""

    META_SCHEMA_VERSION = 2

    def __init__(
        self,
        base_dir,
        *,
        platform_key: str,
        canonicalize_url: CanonicalizeUrl,
        identity_key: IdentityKey | None = None,
        writer: AtomicWriter | None = None,
        protection_policy: MasterProtectionPolicy | None = None,
    ):
        self.base_dir = str(Path(base_dir).resolve())
        self.platform_key = str(platform_key)
        self.canonicalize_url = canonicalize_url
        self.identity_key = identity_key or (lambda value: value)
        self.writer = writer or AtomicWriter()
        self.protection_policy = protection_policy or MasterProtectionPolicy()
        self.json_path = os.path.join(self.base_dir, "MASTER_BLOCKED_DATA.json")
        self.txt_path = os.path.join(self.base_dir, "MASTER_BLOCKED.txt")
        self.meta_path = os.path.join(self.base_dir, "MASTER_BLOCKED_META.json")
        self.backup_path = f"{self.json_path}.bak"
        self.blacklist_path = os.path.join(self.base_dir, "GLOBAL_BLACKLIST.txt")
        self.manual_path = os.path.join(self.base_dir, "MANUAL_MODELS.txt")

    # ------------------------------------------------------------------
    # Read-only surface

    def load(self, *, required: bool = False) -> list[dict]:
        if not os.path.exists(self.json_path):
            if required:
                raise ValueError("master_json_missing")
            return []
        return read_validated_json(self.json_path, self._validate_entries)

    def read_url_set(self, path) -> set[str]:
        if not os.path.exists(path):
            return set()
        values = set()
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            if raw.strip():
                values.add(self.canonicalize_url(raw.strip()))
        return values

    def validate_consistency(self, repair_txt: bool = False) -> MasterConsistency:
        try:
            entries = self.load(required=True)
        except (OSError, ValueError) as exc:
            return MasterConsistency(
                False, False, 0, 0, 0, 0, "master_json_invalid", str(exc)[:200]
            )
        json_names = [item["name"] for item in entries]
        txt_names = []
        if os.path.exists(self.txt_path):
            try:
                txt_names = [
                    self.canonicalize_url(line.strip())
                    for line in Path(self.txt_path)
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line.strip()
                ]
            except (OSError, ValueError):
                txt_names = []
        json_duplicates = len(json_names) - len(set(json_names))
        txt_duplicates = len(txt_names) - len(set(txt_names))
        repaired = False
        if (
            json_names != sorted(set(txt_names)) or txt_duplicates
        ) and repair_txt:
            self.writer.write_lines(self.txt_path, json_names)
            txt_names = list(json_names)
            repaired = True
        valid = (
            not json_duplicates
            and not txt_duplicates
            and set(json_names) == set(txt_names)
        )
        return MasterConsistency(
            valid,
            repaired,
            len(json_names),
            len(txt_names),
            json_duplicates,
            txt_duplicates,
            None if valid else "master_consistency_mismatch",
            "TXT regenerated from canonical JSON." if repaired else "",
        )

    # ------------------------------------------------------------------
    # Legacy single-shot publication (v1-compatible mechanics)

    def publish(self, entries) -> int:
        canonical = self._canonical_entries(entries)
        if os.path.exists(self.json_path):
            previous = Path(self.json_path).read_bytes()
            try:
                self._validate_entries(json.loads(previous.decode("utf-8")))
            except Exception as exc:
                raise ValueError("master_json_corrupt") from exc
            self.writer.write_bytes(
                self.backup_path,
                previous,
                lambda raw: self._validate_entries(
                    json.loads(raw.decode("utf-8"))
                ),
            )
        revision = str(uuid.uuid4())
        self.writer.write_json(self.json_path, canonical)
        self.writer.write_lines(
            self.txt_path, [item["name"] for item in canonical]
        )
        self.writer.write_json(self.meta_path, self._meta_document(revision, canonical))
        return len(canonical)

    def _meta_document(self, revision, canonical, provenance=None, base=None):
        value = {
            "schema_version": self.META_SCHEMA_VERSION,
            "platform_key": self.platform_key,
            "revision_id": revision,
            "item_count": len(canonical),
            "json_sha256": sha256_file(self.json_path),
            "txt_sha256": sha256_file(self.txt_path),
            "published_at": utc_now_iso(),
        }
        if provenance is not None:
            value["provenance"] = provenance.to_dict()
        if base is not None:
            value["base_revision_id"] = base.get("revision_id")
            value["base_json_sha256"] = base.get("json_sha256")
        return value

    # ------------------------------------------------------------------
    # Two-phase publication

    def prepare(
        self,
        entries: Sequence[Mapping[str, object]],
        *,
        provenance: MasterProvenance,
        staging_dir,
    ) -> PreparedMasterRevision:
        """Stage a validated master revision. Never touches live files."""
        staging = Path(staging_dir)
        staging.mkdir(parents=True, exist_ok=True)
        canonical = self._canonical_entries(entries)
        base_revision_id = None
        base_json_sha256 = None
        if os.path.exists(self.json_path):
            base_json_sha256 = sha256_file(self.json_path)
            base_meta = self._read_meta()
            if base_meta:
                base_revision_id = base_meta.get("revision_id")
        revision_id = str(uuid.uuid4())
        json_bytes = canonical_json_bytes(canonical)
        txt_bytes = "".join(
            f"{item['name']}\n" for item in canonical
        ).encode("utf-8")
        json_path = staging / "master_data.staged.json"
        txt_path = staging / "master_text.staged.txt"
        meta_path = staging / "master_meta.staged.json"
        self.writer.write_bytes(json_path, json_bytes)
        self.writer.write_bytes(txt_path, txt_bytes)
        meta = {
            "schema_version": self.META_SCHEMA_VERSION,
            "platform_key": self.platform_key,
            "revision_id": revision_id,
            "item_count": len(canonical),
            "json_sha256": _sha256_bytes(json_bytes),
            "txt_sha256": _sha256_bytes(txt_bytes),
            "published_at": utc_now_iso(),
            "provenance": provenance.to_dict(),
            "base_revision_id": base_revision_id,
            "base_json_sha256": base_json_sha256,
        }
        meta_bytes = canonical_json_bytes(meta)
        self.writer.write_bytes(meta_path, meta_bytes)
        prepared = PreparedMasterRevision(
            transaction_id=provenance.transaction_id,
            revision_id=revision_id,
            staging_dir=str(staging),
            base_revision_id=base_revision_id,
            base_json_sha256=base_json_sha256,
            item_count=len(canonical),
            json_sha256=_sha256_bytes(json_bytes),
            txt_sha256=_sha256_bytes(txt_bytes),
            meta_sha256=_sha256_bytes(meta_bytes),
            provenance=provenance,
        )
        self.validate_prepared(prepared)
        return prepared

    def validate_prepared(self, prepared: PreparedMasterRevision) -> None:
        """Reparse and re-hash every staged file; fail closed on any drift."""
        for path, expected in (
            (prepared.staged_json_path, prepared.json_sha256),
            (prepared.staged_txt_path, prepared.txt_sha256),
            (prepared.staged_meta_path, prepared.meta_sha256),
        ):
            if not os.path.exists(path):
                raise ValueError("prepared_validation_failed")
            if sha256_file(path) != expected:
                raise ValueError("prepared_validation_failed")
        entries = json.loads(
            Path(prepared.staged_json_path).read_text(encoding="utf-8")
        )
        self._validate_entries(entries)
        if len(entries) != prepared.item_count:
            raise ValueError("prepared_validation_failed")
        txt_urls = [
            line.strip()
            for line in Path(prepared.staged_txt_path)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        if {item["name"] for item in entries} != set(txt_urls):
            raise ValueError("prepared_validation_failed")
        meta = json.loads(
            Path(prepared.staged_meta_path).read_text(encoding="utf-8")
        )
        if (
            meta.get("platform_key") != self.platform_key
            or meta.get("revision_id") != prepared.revision_id
            or meta.get("provenance", {}).get("transaction_id")
            != prepared.transaction_id
        ):
            raise ValueError("prepared_validation_failed")

    def commit(self, prepared: PreparedMasterRevision) -> MasterMutationResult:
        """Idempotently project the staged revision onto the live files."""
        self.validate_prepared(prepared)
        live_meta = self._read_meta() or {}
        if (
            os.path.exists(self.json_path)
            and sha256_file(self.json_path) == prepared.json_sha256
            and os.path.exists(self.txt_path)
            and sha256_file(self.txt_path) == prepared.txt_sha256
            and live_meta.get("revision_id") == prepared.revision_id
        ):
            return MasterMutationResult(
                changed=False,
                revision_id=prepared.revision_id,
                item_count=prepared.item_count,
                message="revision already committed",
            )
        if (
            prepared.base_json_sha256 is not None
            and os.path.exists(self.json_path)
            and sha256_file(self.json_path) != prepared.base_json_sha256
            and sha256_file(self.json_path) != prepared.json_sha256
        ):
            return MasterMutationResult(
                changed=False,
                revision_id=None,
                item_count=prepared.item_count,
                error_code="master_revision_conflict",
                message="live master changed since this revision was prepared",
            )
        if os.path.exists(self.json_path):
            previous = Path(self.json_path).read_bytes()
            self.writer.write_bytes(self.backup_path, previous)
        self.writer.write_bytes(
            self.json_path, Path(prepared.staged_json_path).read_bytes()
        )
        self.writer.write_bytes(
            self.txt_path, Path(prepared.staged_txt_path).read_bytes()
        )
        self.writer.write_bytes(
            self.meta_path, Path(prepared.staged_meta_path).read_bytes()
        )
        for path, expected in (
            (self.json_path, prepared.json_sha256),
            (self.txt_path, prepared.txt_sha256),
            (self.meta_path, prepared.meta_sha256),
        ):
            if sha256_file(path) != expected:
                return MasterMutationResult(
                    changed=True,
                    revision_id=None,
                    item_count=prepared.item_count,
                    error_code="projection_failed",
                    message=f"projected file hash mismatch for {os.path.basename(path)}",
                )
        return MasterMutationResult(
            changed=True,
            revision_id=prepared.revision_id,
            item_count=prepared.item_count,
        )

    # ------------------------------------------------------------------
    # Mutation helpers shared with legacy flows

    def rewrite_url_set(self, path, values) -> list[str]:
        canonical = sorted(
            {self.canonicalize_url(item) for item in values if str(item).strip()}
        )
        self.writer.write_lines(path, canonical)
        return canonical

    def apply_verification_records(self, records):
        """Apply tri-state verdicts; uncertainty is never destructive."""
        policy = self.protection_policy
        entries = self.load(required=True)
        by_name = {item["name"]: dict(item) for item in entries}
        blacklist = self.read_url_set(self.blacklist_path)
        removed = []
        pending = []
        applied = 0
        for raw in records:
            record = (
                raw
                if isinstance(raw, VerificationRecord)
                else VerificationRecord.from_dict(raw)
            )
            url = self.canonicalize_url(record.original_url)
            item = by_name.get(url)
            if item is None:
                continue
            if any(item.get(flag) for flag in policy.protected_flags):
                continue
            history = list(item.get("verification_history") or [])[-9:]
            history.append(record.to_dict())
            item["verification_history"] = history
            item["last_verdict"] = record.verdict.value
            item["last_verified_at"] = record.timestamp
            item["verification_reason"] = record.reason_code
            run_ids = [
                str(value)
                for value in item.get("accessible_run_ids", [])
                if value
            ][-policy.removal_confirmations :]
            if record.verdict is VerificationVerdict.BLOCKED:
                run_ids = []
                item["accessible_confirmations"] = 0
                item["pending_verification"] = False
            elif record.verdict is VerificationVerdict.UNKNOWN:
                item["pending_verification"] = True
                pending.append(url)
            else:
                if record.run_id and record.run_id not in run_ids:
                    run_ids.append(record.run_id)
                run_ids = run_ids[-policy.removal_confirmations :]
                item["accessible_confirmations"] = len(run_ids)
                item["pending_verification"] = (
                    len(run_ids) < policy.removal_confirmations
                )
                if (
                    policy.allow_accessible_removal
                    and len(run_ids) >= policy.removal_confirmations
                ):
                    removed.append(url)
                    blacklist.add(url)
            item["accessible_run_ids"] = run_ids
            by_name[url] = item
            applied += 1
        for url in removed:
            by_name.pop(url, None)
        self.publish(list(by_name.values()))
        if removed:
            self.rewrite_url_set(self.blacklist_path, blacklist)
        return {
            "applied": applied,
            "removed": tuple(sorted(removed)),
            "pending": tuple(sorted(set(pending))),
            "remaining": len(by_name),
        }

    # ------------------------------------------------------------------
    # Validation

    def _read_meta(self):
        if not os.path.exists(self.meta_path):
            return None
        try:
            value = json.loads(
                Path(self.meta_path).read_text(encoding="utf-8")
            )
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _validate_entries(self, value):
        if not isinstance(value, list):
            raise ValueError("master JSON root must be a list")
        seen = set()
        seen_keys = {}
        for item in value:
            if not isinstance(item, dict) or not item.get("name"):
                raise ValueError("master entry must contain name")
            canonical = self.canonicalize_url(item["name"])
            if canonical != item["name"]:
                raise ValueError("master entry is not canonical")
            if canonical in seen:
                raise ValueError("master contains duplicate URL")
            seen.add(canonical)
            key = self.identity_key(canonical)
            if key in seen_keys and seen_keys[key] != canonical:
                raise ValueError("master contains identity-key collision")
            seen_keys[key] = canonical

    def _canonical_entries(self, entries):
        by_name = {}
        key_to_name = {}
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError("master entry must be an object")
            item = dict(raw)
            item["name"] = self.canonicalize_url(item.get("name", ""))
            key = self.identity_key(item["name"])
            existing_name = key_to_name.get(key)
            if existing_name is not None and existing_name != item["name"]:
                # Same identity spelled differently: keep the existing master
                # spelling; never rewrite historical case during a merge.
                merged = dict(by_name[existing_name])
                merged.update(
                    {k: v for k, v in item.items() if k != "name"}
                )
                by_name[existing_name] = merged
                continue
            by_name[item["name"]] = item
            key_to_name[key] = item["name"]
        canonical = [by_name[name] for name in sorted(by_name)]
        self._validate_entries(canonical)
        return canonical
