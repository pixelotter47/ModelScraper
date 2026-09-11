"""Generic checkpointed verifier: epochs, budgets, and fail-closed resume."""

import tempfile
import unittest
from pathlib import Path

from workflow_store import GenericRunStore, StepName
from workflow_types import (
    OutcomeStatus,
    VerificationRecord,
    VerificationVerdict,
    utc_now_iso,
)
from workflow_verifier import GenericCheckpointedVerifier


CONFIG = {
    "digest_sha256": "d" * 64,
    "canonicalizer_version": "sc-url-1",
    "classifier_version": "sc-page-1",
    "api_contract_version": "sc-api-1",
}

TERMINAL_NONTARGET = frozenset(
    {"account_disabled_or_deleted", "room_offline", "not_found", "login_required"}
)


def canonicalize(value: str) -> str:
    value = str(value).strip()
    if not value.startswith("https://example.com/"):
        raise ValueError("invalid_url")
    return value if value.endswith("/") else value + "/"


def identity_key(value: str) -> str:
    return value.lower()


class ScriptedProvider:
    """Returns scripted verdicts per URL; records every navigation."""

    def __init__(self, script):
        self.script = dict(script)
        self.calls = []

    def __call__(self, url, attempt):
        self.calls.append((url, attempt))
        item = self.script[url]
        value = item.pop(0) if isinstance(item, list) and item else item
        if isinstance(item, list) and not item:
            self.script[url] = value
        verdict, reason = value
        return VerificationRecord(
            original_url=url,
            verdict=verdict,
            reason_code=reason,
            attempt=attempt,
            timestamp=utc_now_iso(),
            observed_url=url,
        )


class Harness:
    def __init__(self, root, candidates):
        session = Path(root, "session 1 28.07.2026")
        session.mkdir(parents=True, exist_ok=True)
        self.store = GenericRunStore(
            session, platform_key="stripchat", config=dict(CONFIG)
        )
        self.context = self.store.create_run(mode="manual", source="test")
        record = self.store.artifact_lines(
            self.context,
            StepName.COMPARE,
            "candidates.txt",
            candidates,
            logical_name="step3.candidates",
            schema="canonical_url_set/v1",
        )
        from workflow_types import StepOutcome

        self.store.record_step_attempt(
            self.context,
            StepOutcome.succeeded(
                run_id=self.context.run_id,
                generation_id=self.context.generation_id,
                platform=self.context.platform_key,
                step=StepName.COMPARE,
                session_id=self.context.session_id,
                artifacts=(record,),
            ),
        )

    def verifier(self, provider, **overrides):
        values = dict(
            identity_key=identity_key,
            canonicalize_url=canonicalize,
            terminal_nontarget_reasons=TERMINAL_NONTARGET,
            max_retry_epochs=3,
            attempt_budget_per_epoch=2,
        )
        values.update(overrides)
        return GenericCheckpointedVerifier(
            self.store, self.context, provider, **values
        )


BLOCKED = (VerificationVerdict.BLOCKED, "visible_hidden_notice")
ACCESSIBLE = (VerificationVerdict.ACCESSIBLE, "player_present")
TIMEOUT = (VerificationVerdict.UNKNOWN, "navigation_timeout")
DISABLED = (VerificationVerdict.UNKNOWN, "account_disabled_or_deleted")


