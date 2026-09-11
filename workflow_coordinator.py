"""Single typed workflow order and transactional machine-policy restoration.

Two coordinators live here during the migration:

* ``WorkflowCoordinator`` is the legacy runner-and-platform-string path
  used by Chaturbate, MyFreeCams, and XHamsterLive. Its ``_coerce`` of a
  ``None`` return into success is legacy behavior, kept only for those
  adapters.
* ``HardenedWorkflowCoordinator`` dispatches through a platform adapter,
  accepts only typed identity-bound outcomes, acquires the machine lease
  before the workspace lease, and restores machine policy in ``finally``.
"""

from __future__ import annotations

import uuid

from machine_policy import (
    MachinePolicyError,
    MachinePolicyJournal,
    MachinePolicyLease,
    MachinePolicyTarget,
    MachinePolicyController,
)
from platform_workflow import (
    AdapterContractError,
    RunSelection,
    WorkflowOptions,
    validate_step_outcome,
)
from runtime_lock import WorkflowLease
from workflow_types import (
    OutcomeStatus,
    RunOutcome,
    StepName,
    StepOutcome,
    utc_now_iso,
)


REQUIRED_FULL_AUTO_STEPS = (
    StepName.VPN_SNAPSHOT,
    StepName.LOCAL_SNAPSHOT,
    StepName.COMPARE,
    StepName.VERIFY,
)


