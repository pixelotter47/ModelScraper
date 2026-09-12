import datetime
import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path

from app_settings import AppSettings, install_shared_settings
from ctb_core import CTBRunner, active_session_display_priority
from mfcscrape import MFCRunner
from mullvad_vpn import MullvadVpnController
from platform_workflow import RunSelection, WorkflowOptions
from runtime_lock import WorkflowBusyError, WorkflowLease
from session_registry import SessionPathError, SessionRegistry
from storage_utils import AtomicWriter
from vpn_providers import create_controller, provider
from stripchat_browser_runtime import StripchatChromeRuntime
from stripchat_core import StripchatRunner
from temp_profile import cleanup_orphan_profiles
from workflow_coordinator import (
    HardenedWorkflowCoordinator,
    WorkflowCoordinator,
)
from workflow_types import (
    OutcomeStatus,
    ProgressEvent,
    RunOutcome,
    StepName,
    StepOutcome,
    utc_now_iso,
)
from xhamsterlive_core import XHamsterLiveRunner


PLATFORMS = ("Chaturbate", "MyFreeCams", "Stripchat", "XHamsterLive")

# Platforms whose page classification requires a normal browser window.
VISIBLE_BROWSER_PLATFORMS = ("Stripchat",)
SESSION_MASTER_ADDITIONS_FILE = "master_additions.json"


class UnsupportedBrowserMode(ValueError):
    """An explicit headless request a platform cannot honor."""

    code = "unsupported_browser_mode"


