"""One operator-controlled adapter step, with no VPN controller or restoration."""

from __future__ import annotations

import uuid

from machine_policy import MachinePolicyError, MachinePolicyJournal, MachinePolicyLease
from platform_workflow import AdapterContractError, WorkflowOptions, validate_step_outcome
from runtime_lock import WorkflowBusyError, WorkflowLease
from workflow_types import RunOutcome, StepName, StepOutcome, utc_now_iso


def run_manual_adapter_step(
    workspace, adapter, step, options=None, logger=None, progress=None, *,
    manual_network_confirmed=False, machine_lease_factory=MachinePolicyLease,
    workspace_lease_factory=WorkflowLease, journal=None,
):
    """Run one typed step after its operator declaration under both shared locks.

    A pending machine-policy journal is an error, including our own unfinished
    work. This path neither recovers it nor changes the user's current route.
    """
    options = options or WorkflowOptions(headless=False, mode="manual")
    logger = logger or (lambda _message: None)
    progress = progress or (lambda _event: None)
    started = utc_now_iso()
    fallback = str(uuid.uuid4())
    prepared = None
    outcome = None
    methods = {
        StepName.VPN_SNAPSHOT: "run_vpn_snapshot",
        StepName.LOCAL_SNAPSHOT: "run_local_snapshot",
        StepName.COMPARE: "run_compare",
        StepName.VERIFY: "run_verify",
    }
    try:
        if step not in methods:
            raise AdapterContractError("invalid_request", "unsupported manual step")
        if getattr(adapter, "network_provider", None) != "manual":
            raise AdapterContractError("invalid_request", "manual provider is required")
        if step is not StepName.COMPARE and manual_network_confirmed is not True:
            raise AdapterContractError("manual_network_confirmation_required")
        active_journal = journal or MachinePolicyJournal(workspace=workspace)
        with machine_lease_factory():
            pending = active_journal.load_active()
            if pending is not None:
                active_journal.require_owned_document(pending)
                raise MachinePolicyError(
                    "machine_policy_recovery_required",
                    "Restore the pending machine policy from its originating checkout before continuing.",
                )
            with workspace_lease_factory(
                workspace, run_id=fallback, platform=adapter.spec.key,
                task=f"Manual {step.value}",
            ):
                prepared = adapter.prepare_step(step, mode="manual")
                try:
                    if adapter.is_stop_requested():
                        context = prepared.context
                        outcome = StepOutcome.cancelled(
                            run_id=context.run_id, generation_id=context.generation_id,
                            session_id=context.session_id, platform=adapter.spec.key,
                            step=step, error_code="cancelled_by_user",
                        )
                    else:
                        if step is not StepName.COMPARE:
                            adapter.declare_manual_network(prepared, step, confirmed=True)
                        raw = getattr(adapter, methods[step])(prepared, options, progress)
                        outcome = validate_step_outcome(
                            raw, prepared=prepared, spec=adapter.spec, expected_step=step,
                        )
                finally:
                    adapter.clear_manual_network_declaration()
    except Exception as exc:
        code = getattr(exc, "code", None) or (
            "workflow_already_running" if isinstance(exc, WorkflowBusyError)
            else "manual_step_failed"
        )
        logger(f"[ERROR] Manual step failed ({code}): {exc}")
        context = prepared.context if prepared else None
        outcome = StepOutcome.failed(
            run_id=context.run_id if context else fallback,
            generation_id=context.generation_id if context else fallback,
            platform=adapter.spec.key, session_id=context.session_id if context else "",
            step=step, error_code=code, error_message=str(exc)[:300],
        )
    context = prepared.context if prepared else None
    return RunOutcome.calculate(
        run_id=context.run_id if context else fallback,
        generation_id=context.generation_id if context else fallback,
        platform=adapter.spec.key, session_id=context.session_id if context else "",
        mode="manual", steps=[outcome], restoration=None, started_at=started,
        required_steps=(step,),
    )