class WorkflowCoordinator:
    def __init__(
        self,
        workspace,
        vpn,
        *,
        lease_factory=WorkflowLease,
        stop_token=None,
        logger=None,
        machine_lease_factory=MachinePolicyLease,
        journal=None,
    ):
        self.workspace = workspace
        self.vpn = vpn
        self.lease_factory = lease_factory
        self.stop_token = stop_token
        self.logger = logger or (lambda _message: None)
        self.machine_lease_factory = machine_lease_factory
        self.journal = journal or MachinePolicyJournal(workspace=workspace)

    def run(
        self,
        runner,
        *,
        platform,
        headless=True,
        start_minimized=True,
        mode="full_auto",
    ):
        return self._with_machine_policy_guard(
            lambda: self._run_under_machine_lease(
                runner, platform=platform, headless=headless,
                start_minimized=start_minimized, mode=mode,
            ),
            platform=platform, mode=mode, step=StepName.VPN_SNAPSHOT,
        )

    def resume_verify(self, runner, *, platform, start_minimized=True):
        return self._with_machine_policy_guard(
            lambda: self._resume_under_machine_lease(
                runner, platform=platform, start_minimized=start_minimized,
            ),
            platform=platform, mode="full_auto", step=StepName.VERIFY,
        )

    def _with_machine_policy_guard(self, operation, *, platform, mode, step):
        """Fence legacy VPN changes without recovering another checkout.

        Guard failures occur outside the legacy operation's restoration block:
        refusing a pending transaction must not itself change machine routing.
        """
        started_at = utc_now_iso()
        try:
            with self.machine_lease_factory():
                active = self.journal.load_active()
                if active is not None:
                    self.journal.require_owned_document(active)
                    raise MachinePolicyError(
                        "machine_policy_recovery_required",
                        "Restore the pending machine policy before starting a workflow.",
                    )
                return operation()
        except MachinePolicyError as exc:
            run_id = str(uuid.uuid4())
            return RunOutcome.calculate(
                run_id=run_id, generation_id=run_id, platform=platform,
                mode=mode, started_at=started_at, restoration=None,
                message=str(exc),
                steps=[StepOutcome.failed(
                    run_id=run_id, generation_id=run_id, platform=platform,
                    step=step, error_code=exc.code, error_message=str(exc),
                )],
            )

    def _run_under_machine_lease(
        self,
        runner,
        *,
        platform,
        headless=True,
        start_minimized=True,
        mode="full_auto",
    ):
        started_at = utc_now_iso()
        fallback_run_id = str(uuid.uuid4())
        generation_id = fallback_run_id
        outcomes = []
        primary_error = None
        restoration = None
        with self.lease_factory(
            self.workspace,
            run_id=fallback_run_id,
            platform=platform,
            task="Full Auto Flow",
        ):
            try:
                self._cancel_gate()
                self._ensure_chrome_uses_vpn()
                self.vpn.connect_ireland()
                raw_step1 = (
                    runner.step_vpn_list(headless=headless, mode=mode)
                    if platform == "Chaturbate"
                    else runner.step_vpn_list(headless=headless)
                )
                step1 = self._coerce(
                    raw_step1,
                    fallback_run_id,
                    generation_id,
                    platform,
                    StepName.VPN_SNAPSHOT,
                )
                outcomes.append(step1)
                fallback_run_id, generation_id = (
                    step1.run_id or fallback_run_id,
                    step1.generation_id or generation_id,
                )
                self._require_success(step1)
                self.vpn.disconnect()
                self._cancel_gate()
                step2 = self._coerce(
                    runner.step_local_list(headless=headless),
                    fallback_run_id,
                    generation_id,
                    platform,
                    StepName.LOCAL_SNAPSHOT,
                )
                outcomes.append(step2)
                self._require_success(step2)
                self._cancel_gate()
                step3 = self._coerce(
                    runner.compare_lists(),
                    fallback_run_id,
                    generation_id,
                    platform,
                    StepName.COMPARE,
                )
                outcomes.append(step3)
                self._require_success(step3)
                self._cancel_gate()
                if platform == "MyFreeCams":
                    # Step 4 compares who is on air against who this
                    # connection is offered, so it needs one snapshot from
                    # each side, taken close enough together that nobody has
                    # had time to go off air in between.
                    self.vpn.connect_ireland()
                    runner.capture_live_reference()
                    self.vpn.disconnect()
                    runner.capture_local_reference()
                    self._cancel_gate()
                    raw_step4 = runner.verify_live_models(
                        start_minimized=False
                    )
                else:
                    raw_step4 = runner.verify_candidates(
                        start_minimized=start_minimized,
                        reveal_on_captcha=True,
                    )
                step4 = self._coerce(
                    raw_step4,
                    fallback_run_id,
                    generation_id,
                    platform,
                    StepName.VERIFY,
                )
                outcomes.append(step4)
                self._require_success(step4)
                self._cancel_gate()
            except _Cancellation:
                if not outcomes or outcomes[-1].status is not OutcomeStatus.CANCELLED:
                    outcomes.append(
                        StepOutcome.cancelled(
                            run_id=fallback_run_id,
                            generation_id=generation_id,
                            platform=platform,
                            step=(
                                outcomes[-1].step
                                if outcomes
                                else StepName.VPN_SNAPSHOT
                            ),
                            error_code="cancelled_by_user",
                        )
                    )
            except Exception as exc:
                primary_error = exc
                if not outcomes or outcomes[-1].status is not OutcomeStatus.FAILED:
                    outcomes.append(
                        StepOutcome.failed(
                            run_id=fallback_run_id,
                            generation_id=generation_id,
                            platform=platform,
                            step=(
                                outcomes[-1].step
                                if outcomes
                                else StepName.VPN_SNAPSHOT
                            ),
                            error_code="workflow_step_failed",
                            error_message=str(exc)[:200],
                        )
                    )
            finally:
                restoration = self._restore(
                    fallback_run_id, generation_id, platform
                )
        outcome = RunOutcome.calculate(
            run_id=fallback_run_id,
            generation_id=generation_id,
            platform=platform,
            mode=mode,
            steps=outcomes,
            restoration=restoration,
            started_at=started_at,
            message=(
                str(primary_error)[:200] if primary_error is not None else ""
            ),
        )
        return outcome

    def _resume_under_machine_lease(
        self,
        runner,
        *,
        platform,
        start_minimized=True,
    ):
        started_at = utc_now_iso()
        run_id = str(uuid.uuid4())
        generation_id = run_id
        outcomes = []
        primary_error = None
        with self.lease_factory(
            self.workspace,
            run_id=run_id,
            platform=platform,
            task="Resume Full Auto Flow",
        ):
            try:
                self._cancel_gate()
                self.vpn.disconnect()
                raw = runner.verify_candidates(
                    start_minimized=start_minimized,
                    reveal_on_captcha=True,
                )
                step = self._coerce(
                    raw,
                    run_id,
                    generation_id,
                    platform,
                    StepName.VERIFY,
                )
                outcomes.append(step)
                run_id = step.run_id or run_id
                generation_id = step.generation_id or generation_id
                self._require_success(step)
                self._cancel_gate()
            except _Cancellation:
                if not outcomes or outcomes[-1].status is not OutcomeStatus.CANCELLED:
                    outcomes.append(
                        StepOutcome.cancelled(
                            run_id=run_id,
                            generation_id=generation_id,
                            platform=platform,
                            step=StepName.VERIFY,
                            error_code="cancelled_by_user",
                        )
                    )
            except Exception as exc:
                primary_error = exc
                if not outcomes or outcomes[-1].status is not OutcomeStatus.FAILED:
                    outcomes.append(
                        StepOutcome.failed(
                            run_id=run_id,
                            generation_id=generation_id,
                            platform=platform,
                            step=StepName.VERIFY,
                            error_code="resume_failed",
                            error_message=str(exc)[:200],
                        )
                    )
            finally:
                restoration = self._restore(
                    run_id, generation_id, platform
                )
        return RunOutcome.calculate(
            run_id=run_id,
            generation_id=generation_id,
            platform=platform,
            mode="full_auto",
            steps=outcomes,
            restoration=restoration,
            started_at=started_at,
            message=str(primary_error)[:200] if primary_error else "",
        )

    def _restore(self, run_id, generation_id, platform):
        errors = []
        try:
            if hasattr(self.vpn, "ensure_connected_ireland"):
                self.vpn.ensure_connected_ireland()
            else:
                self.vpn.connect_ireland()
        except Exception as exc:
            errors.append(f"VPN relay: {exc}")
        try:
            if hasattr(self.vpn, "ensure_chrome_in_split_tunnel"):
                self.vpn.ensure_chrome_in_split_tunnel()
            else:
                self.vpn.add_chrome_to_split_tunnel()
        except Exception as exc:
            errors.append(f"Chrome: {exc}")
        if errors:
            return StepOutcome.failed(
                run_id=run_id,
                generation_id=generation_id,
                platform=platform,
                step=StepName.RESTORE,
                error_code="machine_policy_restore_failed",
                error_message="; ".join(errors)[:300],
                warnings=tuple(errors),
            )
        return StepOutcome.succeeded(
            run_id=run_id,
            generation_id=generation_id,
            platform=platform,
            step=StepName.RESTORE,
        )

    def _ensure_chrome_uses_vpn(self):
        if hasattr(self.vpn, "ensure_chrome_not_in_split_tunnel"):
            self.vpn.ensure_chrome_not_in_split_tunnel()
        else:
            self.vpn.remove_chrome_from_split_tunnel()

    def _cancel_gate(self):
        if (
            self.stop_token
            and self.stop_token.is_cancelled()
        ):
            raise _Cancellation()

    @staticmethod
    def _require_success(outcome):
        if outcome.status is OutcomeStatus.CANCELLED:
            raise _Cancellation()
        if not outcome.can_continue:
            raise RuntimeError(
                outcome.error_message or outcome.status.value
            )

    @staticmethod
    def _coerce(value, run_id, generation_id, platform, step):
        """Legacy-only translation of an untyped return into success.

        Hardened adapters must never reach this: HardenedWorkflowCoordinator
        rejects a non-StepOutcome as invalid_step_outcome.
        """
        if isinstance(value, StepOutcome):
            return value
        return StepOutcome.succeeded(
            run_id=run_id,
            generation_id=generation_id,
            platform=platform,
            step=step,
        )


