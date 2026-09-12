"""Machine-global VPN/Chrome policy lease, journal, and recovery.

Mullvad connection state and Chrome split-tunnel routing are global to the
Windows machine, so they need a fence wider than the per-checkout workspace
lease. This module provides that fence, an immutable per-transaction
journal written before the first mutation, and demand-driven recovery. It
never installs a background watchdog and never kills another process.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from public_runtime import RUNTIME_NAMESPACE, checkout_root
from runtime_lock import WorkflowBusyError, WorkflowLease
from storage_utils import AtomicWriter, canonical_json_bytes
from workflow_types import utc_now_iso

SCHEMA_VERSION = 1

TRANSACTION_STATUSES = (
    "pending",
    "restoring",
    "restored",
    "restored_manifest_pending",
    "failed",
)
MANIFEST_SYNC_STATUSES = (
    "pending",
    "synced",
    "origin_busy",
    "origin_missing",
    "invalid_origin",
)
VPN_STATES = ("connected", "disconnected", "unknown")


class MachinePolicyError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


def workspace_id(workspace) -> str:
    # Journal paths and identities must agree for Windows short-path aliases.
    normalized = os.path.normcase(str(Path(workspace).resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def default_runtime_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(root, "ModelScraper", "runtime", "machine_policy")


@dataclass(frozen=True)
class MachinePolicyTarget:
    relay_code: str
    vpn_connected_at_end: bool = True
    chrome_excluded_at_end: bool = True

    def to_dict(self) -> dict:
        return {
            "relay_code": self.relay_code,
            "vpn_connected_at_end": self.vpn_connected_at_end,
            "chrome_excluded_at_end": self.chrome_excluded_at_end,
        }


@dataclass(frozen=True)
class MachinePolicySnapshot:
    vpn_state: str
    relay_code: str | None
    chrome_excluded: bool | None
    checked_at: str

    def to_dict(self) -> dict:
        return {
            "vpn_state": self.vpn_state,
            "relay_code": self.relay_code,
            "chrome_excluded": self.chrome_excluded,
            "checked_at": self.checked_at,
        }


class MachinePolicyLease:
    """Machine-wide lock shared by every ModelScraper checkout."""

    def __init__(self, *, runtime_dir=None, owner: Mapping[str, Any] | None = None):
        self.runtime_dir = Path(runtime_dir or default_runtime_dir())
        # A fixed application-scoped path, never a workspace hash: the
        # resource being fenced is the machine, not this checkout.
        self._lease = WorkflowLease(
            "modelscraper-machine-policy",
            run_id=str((owner or {}).get("run_id") or ""),
            platform=str((owner or {}).get("platform_key") or ""),
            task="machine-policy",
            runtime_dir=str(self.runtime_dir / "locks"),
        )
        self._lease.path = self.runtime_dir / "locks" / "machine-policy.lock"

    def acquire(self):
        try:
            self._lease.acquire()
        except WorkflowBusyError as exc:
            raise MachinePolicyError(
                "machine_lease_busy", json.dumps(exc.owner)[:200]
            ) from exc
        return self

    def release(self):
        self._lease.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False


def observe_policy(vpn) -> MachinePolicySnapshot:
    """Normalize the controller's view; never store raw CLI output."""
    state = "unknown"
    relay = None
    chrome_excluded = None
    try:
        status = vpn.status()
        if vpn.is_connected(status):
            state = "connected"
            for token in str(status).split():
                if "-wg-" in token or "-ovpn-" in token:
                    relay = token.strip().rstrip(",")
                    break
            if relay is None:
                relay = getattr(vpn, "relay_location", None)
        elif vpn.is_disconnected(status):
            state = "disconnected"
    except Exception:
        state = "unknown"
    try:
        apps = vpn.split_tunnel_apps()
        chrome_excluded = vpn._path_in_list(vpn.chrome_path, apps)
    except Exception:
        chrome_excluded = None
    return MachinePolicySnapshot(
        vpn_state=state,
        relay_code=relay,
        chrome_excluded=chrome_excluded,
        checked_at=utc_now_iso(),
    )


