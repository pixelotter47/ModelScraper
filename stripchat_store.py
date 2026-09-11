"""Stripchat platform spec, configured stores, legacy inspection, backups.

Binds the generic safety kernel to Stripchat policy: URL grammar, artifact
names, result-affecting configuration and its digest, the read-only legacy
session inspector, and the immutable master backup contract.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from master_repository import (
    GenericMasterRepository,
    MasterProtectionPolicy,
)
from platform_contracts import (
    PlatformArtifactNames,
    PlatformCapabilities,
    PlatformSpec,
)
from storage_utils import (
    AtomicWriter,
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)
from stripchat_classifier import (
    API_CONTRACT_VERSION,
    CANONICALIZER_VERSION,
    CLASSIFIER_VERSION,
    canonical_model_url,
    model_lookup_key,
)
from workflow_store import GenericRunStore
from workflow_types import utc_now_iso


STRIPCHAT_SPEC = PlatformSpec(
    key="stripchat",
    display_name="Stripchat",
    session_root_name="sc sessions",
    url_canonicalizer=canonical_model_url,
    canonicalizer_version=CANONICALIZER_VERSION,
    classifier_version=CLASSIFIER_VERSION,
    api_contract_version=API_CONTRACT_VERSION,
    artifacts=PlatformArtifactNames(
        vpn="sc_vpn_list.txt",
        local="sc_local_list.txt",
        candidates="sc_candidates.txt",
        metadata="sc_metadata.json",
        debug="sc_debug_log.txt",
        final="FINAL_BLOCKED.txt",
        master_json="MASTER_BLOCKED_DATA.json",
        master_txt="MASTER_BLOCKED.txt",
        master_meta="MASTER_BLOCKED_META.json",
    ),
    capabilities=PlatformCapabilities(
        persistent_runs=True,
        resumable_verification=True,
        adaptive_api_snapshots=True,
        machine_policy_required=True,
        typed_outcomes=True,
        master_verification=False,
        waiting_for_user_events=True,
        legacy_adoption_supported=False,
    ),
)

STRIPCHAT_PROTECTION_POLICY = MasterProtectionPolicy(
    protected_flags=("manual",),
    removal_confirmations=2,
    # Initial rollout never removes master entries automatically.
    allow_accessible_removal=False,
)

# Result-affecting configuration. Every value participates in the config
# digest; changing one invalidates artifact/checkpoint reuse by design.
DEFAULT_STRIPCHAT_CONFIG: dict[str, Any] = {
    "vpn_provider": "mullvad",
    "target_relay_prefix": "ie",
    "local_recheck": True,
    "page_size": 400,
    "max_pages": 64,
    "max_models": 50_000,
    "page_retries": 3,
    "minimum_request_interval": 0.25,
    "max_retry_delay": 30.0,
    "min_successful_passes": 3,
    # A window needs three consecutive successful passes and any failed
    # pass resets the chain, so the budget has to cover one lost pass plus
    # a fresh window; at six a single transient failure ended the step with
    # five healthy passes in hand. Passes stop as soon as a window is
    # stable, so the extra budget is only spent when the feed is churning.
    "max_passes": 10,
    "required_stable_pairs": 2,
    # Compare accepted unique models across passes. A live popularity-sorted
    # feed can reshuffle during pagination; allow bounded churn while keeping
    # membership, population and duplicate gates independent.
    "jaccard_threshold": 0.85,
    "count_delta_threshold": 0.02,
    "maximum_quarantine_ratio": 0.12,
    "duplicate_ratio_threshold": 0.10,
    "generation_poll_interval": 2.0,
    "max_generation_wait": 60.0,
    "navigation_timeout_seconds": 45,
    "script_timeout_seconds": 30,
    "challenge_deadline_seconds": 600,
    "max_retry_epochs": 3,
    "attempt_budget_per_epoch": 2,
}

# Reasons that end a candidate without making it a blocked target and
# without keeping the run resumable for that candidate.
TERMINAL_NONTARGET_REASONS = frozenset(
    {
        "account_disabled_or_deleted",
        "room_offline",
        "room_not_viewable",
        "not_found",
        "login_required",
    }
)


def compute_config_digest(relevant_values: Mapping[str, Any]) -> str:
    document = {
        "canonicalizer_version": CANONICALIZER_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "api_contract_version": API_CONTRACT_VERSION,
        "values": dict(sorted(relevant_values.items())),
    }
    return sha256_bytes(canonical_json_bytes(document))


def build_store_config(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    values = dict(DEFAULT_STRIPCHAT_CONFIG)
    if overrides:
        unknown = set(overrides) - set(values)
        if unknown:
            raise ValueError(f"unknown config overrides: {sorted(unknown)}")
        values.update(overrides)
    if values["vpn_provider"] not in ("manual", "mullvad"):
        raise ValueError("unsupported VPN provider")
    if values["vpn_provider"] == "manual":
        if (overrides or {}).get("local_recheck") is True:
            raise ValueError("manual comparison cannot perform a network recheck")
        values["local_recheck"] = False
    elif not isinstance(values["target_relay_prefix"], str) or not values["target_relay_prefix"].strip():
        raise ValueError("Mullvad requires a target relay prefix")
    return {
        "digest_sha256": compute_config_digest(values),
        "canonicalizer_version": CANONICALIZER_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "api_contract_version": API_CONTRACT_VERSION,
        "relevant_values": values,
        "max_retry_epochs": values["max_retry_epochs"],
    }


class StripchatRunStore(GenericRunStore):
    """Schema-v2 run store bound to Stripchat policy."""

    def __init__(
        self,
        session_path,
        *,
        writer: AtomicWriter | None = None,
        config_overrides: Mapping[str, Any] | None = None,
        session_root=None,
    ):
        super().__init__(
            session_path,
            platform_key=STRIPCHAT_SPEC.key,
            config=build_store_config(config_overrides),
            writer=writer,
            session_root=session_root,
        )


def stripchat_master_repository(
    base_dir, writer: AtomicWriter | None = None
) -> GenericMasterRepository:
    return GenericMasterRepository(
        base_dir,
        platform_key=STRIPCHAT_SPEC.key,
        canonicalize_url=canonical_model_url,
        identity_key=model_lookup_key,
        writer=writer,
        protection_policy=STRIPCHAT_PROTECTION_POLICY,
    )


def merge_blocked_into_master(
    *,
    existing_entries,
    blocked_urls,
    manual_urls,
    blacklist,
    session_id: str,
    now_iso: str,
) -> list[dict]:
    """Union-merge this run's blocked set into the master entries.

    Initial rollout policy: never remove an existing entry, preserve
    manual protection, and skip blacklisted URLs. UNKNOWN results never
    reach this function.
    """
    parts = str(session_id).split()
    try:
        session_number = int(parts[1]) if len(parts) >= 2 else 0
    except ValueError:
        session_number = 0
    date_str = str(now_iso)[:10]
    blacklist_keys = {model_lookup_key(url) for url in blacklist}
    manual_keys = {model_lookup_key(url) for url in manual_urls}
    merged: dict[str, dict] = {}
    for entry in existing_entries:
        item = dict(entry)
        key = model_lookup_key(item["name"])
        if key in manual_keys:
            item["manual"] = True
        merged[key] = item
    for url in blocked_urls:
        key = model_lookup_key(url)
        if key in blacklist_keys or key in merged:
            continue
        merged[key] = {
            "name": url,
            "date": date_str,
            "session": session_number,
            "manual": key in manual_keys,
            "timestamp": str(now_iso)[:19],
        }
    for url in manual_urls:
        key = model_lookup_key(url)
        if key in blacklist_keys:
            continue
        if key not in merged:
            merged[key] = {
                "name": url,
                "date": date_str,
                "session": 0,
                "manual": True,
                "timestamp": str(now_iso)[:19],
            }
    return [merged[key] for key in sorted(merged)]


# Root compatibility projections: filename -> (artifact id, mode).
STRIPCHAT_PROJECTIONS = {
    "FINAL_BLOCKED.txt": ("step4.final_blocked", "lines"),
    "sc_vpn_list.txt": ("step1.vpn_urls", "lines"),
    "sc_local_list.txt": ("step2.local_urls", "lines"),
    "sc_candidates.txt": ("step3.candidates", "lines"),
    "sc_debug_log.txt": ("step3.candidates", "joined"),
    "sc_metadata.json": ("step1.vpn_metadata", "bytes"),
}


# ---------------------------------------------------------------------------
# Read-only legacy inspection

_LEGACY_INTERMEDIATES = (
    "sc_vpn_list.txt",
    "sc_local_list.txt",
    "sc_candidates.txt",
    "sc_metadata.json",
    "sc_debug_log.txt",
)


def inspect_legacy_session(session_path) -> dict[str, Any]:
    """Classify one legacy session without touching a single byte."""
    session = Path(session_path)
    if not session.is_dir():
        return {"classification": "legacy_invalid", "reason": "missing"}
    files: dict[str, dict[str, Any]] = {}
    for name in _LEGACY_INTERMEDIATES + ("FINAL_BLOCKED.txt",):
        path = session / name
        if path.is_file():
            stat = path.stat()
            files[name] = {
                "byte_size": stat.st_size,
                "mtime": stat.st_mtime,
                "sha256": sha256_file(path),
                "line_count": sum(
                    1
                    for line in path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    if line.strip()
                ),
            }
    has_manifest = (session / "run_state.json").is_file() or (
        session / "runs"
    ).is_dir()
    final = files.get("FINAL_BLOCKED.txt")
    intermediates = {
        name: info
        for name, info in files.items()
        if name != "FINAL_BLOCKED.txt"
    }
    if has_manifest:
        classification = "hardened"
        reason = "manifest-backed session"
    elif final is None and not intermediates:
        classification = "legacy_invalid"
        reason = "no recognizable artifacts"
    elif final is None:
        classification = "legacy_incomplete"
        reason = "intermediates without a final"
    elif any(
        info["mtime"] > final["mtime"] for info in intermediates.values()
    ):
        classification = "legacy_ambiguous"
        reason = "an intermediate is newer than the final"
    else:
        classification = "legacy_complete"
        reason = "final is the newest artifact"
    resumable = False  # legacy sessions are never a hardened resume source
    report = {
        "session_id": session.name,
        "classification": classification,
        "reason": reason,
        "resumable": resumable,
        "files": {
            name: {
                key: value
                for key, value in info.items()
                if key != "mtime"
            }
            for name, info in files.items()
        },
    }
    return report


def inspect_legacy_sessions(base_dir) -> list[dict[str, Any]]:
    root = Path(base_dir)
    if not root.is_dir():
        return []
    reports = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            reports.append(inspect_legacy_session(entry))
    return reports


# ---------------------------------------------------------------------------
# Immutable master backup contract

BACKUP_SCHEMA_VERSION = 1


class MasterBackupError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


def default_backup_root(workspace_id: str) -> Path:
    root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(
        root, "ModelScraper", "backups", workspace_id, "stripchat-master"
    )


def create_master_backup(
    base_dir,
    *,
    workspace_id: str,
    operation: str,
    intended_transaction_id: str | None = None,
    backups_root=None,
) -> dict[str, Any]:
    """Create a verified immutable backup of the live Stripchat master.

    Never mutates the live master. Fails closed if the live master is
    invalid; a backup must never launder inconsistent data as valid.
    """
    repository = stripchat_master_repository(base_dir)
    if not os.path.exists(repository.json_path) or not os.path.exists(
        repository.txt_path
    ):
        raise MasterBackupError(
            "backup_source_missing", "live master JSON/TXT not found"
        )
    consistency = repository.validate_consistency(repair_txt=False)
    if not consistency.valid:
        raise MasterBackupError(
            "backup_source_invalid",
            consistency.error_code or "master inconsistent",
        )
    backup_id = str(uuid.uuid4())
    parent = Path(backups_root) if backups_root else default_backup_root(
        workspace_id
    )
    parent.mkdir(parents=True, exist_ok=True)
    final_dir = parent / backup_id
    if final_dir.exists():
        raise MasterBackupError("backup_id_collision", str(final_dir))
    temp_dir = parent / f".tmp-{backup_id}"
    temp_dir.mkdir(parents=False, exist_ok=False)
    try:
        (temp_dir / ".modelscraper-backup-owner").write_text(
            canonical_json_bytes(
                {
                    "application": "ModelScraper",
                    "schema_version": BACKUP_SCHEMA_VERSION,
                    "backup_id": backup_id,
                }
            ).decode("utf-8"),
            encoding="utf-8",
        )
        copies = {}
        for source, name in (
            (repository.json_path, "MASTER_BLOCKED_DATA.json"),
            (repository.txt_path, "MASTER_BLOCKED.txt"),
        ):
            shutil.copyfile(source, temp_dir / name)
            copies[name] = {
                "byte_size": (temp_dir / name).stat().st_size,
                "sha256": sha256_file(temp_dir / name),
            }
            if copies[name]["sha256"] != sha256_file(source):
                raise MasterBackupError(
                    "backup_copy_mismatch", f"{name} changed during backup"
                )
        if os.path.exists(repository.meta_path):
            shutil.copyfile(
                repository.meta_path, temp_dir / "MASTER_BLOCKED_META.json"
            )
            copies["MASTER_BLOCKED_META.json"] = {
                "byte_size": (temp_dir / "MASTER_BLOCKED_META.json")
                .stat()
                .st_size,
                "sha256": sha256_file(
                    temp_dir / "MASTER_BLOCKED_META.json"
                ),
            }
        else:
            (temp_dir / "MASTER_BLOCKED_META.absent").write_bytes(b"")
        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "backup_id": backup_id,
            "workspace_id": workspace_id,
            "platform_key": STRIPCHAT_SPEC.key,
            "source_root": str(Path(base_dir).resolve()),
            "created_at": utc_now_iso(),
            "item_count": consistency.json_count,
            "files": copies,
            "meta_present": "MASTER_BLOCKED_META.json" in copies,
            "canonicalizer_version": CANONICALIZER_VERSION,
            "json_txt_sets_equal": True,
            "creator_operation": str(operation),
            "intended_transaction_id": intended_transaction_id,
        }
        AtomicWriter().write_bytes(
            temp_dir / "backup_manifest.json", canonical_json_bytes(manifest)
        )
        os.replace(temp_dir, final_dir)
    except MasterBackupError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise MasterBackupError("backup_failed", str(exc)) from exc
    validation = validate_master_backup(final_dir)
    if not validation["valid"]:
        raise MasterBackupError(
            "backup_validation_failed", validation.get("reason", "")
        )
    return {
        "backup_id": backup_id,
        "path": str(final_dir),
        "manifest": manifest,
    }


def validate_master_backup(backup_dir) -> dict[str, Any]:
    """Independently re-validate a finished backup directory."""
    backup = Path(backup_dir)
    manifest_path = backup / "backup_manifest.json"
    if not manifest_path.is_file():
        return {"valid": False, "reason": "backup manifest missing"}
    try:
        import json

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"valid": False, "reason": f"manifest unreadable: {exc}"}
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        return {"valid": False, "reason": "unsupported backup schema"}
    for name, expected in (manifest.get("files") or {}).items():
        path = backup / name
        if not path.is_file():
            return {"valid": False, "reason": f"{name} missing"}
        if path.stat().st_size != expected.get("byte_size"):
            return {"valid": False, "reason": f"{name} size changed"}
        if sha256_file(path) != expected.get("sha256"):
            return {"valid": False, "reason": f"{name} hash changed"}
    if not manifest.get("meta_present") and not (
        backup / "MASTER_BLOCKED_META.absent"
    ).is_file():
        return {"valid": False, "reason": "absent-meta marker missing"}
    try:
        import json

        entries = json.loads(
            (backup / "MASTER_BLOCKED_DATA.json").read_text(encoding="utf-8")
        )
        urls = [entry.get("name") for entry in entries]
        for url in urls:
            canonical_model_url(url)
        txt = [
            line.strip()
            for line in (backup / "MASTER_BLOCKED.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        if set(urls) != set(txt):
            return {"valid": False, "reason": "backup JSON/TXT set mismatch"}
    except (OSError, ValueError) as exc:
        return {"valid": False, "reason": f"backup content invalid: {exc}"}
    return {"valid": True, "manifest": manifest}
