import tempfile
import unittest
import uuid
from pathlib import Path

from workflow_coordinator import (
    HardenedWorkflowCoordinator,
    WorkflowCoordinator,
)
from workflow_types import (
    OutcomeStatus,
    ProgressEvent,
    ProgressStatus,
    StepName,
    StepOutcome,
)


class _NoopLease:
    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Vpn:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def _call(self, name):
        self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError(name)

    def ensure_chrome_not_in_split_tunnel(self):
        self._call("chrome_uses_vpn")

    def connect_ireland(self):
        self._call("connect_ireland")

    def disconnect(self):
        self._call("disconnect")

    def ensure_connected_ireland(self):
        self._call("restore_ireland")

    def ensure_chrome_in_split_tunnel(self):
        self._call("restore_chrome")


class _Runner:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _outcome(step):
        return StepOutcome.succeeded(
            run_id="run",
            generation_id="generation",
            platform="Chaturbate",
            step=step,
        )

    def step_vpn_list(self, headless=True, mode="manual"):
        self.calls.append(("step1", mode))
        return self._outcome(StepName.VPN_SNAPSHOT)

    def step_local_list(self, headless=True):
        self.calls.append("step2")
        return self._outcome(StepName.LOCAL_SNAPSHOT)

    def compare_lists(self):
        self.calls.append("step3")
        return self._outcome(StepName.COMPARE)

    def verify_candidates(self, **_kwargs):
        self.calls.append("step4")
        return self._outcome(StepName.VERIFY)


class _MfcRunner:
    """MyFreeCams step 4 compares an on-air snapshot against a viewable one,
    so each has to be taken on the right side of the VPN."""

    def __init__(self, timeline):
        self.timeline = timeline

    @staticmethod
    def _outcome(step):
        return StepOutcome.succeeded(
            run_id="run",
            generation_id="generation",
            platform="MyFreeCams",
            step=step,
        )

    def step_vpn_list(self, headless=True):
        self.timeline.append("step1")
        return self._outcome(StepName.VPN_SNAPSHOT)

    def step_local_list(self, headless=True):
        self.timeline.append("step2")
        return self._outcome(StepName.LOCAL_SNAPSHOT)

    def compare_lists(self):
        self.timeline.append("step3")
        return self._outcome(StepName.COMPARE)

    def capture_live_reference(self):
        self.timeline.append("capture_on_air")
        return 5

    def capture_local_reference(self):
        self.timeline.append("capture_viewable")
        return 4

    def verify_live_models(self, **_kwargs):
        self.timeline.append("step4")
        return self._outcome(StepName.VERIFY)