class MachinePolicyJournal:
    """Shared recovery pointer, with writes restricted to this checkout.

    The global journal is deliberately retained so an unfinished personal
    installation cannot be silently bypassed. Public recovery refuses a
    foreign transaction; it must be repaired by its originating checkout.
    Explicit runtime directories are injectable for isolated tests and tools.
    """

    def __init__(
        self, runtime_dir=None, writer: AtomicWriter | None = None, *, workspace=None
    ):
        self.runtime_dir = Path(runtime_dir or default_runtime_dir())
        if workspace is None and runtime_dir is None:
            workspace = checkout_root()
        self.origin_workspace_id = workspace_id(workspace) if workspace is not None else None
        self.writer = writer or AtomicWriter()
        self.active_path = self.runtime_dir / "active.json"
        self.transactions_dir = self.runtime_dir / "transactions"

    def owns_document(self, document) -> bool:
        if self.origin_workspace_id is None:
            return True
        return bool(
            isinstance(document, dict)
            and document.get("runtime_namespace") == RUNTIME_NAMESPACE
            and document.get("origin_workspace_id") == self.origin_workspace_id
        )

    def require_owned_document(self, document) -> None:
        if not self.owns_document(document):
            raise MachinePolicyError(
                "machine_policy_foreign_workspace",
                "A different ModelScraper checkout owns the pending machine "
                "policy. Restore it from that checkout before continuing.",
            )

    def begin(
        self,
        *,
        origin_workspace,
        origin_session_relative_path: str,
        origin_manifest_relative_path: str,
        task_id: str,
        platform_key: str,
        session_id: str,
        run_id: str,
        generation_id: str,
        target: MachinePolicyTarget,
        initial: MachinePolicySnapshot,
    ) -> dict:
        """Write the immutable journal and active pointer BEFORE mutating."""
        if self.origin_workspace_id is not None:
            if workspace_id(origin_workspace) != self.origin_workspace_id:
                self.require_owned_document(None)
            active = self.load_active()
            if active is not None:
                self.require_owned_document(active)
        transaction_id = str(uuid.uuid4())
        self.transactions_dir.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": SCHEMA_VERSION,
            "runtime_namespace": RUNTIME_NAMESPACE,
            "transaction_id": transaction_id,
            "origin_workspace_id": workspace_id(origin_workspace),
            "origin_workspace_path": str(
                Path(origin_workspace).resolve()
            ),
            "origin_session_relative_path": origin_session_relative_path,
            "origin_manifest_relative_path": origin_manifest_relative_path,
            "task_id": task_id,
            "platform_key": platform_key,
            "session_id": session_id,
            "run_id": run_id,
            "generation_id": generation_id,
            "status": "pending",
            "manifest_sync_status": "pending",
            "target": target.to_dict(),
            "initial": initial.to_dict(),
            "transitions": [],
            "restoration_attempts": [],
            "updated_at": utc_now_iso(),
        }
        path = self.transactions_dir / f"{transaction_id}.json"
        self.writer.write_bytes(path, canonical_json_bytes(document))
        self.writer.write_bytes(
            self.active_path,
            canonical_json_bytes(
                {
                    "schema_version": SCHEMA_VERSION,
                    "transaction_id": transaction_id,
                    "transaction_relative_path": (
                        f"transactions/{transaction_id}.json"
                    ),
                    "updated_at": utc_now_iso(),
                }
            ),
        )
        return document

    def load_active(self) -> dict | None:
        if not self.active_path.is_file():
            return None
        try:
            pointer = json.loads(
                self.active_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            if self.origin_workspace_id is not None:
                raise MachinePolicyError("machine_policy_recovery_required")
            return None
        if not isinstance(pointer, dict):
            raise MachinePolicyError("machine_policy_recovery_required")
        transaction_id = pointer.get("transaction_id")
        if not transaction_id:
            return None
        document = self.load_transaction(transaction_id)
        if document is None and self.origin_workspace_id is not None:
            raise MachinePolicyError("machine_policy_recovery_required")
        return document

    def load_transaction(self, transaction_id) -> dict | None:
        path = self.transactions_dir / f"{transaction_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def record_transition(
        self, transaction_id, *, action: str, intended: str, observed, error_code=None
    ) -> dict:
        def mutate(document):
            document["transitions"].append(
                {
                    "action": action,
                    "intended": intended,
                    "observed": (
                        observed.to_dict()
                        if isinstance(observed, MachinePolicySnapshot)
                        else observed
                    ),
                    "error_code": error_code,
                    "at": utc_now_iso(),
                }
            )

        return self._update(transaction_id, mutate)

    def record_restoration_attempt(
        self, transaction_id, *, results: Mapping[str, Any], status: str
    ) -> dict:
        if status not in TRANSACTION_STATUSES:
            raise MachinePolicyError(
                "invalid_request", f"invalid status {status!r}"
            )

        def mutate(document):
            document["restoration_attempts"].append(
                {"results": dict(results), "at": utc_now_iso()}
            )
            document["status"] = status

        return self._update(transaction_id, mutate)

    def set_manifest_sync(self, transaction_id, status: str) -> dict:
        if status not in MANIFEST_SYNC_STATUSES:
            raise MachinePolicyError(
                "invalid_request", f"invalid sync status {status!r}"
            )
        return self._update(
            transaction_id,
            lambda document: document.__setitem__(
                "manifest_sync_status", status
            ),
        )

    def clear_active(self, transaction_id) -> None:
        """Clear the pointer only after the global target is verified."""
        self.require_owned_document(self.load_transaction(transaction_id))
        pointer = None
        if self.active_path.is_file():
            try:
                pointer = json.loads(
                    self.active_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                pointer = None
        if pointer and pointer.get("transaction_id") != transaction_id:
            return
        self.writer.write_bytes(
            self.active_path,
            canonical_json_bytes(
                {
                    "schema_version": SCHEMA_VERSION,
                    "transaction_id": None,
                    "transaction_relative_path": None,
                    "updated_at": utc_now_iso(),
                }
            ),
        )

    def _update(self, transaction_id, mutate) -> dict:
        document = self.load_transaction(transaction_id)
        if document is None:
            raise MachinePolicyError(
                "invalid_request", "transaction journal missing"
            )
        self.require_owned_document(document)
        mutate(document)
        document["updated_at"] = utc_now_iso()
        self.writer.write_bytes(
            self.transactions_dir / f"{transaction_id}.json",
            canonical_json_bytes(document),
        )
        return document


class MachinePolicyController:
    """Applies and restores machine policy under the machine lease."""

    def __init__(
        self,
        vpn,
        journal: MachinePolicyJournal,
        *,
        logger=None,
        stop_token=None,
    ):
        self.vpn = vpn
        self.journal = journal
        self.logger = logger or (lambda _message: None)
        self.stop_token = stop_token

    def observe(self) -> MachinePolicySnapshot:
        return observe_policy(self.vpn)

    def apply_vpn_route_for_snapshot(self, transaction_id) -> None:
        """Chrome on the VPN route, then the configured relay connected."""
        self.vpn.ensure_chrome_not_in_split_tunnel()
        self.journal.record_transition(
            transaction_id,
            action="chrome_on_vpn_route",
            intended="chrome_not_excluded",
            observed=self.observe(),
        )
        self.vpn.connect_relay()
        self.journal.record_transition(
            transaction_id,
            action="connect_relay",
            intended="connected",
            observed=self.observe(),
        )

    def disconnect_for_local(self, transaction_id) -> None:
        self.vpn.disconnect()
        self.journal.record_transition(
            transaction_id,
            action="disconnect",
            intended="disconnected",
            observed=self.observe(),
        )

    def restore(self, transaction_id, target: MachinePolicyTarget) -> dict:
        """Restore both targets independently; one failure never skips the other."""
        self.journal.require_owned_document(
            self.journal.load_transaction(transaction_id)
        )
        results: dict[str, Any] = {}
        try:
            if target.vpn_connected_at_end:
                self.vpn.ensure_connected_relay()
            else:
                self.vpn.disconnect()
            results["vpn"] = {"ok": True, "error_code": None}
        except Exception as exc:
            results["vpn"] = {
                "ok": False,
                "error_code": "vpn_transition_failed",
                "message": str(exc)[:160],
            }
        try:
            if target.chrome_excluded_at_end:
                self.vpn.ensure_chrome_in_split_tunnel()
            else:
                self.vpn.ensure_chrome_not_in_split_tunnel()
            results["chrome"] = {"ok": True, "error_code": None}
        except Exception as exc:
            results["chrome"] = {
                "ok": False,
                "error_code": "split_tunnel_failed",
                "message": str(exc)[:160],
            }
        observed = self.observe()
        results["observed"] = observed.to_dict()
        ok = results["vpn"]["ok"] and results["chrome"]["ok"]
        status = "restored" if ok else "failed"
        try:
            self.journal.record_restoration_attempt(
                transaction_id, results=results, status=status
            )
            results["persisted"] = True
        except Exception as exc:
            # Correct machine state plus a failed journal write is NOT a
            # full success; the caller must report it truthfully.
            results["persisted"] = False
            results["persist_error"] = str(exc)[:160]
            ok = False
            results["error_code"] = "restoration_persist_failed"
        results["ok"] = ok
        return results

    def recover_pending(self, *, origin_lease_factory=None) -> dict | None:
        """Repair a machine target left behind by a crashed run.

        Returns None when nothing is pending. Never changes machine policy
        again once the global target is verified; a missing or busy origin
        only defers the manifest mirror.
        """
        document = self.journal.load_active()
        if document is None:
            return None
        if not self.journal.owns_document(document):
            return {
                "recovered": False,
                "error_code": "machine_policy_foreign_workspace",
            }
        if document.get("status") in ("restored", "restored_manifest_pending"):
            self.journal.clear_active(document["transaction_id"])
            return {
                "transaction_id": document["transaction_id"],
                "recovered": False,
                "reason": "already restored",
            }
        target_values = document.get("target") or {}
        target = MachinePolicyTarget(
            relay_code=str(target_values.get("relay_code") or "ie"),
            vpn_connected_at_end=bool(
                target_values.get("vpn_connected_at_end", True)
            ),
            chrome_excluded_at_end=bool(
                target_values.get("chrome_excluded_at_end", True)
            ),
        )
        results = self.restore(document["transaction_id"], target)
        if not results.get("ok"):
            return {
                "transaction_id": document["transaction_id"],
                "recovered": False,
                "error_code": "machine_policy_recovery_required",
                "results": results,
            }
        sync_status = self._mirror_origin(document, origin_lease_factory)
        self.journal.set_manifest_sync(
            document["transaction_id"], sync_status
        )
        if sync_status != "synced":
            self.journal.record_restoration_attempt(
                document["transaction_id"],
                results={"manifest_sync": sync_status},
                status="restored_manifest_pending",
            )
        self.journal.clear_active(document["transaction_id"])
        return {
            "transaction_id": document["transaction_id"],
            "recovered": True,
            "manifest_sync_status": sync_status,
            "results": results,
        }

    def _mirror_origin(self, document, origin_lease_factory) -> str:
        if not self.journal.owns_document(document):
            return "invalid_origin"
        origin_path = document.get("origin_workspace_path") or ""
        if not origin_path:
            return "invalid_origin"
        origin = Path(origin_path)
        if not origin.is_dir():
            return "origin_missing"
        if workspace_id(origin) != document.get("origin_workspace_id"):
            return "invalid_origin"
        manifest_relative = document.get("origin_manifest_relative_path") or ""
        if not manifest_relative or manifest_relative.startswith(("/", "\\")):
            return "invalid_origin"
        manifest_path = (origin / manifest_relative).resolve()
        if origin.resolve() not in manifest_path.parents:
            return "invalid_origin"
        if not manifest_path.is_file():
            return "origin_missing"
        if origin_lease_factory is None:
            return "origin_busy"
        try:
            lease = origin_lease_factory(origin)
        except Exception:
            return "origin_busy"
        try:
            with lease:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                restoration = dict(manifest.get("restoration") or {})
                restoration.update(
                    {
                        "required": True,
                        "state": "restored",
                        "desired_final_policy": document.get("target"),
                        "original_policy": document.get("initial"),
                        "last_error_code": None,
                        "updated_at": utc_now_iso(),
                    }
                )
                manifest["restoration"] = restoration
                manifest["manifest_revision"] = (
                    int(manifest.get("manifest_revision", 1)) + 1
                )
                manifest["updated_at"] = utc_now_iso()
                self.journal.writer.write_bytes(
                    manifest_path, canonical_json_bytes(manifest)
                )
        except (WorkflowBusyError, MachinePolicyError):
            return "origin_busy"
        except (OSError, ValueError):
            return "invalid_origin"
        return "synced"