class HardenedWorkflowCoordinator:
    """Adapter-driven Full Auto with ordered leases and durable restoration."""

    def __init__(
        self,
        workspace,
        vpn,
        *,
        lease_factory=WorkflowLease,
        machine_lease_factory=MachinePolicyLease,
        journal=None,
        stop_token=None,
        logger=None,
        task_id="",
        progress=None,
    ):
        self.workspace = workspace
        self.vpn = vpn
        self.lease_factory = lease_factory
        self.machine_lease_factory = machine_lease_factory
        self.journal = journal or MachinePolicyJournal()
        self.stop_token = stop_token
        self.logger = logger or (lambda _message: None)
        self.task_id = task_id or str(uuid.uuid4())
        self.progress = progress or (lambda _event: None)

    # ------------------------------------------------------------------

    def run(self, adapter, *, options: WorkflowOptions) -> RunOutcome:
        started_at = utc_now_iso()
        spec = adapter.spec
        target = MachinePolicyTarget(
            relay_code=getattr(self.vpn, "relay_location", "ie")
        )
        controller = MachinePolicyController(
            self.vpn, self.journal, logger=self.logger,
            stop_token=self.stop_token,
        )
        outcomes: list[StepOutcome] = []
        restoration = None
        prepared = None
        message = ""
        transaction = None
        required_steps = REQUIRED_FULL_AUTO_STEPS
        try:
            machine_lease = self.machine_lease_factory()
        except MachinePolicyError as exc:
            return self._policy_failure(
                spec, started_at, exc.code, str(exc), options.mode
            )
        try:
            with machine_lease:
                # Repair any machine target a crashed run left behind
                # BEFORE creating anything in this workspace.
                recovery = controller.recover_pending(
                    origin_lease_factory=self._origin_lease
                )
                if recovery is not None and recovery.get("error_code"):
                    return self._policy_failure(
                        spec,
                        started_at,
                        "machine_policy_recovery_required",
                        "A previous run's machine policy could not be "
                        "restored; repair it before starting a new run.",
                        options.mode,
                    )
                with self.lease_factory(
                    self.workspace,
                    run_id=self.task_id,
                    platform=spec.key,
                    task="Full Auto Flow",
                ):
                    try:
                        prepared = adapter.prepare_run(
                            options.run_selection, options.mode
                        )
                    except AdapterContractError as exc:
                        return self._policy_failure(
                            spec,
                            started_at,
                            exc.code,
                            str(exc),
                            options.mode,
                        )
                    context = prepared.context
                    transaction = self.journal.begin(
                        origin_workspace=self.workspace,
                        origin_session_relative_path=context.session_id,
                        origin_manifest_relative_path=(
                            f"{spec.session_root_name}/{context.session_id}/"
                            f"runs/{context.run_id}/manifest.json"
                        ),
                        task_id=self.task_id,
                        platform_key=spec.key,
                        session_id=context.session_id,
                        run_id=context.run_id,
                        generation_id=context.generation_id,
                        target=target,
                        initial=controller.observe(),
                    )
                    current_step = prepared.next_step
                    try:
                        try:
                            start_index = REQUIRED_FULL_AUTO_STEPS.index(
                                prepared.next_step
                            )
                        except ValueError as exc:
                            raise AdapterContractError(
                                "invalid_request",
                                "adapter selected an unsupported next step",
                            ) from exc
                        required_steps = REQUIRED_FULL_AUTO_STEPS[start_index:]
                        methods = {
                            StepName.VPN_SNAPSHOT: adapter.run_vpn_snapshot,
                            StepName.LOCAL_SNAPSHOT: adapter.run_local_snapshot,
                            StepName.COMPARE: adapter.run_compare,
                            StepName.VERIFY: adapter.run_verify,
                        }
                        if start_index:
                            controller.disconnect_for_local(
                                transaction["transaction_id"]
                            )
                        for current_step in required_steps:
                            self._cancel_gate()
                            if current_step is StepName.VPN_SNAPSHOT:
                                controller.apply_vpn_route_for_snapshot(
                                    transaction["transaction_id"]
                                )
                            elif (
                                current_step is StepName.LOCAL_SNAPSHOT
                                and start_index == 0
                            ):
                                controller.disconnect_for_local(
                                    transaction["transaction_id"]
                                )
                            self._cancel_gate()
                            outcomes.append(
                                self._step(
                                    adapter,
                                    prepared,
                                    options,
                                    current_step,
                                    methods[current_step],
                                )
                            )
                            self._require_success(outcomes[-1])
                    except _Cancellation:
                        outcomes.append(
                            self._synthesize(
                                spec,
                                prepared,
                                outcomes,
                                OutcomeStatus.CANCELLED,
                                "cancelled_by_user",
                                step=current_step,
                            )
                        )
                    except AdapterContractError as exc:
                        message = exc.code
                        outcomes.append(
                            self._synthesize(
                                spec,
                                prepared,
                                outcomes,
                                OutcomeStatus.FAILED,
                                exc.code,
                                step=current_step,
                            )
                        )
                    except _StepStopped:
                        pass
                    except Exception as exc:
                        message = str(exc)[:200]
                        outcomes.append(
                            self._synthesize(
                                spec,
                                prepared,
                                outcomes,
                                OutcomeStatus.FAILED,
                                "workflow_step_failed",
                                message,
                                step=current_step,
                            )
                        )
                    finally:
                        # Restoration happens even after Stop or a failure.
                        results = controller.restore(
                            transaction["transaction_id"], target
                        )
                        restoration = self._restoration_outcome(
                            spec, prepared, results
                        )
                        try:
                            adapter.record_restoration(
                                prepared, restoration, results
                            )
                        except Exception:
                            pass
                        if results.get("ok"):
                            self.journal.set_manifest_sync(
                                transaction["transaction_id"], "synced"
                            )
                            self.journal.clear_active(
                                transaction["transaction_id"]
                            )
        except MachinePolicyError as exc:
            return self._policy_failure(
                spec, started_at, exc.code, str(exc), options.mode
            )
        context = prepared.context if prepared else None
        return RunOutcome.calculate(
            run_id=context.run_id if context else self.task_id,
            generation_id=context.generation_id if context else self.task_id,
            platform=spec.key,
            mode=options.mode,
            steps=outcomes,
            restoration=restoration,
            started_at=started_at,
            message=message,
            session_id=context.session_id if context else "",
            required_steps=required_steps,
        )

    def run_step(
        self,
        adapter,
        *,
        step: StepName,
        options: WorkflowOptions,
    ) -> RunOutcome:
        """Run one manual adapter step under the full machine-policy fence."""
        if step not in REQUIRED_FULL_AUTO_STEPS:
            return self._policy_failure(
                adapter.spec,
                utc_now_iso(),
                "invalid_request",
                f"unsupported manual step: {step}",
                options.mode,
                step=step,
            )
        started_at = utc_now_iso()
        spec = adapter.spec
        target = MachinePolicyTarget(
            relay_code=getattr(self.vpn, "relay_location", "ie")
        )
        controller = MachinePolicyController(
            self.vpn,
            self.journal,
            logger=self.logger,
            stop_token=self.stop_token,
        )
        outcomes: list[StepOutcome] = []
        restoration = None
        prepared = None
        message = ""
        try:
            machine_lease = self.machine_lease_factory()
        except MachinePolicyError as exc:
            return self._policy_failure(
                spec,
                started_at,
                exc.code,
                str(exc),
                options.mode,
                step=step,
            )
        try:
            with machine_lease:
                recovery = controller.recover_pending(
                    origin_lease_factory=self._origin_lease
                )
                if recovery is not None and recovery.get("error_code"):
                    return self._policy_failure(
                        spec,
                        started_at,
                        "machine_policy_recovery_required",
                        "A previous run's machine policy could not be restored.",
                        options.mode,
                        step=step,
                    )
                with self.lease_factory(
                    self.workspace,
                    run_id=self.task_id,
                    platform=spec.key,
                    task=f"Manual {step.value}",
                ):
                    try:
                        prepared = adapter.prepare_step(step, options.mode)
                    except AdapterContractError as exc:
                        return self._policy_failure(
                            spec,
                            started_at,
                            exc.code,
                            str(exc),
                            options.mode,
                            step=step,
                        )
                    context = prepared.context
                    transaction = self.journal.begin(
                        origin_workspace=self.workspace,
                        origin_session_relative_path=context.session_id,
                        origin_manifest_relative_path=(
                            f"{spec.session_root_name}/{context.session_id}/"
                            f"runs/{context.run_id}/manifest.json"
                        ),
                        task_id=self.task_id,
                        platform_key=spec.key,
                        session_id=context.session_id,
                        run_id=context.run_id,
                        generation_id=context.generation_id,
                        target=target,
                        initial=controller.observe(),
                    )
                    try:
                        self._cancel_gate()
                        if step is StepName.VPN_SNAPSHOT:
                            controller.apply_vpn_route_for_snapshot(
                                transaction["transaction_id"]
                            )
                        else:
                            controller.disconnect_for_local(
                                transaction["transaction_id"]
                            )
                        methods = {
                            StepName.VPN_SNAPSHOT: adapter.run_vpn_snapshot,
                            StepName.LOCAL_SNAPSHOT: adapter.run_local_snapshot,
                            StepName.COMPARE: adapter.run_compare,
                            StepName.VERIFY: adapter.run_verify,
                        }
                        self._cancel_gate()
                        outcomes.append(
                            self._step(
                                adapter,
                                prepared,
                                options,
                                step,
                                methods[step],
                            )
                        )
                        self._require_success(outcomes[-1])
                    except _Cancellation:
                        outcomes.append(
                            self._synthesize(
                                spec,
                                prepared,
                                outcomes,
                                OutcomeStatus.CANCELLED,
                                "cancelled_by_user",
                                step=step,
                            )
                        )
                    except AdapterContractError as exc:
                        message = exc.code
                        outcomes.append(
                            self._synthesize(
                                spec,
                                prepared,
                                outcomes,
                                OutcomeStatus.FAILED,
                                exc.code,
                                step=step,
                            )
                        )
                    except _StepStopped:
                        pass
                    except Exception as exc:
                        message = str(exc)[:200]
                        outcomes.append(
                            self._synthesize(
                                spec,
                                prepared,
                                outcomes,
                                OutcomeStatus.FAILED,
                                "workflow_step_failed",
                                message,
                                step=step,
                            )
                        )
                    finally:
                        results = controller.restore(
                            transaction["transaction_id"], target
                        )
                        restoration = self._restoration_outcome(
                            spec, prepared, results
                        )
                        try:
                            adapter.record_restoration(
                                prepared, restoration, results
                            )
                        except Exception:
                            pass
                        if results.get("ok"):
                            self.journal.set_manifest_sync(
                                transaction["transaction_id"], "synced"
                            )
                            self.journal.clear_active(
                                transaction["transaction_id"]
                            )
        except MachinePolicyError as exc:
            return self._policy_failure(
                spec,
                started_at,
                exc.code,
                str(exc),
                options.mode,
                step=step,
            )
        context = prepared.context if prepared else None
        return RunOutcome.calculate(
            run_id=context.run_id if context else self.task_id,
            generation_id=context.generation_id if context else self.task_id,
            platform=spec.key,
            mode=options.mode,
            steps=outcomes,
            restoration=restoration,
            started_at=started_at,
            message=message,
            session_id=context.session_id if context else "",
            required_steps=(step,),
        )

    # ------------------------------------------------------------------

    def _origin_lease(self, origin):
        return self.lease_factory(
            origin, platform="", task="Machine Policy Reconcile"
        )

    def _step(self, adapter, prepared, options, step, method) -> StepOutcome:
        raw = method(prepared, options, self._progress)
        return validate_step_outcome(
            raw, prepared=prepared, spec=adapter.spec, expected_step=step
        )

    def _progress(self, event):
        self.progress(event)

    def _cancel_gate(self):
        if self.stop_token and self.stop_token.is_cancelled():
            raise _Cancellation()

    @staticmethod
    def _require_success(outcome):
        if outcome.status is OutcomeStatus.SUCCEEDED:
            return
        # A non-success step stops progression but must not erase its own
        # persisted outcome; restoration still runs in finally.
        raise _StepStopped()

    @staticmethod
    def _synthesize(
        spec,
        prepared,
        outcomes,
        status,
        code,
        message="",
        *,
        step=None,
    ):
        step = step or (
            outcomes[-1].step if outcomes else StepName.VPN_SNAPSHOT
        )
        factory = {
            OutcomeStatus.CANCELLED: StepOutcome.cancelled,
            OutcomeStatus.FAILED: StepOutcome.failed,
        }[status]
        context = prepared.context if prepared else None
        return factory(
            run_id=context.run_id if context else "",
            generation_id=context.generation_id if context else "",
            platform=spec.key,
            step=step,
            error_code=code,
            error_message=message,
            session_id=context.session_id if context else "",
        )

    @staticmethod
    def _restoration_outcome(spec, prepared, results) -> StepOutcome:
        context = prepared.context if prepared else None
        base = dict(
            run_id=context.run_id if context else "",
            generation_id=context.generation_id if context else "",
            platform=spec.key,
            step=StepName.RESTORE,
            session_id=context.session_id if context else "",
        )
        if results.get("ok"):
            return StepOutcome.succeeded(**base)
        codes = [
            results.get("error_code"),
            (results.get("vpn") or {}).get("error_code"),
            (results.get("chrome") or {}).get("error_code"),
        ]
        code = next((item for item in codes if item), "restoration_failed")
        warnings = tuple(
            f"{key}: {(results.get(key) or {}).get('message', '')}"
            for key in ("vpn", "chrome")
            if not (results.get(key) or {}).get("ok", True)
        )
        return StepOutcome.failed(
            error_code=code,
            error_message="Machine policy restoration did not complete.",
            warnings=warnings,
            **base,
        )

    def _policy_failure(
        self,
        spec,
        started_at,
        code,
        message,
        mode,
        *,
        step=StepName.VPN_SNAPSHOT,
    ):
        failure = StepOutcome.failed(
            run_id=self.task_id,
            generation_id=self.task_id,
            platform=spec.key,
            step=step,
            error_code=code,
            error_message=str(message)[:200],
        )
        return RunOutcome.calculate(
            run_id=self.task_id,
            generation_id=self.task_id,
            platform=spec.key,
            mode=mode,
            steps=[failure],
            restoration=None,
            started_at=started_at,
            message=str(message)[:200],
        )


class _Cancellation(Exception):
    pass


class _StepStopped(Exception):
    pass