class WorkflowCoordinatorTests(unittest.TestCase):
    def test_myfreecams_takes_each_snapshot_on_the_right_side_of_the_vpn(self):
        with tempfile.TemporaryDirectory() as tmp:
            vpn = _Vpn()
            runner = _MfcRunner(vpn.calls)

            outcome = WorkflowCoordinator(
                tmp, vpn, lease_factory=_NoopLease
            ).run(runner, platform="MyFreeCams")

            self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(
                vpn.calls,
                [
                    "chrome_uses_vpn",
                    "connect_ireland",
                    "step1",
                    "disconnect",
                    "step2",
                    "step3",
                    "connect_ireland",
                    "capture_on_air",
                    "disconnect",
                    "capture_viewable",
                    "step4",
                    "restore_ireland",
                    "restore_chrome",
                ],
            )

    def test_successful_flow_order_and_restoration(self):
        with tempfile.TemporaryDirectory() as tmp:
            vpn = _Vpn()
            runner = _Runner()
            outcome = WorkflowCoordinator(
                tmp, vpn, lease_factory=_NoopLease
            ).run(runner, platform="Chaturbate")
            self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(
                vpn.calls,
                [
                    "chrome_uses_vpn",
                    "connect_ireland",
                    "disconnect",
                    "restore_ireland",
                    "restore_chrome",
                ],
            )
            self.assertEqual(
                runner.calls,
                [("step1", "full_auto"), "step2", "step3", "step4"],
            )

    def test_initial_vpn_connect_failure_still_restores_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            vpn = _Vpn(fail_on={"connect_ireland"})
            outcome = WorkflowCoordinator(
                tmp, vpn, lease_factory=_NoopLease
            ).run(_Runner(), platform="Chaturbate")
            self.assertEqual(outcome.status, OutcomeStatus.FAILED)
            self.assertIn("restore_ireland", vpn.calls)
            self.assertIn("restore_chrome", vpn.calls)
            self.assertIn("connect_ireland", outcome.message)

    def test_restoration_errors_do_not_hide_primary_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            vpn = _Vpn(
                fail_on={
                    "connect_ireland",
                    "restore_ireland",
                    "restore_chrome",
                }
            )
            outcome = WorkflowCoordinator(
                tmp, vpn, lease_factory=_NoopLease
            ).run(_Runner(), platform="Chaturbate")
            self.assertEqual(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(
                outcome.restoration.error_code,
                "machine_policy_restore_failed",
            )
            self.assertEqual(len(outcome.restoration.warnings), 2)
            self.assertIn("connect_ireland", outcome.message)


class LegacyMachineGuardTests(unittest.TestCase):
    def test_busy_machine_blocks_run_and_resume_without_restoration(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp, "runtime")
            with MachinePolicyLease(runtime_dir=runtime):
                for operation in ("run", "resume_verify"):
                    with self.subTest(operation=operation):
                        vpn = _Vpn()
                        runner = _Runner()
                        coordinator = WorkflowCoordinator(
                            tmp, vpn, lease_factory=_NoopLease,
                            machine_lease_factory=lambda: MachinePolicyLease(runtime_dir=runtime),
                            journal=MachinePolicyJournal(runtime, workspace=tmp),
                        )
                        outcome = getattr(coordinator, operation)(runner, platform="Chaturbate")
                        self.assertEqual(outcome.error_code, "machine_lease_busy")
                        self.assertEqual(vpn.calls, [])
                        self.assertEqual(runner.calls, [])
                        self.assertIsNone(outcome.restoration)

    def test_foreign_policy_blocks_legacy_paths_without_touching_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp, "runtime")
            public = Path(tmp, "public-checkout")
            foreign = Path(tmp, "foreign-checkout")
            begin_policy(MachinePolicyJournal(runtime), foreign)
            before = {p: p.read_bytes() for p in runtime.rglob("*.json")}
            for operation in ("run", "resume_verify"):
                for platform in ("Chaturbate", "MyFreeCams", "XHamsterLive"):
                    with self.subTest(operation=operation, platform=platform):
                        vpn = _Vpn()
                        runner = _Runner()
                        coordinator = WorkflowCoordinator(
                            public, vpn, lease_factory=_NoopLease,
                            machine_lease_factory=lambda: MachinePolicyLease(runtime_dir=runtime),
                            journal=MachinePolicyJournal(runtime, workspace=public),
                        )
                        outcome = getattr(coordinator, operation)(runner, platform=platform)
                        self.assertEqual(outcome.error_code, "machine_policy_foreign_workspace")
                        self.assertEqual(vpn.calls, [])
                        self.assertEqual(runner.calls, [])
                        self.assertIsNone(outcome.restoration)
            self.assertEqual({p: p.read_bytes() for p in runtime.rglob("*.json")}, before)

    def test_machine_lease_covers_legacy_operations_and_restoration(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp, "runtime")
            testcase = self

            class CheckingVpn(_Vpn):
                def _call(self, name):
                    with testcase.assertRaises(MachinePolicyError):
                        MachinePolicyLease(runtime_dir=runtime).acquire()
                    super()._call(name)

            for operation in ("run", "resume_verify"):
                with self.subTest(operation=operation):
                    vpn = CheckingVpn()
                    coordinator = WorkflowCoordinator(
                        tmp, vpn, lease_factory=_NoopLease,
                        machine_lease_factory=lambda: MachinePolicyLease(runtime_dir=runtime),
                        journal=MachinePolicyJournal(runtime, workspace=tmp),
                    )
                    outcome = getattr(coordinator, operation)(_Runner(), platform="Chaturbate")
                    self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
                    self.assertIn("restore_chrome", vpn.calls)


# ---------------------------------------------------------------------------
# Hardened adapter-driven coordinator


from machine_policy import MachinePolicyError, MachinePolicyJournal, MachinePolicyLease
from platform_contracts import (
    PlatformArtifactNames,
    PlatformCapabilities,
    PlatformSpec,
)
from platform_workflow import (
    PreparedWorkflowRun,
    ResumeDescriptor,
    RunSelection,
    WorkflowOptions,
)
from test_machine_policy import FakeVpn, begin as begin_policy
from workflow_store import RunContext