class VerifierLifecycleTests(unittest.TestCase):
    def test_terminal_verdicts_complete_without_finalizer(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(
                root,
                [
                    "https://example.com/Blocked-One/",
                    "https://example.com/Open-One/",
                    "https://example.com/Gone-One/",
                ],
            )
            provider = ScriptedProvider(
                {
                    "https://example.com/Blocked-One/": BLOCKED,
                    "https://example.com/Open-One/": ACCESSIBLE,
                    "https://example.com/Gone-One/": DISABLED,
                }
            )
            outcome = harness.verifier(provider).verify("step3.candidates")
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(outcome.output_count, 1)
            self.assertEqual(outcome.summary_counts["unknown_terminal"], 1)
            self.assertTrue(outcome.warnings)
            checkpoint = harness.store.load_checkpoint(harness.context)
            self.assertEqual(checkpoint["summary"]["blocked"], 1)

    def test_transient_unknowns_block_completion(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, ["https://example.com/Flaky-One/"])
            provider = ScriptedProvider(
                {"https://example.com/Flaky-One/": TIMEOUT}
            )
            outcome = harness.verifier(provider).verify("step3.candidates")
            self.assertIs(outcome.status, OutcomeStatus.INCOMPLETE)
            self.assertEqual(
                outcome.error_code, "transient_unknowns_remaining"
            )
            self.assertTrue(outcome.resumable)
            self.assertFalse(outcome.new_generation_required)
            self.assertEqual(len(provider.calls), 2, "epoch budget is 2")

    def test_resume_advances_epoch_and_skips_terminal_records(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(
                root,
                [
                    "https://example.com/Blocked-One/",
                    "https://example.com/Flaky-One/",
                ],
            )
            provider = ScriptedProvider(
                {
                    "https://example.com/Blocked-One/": BLOCKED,
                    "https://example.com/Flaky-One/": TIMEOUT,
                }
            )
            first = harness.verifier(provider).verify("step3.candidates")
            self.assertIs(first.status, OutcomeStatus.INCOMPLETE)
            resumed_provider = ScriptedProvider(
                {
                    "https://example.com/Blocked-One/": BLOCKED,
                    "https://example.com/Flaky-One/": ACCESSIBLE,
                }
            )
            second = harness.verifier(resumed_provider).verify(
                "step3.candidates"
            )
            self.assertIs(second.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(
                [url for url, _ in resumed_provider.calls],
                ["https://example.com/Flaky-One/"],
                "terminal records are not re-navigated on resume",
            )
            checkpoint = harness.store.load_checkpoint(harness.context)
            self.assertEqual(checkpoint["retry_epoch"], 1)

    def test_epoch_ceiling_requires_a_new_generation(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, ["https://example.com/Flaky-One/"])
            provider = ScriptedProvider(
                {"https://example.com/Flaky-One/": TIMEOUT}
            )
            outcomes = []
            for _ in range(3):
                outcomes.append(
                    harness.verifier(provider).verify("step3.candidates")
                )
            final = harness.verifier(provider).verify("step3.candidates")
            self.assertIs(final.status, OutcomeStatus.INCOMPLETE)
            self.assertEqual(final.error_code, "retry_epochs_exhausted")
            self.assertTrue(final.new_generation_required)
            self.assertFalse(final.resumable)

    def test_stop_checkpoints_and_reports_cancelled(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(
                root,
                [
                    "https://example.com/One/",
                    "https://example.com/Two/",
                ],
            )

            class StopAfterFirst:
                def __init__(self):
                    self.count = 0

                def is_cancelled(self):
                    self.count += 1
                    return self.count > 1

            provider = ScriptedProvider(
                {
                    "https://example.com/One/": BLOCKED,
                    "https://example.com/Two/": ACCESSIBLE,
                }
            )
            outcome = harness.verifier(
                provider, stop_token=StopAfterFirst()
            ).verify("step3.candidates")
            self.assertIs(outcome.status, OutcomeStatus.CANCELLED)
            self.assertTrue(outcome.resumable)
            checkpoint = harness.store.load_checkpoint(harness.context)
            self.assertEqual(checkpoint["summary"]["blocked"], 1)
            self.assertEqual(checkpoint["summary"]["unattempted"], 1)

    def test_candidate_change_after_checkpoint_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, ["https://example.com/Flaky-One/"])
            provider = ScriptedProvider(
                {"https://example.com/Flaky-One/": TIMEOUT}
            )
            harness.verifier(provider).verify("step3.candidates")
            candidate_path = Path(
                harness.context.artifacts_dir, "step3", "candidates.txt"
            )
            candidate_path.write_text(
                "https://example.com/Other-One/\n", encoding="utf-8"
            )
            outcome = harness.verifier(provider).verify("step3.candidates")
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "artifact_hash_mismatch")

    def test_identity_collision_in_candidates_fails(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(
                root,
                [
                    "https://example.com/Model-A/",
                    "https://example.com/MODEL-a/",
                ],
            )
            provider = ScriptedProvider({})
            outcome = harness.verifier(provider).verify("step3.candidates")
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "invalid_candidate_url")

    def test_provider_exception_becomes_structured_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, ["https://example.com/Crash-One/"])

            def crashing_provider(url, attempt):
                raise RuntimeError("boom")

            outcome = harness.verifier(crashing_provider).verify(
                "step3.candidates"
            )
            self.assertIs(outcome.status, OutcomeStatus.INCOMPLETE)
            checkpoint = harness.store.load_checkpoint(harness.context)
            record = next(iter(checkpoint["records"].values()))
            self.assertEqual(record["reason_code"], "selector_error")


if __name__ == "__main__":
    unittest.main()
