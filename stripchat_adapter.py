"""Stripchat platform workflow adapter.

The only place that maps the service's platform selection onto the
hardened Stripchat runner, store, finalizer, and master repository.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Callable, Mapping

from platform_contracts import validate_session_containment
from platform_workflow import (
    AdapterContractError,
    PlatformWorkflowAdapter,
    PreparedWorkflowRun,
    ResumeDescriptor,
    RunSelection,
    WorkflowOptions,
)
from stripchat_core import HardenedStripchatRunner, StripchatBrowserProvider
from stripchat_store import (
    STRIPCHAT_SPEC,
    StripchatRunStore,
    inspect_legacy_sessions,
    stripchat_master_repository,
)
from workflow_store import RunStoreError
from workflow_types import (
    ProgressEvent,
    ProgressStatus,
    StepName,
    StepOutcome,
    utc_now_iso,
)


class StripchatWorkflowAdapter(PlatformWorkflowAdapter):
    spec = STRIPCHAT_SPEC

    def __init__(
        self,
        base_dir,
        *,
        logger=None,
        driver_factory=None,
        window_manager=None,
        policy_observer=None,
        shadow: bool = True,
        config_overrides: Mapping[str, object] | None = None,
    ):
        self.base_dir = str(Path(base_dir).resolve())
        self.logger = logger or (lambda _message: None)
        self.driver_factory = driver_factory
        self.window_manager = window_manager
        self.policy_observer = policy_observer
        # Publication stays staged-only until the operator promotes it.
        self.shadow = bool(shadow)
        self.config_overrides = dict(config_overrides or {})
        self.session_path: str | None = None
        self._store: StripchatRunStore | None = None
        self._stop_requested = False
        self._progress: Callable[[ProgressEvent], None] = lambda _event: None
        self._manual_network_declaration = None

    def configure_network(self, provider: str, relay: str = "ie") -> None:
        """Bind provider/relay to the immutable run config before prepare/load."""
        if provider not in ("manual", "mullvad"):
            raise AdapterContractError("invalid_request", "unsupported VPN provider")
        if provider == "mullvad" and not str(relay or "").strip():
            raise AdapterContractError("invalid_request", "Mullvad requires a relay")
        self.config_overrides.update({
            "vpn_provider": provider,
            "target_relay_prefix": str(relay).strip().lower() if provider == "mullvad" else "",
            "local_recheck": provider == "mullvad",
        })
        self._manual_network_declaration = None
        if self.session_path:
            self.bind_session(self.session_path)

    @property
    def network_provider(self):
        return self.config_overrides.get("vpn_provider", "mullvad")

    def declare_manual_network(self, prepared, step, *, confirmed: bool) -> None:
        """Hold one explicit operator declaration; never claim observed routing."""
        self._manual_network_declaration = None
        if self.network_provider != "manual" or confirmed is not True:
            raise AdapterContractError("manual_network_confirmation_required")
        if step not in (StepName.VPN_SNAPSHOT, StepName.LOCAL_SNAPSHOT, StepName.VERIFY):
            raise AdapterContractError("invalid_request", "step does not use the network")
        context = prepared.context
        self._manual_network_declaration = {
            "source": "operator_declared", "vpn_state": "unknown",
            "declared_route": "vpn" if step is StepName.VPN_SNAPSHOT else "local",
            "operator_confirmed": True, "step": step.value,
            "run_id": context.run_id, "generation_id": context.generation_id,
            "session_id": context.session_id, "declared_at": utc_now_iso(),
        }

    def clear_manual_network_declaration(self) -> None:
        self._manual_network_declaration = None

    # ------------------------------------------------------------------
    # Sessions

    @property
    def session_root(self) -> Path:
        return Path(self.base_dir)

    def bind_session(self, path) -> None:
        resolved = validate_session_containment(path, self.base_dir)
        self.session_path = str(resolved)
        self._store = StripchatRunStore(
            resolved,
            config_overrides=self.config_overrides,
            session_root=self.base_dir,
        )

    def create_session(self) -> Path:
        root = Path(self.base_dir)
        root.mkdir(parents=True, exist_ok=True)
        date_str = datetime.datetime.now().strftime("%d.%m.%Y")
        numbers = []
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            parts = entry.name.split()
            if len(parts) >= 2 and parts[0] == "session":
                try:
                    numbers.append(int(parts[1]))
                except ValueError:
                    continue
        number = max(numbers, default=0) + 1
        for _attempt in range(10):
            candidate = root / f"session {number} {date_str}"
            try:
                candidate.mkdir(exist_ok=False)
            except FileExistsError:
                number += 1
                continue
            self.bind_session(candidate)
            return candidate
        raise AdapterContractError(
            "invalid_request", "could not reserve a session number"
        )

    def use_latest_session(self) -> Path:
        root = Path(self.base_dir)
        sessions = [entry for entry in root.iterdir() if entry.is_dir()]
        if not sessions:
            return self.create_session()
        latest = max(sessions, key=lambda entry: entry.stat().st_mtime)
        self.bind_session(latest)
        return latest

    def _require_store(self) -> StripchatRunStore:
        if self._store is None:
            raise AdapterContractError(
                "invalid_request", "no Stripchat session is bound"
            )
        return self._store

    @staticmethod
    def _require_current_config(store, context):
        manifest = store.load_manifest(context)
        if manifest.get("config", {}).get("digest_sha256") != store.config["digest_sha256"]:
            raise AdapterContractError(
                "config_mismatch", "The saved run uses different network or scraper settings. Start a new run."
            )
        return manifest

    # ------------------------------------------------------------------
    # Runs

    def prepare_run(
        self, selection: RunSelection, mode: str
    ) -> PreparedWorkflowRun:
        store = self._require_store()
        if selection is RunSelection.NEW:
            context = store.create_run(mode=mode, source="gui")
            return PreparedWorkflowRun(context, False, StepName.VPN_SNAPSHOT)
        descriptor = self.inspect_resume()
        if selection is RunSelection.RESUME:
            if not descriptor.available:
                raise AdapterContractError(
                    "resume_unavailable",
                    descriptor.message or "no resumable Stripchat run",
                )
        if descriptor.available:
            context = store.load_active()
            return PreparedWorkflowRun(
                context, True, descriptor.next_step or StepName.VERIFY
            )
        if selection is RunSelection.RESUME:
            raise AdapterContractError("resume_unavailable")
        context = store.create_run(mode=mode, source="gui")
        return PreparedWorkflowRun(context, False, StepName.VPN_SNAPSHOT)

    def prepare_step(
        self, step: StepName, mode: str = "manual"
    ) -> PreparedWorkflowRun:
        if step not in (
            StepName.VPN_SNAPSHOT,
            StepName.LOCAL_SNAPSHOT,
            StepName.COMPARE,
            StepName.VERIFY,
        ):
            raise AdapterContractError(
                "invalid_request", f"unsupported manual step: {step}"
            )
        store = self._require_store()
        if step is StepName.VPN_SNAPSHOT:
            context = store.create_run(mode=mode, source="gui")
            return PreparedWorkflowRun(context, False, step)
        try:
            context = store.load_active()
            if context is None:
                raise AdapterContractError(
                    "no_active_run",
                    "Run Step 1 before starting a later Stripchat step.",
                )
            self._require_current_config(store, context)
            resumed = (
                step is StepName.VERIFY
                and store.load_checkpoint(context) is not None
            )
            return PreparedWorkflowRun(context, resumed, step)
        except AdapterContractError:
            raise
        except (OSError, ValueError, RunStoreError) as exc:
            code = getattr(exc, "code", "invalid_run_state")
            raise AdapterContractError(code, str(exc)[:160]) from exc

    def inspect_resume(self) -> ResumeDescriptor:
        if self._store is None:
            return ResumeDescriptor(
                False, self.spec.key, reason_code="no_session"
            )
        store = self._store
        try:
            context = store.load_active()
        except (OSError, ValueError, RunStoreError) as exc:
            return ResumeDescriptor(
                False,
                self.spec.key,
                reason_code="invalid_run_state",
                message=str(exc)[:160],
            )
        if context is None:
            return ResumeDescriptor(
                False, self.spec.key, reason_code="no_active_run"
            )
        try:
            manifest = self._require_current_config(store, context)
            checkpoint = store.load_checkpoint(context)
        except (OSError, ValueError, RunStoreError, AdapterContractError) as exc:
            # Reusing verdicts a different classifier produced would mix two
            # decision tables in one blocked list, so say what to do instead
            # of only naming the mismatch.
            return ResumeDescriptor(
                False,
                self.spec.key,
                session_id=context.session_id,
                run_id=context.run_id,
                generation_id=context.generation_id,
                reason_code="checkpoint_mismatch",
                message=(
                    "This run's saved verification cannot be reused "
                    f"({exc}). Start a new run."
                )[:160],
            )
        verify = manifest.get("steps", {}).get(
            StepName.VERIFY.value, {}
        ).get("current") or {}
        publication = manifest.get("publication", {})
        summary = (checkpoint or {}).get("summary", {})
        remaining = int(summary.get("unknown_transient", 0)) + int(
            summary.get("unattempted", 0)
        )
        processed = int(checkpoint.get("candidate_count", 0)) - remaining if (
            checkpoint
        ) else 0
        if publication.get("recovery_required"):
            return ResumeDescriptor(
                True,
                self.spec.key,
                session_id=context.session_id,
                run_id=context.run_id,
                generation_id=context.generation_id,
                next_step=StepName.VERIFY,
                processed_count=processed,
                remaining_count=remaining,
                candidate_count=int(
                    (checkpoint or {}).get("candidate_count", 0)
                ),
                reason_code="publication_recovery_required",
                message="A committed publication needs to be rolled forward.",
            )
        if verify.get("new_generation_required"):
            return ResumeDescriptor(
                False,
                self.spec.key,
                session_id=context.session_id,
                run_id=context.run_id,
                generation_id=context.generation_id,
                reason_code="retry_epochs_exhausted",
                message="Start a new run to retry the remaining unknowns.",
            )
        if checkpoint is None:
            return ResumeDescriptor(
                False,
                self.spec.key,
                session_id=context.session_id,
                run_id=context.run_id,
                generation_id=context.generation_id,
                reason_code="no_checkpoint",
            )
        if verify.get("status") in ("cancelled", "incomplete") or remaining:
            return ResumeDescriptor(
                True,
                self.spec.key,
                session_id=context.session_id,
                run_id=context.run_id,
                generation_id=context.generation_id,
                next_step=StepName.VERIFY,
                processed_count=processed,
                remaining_count=remaining,
                candidate_count=int(checkpoint.get("candidate_count", 0)),
            )
        return ResumeDescriptor(
            False,
            self.spec.key,
            session_id=context.session_id,
            run_id=context.run_id,
            generation_id=context.generation_id,
            reason_code="nothing_to_resume",
        )

    # ------------------------------------------------------------------
    # Steps

    def _runner(self, progress) -> HardenedStripchatRunner:
        adapter = self

        class StopToken:
            def is_cancelled(self):
                return adapter._stop_requested

        return HardenedStripchatRunner(
            self._require_store(),
            policy_observer=(
                lambda: dict(self._manual_network_declaration or {})
            ) if self.network_provider == "manual" else self.policy_observer,
            target_relay_prefix=self._require_store().config["relevant_values"]["target_relay_prefix"],
            logger=self.logger,
            stop_token=StopToken(),
            progress=progress,
        )

    def run_vpn_snapshot(self, prepared, options, progress) -> StepOutcome:
        return self._runner(progress).run_step1(prepared.context)

    def run_local_snapshot(self, prepared, options, progress) -> StepOutcome:
        return self._runner(progress).run_step2(prepared.context)

    def run_compare(self, prepared, options, progress) -> StepOutcome:
        return self._runner(progress).run_step3(prepared.context)

    def run_verify(self, prepared, options, progress) -> StepOutcome:
        if options.headless:
            # Stripchat classification needs a normal browser window; a
            # headless request must be rejected at the boundary, never
            # silently downgraded.
            raise AdapterContractError("unsupported_browser_mode")
        runner = self._runner(progress)
        finalizer = runner.build_finalizer(
            prepared.context, self.base_dir, shadow=self.shadow
        )
        try:
            recovered = finalizer.recover()
        except Exception as exc:
            # Recovery must never leave the step without a typed outcome;
            # an untyped escape reaches the coordinator as a generic
            # workflow_step_failed with no manifest attempt recorded.
            raise AdapterContractError(
                "publication_recovery_failed", str(exc)[:200]
            ) from exc
        if recovered is not None:
            return recovered
        return runner.run_step4(
            prepared.context,
            provider_factory=lambda **kwargs: self._provider_factory(
                prepared,
                options,
                progress,
                **kwargs,
            ),
            finalizer=finalizer.finalize,
            resume=prepared.resumed,
        )

    def _provider_factory(
        self,
        prepared,
        options,
        progress,
        **kwargs,
    ) -> StripchatBrowserProvider:
        if self.driver_factory is None:
            raise AdapterContractError(
                "invalid_request", "no Chrome driver factory configured"
            )
        configured = set()
        for target in (self.driver_factory, self.window_manager):
            if target is None or id(target) in configured:
                continue
            configured.add(id(target))
            configure = getattr(target, "configure", None)
            if callable(configure):
                configure(start_minimized=bool(options.start_minimized))

        original_waiting = kwargs.pop("on_waiting", None)
        original_cleared = kwargs.pop("on_waiting_cleared", None)

        def emit_waiting(reason, deadline):
            if callable(original_waiting):
                original_waiting(reason, deadline)
            progress(
                ProgressEvent(
                    run_id=prepared.context.run_id,
                    generation_id=prepared.context.generation_id,
                    platform=self.spec.key,
                    step=StepName.VERIFY,
                    phase="waiting_for_user",
                    completed=0,
                    total=None,
                    message="Browser challenge needs user action.",
                    status=ProgressStatus.WAITING_FOR_USER,
                    occurred_at=utc_now_iso(),
                    requires_user=True,
                    waiting_reason=reason,
                    session_id=prepared.context.session_id,
                )
            )

        def emit_cleared():
            if callable(original_cleared):
                original_cleared()
            progress(
                ProgressEvent(
                    run_id=prepared.context.run_id,
                    generation_id=prepared.context.generation_id,
                    platform=self.spec.key,
                    step=StepName.VERIFY,
                    phase="verify",
                    completed=0,
                    total=None,
                    message="Browser challenge resolved; verification resumed.",
                    status=ProgressStatus.RUNNING,
                    occurred_at=utc_now_iso(),
                    session_id=prepared.context.session_id,
                )
            )

        return StripchatBrowserProvider(
            driver_factory=self.driver_factory,
            window_manager=self.window_manager,
            logger=self.logger,
            on_waiting=emit_waiting,
            on_waiting_cleared=emit_cleared,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Control and views

    def request_stop(self) -> None:
        self._stop_requested = True

    def clear_stop(self) -> None:
        self._stop_requested = False

    def is_stop_requested(self) -> bool:
        return self._stop_requested

    def blocked_projection_path(self) -> Path:
        return self._active_artifact_path(
            "step4.final_blocked", self.spec.artifacts.final
        )

    def candidate_projection_path(self) -> Path:
        return self._active_artifact_path(
            "step3.candidates", self.spec.artifacts.candidates
        )

    def _active_artifact_path(self, logical_name, fallback_name) -> Path:
        fallback = Path(self.session_path or self.base_dir, fallback_name)
        if self._store is None:
            return fallback
        try:
            context = self._store.load_active()
            if context is None:
                return fallback
            _document, path = self._store.load_artifact(
                context, logical_name
            )
            return Path(path)
        except (OSError, ValueError, RunStoreError):
            return fallback

    def get_master_models(self) -> list[dict]:
        return stripchat_master_repository(self.base_dir).load()

    def compile_master(self):
        # Stripchat master mutation only happens through a committed
        # publication; there is no ad-hoc rebuild entry point.
        return stripchat_master_repository(
            self.base_dir
        ).validate_consistency(repair_txt=False)

    def inspect_legacy(self) -> list[dict]:
        return inspect_legacy_sessions(self.base_dir)

    def record_restoration(self, prepared, outcome, evidence) -> None:
        store = self._require_store()
        store.record_restoration(
            prepared.context,
            {
                "required": True,
                "state": (
                    "restored"
                    if outcome.status.value == "succeeded"
                    else "failed"
                ),
                "original_policy": (evidence or {}).get("observed"),
                "desired_final_policy": {
                    "relay_code": (evidence or {})
                    .get("observed", {})
                    .get("relay_code"),
                    "chrome_excluded_at_end": True,
                    "vpn_connected_at_end": True,
                },
                "last_error_code": outcome.error_code,
            },
        )