def hardened_spec():
    return PlatformSpec(
        key="stripchat",
        display_name="Stripchat",
        session_root_name="sc sessions",
        url_canonicalizer=lambda value: value,
        canonicalizer_version="1",
        classifier_version="1",
        api_contract_version="1",
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
        ),
    )


class FakeAdapter:
    """Adapter whose steps can be scripted with typed or broken returns."""

    def __init__(self, script=None):
        self.spec = hardened_spec()
        self.script = dict(script or {})
        self.calls = []
        self.restorations = []
        self.context = RunContext(
            session_path=r"C:\sessions\session 1",
            session_id="session 1",
            run_id=str(uuid.uuid4()),
            generation_id=str(uuid.uuid4()),
            platform_key="stripchat",
            mode="full_auto",
        )

    def prepare_run(self, selection, mode):
        self.calls.append(("prepare_run", selection))
        return PreparedWorkflowRun(
            self.context, False, StepName.VPN_SNAPSHOT
        )

    def prepare_step(self, step, mode="manual"):
        self.calls.append(("prepare_step", step))
        return PreparedWorkflowRun(
            self.context, False, step
        )

    def inspect_resume(self):
        return ResumeDescriptor(False, self.spec.key)

    def _result(self, step):
        self.calls.append(step.value)
        value = self.script.get(step, "succeeded")
        if value in ("succeeded", "incomplete", "failed", "cancelled"):
            factory = {
                "succeeded": StepOutcome.succeeded,
                "incomplete": StepOutcome.incomplete,
                "failed": StepOutcome.failed,
                "cancelled": StepOutcome.cancelled,
            }[value]
            return factory(
                run_id=self.context.run_id,
                generation_id=self.context.generation_id,
                platform=self.spec.key,
                step=step,
                session_id=self.context.session_id,
            )
        return value

    def run_vpn_snapshot(self, prepared, options, progress):
        return self._result(StepName.VPN_SNAPSHOT)

    def run_local_snapshot(self, prepared, options, progress):
        return self._result(StepName.LOCAL_SNAPSHOT)

    def run_compare(self, prepared, options, progress):
        return self._result(StepName.COMPARE)

    def run_verify(self, prepared, options, progress):
        return self._result(StepName.VERIFY)

    def record_restoration(self, prepared, outcome, evidence):
        self.restorations.append(outcome)


def coordinator_for(root, vpn, adapter=None, stop_token=None):
    return HardenedWorkflowCoordinator(
        root,
        vpn,
        lease_factory=_NoopLease,
        machine_lease_factory=lambda: MachinePolicyLease(
            runtime_dir=Path(root, "machine_policy")
        ),
        journal=MachinePolicyJournal(Path(root, "machine_policy")),
        stop_token=stop_token,
    )


OPTIONS = WorkflowOptions(
    headless=False, run_selection=RunSelection.NEW, mode="full_auto"
)


