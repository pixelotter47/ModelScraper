import unittest

from workflow_types import (
    OutcomeStatus,
    RunOutcome,
    StepName,
    StepOutcome,
)


class WorkflowTypeTests(unittest.TestCase):
    def test_step_outcome_round_trip_and_continue_contract(self):
        outcome = StepOutcome.succeeded(
            run_id="run",
            generation_id="generation",
            platform="Chaturbate",
            step=StepName.COMPARE,
            input_count=3,
            output_count=2,
        )
        restored = StepOutcome.from_dict(outcome.to_dict())
        self.assertEqual(restored, outcome)
        self.assertTrue(restored.can_continue)

    def test_only_succeeded_can_continue(self):
        for constructor in (
            StepOutcome.cancelled,
            StepOutcome.failed,
            StepOutcome.incomplete,
        ):
            outcome = constructor(
                run_id="run",
                generation_id="generation",
                platform="Chaturbate",
                step=StepName.VERIFY,
            )
            self.assertFalse(outcome.can_continue)

    def test_restoration_failure_makes_successful_scrape_fail(self):
        step = StepOutcome.succeeded(
            run_id="run",
            generation_id="generation",
            platform="Chaturbate",
            step=StepName.VERIFY,
        )
        restoration = StepOutcome.failed(
            run_id="run",
            generation_id="generation",
            platform="Chaturbate",
            step=StepName.RESTORE,
            error_code="chrome_policy_failed",
        )
        outcome = RunOutcome.calculate(
            run_id="run",
            generation_id="generation",
            platform="Chaturbate",
            mode="full_auto",
            steps=[step],
            restoration=restoration,
            started_at=step.started_at,
        )
        self.assertEqual(outcome.status, OutcomeStatus.FAILED)
        self.assertEqual(outcome.error_code, "chrome_policy_failed")


class ProgressSchemaV2Tests(unittest.TestCase):
    def _event(self, **overrides):
        from workflow_types import ProgressEvent, ProgressStatus

        values = {
            "run_id": "run",
            "generation_id": "generation",
            "platform": "stripchat",
            "step": StepName.VERIFY,
            "phase": "verify",
            "completed": 1,
            "total": 4,
            "message": "Verified 1 of 4",
            "status": ProgressStatus.RUNNING,
        }
        values.update(overrides)
        return ProgressEvent(**values)

    def test_progress_event_rejects_outcome_status(self):
        with self.assertRaises(TypeError):
            self._event(status=OutcomeStatus.SUCCEEDED)
        with self.assertRaises(TypeError):
            self._event(status="running")

    def test_progress_event_round_trip(self):
        from workflow_types import ProgressEvent, ProgressStatus

        event = self._event(
            status=ProgressStatus.WAITING_FOR_USER,
            requires_user=True,
            waiting_reason="captcha_required",
            sequence=7,
            session_id="session 1",
        )
        value = event.to_dict()
        self.assertEqual(value["status"], "waiting_for_user")
        restored = ProgressEvent.from_dict(value)
        self.assertEqual(restored, event)

    def test_legacy_progress_statuses_normalize(self):
        from workflow_types import ProgressEvent, ProgressStatus

        base = self._event().to_dict()
        for legacy, expected in (
            ("succeeded", ProgressStatus.RUNNING),
            ("incomplete", ProgressStatus.RUNNING),
            ("running", ProgressStatus.RUNNING),
            ("waiting_for_user", ProgressStatus.WAITING_FOR_USER),
        ):
            with self.subTest(legacy=legacy):
                payload = dict(base)
                payload["status"] = legacy
                self.assertIs(
                    ProgressEvent.from_dict(payload).status, expected
                )

    def test_legacy_terminal_progress_statuses_are_rejected(self):
        from workflow_types import ProgressEvent

        base = self._event().to_dict()
        for legacy in ("failed", "cancelled"):
            with self.subTest(legacy=legacy):
                payload = dict(base)
                payload["status"] = legacy
                with self.assertRaises(ValueError) as caught:
                    ProgressEvent.from_dict(payload)
                self.assertIn(
                    "invalid_legacy_progress_status", str(caught.exception)
                )

    def test_percent_needs_a_stable_denominator(self):
        self.assertIsNone(self._event(total=None).percent)
        self.assertIsNone(self._event(total=0).percent)
        self.assertEqual(self._event(completed=8, total=4).percent, 100.0)


class OutcomeV2FieldTests(unittest.TestCase):
    def test_v1_step_outcome_payload_still_loads(self):
        outcome = StepOutcome.succeeded(
            run_id="run",
            generation_id="generation",
            platform="Chaturbate",
            step=StepName.COMPARE,
        )
        payload = outcome.to_dict()
        for key in (
            "resumable",
            "retryable",
            "waiting_reason",
            "summary_counts",
            "session_id",
            "new_generation_required",
        ):
            payload.pop(key, None)
        restored = StepOutcome.from_dict(payload)
        self.assertEqual(restored.session_id, "")
        self.assertFalse(restored.resumable)

    def test_waiting_step_outcome_never_calculates_success(self):
        waiting = StepOutcome._create(
            OutcomeStatus.WAITING_FOR_USER,
            run_id="run",
            generation_id="generation",
            platform="stripchat",
            step=StepName.VERIFY,
        )
        outcome = RunOutcome.calculate(
            run_id="run",
            generation_id="generation",
            platform="stripchat",
            mode="full_auto",
            steps=[waiting],
            restoration=None,
            started_at=waiting.started_at,
        )
        self.assertIs(outcome.status, OutcomeStatus.FAILED)
        self.assertEqual(outcome.error_code, "invalid_waiting_outcome")

    def test_required_steps_gate_full_auto_success(self):
        def success(step):
            return StepOutcome.succeeded(
                run_id="run",
                generation_id="generation",
                platform="stripchat",
                step=step,
            )

        required = (
            StepName.VPN_SNAPSHOT,
            StepName.LOCAL_SNAPSHOT,
            StepName.COMPARE,
            StepName.VERIFY,
        )
        partial = RunOutcome.calculate(
            run_id="run",
            generation_id="generation",
            platform="stripchat",
            mode="full_auto",
            steps=[success(StepName.VPN_SNAPSHOT)],
            restoration=None,
            started_at="2026-07-28T00:00:00+00:00",
            required_steps=required,
        )
        self.assertIs(partial.status, OutcomeStatus.INCOMPLETE)
        complete = RunOutcome.calculate(
            run_id="run",
            generation_id="generation",
            platform="stripchat",
            mode="full_auto",
            steps=[success(step) for step in required],
            restoration=None,
            started_at="2026-07-28T00:00:00+00:00",
            required_steps=required,
        )
        self.assertIs(complete.status, OutcomeStatus.SUCCEEDED)
