"""Adapter registry and strict step-outcome identity validation."""

import unittest
import uuid

from platform_contracts import (
    PlatformArtifactNames,
    PlatformCapabilities,
    PlatformSpec,
)
from platform_workflow import (
    AdapterContractError,
    PlatformAdapterRegistry,
    PreparedWorkflowRun,
    RunSelection,
    validate_step_outcome,
)
from workflow_store import RunContext
from workflow_types import StepName, StepOutcome


def spec_for(key):
    return PlatformSpec(
        key=key,
        display_name=key.capitalize(),
        session_root_name=f"{key} sessions",
        url_canonicalizer=lambda value: value,
        canonicalizer_version="1",
        classifier_version="1",
        api_contract_version="1",
        artifacts=PlatformArtifactNames(
            vpn="vpn.txt",
            local="local.txt",
            candidates="candidates.txt",
            metadata="metadata.json",
            debug="debug.txt",
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


def prepared_for(spec):
    context = RunContext(
        session_path=r"C:\sessions\session 1",
        session_id="session 1",
        run_id=str(uuid.uuid4()),
        generation_id=str(uuid.uuid4()),
        platform_key=spec.key,
        mode="full_auto",
    )
    return PreparedWorkflowRun(
        context=context, resumed=False, next_step=StepName.VPN_SNAPSHOT
    )


class ValidateStepOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.spec = spec_for("stripchat")
        self.prepared = prepared_for(self.spec)

    def outcome(self, **overrides):
        values = {
            "run_id": self.prepared.context.run_id,
            "generation_id": self.prepared.context.generation_id,
            "platform": "stripchat",
            "step": StepName.VPN_SNAPSHOT,
            "session_id": "session 1",
        }
        values.update(overrides)
        return StepOutcome.succeeded(**values)

    def test_exact_identity_passes(self):
        outcome = self.outcome()
        self.assertIs(
            validate_step_outcome(
                outcome,
                prepared=self.prepared,
                spec=self.spec,
                expected_step=StepName.VPN_SNAPSHOT,
            ),
            outcome,
        )

    def test_none_is_an_invalid_step_outcome(self):
        with self.assertRaises(AdapterContractError) as caught:
            validate_step_outcome(
                None,
                prepared=self.prepared,
                spec=self.spec,
                expected_step=StepName.VPN_SNAPSHOT,
            )
        self.assertEqual(caught.exception.code, "invalid_step_outcome")

    def test_every_identity_mismatch_is_rejected(self):
        cases = {
            "step_platform_mismatch": {"platform": "Stripchat"},
            "step_run_mismatch": {"run_id": str(uuid.uuid4())},
            "step_generation_mismatch": {"generation_id": str(uuid.uuid4())},
            "step_session_mismatch": {"session_id": "other session"},
            "step_name_mismatch": {"step": StepName.COMPARE},
        }
        for code, overrides in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(AdapterContractError) as caught:
                    validate_step_outcome(
                        self.outcome(**overrides),
                        prepared=self.prepared,
                        spec=self.spec,
                        expected_step=StepName.VPN_SNAPSHOT,
                    )
                self.assertEqual(caught.exception.code, code)


class RegistryTests(unittest.TestCase):
    def test_resolve_by_key_and_display_name(self):
        registry = PlatformAdapterRegistry()

        class FakeAdapter:
            spec = spec_for("stripchat")

        adapter = FakeAdapter()
        registry.register(adapter, display_name="Stripchat")
        self.assertIs(registry.resolve("stripchat"), adapter)
        self.assertIs(registry.resolve("Stripchat"), adapter)

    def test_unknown_platform_is_rejected(self):
        registry = PlatformAdapterRegistry()
        with self.assertRaises(AdapterContractError) as caught:
            registry.resolve("nosuch")
        self.assertEqual(caught.exception.code, "unsupported_platform")

    def test_duplicate_registration_is_rejected(self):
        registry = PlatformAdapterRegistry()

        class FakeAdapter:
            spec = spec_for("stripchat")

        registry.register(FakeAdapter())
        with self.assertRaises(AdapterContractError):
            registry.register(FakeAdapter())

    def test_run_selection_values(self):
        self.assertEqual(
            {item.value for item in RunSelection}, {"auto", "resume", "new"}
        )


if __name__ == "__main__":
    unittest.main()