class ModelScraperService:
    def __init__(
        self,
        base_dir,
        runners=None,
        vpn_factory=MullvadVpnController,
        *,
        settings=None,
        adapters=None,
        hardened_coordinator_factory=HardenedWorkflowCoordinator,
        stripchat_runtime_factory=StripchatChromeRuntime,
    ):
        self.base_dir = os.path.abspath(base_dir)
        self.settings = settings or AppSettings(self.base_dir)
        install_shared_settings(self.settings)
        self._vpn_factory = vpn_factory
        self._hardened_coordinator_factory = hardened_coordinator_factory
        self._stripchat_runtime_factory = stripchat_runtime_factory
        self._stripchat_runtime = None
        self._lock = threading.RLock()
        self._subscribers = set()
        self._logs = []
        self._max_logs = 500

        self._status = "Idle"
        self._busy = False
        self._session_path = ""
        self._blocked_models = []
        self._current_task = ""
        self._task_platform = ""
        self._task_error = None
        self._last_outcome = None
        # Per-platform state so switching platforms cannot show another
        # platform's outcome as if it belonged to this one.
        self._outcomes_by_platform = {}
        self._progress = {}
        self._manual_mfc_references = {}
        self._thread = None
        self._platform = "Chaturbate"
        self._master_list_sort_mode = "date_newest"
        self._master_list_country_filter = "All"

        self._effective_options = {"headless": None, "startMinimized": True}
        self._runners = runners or self._create_default_runners()
        self._adapters = (
            self._create_adapters() if adapters is None else dict(adapters)
        )
        cleanup_report = cleanup_orphan_profiles(logger=self._log)
        if cleanup_report["removed"]:
            self._log(
                f"[INFO] Removed {len(cleanup_report['removed'])} "
                "owned orphan Chrome profiles."
            )
        self._session_registry = SessionRegistry(
            {
                platform: getattr(runner, "base_dir", runner.session_folder)
                for platform, runner in self._runners.items()
            }
        )
        self._session_new_master_models = {
            platform: None for platform in self._runners
        }
        self._initial_master_models = self._load_initial_master_models()
        self._log(
            "[INFO] App started. Master list baseline: "
            + ", ".join(
                f"{platform}={len(models)}"
                for platform, models in self._initial_master_models.items()
            )
        )

    def _create_default_runners(self):
        ctb_sessions_dir = os.path.join(self.base_dir, "cb sessions")
        mfc_sessions_dir = os.path.join(self.base_dir, "mfc sessions")
        sc_sessions_dir = os.path.join(self.base_dir, "sc sessions")
        xhl_sessions_dir = os.path.join(self.base_dir, "xhl sessions")
        for session_dir in (
            ctb_sessions_dir,
            mfc_sessions_dir,
            sc_sessions_dir,
            xhl_sessions_dir,
        ):
            os.makedirs(session_dir, exist_ok=True)
        return {
            "Chaturbate": CTBRunner(
                ctb_sessions_dir, logger=self._log, settings=self.settings
            ),
            "MyFreeCams": MFCRunner(mfc_sessions_dir, logger=self._log),
            "Stripchat": StripchatRunner(sc_sessions_dir, logger=self._log),
            "XHamsterLive": XHamsterLiveRunner(xhl_sessions_dir, logger=self._log),
        }

    def update_settings(self, updates):
        """Validate and persist a preference change under the workflow fence."""
        if not isinstance(updates, dict) or not updates:
            raise ValueError("Provide at least one preference.")
        with self._lock:
            if self._busy:
                raise RuntimeError("Stop the active workflow before changing preferences.")
            try:
                with WorkflowLease(self.base_dir, task="Update Preferences"):
                    # Another GUI/Web process may have changed preferences since startup.
                    self.settings.load(strict=True)
                    validated = {}
                    for key, value in updates.items():
                        normalized = self.settings._coerce(key, value)
                        if normalized is None:
                            raise ValueError(f"Invalid preference: {key}")
                        validated[key] = normalized
                    previous = self.settings.as_dict()
                    self.settings._values.update(validated)
                    if not self.settings.save():
                        self.settings._values = previous
                        raise OSError("Could not save preferences.")
                    if previous.get("vpnProvider") != self.settings.get("vpnProvider"):
                        self._manual_mfc_references = {}
                    install_shared_settings(self.settings)
            except WorkflowBusyError as exc:
                raise RuntimeError("Another process owns the workflow.") from exc
        self._emit_state()
        return True

    def _current_vpn_provider(self):
        previous = self.settings.get("vpnProvider")
        self.settings.load(strict=True)
        if previous != self.settings.get("vpnProvider"):
            self._manual_mfc_references = {}
        install_shared_settings(self.settings)
        return provider(self.settings)

    def get_settings(self):
        """Expose fresh idle preferences; preserve active task configuration."""
        with self._lock:
            if not self._busy:
                self._current_vpn_provider()
            return self.settings.as_dict()

    def edit_search_providers(self, operation, *args):
        if operation not in {
            "set_provider_enabled", "add_provider", "remove_provider",
            "move_provider", "reset_providers",
        }:
            raise ValueError("Unsupported search provider operation.")
        with self._lock:
            if self._busy:
                raise RuntimeError("Stop the active workflow before changing preferences.")
            try:
                with WorkflowLease(self.base_dir, task="Update Search Providers"):
                    self.settings.load(strict=True)
                    return getattr(self.settings, operation)(*args)
            except WorkflowBusyError as exc:
                raise RuntimeError("Another process owns the workflow.") from exc

    def _new_vpn_controller(self):
        # Resolve fresh persisted preferences, including edits from another UI.
        self._current_vpn_provider()
        return create_controller(self.settings, mullvad_factory=self._vpn_factory)

    def _manual_network_gate(self, step, confirmed):
        try:
            selected = self._current_vpn_provider()
        except (OSError, ValueError) as exc:
            self._log(f"[ERROR] Invalid VPN preferences: {exc}")
            return False
        if not selected.automatic and step is not StepName.COMPARE and not confirmed:
            route = "VPN connected and browser routed through it" if step is StepName.VPN_SNAPSHOT else "VPN disconnected or browser bypassing it"
            self._log(f"[WARN] Manual network confirmation required: {route}.")
            return False
        return True

    @staticmethod
    def resolve_headless(platform, requested):
        """Resolve the platform-safe browser mode.

        ``None`` means "use the platform default". An explicit headless
        request for a visible-only platform is rejected rather than
        silently downgraded, so the UI can never misreport how the
        browser ran.
        """
        if platform in VISIBLE_BROWSER_PLATFORMS:
            if requested is True:
                raise UnsupportedBrowserMode(platform)
            return False
        return True if requested is None else bool(requested)

    def _create_adapters(self):
        """Hardened adapters keyed by GUI platform name.

        Only Stripchat is adapter-driven so far; the other platforms keep
        their legacy runner path until they are hardened too.
        """
        adapters = {}
        try:
            from stripchat_adapter import StripchatWorkflowAdapter

            runtime = self._stripchat_runtime_factory(logger=self._log)
            self._stripchat_runtime = runtime
            adapters["Stripchat"] = StripchatWorkflowAdapter(
                os.path.join(self.base_dir, "sc sessions"),
                logger=self._log,
                driver_factory=runtime,
                window_manager=runtime,
                policy_observer=self._observe_stripchat_policy,
                shadow=self._stripchat_shadow_mode(),
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"[WARN] Stripchat adapter unavailable: {exc}")
        return adapters

    def _observe_stripchat_policy(self):
        from machine_policy import observe_policy

        if not self._current_vpn_provider().automatic:
            return {"vpn_state": "unknown", "relay_code": None, "source": "manual"}
        return observe_policy(self._new_vpn_controller()).to_dict()

    @staticmethod
    def _stripchat_shadow_mode():
        """Publication is live; shadow is the explicit rehearsal opt-out.

        Shadow stages the final list and the master revision and then stops
        before the commit witness, so a run verifies its candidates and
        changes nothing anyone can see - no master entries, no session
        projections. That was the right default while the hardened flow was
        unproven. It has now run end to end against the live site, so the
        default is to publish; ``MODEL_SCRAPER_STRIPCHAT_WORKFLOW=shadow``
        still asks for a rehearsal.
        """
        value = os.environ.get("MODEL_SCRAPER_STRIPCHAT_WORKFLOW", "")
        return str(value).strip().lower() == "shadow"

    def _load_initial_master_models(self):
        result = {}
        for platform, runner in self._runners.items():
            try:
                adapter = self._adapters.get(platform)
                models = (
                    adapter.get_master_models()
                    if adapter is not None
                    else runner.get_master_list_models()
                )
                result[platform] = {
                    model.get("name")
                    for model in models
                    if model.get("name")
                }
            except Exception:
                result[platform] = set()
        return result

    @staticmethod
    def _model_names(models):
        return {
            model.get("name")
            for model in models
            if isinstance(model, dict) and model.get("name")
        }

    @staticmethod
    def _session_master_additions_path(session_path):
        return os.path.join(session_path, SESSION_MASTER_ADDITIONS_FILE)

    def _read_session_master_additions(self, session_path):
        path = self._session_master_additions_path(session_path)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("unsupported master additions record")
        if payload.get("platform") != "Chaturbate":
            raise ValueError("master additions platform mismatch")
        if payload.get("session") != os.path.basename(
            os.path.normpath(session_path)
        ):
            raise ValueError("master additions session mismatch")
        models = payload.get("models")
        if not isinstance(models, list) or not all(
            isinstance(item, str) and item.strip() for item in models
        ):
            raise ValueError("invalid master additions model list")
        return {item.strip() for item in models}

    @staticmethod
    def _session_number_and_date(session_path):
        parts = os.path.basename(os.path.normpath(session_path)).split()
        if len(parts) < 3 or parts[0].lower() != "session":
            return None, None
        try:
            session_number = int(parts[1])
            session_date = datetime.datetime.strptime(
                parts[2], "%d.%m.%Y"
            ).strftime("%Y-%m-%d")
        except ValueError:
            return None, None
        return session_number, session_date

    def _infer_chaturbate_master_additions(self, session_path):
        """Recover one completed run's additions from its master publication."""
        session_number, session_date = self._session_number_and_date(session_path)
        if session_number is None:
            return None

        state_path = os.path.join(session_path, "run_state.json")
        runner = self._runners["Chaturbate"]
        master_path = runner.get_master_json_path()
        backup_path = master_path + ".bak"
        if not all(
            os.path.exists(path)
            for path in (state_path, master_path, backup_path)
        ):
            return None

        try:
            with open(state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            if state.get("status") != "succeeded":
                return None
            finished_at = datetime.datetime.fromisoformat(state["updated_at"])
            if finished_at.tzinfo is None:
                finished_at = finished_at.replace(datetime.timezone.utc)
            publication_delay = (
                os.path.getmtime(master_path) - finished_at.timestamp()
            )
            if publication_delay < -2 or publication_delay > 300:
                return None
            with open(master_path, "r", encoding="utf-8") as handle:
                current = json.load(handle)
            with open(backup_path, "r", encoding="utf-8") as handle:
                previous = json.load(handle)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

        if not isinstance(current, list) or not isinstance(previous, list):
            return None
        previous_names = self._model_names(previous)
        added_entries = [
            item
            for item in current
            if isinstance(item, dict) and item.get("name") not in previous_names
        ]
        if any(
            item.get("manual")
            or item.get("session") != session_number
            or item.get("date") != session_date
            for item in added_entries
        ):
            return None
        return self._model_names(added_entries)

    def _write_session_master_additions(self, session_path, models, *, source):
        normalized = sorted(
            {
                item.strip()
                for item in models
                if isinstance(item, str) and item.strip()
            }
        )
        AtomicWriter().write_json(
            self._session_master_additions_path(session_path),
            {
                "schema_version": 1,
                "platform": "Chaturbate",
                "session": os.path.basename(os.path.normpath(session_path)),
                "source": source,
                "updated_at": utc_now_iso(),
                "models": normalized,
            },
        )

    def _load_session_master_additions(self, platform, session_path):
        if platform != "Chaturbate" or not session_path:
            return None
        try:
            additions = self._read_session_master_additions(session_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._log(f"[WARN] Invalid session master additions record: {exc}")
            additions = None
        if additions is not None:
            return additions

        additions = self._infer_chaturbate_master_additions(session_path)
        if additions is None:
            return None
        try:
            self._write_session_master_additions(
                session_path,
                additions,
                source="master_backup_recovery",
            )
        except OSError as exc:
            self._log(f"[WARN] Could not persist recovered master additions: {exc}")
        self._log(
            f"[INFO] Recovered {len(additions)} new Master List models "
            "for the selected Chaturbate session."
        )
        return additions

    def _record_session_master_additions(
        self, platform, before_models, after_models, *, source
    ):
        if platform != "Chaturbate":
            return set()
        session_path = self._session_registry.get(platform)
        if not session_path:
            return set()
        additions = self._model_names(after_models) - self._model_names(
            before_models
        )
        existing = self._session_new_master_models.get(platform)
        combined = set(existing or ()) | additions
        try:
            self._write_session_master_additions(
                session_path,
                combined,
                source=source,
            )
        except OSError as exc:
            self._log(f"[WARN] Could not persist session master additions: {exc}")
        self._session_new_master_models[platform] = combined
        return additions

    def subscribe(self):
        subscriber = queue.Queue()
        with self._lock:
            self._subscribers.add(subscriber)
            for message in self._logs:
                subscriber.put({"type": "log", "message": message})
            subscriber.put({"type": "state", "state": self.get_state()})
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            self._subscribers.discard(subscriber)

    def _emit(self, event):
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def _emit_state(self):
        self._emit({"type": "state", "state": self.get_state()})

    def _emit_master_list_compiled(self, added_count, total_count):
        self._emit(
            {
                "type": "masterListCompiled",
                "addedCount": added_count,
                "totalCount": total_count,
            }
        )

    def _on_progress(self, event):
        if not isinstance(event, ProgressEvent):
            self._log("[WARN] Ignored an invalid workflow progress event.")
            return
        with self._lock:
            sequence = max(
                int(event.sequence or 0),
                int(self._progress.get("sequence", 0)) + 1,
            )
        progress = {
            "sequence": sequence,
            "stepIndex": {
                StepName.VPN_SNAPSHOT: 1,
                StepName.LOCAL_SNAPSHOT: 2,
                StepName.COMPARE: 3,
                StepName.VERIFY: 4,
            }.get(event.step, 0),
            "stepCount": 4,
            "phase": event.phase,
            "completed": event.completed,
            "total": event.total,
            "percent": event.percent,
            "message": event.message,
            "status": event.status.value,
            "requiresUser": event.requires_user,
            "waitingReason": event.waiting_reason,
            "runId": event.run_id,
            "generationId": event.generation_id,
            "sessionId": event.session_id,
        }
        with self._lock:
            self._progress = progress
        self._emit({"type": "progress", "progress": dict(progress)})
        if event.requires_user:
            self._emit(
                {
                    "type": "waitingForUser",
                    "reason": event.waiting_reason,
                    "progress": dict(progress),
                }
            )

    def _log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"{timestamp} {message}"
        with self._lock:
            self._logs.append(line)
            if len(self._logs) > self._max_logs:
                self._logs = self._logs[-self._max_logs :]
        self._emit({"type": "log", "message": line})

    def _active_runner(self):
        return self._runners[self._platform]

    def _active_adapter(self):
        return self._adapters.get(self._platform)

    def _reject_missing_hardened_adapter(self):
        if self._platform == "Stripchat" and self._active_adapter() is None:
            self._log(
                "[ERROR] Hardened Stripchat adapter is unavailable; the "
                "legacy workflow is disabled for safety."
            )
            return True
        return False

    def _all_runners(self):
        return tuple(self._runners.values())

    def _blocked_list_path(self):
        if not self._session_path:
            return ""
        if self._platform == "MyFreeCams":
            preferred = os.path.join(self._session_path, "mfc_verified_live.txt")
            if os.path.exists(preferred):
                return preferred
            legacy = os.path.join(self._session_path, "BLOCKED_MODELS.txt")
            if os.path.exists(legacy):
                return legacy
            return preferred
        if self._platform == "Stripchat":
            adapter = self._active_adapter()
            if adapter is not None:
                return str(adapter.blocked_projection_path())
            verified = os.path.join(self._session_path, "FINAL_BLOCKED.txt")
            if os.path.exists(verified):
                return verified
            candidates = os.path.join(self._session_path, "sc_candidates.txt")
            if os.path.exists(candidates):
                return candidates
            return verified
        if self._platform == "XHamsterLive":
            verified = os.path.join(self._session_path, "FINAL_BLOCKED.txt")
            if os.path.exists(verified):
                return verified
            candidates = os.path.join(self._session_path, "xhl_candidates.txt")
            if os.path.exists(candidates):
                return candidates
            return verified
        return os.path.join(self._session_path, "FINAL_BLOCKED.txt")

    def _set_status(self, value):
        changed = False
        with self._lock:
            if self._status != value:
                self._status = value
                changed = True
        if changed:
            self._emit_state()

    def _set_busy(self, value):
        changed = False
        with self._lock:
            if self._busy != value:
                self._busy = value
                changed = True
        if changed:
            self._emit_state()

    def _set_session(self, path, platform=None):
        platform = platform or self._platform
        try:
            session_path = self._session_registry.set(platform, path)
        except SessionPathError as exc:
            self._log(f"[ERROR] Invalid session path: {exc}")
            return False
        adapter = self._adapters.get(platform)
        if session_path and adapter is not None:
            try:
                adapter.bind_session(session_path)
            except Exception as exc:
                self._log(f"[ERROR] Adapter rejected session path: {exc}")
                return False
        with self._lock:
            if platform == self._platform:
                self._session_path = session_path
        if session_path:
            self._runners[platform].set_session(session_path)
        self._session_new_master_models[platform] = (
            self._load_session_master_additions(platform, session_path)
        )
        if platform == self._platform:
            self._load_blocked_models()
            self._emit_state()
        return True

    def _set_blocked_models(self, models):
        normalized = list(models)
        changed = False
        with self._lock:
            if normalized != self._blocked_models:
                self._blocked_models = normalized
                changed = True
        if changed:
            self._emit_state()

    def _load_blocked_models(self, path_override=None):
        with self._lock:
            session_path = self._session_path
        if not session_path:
            self._set_blocked_models([])
            return
        verified_path = path_override or self._blocked_list_path()
        if not verified_path or not os.path.exists(verified_path):
            self._set_blocked_models([])
            return
        try:
            with open(verified_path, "r", encoding="utf-8") as handle:
                models = [line.strip() for line in handle if line.strip()]
        except Exception as exc:
            self._log(f"[ERROR] Failed to read blocked list: {exc}")
            self._set_blocked_models([])
            return
        self._set_blocked_models(sorted(models))

    def _master_list_models(self):
        try:
            adapter = self._active_adapter()
            data = (
                adapter.get_master_models()
                if adapter is not None
                else self._active_runner().get_master_list_models()
            )
        except Exception as exc:
            self._log(f"[ERROR] Failed to read master list: {exc}")
            return []

        data = [dict(item) for item in data]
        if self._master_list_country_filter != "All":
            target = self._master_list_country_filter
            data = [item for item in data if item.get("country") == target]

        temp_data = sorted(data, key=lambda item: (item.get("name") or "").lower())
        mode = self._master_list_sort_mode
        if mode in ("date_newest", "date_oldest"):
            reverse = mode == "date_newest"

            def date_key(item):
                timestamp = item.get("timestamp")
                if timestamp:
                    return timestamp
                date_value = item.get("date", "")
                if date_value == "Manual":
                    return "9999-99-99"
                if date_value == "Unknown" or not date_value:
                    date_value = "0000-00-00"
                return f"{date_value}-S{item.get('session', 0):04d}"

            sorted_data = sorted(temp_data, key=date_key, reverse=reverse)
        elif mode == "country":
            sorted_data = sorted(
                temp_data,
                key=lambda item: (
                    (item.get("country") or "ZZ").lower(),
                    (item.get("name") or "").lower(),
                ),
            )
        else:
            sorted_data = temp_data

        initial_set = self._initial_master_models.get(self._platform, set())
        if self._platform == "Chaturbate" and self._session_path:
            session_additions = self._session_new_master_models.get(self._platform)
            if session_additions is not None:
                initial_set = self._model_names(data) - session_additions
        if self._platform == "Chaturbate" and self._session_path:
            sorted_data = sorted(
                sorted_data,
                key=lambda item: active_session_display_priority(
                    item,
                    self._session_path,
                    initial_set,
                ),
            )

        for item in sorted_data:
            item["is_new"] = item.get("name") not in initial_set
        return sorted_data

    def get_state(self):
        with self._lock:
            outcome = self._outcomes_by_platform.get(self._platform)
            resume = self._resume_descriptor()
            return {
                "schemaVersion": 2,
                "platform": self._platform,
                "platforms": list(PLATFORMS),
                "status": self._status,
                "busy": self._busy,
                "currentTask": self._current_task,
                "sessionPath": self._session_path,
                "blockedModels": list(self._blocked_models),
                "masterListModels": self._master_list_models(),
                "masterListSortMode": self._master_list_sort_mode,
                "masterListCountryFilter": self._master_list_country_filter,
                "logs": list(self._logs),
                "progress": dict(self._progress),
                "effectiveOptions": dict(self._effective_options),
                "lastOutcome": (
                    outcome.to_dict() if outcome is not None else None
                ),
                "resume": resume,
                # Compatibility alias; the resume object is authoritative.
                "resumable": bool(resume["available"]),
                "machinePolicy": self._machine_policy_state(),
            }

    def _resume_descriptor(self):
        """Adapter-driven resume; a busy task always disables Resume."""
        base = {
            "available": False,
            "platform": self._platform,
            "runId": None,
            "generationId": None,
            "nextStep": None,
            "processedCount": 0,
            "remainingCount": 0,
            "candidateCount": 0,
            "reasonCode": None,
            "newGenerationRequired": False,
            "message": "",
        }
        if self._busy:
            # While a worker owns the leases, Resume is never offered - not
            # even at waiting_for_user.
            base["reasonCode"] = "task_active"
            return base
        adapter = self._adapters.get(self._platform)
        if adapter is not None:
            try:
                selected = self._current_vpn_provider()
                if hasattr(adapter, "configure_network"):
                    adapter.configure_network(selected.key, self.settings.get("vpnRelayLocation"))
                if self._session_path:
                    adapter.bind_session(self._session_path)
                descriptor = adapter.inspect_resume()
            except Exception as exc:
                base["reasonCode"] = "resume_inspection_failed"
                base["message"] = str(exc)[:160]
                return base
            base.update(
                {
                    "available": bool(descriptor.available),
                    "runId": descriptor.run_id,
                    "generationId": descriptor.generation_id,
                    "nextStep": (
                        descriptor.next_step.value
                        if descriptor.next_step
                        else None
                    ),
                    "processedCount": descriptor.processed_count,
                    "remainingCount": descriptor.remaining_count,
                    "candidateCount": descriptor.candidate_count,
                    "reasonCode": descriptor.reason_code,
                    "newGenerationRequired": (
                        descriptor.reason_code == "retry_epochs_exhausted"
                    ),
                    "message": descriptor.message,
                }
            )
            return base
        outcome = self._outcomes_by_platform.get(self._platform)
        legacy = bool(
            (
                outcome
                and outcome.status
                in (OutcomeStatus.CANCELLED, OutcomeStatus.INCOMPLETE)
            )
            or self._legacy_resume_available()
        )
        base["available"] = legacy
        if legacy:
            base["nextStep"] = StepName.VERIFY.value
        return base

    def _machine_policy_state(self):
        try:
            from machine_policy import MachinePolicyJournal

            document = MachinePolicyJournal().load_active()
        except Exception:
            return {
                "targetRelay": None,
                "recoveryRequired": False,
                "restorationStatus": "unknown",
                "manifestSyncPending": False,
            }
        if document is None:
            return {
                "targetRelay": None,
                "recoveryRequired": False,
                "restorationStatus": "not_required",
                "manifestSyncPending": False,
            }
        status = document.get("status", "pending")
        return {
            "targetRelay": (document.get("target") or {}).get("relay_code"),
            "recoveryRequired": status in ("pending", "restoring", "failed"),
            "restorationStatus": status,
            "manifestSyncPending": (
                document.get("manifest_sync_status") != "synced"
            ),
        }

    def restore_machine_policy(self):
        """Explicit operator repair of a pending machine-policy transaction."""
        from machine_policy import (
            MachinePolicyController,
            MachinePolicyError,
            MachinePolicyJournal,
            MachinePolicyLease,
        )
        from runtime_lock import WorkflowLease as _WorkspaceLease

        if not self._current_vpn_provider().automatic:
            return {"ok": False, "error_code": "automatic_vpn_required"}
        if self.get_state()["busy"]:
            return {"ok": False, "error_code": "workflow_already_running"}
        journal = MachinePolicyJournal()
        try:
            with MachinePolicyLease():
                controller = MachinePolicyController(
                    self._new_vpn_controller(), journal, logger=self._log
                )
                result = controller.recover_pending(
                    origin_lease_factory=lambda origin: _WorkspaceLease(
                        origin, task="Machine Policy Reconcile"
                    )
                )
        except MachinePolicyError as exc:
            return {"ok": False, "error_code": exc.code}
        if result is None:
            return {"ok": True, "recovered": False, "reason": "nothing pending"}
        self._emit_state()
        return {"ok": bool(result.get("recovered")), **result}

    def reconcile_machine_policy_record(self):
        """Retry only the origin-manifest mirror; never touches VPN/Chrome."""
        from machine_policy import (
            MachinePolicyError,
            MachinePolicyJournal,
            MachinePolicyLease,
            MachinePolicyController,
        )
        from runtime_lock import WorkflowLease as _WorkspaceLease

        journal = MachinePolicyJournal()
        document = journal.load_active()
        if document is None:
            return {"ok": True, "reconciled": False, "reason": "nothing pending"}
        try:
            with MachinePolicyLease():
                controller = MachinePolicyController(
                    None, journal, logger=self._log
                )
                status = controller._mirror_origin(
                    document,
                    lambda origin: _WorkspaceLease(
                        origin, task="Machine Policy Reconcile"
                    ),
                )
                journal.set_manifest_sync(document["transaction_id"], status)
        except MachinePolicyError as exc:
            return {"ok": False, "error_code": exc.code}
        self._emit_state()
        return {"ok": status == "synced", "manifestSyncStatus": status}

    def _legacy_resume_available(self):
        if self._platform != "Chaturbate" or not self._session_path:
            return False
        try:
            from ctb_store import ChaturbateRunStore

            store = ChaturbateRunStore(self._session_path)
            context = store.load_active()
            if context is None or not os.path.exists(context.checkpoint_path):
                return False
            manifest = store.load_manifest(context)
            return (
                manifest.get("status") == "incomplete"
                and manifest.get("steps", {})
                .get("step3_compare", {})
                .get("status")
                == "succeeded"
            )
        except Exception:
            return False

    def create_session(self):
        try:
            with WorkflowLease(
                self.base_dir,
                platform=self._platform,
                task="Create Session",
            ):
                adapter = self._active_adapter()
                target = adapter if adapter is not None else self._active_runner()
                path = target.create_session()
            self._set_session(path)
            return path
        except WorkflowBusyError as exc:
            self._log(
                f"[WARN] Another process owns the workflow: {exc.owner}"
            )
            return ""
        except Exception as exc:
            self._log(f"[ERROR] Failed to create session: {exc}")
            return ""

    def use_latest_session(self):
        try:
            with WorkflowLease(
                self.base_dir,
                platform=self._platform,
                task="Select Session",
            ):
                adapter = self._active_adapter()
                target = adapter if adapter is not None else self._active_runner()
                path = target.use_latest_session()
            self._set_session(path)
            return path
        except Exception as exc:
            self._log(f"[ERROR] Failed to load session: {exc}")
            return ""

    def open_session_folder(self):
        if not self._session_path:
            self._log("[WARN] No session folder set.")
            return False
        try:
            os.startfile(self._session_path)
            return True
        except Exception as exc:
            self._log(f"[ERROR] Could not open folder: {exc}")
            return False

    def set_platform(self, name):
        if name not in PLATFORMS:
            self._log(f"[WARN] Unknown platform '{name}'.")
            return False
        with self._lock:
            if self._busy:
                self._log("[WARN] Cannot switch platform while a task is running.")
                return False
            if name == self._platform:
                return True
            self._platform = name
            self._session_path = self._session_registry.get(name)
        session_path = self._session_path
        if session_path:
            self._active_runner().set_session(session_path)
            adapter = self._active_adapter()
            if adapter is not None:
                try:
                    adapter.bind_session(session_path)
                except Exception as exc:
                    self._log(f"[ERROR] Adapter rejected session path: {exc}")
                    return False
        self._session_new_master_models[name] = (
            self._load_session_master_additions(name, session_path)
        )
        self._load_blocked_models()
        self._emit_state()
        return True

    def _start_task(
        self,
        name,
        func,
        *,
        managed_leases=False,
        session_strategy=None,
    ):
        with self._lock:
            if self._busy:
                self._log("[WARN] Another task is running.")
                return False
            self._busy = True
            self._status = f"Running {name}..."
            self._current_task = name
            self._task_platform = self._platform
            self._task_error = None
            self._progress = {}
            task_platform = self._task_platform
            runner = self._runners[task_platform]
            adapter = self._adapters.get(task_platform)
        try:
            runner_session = runner.session_folder
            root_path = self._session_registry.root(task_platform)
            has_selected_session = bool(runner_session) and (
                Path(runner_session).resolve() != Path(root_path).resolve()
            )
            if not has_selected_session:
                with WorkflowLease(
                    self.base_dir,
                    platform=task_platform,
                    task=f"{name} Session",
                ):
                    target = adapter if adapter is not None else runner
                    if session_strategy == "latest":
                        selected = target.use_latest_session()
                    elif session_strategy == "new":
                        selected = target.create_session()
                    elif name in ("Step 1", "Step 2", "Full Auto Flow"):
                        selected = target.create_session()
                    else:
                        selected = target.use_latest_session()
                if not self._set_session(selected, task_platform):
                    raise RuntimeError("session reservation failed")
            else:
                if not self._set_session(
                    runner.session_folder, task_platform
                ):
                    raise RuntimeError("session validation failed")
            stop_target = adapter if adapter is not None else runner
            if hasattr(stop_target, "clear_stop"):
                stop_target.clear_stop()
            thread = threading.Thread(
                target=self._run_task_thread,
                args=(name, task_platform, func, managed_leases),
                daemon=True,
            )
            with self._lock:
                self._thread = thread
            self._emit_state()
            thread.start()
        except Exception as exc:
            with self._lock:
                self._busy = False
                self._status = "Idle"
                self._current_task = ""
                self._task_platform = ""
                self._task_error = str(exc)
                self._thread = None
            self._log(f"[ERROR] Failed to start {name}: {exc}")
            self._emit_state()
            return False
        return True

    @staticmethod
    def _synthesized_failure(platform, mode, code, message, step=None):
        """A typed stand-in when a worker produced no structured outcome."""
        return RunOutcome.calculate(
            run_id="",
            generation_id="",
            platform=platform,
            mode=mode,
            steps=[
                StepOutcome.failed(
                    run_id="",
                    generation_id="",
                    platform=platform,
                    step=step or StepName.VERIFY,
                    error_code=code,
                    error_message=message,
                )
            ],
            restoration=None,
            started_at=utc_now_iso(),
            message=code,
        )

    def _run_task_thread(self, name, platform, func, managed_leases=False):
        error = None
        result = None
        # Every hardened task must end with a typed RunOutcome. Without
        # this, a step that crashed left lastOutcome showing the PREVIOUS
        # run, so the UI reported a stale success for a failed click.
        hardened = name == "Full Auto Flow" or managed_leases
        mode = "full_auto" if name == "Full Auto Flow" else "manual"
        step_hint = {
            "Step 1": StepName.VPN_SNAPSHOT,
            "Step 2": StepName.LOCAL_SNAPSHOT,
            "Step 3": StepName.COMPARE,
            "Step 4": StepName.VERIFY,
        }.get(name)
        try:
            if hardened:
                result = func()
            else:
                with WorkflowLease(
                    self.base_dir,
                    platform=platform,
                    task=name,
                ):
                    result = func()
            if hardened and not isinstance(result, RunOutcome):
                result = self._synthesized_failure(
                    platform,
                    mode,
                    "missing_run_outcome",
                    "The workflow worker returned no structured outcome.",
                    step_hint,
                )
                error = error or "missing_run_outcome"
            if isinstance(result, RunOutcome):
                with self._lock:
                    self._last_outcome = result
                    self._progress = self._progress_from_outcome(result)
                    self._outcomes_by_platform[platform] = result
            elif isinstance(result, StepOutcome):
                with self._lock:
                    self._progress = {
                        "phase": result.step.value,
                        "completed": result.processed_count,
                        "total": result.input_count,
                        "percent": (
                            round(
                                result.processed_count
                                * 100.0
                                / result.input_count,
                                2,
                            )
                            if result.input_count
                            else None
                        ),
                        "requiresUser": False,
                    }
        except WorkflowBusyError as exc:
            error = "workflow_already_running"
            self._log(
                "[ERROR] Another ModelScraper process owns the workflow: "
                f"{exc.owner}"
            )
            result = self._record_task_failure(
                hardened, platform, mode, error, str(exc.owner)[:200], step_hint
            )
        except Exception as exc:
            error = str(exc)
            self._log(f"[ERROR] {error}")
            result = self._record_task_failure(
                hardened, platform, mode, "workflow_task_failed", error,
                step_hint,
            )
        finally:
            self._finish_task(name, platform, error, result)

    def _record_task_failure(
        self, hardened, platform, mode, code, message, step_hint
    ):
        """Publish a typed failure so no stale outcome survives a crash."""
        if not hardened:
            return None
        outcome = self._synthesized_failure(
            platform, mode, code, message, step_hint
        )
        with self._lock:
            self._last_outcome = outcome
            self._outcomes_by_platform[platform] = outcome
            self._progress = self._progress_from_outcome(outcome)
        return outcome

    def _finish_task(
        self, finished_task, finished_platform, error, result=None
    ):
        with self._lock:
            self._status = "Idle"
            self._busy = False
            self._current_task = ""
            self._task_platform = ""
            self._task_error = error
            # The thread handle is cleared only after taskFinished is
            # emitted; clearing it here let wait_for_current_task() return
            # before the terminal event reached subscribers.

        if finished_platform == "Stripchat" and finished_task == "Step 3":
            adapter = self._adapters.get("Stripchat")
            path = (
                str(adapter.candidate_projection_path())
                if adapter is not None
                else self._runners["Stripchat"].paths.candidates
            )
            self._load_blocked_models(path)
        if finished_platform == "XHamsterLive" and finished_task == "Step 3":
            self._load_blocked_models(self._runners["XHamsterLive"].paths.candidates)
        if finished_platform == "MyFreeCams" and finished_task in ("Step 3", "Step 4"):
            self._load_blocked_models()
        if finished_platform != "MyFreeCams" and finished_task == "Step 4":
            adapter = self._adapters.get(finished_platform)
            self._load_blocked_models(
                str(adapter.blocked_projection_path())
                if adapter is not None
                else None
            )
        if finished_task == "Full Auto Flow":
            adapter = self._adapters.get(finished_platform)
            self._load_blocked_models(
                str(adapter.blocked_projection_path())
                if adapter is not None
                else None
            )

        self._emit_state()
        self._emit(
            {
                "type": "taskFinished",
                "task": finished_task,
                "platform": finished_platform,
                "error": error,
                "outcome": (
                    result.to_dict()
                    if isinstance(result, (RunOutcome, StepOutcome))
                    else None
                ),
            }
        )
        with self._lock:
            self._thread = None

    @staticmethod
    def _progress_from_outcome(outcome):
        if not outcome.steps:
            return {}
        step = outcome.steps[-1]
        return {
            "stepIndex": min(4, len(outcome.steps)),
            "stepCount": 4,
            "phase": step.step.value,
            "completed": step.processed_count,
            "total": step.input_count,
            "percent": (
                round(step.processed_count * 100.0 / step.input_count, 2)
                if step.input_count
                else None
            ),
            "requiresUser": False,
        }

    def wait_for_current_task(self, timeout=None):
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def stop_current_task(self):
        was_busy = self.get_state()["busy"]
        if was_busy:
            self._set_status("Stopping...")
            self._log("[INFO] Stop requested. Attempting to halt current task...")
        else:
            self._log("[WARN] No running task to stop.")
            return False
        with self._lock:
            platform = self._task_platform or self._platform
        target = self._adapters.get(platform)
        if platform == "Stripchat" and target is None:
            self._log(
                "[ERROR] Hardened Stripchat adapter is unavailable; no "
                "legacy stop target was used."
            )
            return False
        target = target or self._runners.get(platform)
        if target is not None and hasattr(target, "request_stop"):
            target.request_stop()
        return True

    def _hardened_coordinator(self, adapter, vpn):
        from machine_policy import observe_policy

        adapter.policy_observer = lambda: observe_policy(vpn).to_dict()

        class AdapterStopToken:
            def is_cancelled(_self):
                return adapter.is_stop_requested()

        return self._hardened_coordinator_factory(
            self.base_dir,
            vpn,
            stop_token=AdapterStopToken(),
            logger=self._log,
            progress=self._on_progress,
        )

    def _run_hardened_step_sync(
        self,
        adapter,
        step,
        *,
        headless=False,
        start_minimized=True,
        vpn=None,
        manual_network_confirmed=False,
    ):
        options = WorkflowOptions(
            headless=headless,
            start_minimized=bool(start_minimized),
            mode="manual",
            run_selection=RunSelection.AUTO,
        )
        if not self._current_vpn_provider().automatic:
            from manual_workflow import run_manual_adapter_step

            if hasattr(adapter, "configure_network"):
                adapter.configure_network("manual", self.settings.get("vpnRelayLocation"))
            return run_manual_adapter_step(
                self.base_dir, adapter, step, options,
                logger=self._log, progress=self._on_progress,
                manual_network_confirmed=manual_network_confirmed,
            )
        vpn = vpn or self._new_vpn_controller()
        if hasattr(adapter, "configure_network"):
            adapter.configure_network("mullvad", self.settings.get("vpnRelayLocation"))
        return self._hardened_coordinator(adapter, vpn).run_step(
            adapter, step=step, options=options
        )

    def start_vpn_list(self, headless=None, manual_network_confirmed=False):
        if not self._manual_network_gate(StepName.VPN_SNAPSHOT, manual_network_confirmed):
            return False
        adapter = self._active_adapter()
        if self._reject_missing_hardened_adapter():
            return False
        if adapter is not None:
            try:
                headless = self.resolve_headless(self._platform, headless)
            except UnsupportedBrowserMode:
                self._log(
                    "[ERROR] Stripchat uses a visible browser for verification; "
                    "headless was rejected."
                )
                return False

            def hardened_task():
                return self._run_hardened_step_sync(
                    adapter,
                    StepName.VPN_SNAPSHOT,
                    headless=headless,
                    start_minimized=False,
                    manual_network_confirmed=manual_network_confirmed,
                )

            return self._start_task(
                "Step 1",
                hardened_task,
                managed_leases=True,
                session_strategy="new",
            )
        runner = self._active_runner()
        platform = self._platform

        def task():
            if platform == "MyFreeCams":
                manual = not self._current_vpn_provider().automatic
                if manual:
                    self._manual_mfc_references = {}
                result = runner.step_vpn_list()
                if manual:
                    runner.capture_live_reference()
                    self._manual_mfc_references = {
                        "session": runner.session_folder, "vpn_at": time.monotonic(),
                    }
                return result
            return runner.step_vpn_list(headless=headless)

        return self._start_task("Step 1", task)

    def start_local_list(self, headless=None, manual_network_confirmed=False):
        if not self._manual_network_gate(StepName.LOCAL_SNAPSHOT, manual_network_confirmed):
            return False
        adapter = self._active_adapter()
        if self._reject_missing_hardened_adapter():
            return False
        if adapter is not None:
            try:
                headless = self.resolve_headless(self._platform, headless)
            except UnsupportedBrowserMode:
                self._log(
                    "[ERROR] Stripchat uses a visible browser for verification; "
                    "headless was rejected."
                )
                return False

            def hardened_task():
                return self._run_hardened_step_sync(
                    adapter,
                    StepName.LOCAL_SNAPSHOT,
                    headless=headless,
                    start_minimized=False,
                    manual_network_confirmed=manual_network_confirmed,
                )

            return self._start_task(
                "Step 2",
                hardened_task,
                managed_leases=True,
                session_strategy="latest",
            )
        runner = self._active_runner()
        platform = self._platform

        def task():
            if platform == "MyFreeCams":
                manual = not self._current_vpn_provider().automatic
                if manual and self._manual_mfc_references.get("session") != runner.session_folder:
                    self._manual_mfc_references = {"session": runner.session_folder}
                if manual:
                    self._manual_mfc_references.pop("local_at", None)
                result = runner.step_local_list()
                if manual:
                    runner.capture_local_reference()
                    self._manual_mfc_references["local_at"] = time.monotonic()
                return result
            return runner.step_local_list(headless=headless)

        return self._start_task("Step 2", task)

    def start_compare(self):
        adapter = self._active_adapter()
        if self._reject_missing_hardened_adapter():
            return False
        if adapter is not None:
            return self._start_task(
                "Step 3",
                lambda: self._run_hardened_step_sync(
                    adapter,
                    StepName.COMPARE,
                    headless=False,
                    start_minimized=False,
                ),
                managed_leases=True,
                session_strategy="latest",
            )
        runner = self._active_runner()

        def task():
            return runner.compare_lists()

        return self._start_task("Step 3", task)

    def _verify_mfc_with_automatic_vpn(self, runner):
        """Capture and verify under the machine lease; restore only after entry."""
        vpn = self._new_vpn_controller()
        coordinator = WorkflowCoordinator(self.base_dir, vpn, logger=self._log)

        def verify_with_restoration():
            with WorkflowLease(self.base_dir, platform="MyFreeCams", task="Step 4"):
                started_at = utc_now_iso()
                run_id = str(uuid.uuid4())
                generation_id = run_id
                try:
                    coordinator._ensure_chrome_uses_vpn()
                    vpn.connect_ireland()
                    if not runner.capture_live_reference():
                        raise RuntimeError("The VPN reference snapshot is empty.")
                    vpn.disconnect()
                    if not runner.capture_local_reference():
                        raise RuntimeError("The local reference snapshot is empty.")
                    step = runner.verify_live_models(start_minimized=False)
                    if not isinstance(step, StepOutcome):
                        raise RuntimeError("MyFreeCams verification returned no typed outcome.")
                    run_id, generation_id = step.run_id, step.generation_id
                except Exception as exc:
                    step = StepOutcome.failed(
                        run_id=run_id, generation_id=run_id, platform="MyFreeCams",
                        step=StepName.VERIFY, error_code="mfc_verification_failed",
                        error_message=str(exc)[:200],
                    )
                finally:
                    restoration = coordinator._restore(run_id, generation_id, "MyFreeCams")
                return RunOutcome.calculate(
                    run_id=step.run_id, generation_id=step.generation_id,
                    platform="MyFreeCams", mode="manual", steps=[step],
                    restoration=restoration, started_at=started_at,
                    required_steps=(StepName.VERIFY,),
                )

        return coordinator._with_machine_policy_guard(
            verify_with_restoration,
            platform="MyFreeCams", mode="manual", step=StepName.VERIFY,
        )

    def start_verify(self, start_minimized=True, manual_network_confirmed=False):
        if not self._manual_network_gate(StepName.VERIFY, manual_network_confirmed):
            return False
        adapter = self._active_adapter()
        if self._reject_missing_hardened_adapter():
            return False
        if adapter is not None:
            return self._start_task(
                "Step 4",
                lambda: self._run_hardened_step_sync(
                    adapter,
                    StepName.VERIFY,
                    headless=False,
                    start_minimized=start_minimized,
                    manual_network_confirmed=manual_network_confirmed,
                ),
                managed_leases=True,
                session_strategy="latest",
            )
        runner = self._active_runner()
        platform = self._platform
        automatic_mfc = platform == "MyFreeCams" and self._current_vpn_provider().automatic

        def task():
            pre_models = runner.get_master_list_models()
            pre_count = len(pre_models)
            if platform == "MyFreeCams":
                # Run on its own, step 4 still needs one snapshot from each
                # side of the VPN, and the runner cannot move the VPN itself.
                if automatic_mfc:
                    outcome = self._verify_mfc_with_automatic_vpn(runner)
                else:
                    if self._current_vpn_provider().automatic:
                        raise RuntimeError("The VPN provider changed; repeat verification with its current settings.")
                    references = self._manual_mfc_references
                    now = time.monotonic()
                    vpn_at = references.get("vpn_at")
                    local_at = references.get("local_at")
                    if (references.get("session") != runner.session_folder
                            or vpn_at is None or local_at is None
                            or not 0 <= now - vpn_at <= 120
                            or not vpn_at <= local_at <= now):
                        raise RuntimeError("Repeat manual Steps 1 and 2 in this session, then verify within two minutes of the VPN reference.")
                    self._manual_mfc_references = {}
                    outcome = runner.verify_live_models(start_minimized=False)
            else:
                outcome = runner.verify_candidates(
                    start_minimized=start_minimized,
                    reveal_on_captcha=True,
                )
            post_models = runner.get_master_list_models()
            if (
                getattr(getattr(outcome, "status", None), "value", None)
                == "succeeded"
            ):
                self._record_session_master_additions(
                    platform,
                    pre_models,
                    post_models,
                    source="step4_verify",
                )
            added_count = len(post_models) - pre_count
            self._emit_master_list_compiled(added_count, len(post_models))
            return outcome

        return self._start_task("Step 4", task, managed_leases=automatic_mfc)

    def _full_auto_stop_requested(self, runner):
        if not hasattr(runner, "_should_stop"):
            return False
        try:
            return runner._should_stop()
        except Exception:
            return False

    def _require_recent_nonempty_file(self, file_path, label, started_at):
        if not file_path or not os.path.exists(file_path):
            raise RuntimeError(f"{label} did not create an output file.")
        if os.path.getsize(file_path) <= 0:
            raise RuntimeError(f"{label} created an empty output file.")
        if os.path.getmtime(file_path) < started_at - 2.0:
            raise RuntimeError(
                f"{label} did not refresh its output file. Stopping full auto flow."
            )

    def run_full_auto_flow_sync(
        self,
        runner,
        platform,
        headless,
        start_minimized,
        vpn=None,
        run_selection="auto",
    ):
        adapter = self._adapters.get(platform)
        if platform == "Stripchat" and adapter is None:
            failure = StepOutcome.failed(
                run_id="",
                generation_id="",
                platform="stripchat",
                step=StepName.VPN_SNAPSHOT,
                error_code="hardened_adapter_unavailable",
                error_message=(
                    "The hardened Stripchat adapter is unavailable; the "
                    "legacy workflow was not started."
                ),
            )
            return RunOutcome.calculate(
                run_id="",
                generation_id="",
                platform="stripchat",
                mode="full_auto",
                steps=[failure],
                restoration=None,
                started_at=utc_now_iso(),
            )
        if adapter is not None:
            return self._run_hardened_full_auto_sync(
                adapter,
                platform=platform,
                headless=headless,
                start_minimized=start_minimized,
                vpn=vpn,
                run_selection=run_selection,
            )
        if not self._current_vpn_provider().automatic:
            raise RuntimeError("Full Auto requires an automatic VPN integration. Use manual Steps 1–4 with your VPN.")
        vpn = vpn or self._new_vpn_controller()
        self._log(f"[INFO] Full auto flow started for {platform}.")
        pre_models = runner.get_master_list_models()
        pre_count = len(pre_models)
        class RunnerStopToken:
            def is_cancelled(_self):
                return self._full_auto_stop_requested(runner)

        coordinator = WorkflowCoordinator(
            self.base_dir,
            vpn,
            stop_token=RunnerStopToken(),
            logger=self._log,
        )
        should_resume = run_selection == "resume"
        if run_selection == "auto" and platform == "Chaturbate":
            try:
                from ctb_store import ChaturbateRunStore

                context = ChaturbateRunStore(
                    runner.session_folder
                ).load_active()
                if context:
                    manifest = ChaturbateRunStore(
                        runner.session_folder
                    ).load_manifest(context)
                    verify = manifest.get("steps", {}).get(
                        "step4_verify", {}
                    )
                    should_resume = (
                        verify.get("status")
                        in ("cancelled", "incomplete")
                        or (
                            os.path.exists(context.checkpoint_path)
                            and manifest.get("steps", {})
                            .get("step3_compare", {})
                            .get("status")
                            == "succeeded"
                        )
                    )
            except Exception:
                should_resume = False
        if should_resume and platform == "Chaturbate":
            outcome = coordinator.resume_verify(
                runner,
                platform=platform,
                start_minimized=start_minimized,
            )
        else:
            outcome = coordinator.run(
                runner,
                platform=platform,
                headless=headless,
                start_minimized=start_minimized,
            )
        if outcome.status.value == "succeeded":
            post_models = runner.get_master_list_models()
            self._record_session_master_additions(
                platform,
                pre_models,
                post_models,
                source="full_auto",
            )
            self._emit_master_list_compiled(
                len(post_models) - pre_count,
                len(post_models),
            )
            self._log(f"[INFO] Full auto flow finished for {platform}.")
        else:
            # The code names the shape of the failure, the message names the
            # cause. Printing only the code hides every exception behind
            # "workflow_step_failed", so keep both when both exist.
            detail = " - ".join(
                part
                for part in (outcome.error_code, outcome.message)
                if part
            )
            self._log(
                f"[WARN] Full auto flow ended as {outcome.status.value}: "
                f"{detail or 'no detail reported'}"
            )
        return outcome

    def _run_hardened_full_auto_sync(
        self,
        adapter,
        *,
        platform,
        headless,
        start_minimized,
        vpn=None,
        run_selection="auto",
    ):
        if not self._current_vpn_provider().automatic:
            raise RuntimeError("Full Auto requires an automatic VPN integration.")
        vpn = vpn or self._new_vpn_controller()
        if hasattr(adapter, "configure_network"):
            adapter.configure_network("mullvad", self.settings.get("vpnRelayLocation"))
        self._log(f"[INFO] Hardened full auto flow started for {platform}.")
        pre_count = len(adapter.get_master_models())
        try:
            selection = RunSelection(run_selection)
        except ValueError:
            failure = StepOutcome.failed(
                run_id="",
                generation_id="",
                platform=adapter.spec.key,
                step=StepName.VPN_SNAPSHOT,
                error_code="invalid_run_selection",
            )
            return RunOutcome.calculate(
                run_id="",
                generation_id="",
                platform=adapter.spec.key,
                mode="full_auto",
                steps=[failure],
                restoration=None,
                started_at=utc_now_iso(),
            )
        options = WorkflowOptions(
            headless=headless,
            start_minimized=bool(start_minimized),
            mode="full_auto",
            run_selection=selection,
        )
        outcome = self._hardened_coordinator(adapter, vpn).run(
            adapter, options=options
        )
        if outcome.status is OutcomeStatus.SUCCEEDED:
            post_models = adapter.get_master_models()
            self._emit_master_list_compiled(
                len(post_models) - pre_count,
                len(post_models),
            )
            self._log(
                f"[INFO] Hardened full auto flow finished for {platform}."
            )
        else:
            detail = " - ".join(
                part
                for part in (outcome.error_code, outcome.message)
                if part
            )
            self._log(
                f"[WARN] Hardened full auto flow ended as "
                f"{outcome.status.value}: {detail or 'no detail reported'}"
            )
        return outcome

    def start_full_auto_flow(
        self,
        headless=True,
        start_minimized=True,
        run_selection="auto",
    ):
        if not self._current_vpn_provider().automatic:
            self._log("[WARN] Full Auto is unavailable in Manual / any VPN mode. Use Steps 1–4.")
            return False
        if run_selection not in ("auto", "resume", "new"):
            return False
        if self._reject_missing_hardened_adapter():
            return False
        runner = self._active_runner()
        platform = self._platform
        try:
            headless = self.resolve_headless(platform, headless)
        except UnsupportedBrowserMode:
            self._log(
                "[ERROR] Stripchat verification needs a visible browser "
                "window; headless was rejected."
            )
            return False
        with self._lock:
            self._effective_options = {
                "headless": headless,
                "startMinimized": bool(start_minimized),
            }

        def task():
            # The worker's RunOutcome IS the task result: dropping it left
            # GUI and Web with no structured final state at all.
            return self.run_full_auto_flow_sync(
                runner,
                platform=platform,
                headless=headless,
                start_minimized=start_minimized,
                run_selection=run_selection,
            )

        return self._start_task(
            "Full Auto Flow",
            task,
            managed_leases=bool(self._adapters.get(platform)),
            session_strategy=(
                "latest" if run_selection == "resume" else "new"
            ),
        )

    def query_master_list(
        self,
        offset=0,
        limit=200,
        query="",
        sort=None,
        country=None,
    ):
        offset = max(0, int(offset))
        limit = min(500, max(1, int(limit)))
        models = self._master_list_models()
        needle = str(query or "").strip().lower()
        if needle:
            models = [
                item
                for item in models
                if needle in str(item.get("name") or "").lower()
            ]
        if country and country != "All":
            models = [
                item
                for item in models
                if item.get("country") == country
            ]
        total = len(models)
        return {
            "rows": models[offset : offset + limit],
            "total": total,
            "offset": offset,
            "limit": limit,
            "sort": sort or self._master_list_sort_mode,
            "country": country or self._master_list_country_filter,
        }

    def load_blocked_list_text(self, text):
        models = sorted(line.strip() for line in text.splitlines() if line.strip())
        self._set_blocked_models(models)
        self._log(f"[INFO] Loaded {len(models)} models from browser upload.")
        return models

    def compile_master_list_sync(self):
        adapter = self._active_adapter()
        if self._platform == "Stripchat" and adapter is None:
            self._reject_missing_hardened_adapter()
            return False
        if adapter is not None:
            adapter.compile_master()
            count = len(adapter.get_master_models())
            self._log(
                f"[INFO] Hardened {self._platform} master validated with "
                f"{count} models; no ad-hoc rebuild was performed."
            )
            self._emit_master_list_compiled(0, count)
            self._emit_state()
            return count
        runner = self._active_runner()
        pre_count = len(runner.get_master_list_models())
        if hasattr(runner, "resolve_verification_discrepancy"):
            runner.resolve_verification_discrepancy()
        count = runner.compile_master_list()
        self._log(f"[INFO] Master list compiled with {count} models.")
        self._emit_master_list_compiled(count - pre_count, count)
        self._emit_state()
        return count

    def compile_master_list(self):
        return self._start_task("Compile Master List", self.compile_master_list_sync)

    def verify_master_list_sync(self, start_minimized=True, manual_network_confirmed=False):
        if not self._manual_network_gate(StepName.VERIFY, manual_network_confirmed):
            return False
        runner = self._active_runner()
        if self._platform in ("MyFreeCams", "Stripchat", "XHamsterLive"):
            self._log("[WARN] Master list verification not implemented for this platform.")
            return False
        runner.verify_master_list(start_minimized=start_minimized)
        self._emit_state()
        return True

    def verify_master_list(self, start_minimized=True, manual_network_confirmed=False):
        if not self._manual_network_gate(StepName.VERIFY, manual_network_confirmed):
            return False
        return self._start_task(
            "Verify Master List",
            lambda: self.verify_master_list_sync(
                start_minimized=start_minimized,
                manual_network_confirmed=manual_network_confirmed,
            ),
        )

    def add_manual_to_master(self, username):
        if not username:
            return False
        if self._platform == "Stripchat":
            self._log(
                "[WARN] Direct Stripchat master edits are disabled in the "
                "hardened flow; publication is transaction-only."
            )
            return False
        runner = self._active_runner()
        if not hasattr(runner, "add_manual_to_master"):
            self._log(f"[ERROR] add_manual_to_master not implemented for {self._platform}")
            return False
        try:
            with WorkflowLease(
                self.base_dir,
                platform=self._platform,
                task="Add Manual Model",
            ):
                runner.add_manual_to_master(username)
        except WorkflowBusyError:
            return False
        self._log(f"[INFO] Added {username} manually to {self._platform} master list.")
        self._emit_state()
        return True

    def delete_from_master_list(self, model_input, block=False):
        if not model_input:
            return False
        if self._platform == "Stripchat":
            self._log(
                "[WARN] Direct Stripchat master edits are disabled in the "
                "hardened flow; publication is transaction-only."
            )
            return False
        runner = self._active_runner()
        try:
            with WorkflowLease(
                self.base_dir,
                platform=self._platform,
                task="Delete Master Model",
            ):
                if block:
                    runner.block_model(model_input)
                    self._log(f"[INFO] Blocked model: {model_input}")
                else:
                    runner.remove_from_master_list(model_input)
                    self._log(f"[INFO] Deleted model: {model_input}")
        except WorkflowBusyError:
            return False
        self._emit_state()
        return True

    def sort_master_list(self, mode):
        if mode not in ("name", "date_newest", "date_oldest", "country"):
            return False
        with self._lock:
            self._master_list_sort_mode = mode
        self._emit_state()
        return True

    def set_master_list_country_filter(self, country):
        with self._lock:
            self._master_list_country_filter = country or "All"
        self._emit_state()
        return True

    def update_master_model_country(self, model_url, country):
        if self._platform == "Stripchat":
            self._log(
                "[WARN] Direct Stripchat master edits are disabled in the "
                "hardened flow; publication is transaction-only."
            )
            return False
        try:
            with WorkflowLease(
                self.base_dir,
                platform=self._platform,
                task="Update Master Country",
            ):
                updated = self._active_runner().update_master_model_country(
                    model_url, country
                )
        except WorkflowBusyError:
            return False
        if updated:
            self._log(f"[INFO] Updated {model_url} country to: {country}")
            self._emit_state()
            return True
        return False