class HardenedCoordinatorTests(unittest.TestCase):
    def test_exact_step_and_policy_order(self):
        with tempfile.TemporaryDirectory() as root:
            vpn = FakeVpn(connected=False, excluded=True)
            adapter = FakeAdapter()
            outcome = coordinator_for(root, vpn).run(
                adapter, options=OPTIONS
            )
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(
                [item for item in adapter.calls if isinstance(item, str)],
                [
                    "step1_vpn_snapshot",
                    "step2_local_snapshot",
                    "step3_compare",
                    "step4_verify",
                ],
            )
            self.assertEqual(
                vpn.calls[:3], ["chrome_out", "connect_relay", "disconnect"]
            )
            self.assertEqual(vpn.calls[-2:], ["ensure_connected_relay", "chrome_in"])
            self.assertTrue(vpn.connected)
            self.assertTrue(vpn.excluded)

    def test_none_from_a_hardened_adapter_is_a_contract_failure(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = FakeAdapter({StepName.VPN_SNAPSHOT: None})
            outcome = coordinator_for(root, FakeVpn()).run(
                adapter, options=OPTIONS
            )
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "invalid_step_outcome")
            self.assertNotIn("step2_local_snapshot", adapter.calls)

    def test_identity_mismatch_stops_progression(self):
        with tempfile.TemporaryDirectory() as root:
            wrong = StepOutcome.succeeded(
                run_id=str(uuid.uuid4()),
                generation_id=str(uuid.uuid4()),
                platform="stripchat",
                step=StepName.VPN_SNAPSHOT,
                session_id="session 1",
            )
            adapter = FakeAdapter({StepName.VPN_SNAPSHOT: wrong})
            outcome = coordinator_for(root, FakeVpn()).run(
                adapter, options=OPTIONS
            )
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "step_run_mismatch")

    def test_incomplete_step_stops_later_steps_but_still_restores(self):
        with tempfile.TemporaryDirectory() as root:
            vpn = FakeVpn(connected=False, excluded=False)
            adapter = FakeAdapter({StepName.LOCAL_SNAPSHOT: "incomplete"})
            outcome = coordinator_for(root, vpn).run(
                adapter, options=OPTIONS
            )
            self.assertIs(outcome.status, OutcomeStatus.INCOMPLETE)
            self.assertNotIn("step3_compare", adapter.calls)
            self.assertIs(
                outcome.restoration.status, OutcomeStatus.SUCCEEDED
            )
            self.assertTrue(vpn.connected)
            self.assertTrue(vpn.excluded)

    def test_restoration_failure_fails_an_otherwise_successful_run(self):
        with tempfile.TemporaryDirectory() as root:
            vpn = FakeVpn()
            vpn.fail.add("ensure_connected_relay")
            adapter = FakeAdapter()
            outcome = coordinator_for(root, vpn).run(
                adapter, options=OPTIONS
            )
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "vpn_transition_failed")
            self.assertIn("chrome_in", vpn.calls)
            self.assertTrue(adapter.restorations)

    def test_journal_is_written_before_any_policy_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            journal = MachinePolicyJournal(Path(root, "machine_policy"))
            vpn = FakeVpn(connected=False, excluded=True)
            observed = []

            original = vpn.ensure_chrome_not_in_split_tunnel

            def watched():
                observed.append(journal.load_active() is not None)
                return original()

            vpn.ensure_chrome_not_in_split_tunnel = watched
            coordinator_for(root, vpn).run(FakeAdapter(), options=OPTIONS)
            self.assertEqual(observed, [True])

    def test_second_process_loses_machine_lease_contention_safely(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Path(root, "machine_policy")
            holder = MachinePolicyLease(runtime_dir=runtime)
            holder.acquire()
            try:
                adapter = FakeAdapter()
                outcome = coordinator_for(root, FakeVpn()).run(
                    adapter, options=OPTIONS
                )
                self.assertIs(outcome.status, OutcomeStatus.FAILED)
                self.assertEqual(outcome.error_code, "machine_lease_busy")
                self.assertEqual(adapter.calls, [])
            finally:
                holder.release()

    def test_pending_transaction_is_repaired_before_a_new_run(self):
        with tempfile.TemporaryDirectory() as root:
            journal = MachinePolicyJournal(Path(root, "machine_policy"))
            from test_machine_policy import begin

            begin(journal, root)
            vpn = FakeVpn(connected=False, excluded=False)
            adapter = FakeAdapter()
            outcome = coordinator_for(root, vpn).run(
                adapter, options=OPTIONS
            )
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertIn("ensure_connected_relay", vpn.calls[:3])

    def test_unrepairable_machine_state_blocks_the_run(self):
        with tempfile.TemporaryDirectory() as root:
            journal = MachinePolicyJournal(Path(root, "machine_policy"))
            from test_machine_policy import begin

            begin(journal, root)
            vpn = FakeVpn(connected=False, excluded=False)
            vpn.fail.add("ensure_connected_relay")
            adapter = FakeAdapter()
            outcome = coordinator_for(root, vpn).run(
                adapter, options=OPTIONS
            )
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(
                outcome.error_code, "machine_policy_recovery_required"
            )
            self.assertEqual(adapter.calls, [])

    def test_stop_before_step_one_cancels_and_restores(self):
        with tempfile.TemporaryDirectory() as root:

            class AlwaysStopped:
                def is_cancelled(self):
                    return True

            vpn = FakeVpn(connected=False, excluded=False)
            adapter = FakeAdapter()
            outcome = coordinator_for(
                root, vpn, stop_token=AlwaysStopped()
            ).run(adapter, options=OPTIONS)
            self.assertIs(outcome.status, OutcomeStatus.CANCELLED)
            self.assertEqual(adapter.calls[1:], [])
            self.assertTrue(vpn.connected)
            self.assertTrue(vpn.excluded)

    def test_partial_success_never_reports_full_auto_success(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = FakeAdapter()
            coordinator = coordinator_for(root, FakeVpn())
            adapter.run_compare = lambda *args, **kwargs: (
                StepOutcome.succeeded(
                    run_id=adapter.context.run_id,
                    generation_id=adapter.context.generation_id,
                    platform="stripchat",
                    step=StepName.COMPARE,
                    session_id="session 1",
                )
            )
            adapter.run_verify = lambda *args, **kwargs: (
                StepOutcome.succeeded(
                    run_id=adapter.context.run_id,
                    generation_id=adapter.context.generation_id,
                    platform="stripchat",
                    step=StepName.COMPARE,
                    session_id="session 1",
                )
            )
            outcome = coordinator.run(adapter, options=OPTIONS)
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "step_name_mismatch")
            self.assertEqual(
                [item.step for item in outcome.steps],
                [
                    StepName.VPN_SNAPSHOT,
                    StepName.LOCAL_SNAPSHOT,
                    StepName.COMPARE,
                    StepName.VERIFY,
                ],
            )
            self.assertIs(outcome.steps[-1].status, OutcomeStatus.FAILED)

    def test_manual_step_uses_adapter_under_machine_policy_and_restores(self):
        with tempfile.TemporaryDirectory() as root:
            vpn = FakeVpn(connected=True, excluded=True)
            adapter = FakeAdapter()
            outcome = coordinator_for(root, vpn).run_step(
                adapter,
                step=StepName.COMPARE,
                options=WorkflowOptions(headless=False, mode="manual"),
            )

            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertIn(
                ("prepare_step", StepName.COMPARE), adapter.calls
            )
            self.assertIn("step3_compare", adapter.calls)
            self.assertNotIn("step1_vpn_snapshot", adapter.calls)
            self.assertIn("disconnect", vpn.calls)
            self.assertEqual(
                vpn.calls[-2:], ["ensure_connected_relay", "chrome_in"]
            )

    def test_manual_stop_is_bound_to_the_requested_step(self):
        with tempfile.TemporaryDirectory() as root:

            class AlwaysStopped:
                def is_cancelled(self):
                    return True

            adapter = FakeAdapter()
            outcome = coordinator_for(
                root,
                FakeVpn(),
                stop_token=AlwaysStopped(),
            ).run_step(
                adapter,
                step=StepName.VERIFY,
                options=WorkflowOptions(headless=False, mode="manual"),
            )

            self.assertIs(outcome.status, OutcomeStatus.CANCELLED)
            self.assertIs(outcome.steps[-1].step, StepName.VERIFY)
            self.assertIs(outcome.last_resumable_step, StepName.VERIFY)

    def test_resume_full_auto_runs_only_the_persisted_next_step(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = FakeAdapter()

            def prepare_resume(selection, mode):
                adapter.calls.append(("prepare_run", selection))
                return PreparedWorkflowRun(
                    adapter.context, True, StepName.VERIFY
                )

            adapter.prepare_run = prepare_resume
            outcome = coordinator_for(root, FakeVpn()).run(
                adapter,
                options=WorkflowOptions(
                    headless=False,
                    mode="full_auto",
                    run_selection=RunSelection.RESUME,
                ),
            )

            step_calls = [
                item for item in adapter.calls if isinstance(item, str)
            ]
            self.assertEqual(step_calls, ["step4_verify"])
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)

    def test_structured_progress_is_forwarded_to_the_service_callback(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = FakeAdapter()
            received = []

            def emit_progress(prepared, options, progress):
                progress(
                    ProgressEvent(
                        run_id=adapter.context.run_id,
                        generation_id=adapter.context.generation_id,
                        platform="stripchat",
                        step=StepName.VERIFY,
                        phase="verify",
                        completed=1,
                        total=2,
                        message="Verified 1 of 2",
                        status=ProgressStatus.RUNNING,
                        session_id=adapter.context.session_id,
                    )
                )
                return adapter._result(StepName.VERIFY)

            adapter.run_verify = emit_progress
            coordinator = HardenedWorkflowCoordinator(
                root,
                FakeVpn(),
                lease_factory=_NoopLease,
                machine_lease_factory=lambda: MachinePolicyLease(
                    runtime_dir=Path(root, "machine_policy")
                ),
                journal=MachinePolicyJournal(Path(root, "machine_policy")),
                progress=received.append,
            )
            coordinator.run_step(
                adapter,
                step=StepName.VERIFY,
                options=WorkflowOptions(headless=False, mode="manual"),
            )

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].message, "Verified 1 of 2")
