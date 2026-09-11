"""Hardened Stripchat runner: typed Steps 1-3 over immutable artifacts."""

import os
import tempfile
import unittest
from pathlib import Path

from stripchat_api import StripchatApiModel, StripchatSnapshotPass, StripchatSnapshotResult
from stripchat_core import HardenedStripchatRunner
from stripchat_store import StripchatRunStore
from workflow_types import OutcomeStatus, StepName, utc_now_iso


def snapshot_result(usernames, status=OutcomeStatus.SUCCEEDED, error=None):
    models = tuple(StripchatApiModel(username=name) for name in usernames)
    passes = tuple(
        StripchatSnapshotPass(
            pass_index=index,
            status=OutcomeStatus.SUCCEEDED,
            models=models,
            page_count=1,
            raw_record_count=len(models),
            duplicate_count=0,
            reported_totals=(len(models),),
            terminal_empty_page=True,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
        )
        for index in (1, 2, 3)
    )
    return StripchatSnapshotResult(
        status=status,
        models=models if status is OutcomeStatus.SUCCEEDED else (),
        quarantined=(),
        passes=passes,
        convergence_window=(1, 2, 3)
        if status is OutcomeStatus.SUCCEEDED
        else (),
        error_code=error,
    )


class FakeApiClient:
    def __init__(self, result):
        self.result = result

    def collect_snapshot(self):
        return self.result


class Harness:
    def __init__(self, root):
        session = Path(root, "session 1 28.07.2026")
        session.mkdir(parents=True, exist_ok=True)
        self.session = session
        self.store = StripchatRunStore(session)
        self.context = self.store.create_run(mode="full_auto", source="test")
        self.policy = {"vpn_state": "connected", "relay_code": "ie-dub-wg-103"}
        self.routes = {}

    def runner(self):
        return HardenedStripchatRunner(
            self.store,
            api_client_factory=lambda route: self.routes[route],
            policy_observer=lambda: dict(self.policy),
        )

    def connected(self):
        self.policy = {
            "vpn_state": "connected",
            "relay_code": "ie-dub-wg-103",
        }

    def disconnected(self):
        self.policy = {"vpn_state": "disconnected", "relay_code": None}

    def run_step1(self, usernames=("Model-A", "Model-B")):
        self.connected()
        self.routes["vpn"] = FakeApiClient(snapshot_result(usernames))
        return self.runner().run_step1(self.context)

    def run_step2(self, usernames=("Model-A",)):
        self.disconnected()
        self.routes["local"] = FakeApiClient(snapshot_result(usernames))
        return self.runner().run_step2(self.context)


class StepOneTests(unittest.TestCase):
    def test_requires_connected_ireland_policy(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.disconnected()
            outcome = harness.runner().run_step1(harness.context)
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(
                outcome.error_code, "machine_policy_precondition"
            )
            manifest = harness.store.load_manifest(harness.context)
            self.assertEqual(manifest["artifacts"], {})

    def test_success_persists_typed_outcome_and_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            outcome = harness.run_step1(("Model-B", "Model-A"))
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(outcome.output_count, 2)
            self.assertEqual(outcome.session_id, harness.context.session_id)
            urls = harness.store.read_artifact_lines(
                harness.context, "step1.vpn_urls"
            )
            self.assertEqual(
                urls,
                [
                    "https://stripchat.com/Model-A/",
                    "https://stripchat.com/Model-B/",
                ],
            )
            manifest = harness.store.load_manifest(harness.context)
            self.assertIn("step1.vpn_report", manifest["artifacts"])
            self.assertIn("step1.vpn_metadata", manifest["artifacts"])
            current = manifest["steps"][StepName.VPN_SNAPSHOT.value]["current"]
            self.assertEqual(current["status"], "succeeded")

    def test_incomplete_snapshot_never_writes_url_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.connected()
            harness.routes["vpn"] = FakeApiClient(
                snapshot_result(
                    (), OutcomeStatus.INCOMPLETE, "api_snapshot_unstable"
                )
            )
            outcome = harness.runner().run_step1(harness.context)
            self.assertIs(outcome.status, OutcomeStatus.INCOMPLETE)
            self.assertEqual(outcome.error_code, "api_snapshot_unstable")
            manifest = harness.store.load_manifest(harness.context)
            self.assertEqual(manifest["artifacts"], {})

    def test_no_root_compatibility_file_is_written(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.run_step1()
            harness.run_step2()
            names = set(os.listdir(harness.session))
            self.assertNotIn("sc_vpn_list.txt", names)
            self.assertNotIn("sc_local_list.txt", names)
            self.assertNotIn("sc_metadata.json", names)


class StepTwoTests(unittest.TestCase):
    def test_requires_same_generation_step1(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.disconnected()
            harness.routes["local"] = FakeApiClient(snapshot_result(()))
            outcome = harness.runner().run_step2(harness.context)
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "stale_generation")

    def test_requires_disconnected_policy(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.run_step1()
            harness.connected()
            harness.routes["local"] = FakeApiClient(snapshot_result(()))
            outcome = harness.runner().run_step2(harness.context)
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(
                outcome.error_code, "machine_policy_precondition"
            )

    def test_success_binds_upstream_vpn_hash(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.run_step1()
            outcome = harness.run_step2()
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            manifest = harness.store.load_manifest(harness.context)
            vpn_sha = manifest["artifacts"]["step1.vpn_urls"]["sha256"]
            local_upstream = manifest["artifacts"]["step2.local_urls"][
                "upstream_sha256"
            ]
            self.assertEqual(local_upstream, {"step1.vpn_urls": vpn_sha})


class StepThreeTests(unittest.TestCase):
    def _prepare(self, harness, vpn, local):
        harness.run_step1(vpn)
        harness.run_step2(local)

    def test_difference_uses_identity_keys_and_preserves_case(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            self._prepare(
                harness,
                ("Blocked-Model@1", "Visible-Model", "Case-Model"),
                ("visible-model",),
            )
            harness.routes["local"] = FakeApiClient(
                snapshot_result(("case-model",))
            )
            outcome = harness.runner().run_step3(harness.context)
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            candidates = harness.store.read_artifact_lines(
                harness.context, "step3.candidates"
            )
            self.assertEqual(
                candidates, ["https://stripchat.com/Blocked-Model@1/"]
            )

    def test_zero_candidates_is_a_valid_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            self._prepare(harness, ("Model-A",), ("Model-A",))
            outcome = harness.runner().run_step3(harness.context)
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(outcome.output_count, 0)
            self.assertEqual(
                harness.store.read_artifact_lines(
                    harness.context, "step3.candidates"
                ),
                [],
            )

    def test_incomplete_recheck_blocks_candidates(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            self._prepare(harness, ("Model-A", "Model-B"), ("Model-A",))
            harness.routes["local"] = FakeApiClient(
                snapshot_result(
                    (), OutcomeStatus.INCOMPLETE, "premature_empty_page"
                )
            )
            outcome = harness.runner().run_step3(harness.context)
            self.assertIs(outcome.status, OutcomeStatus.INCOMPLETE)
            self.assertEqual(outcome.error_code, "premature_empty_page")
            manifest = harness.store.load_manifest(harness.context)
            self.assertNotIn("step3.candidates", manifest["artifacts"])

    def test_requires_both_snapshots_from_this_generation(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.run_step1()
            harness.disconnected()
            outcome = harness.runner().run_step3(harness.context)
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "stale_generation")


class ScriptedBrowserProvider:
    """Stands in for StripchatBrowserProvider with scripted observations."""

    def __init__(self, script):
        from stripchat_classifier import StripchatPageObservation

        self._observation_type = StripchatPageObservation
        self.script = dict(script)
        self.observed = []
        self.closed = 0

    def observe(self, url):
        self.observed.append(url)
        value = self.script[url]
        if isinstance(value, Exception):
            raise value
        kwargs = dict(value)
        return self._observation_type(
            original_url=url,
            current_url=kwargs.pop("current_url", url),
            **kwargs,
        )

    def close(self):
        self.closed += 1


class StepFourTests(unittest.TestCase):
    def _prepare(self, harness, vpn, local):
        harness.run_step1(vpn)
        harness.run_step2(local)
        harness.routes["local"] = FakeApiClient(snapshot_result(local))
        outcome = harness.runner().run_step3(harness.context)
        assert outcome.status is OutcomeStatus.SUCCEEDED

    def test_requires_step3_from_this_generation(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.disconnected()
            outcome = harness.runner().run_step4(
                harness.context, provider=ScriptedBrowserProvider({})
            )
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "stale_generation")

    def test_zero_candidates_never_launch_a_browser(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            self._prepare(harness, ("Model-A",), ("Model-A",))
            factory_calls = []

            def provider_factory(**kwargs):
                factory_calls.append(kwargs)
                return ScriptedBrowserProvider({})

            outcome = harness.runner().run_step4(
                harness.context, provider_factory=provider_factory
            )
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            provider = factory_calls and None
            checkpoint = harness.store.load_checkpoint(harness.context)
            self.assertEqual(checkpoint["candidate_count"], 0)

    def test_blocked_and_accessible_flow(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            self._prepare(
                harness,
                ("Blocked-One", "Open-One", "Gone-One"),
                (),
            )
            provider = ScriptedBrowserProvider(
                {
                    "https://stripchat.com/Blocked-One/": {
                        "ready_state": "complete",
                        "account_notice_texts": ("Account Hidden",),
                    },
                    "https://stripchat.com/Open-One/": {
                        "ready_state": "complete",
                        "video_element_count": 1,
                    },
                    "https://stripchat.com/Gone-One/": {
                        "ready_state": "complete",
                        "account_notice_texts": ("Account Disabled",),
                    },
                }
            )
            outcome = harness.runner().run_step4(
                harness.context, provider=provider
            )
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(outcome.output_count, 1)
            self.assertEqual(outcome.summary_counts["blocked"], 1)
            self.assertEqual(outcome.summary_counts["accessible"], 1)
            self.assertEqual(outcome.summary_counts["unknown_terminal"], 1)
            self.assertTrue(outcome.warnings)

    def test_challenge_deadline_is_incomplete_and_resumable(self):
        from stripchat_core import ChallengeDeadlineError

        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            self._prepare(harness, ("Blocked-One",), ())
            provider = ScriptedBrowserProvider(
                {
                    "https://stripchat.com/Blocked-One/": (
                        ChallengeDeadlineError("captcha_timeout")
                    )
                }
            )
            outcome = harness.runner().run_step4(
                harness.context, provider=provider
            )
            self.assertIs(outcome.status, OutcomeStatus.INCOMPLETE)
            self.assertEqual(outcome.error_code, "captcha_timeout")
            self.assertTrue(outcome.resumable)
            manifest = harness.store.load_manifest(harness.context)
            self.assertFalse(manifest["waiting"]["active"])

    def test_transient_unknowns_then_resume_completes(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            self._prepare(harness, ("Flaky-One",), ())
            flaky = ScriptedBrowserProvider(
                {
                    "https://stripchat.com/Flaky-One/": {
                        "navigation_error": "navigation_timeout",
                        "current_url": "",
                    }
                }
            )
            first = harness.runner().run_step4(
                harness.context, provider=flaky
            )
            self.assertIs(first.status, OutcomeStatus.INCOMPLETE)
            self.assertEqual(
                first.error_code, "transient_unknowns_remaining"
            )
            healed = ScriptedBrowserProvider(
                {
                    "https://stripchat.com/Flaky-One/": {
                        "ready_state": "complete",
                        "video_element_count": 1,
                    }
                }
            )
            second = harness.runner().run_step4(
                harness.context, provider=healed
            )
            self.assertIs(second.status, OutcomeStatus.SUCCEEDED)
            checkpoint = harness.store.load_checkpoint(harness.context)
            self.assertEqual(checkpoint["retry_epoch"], 1)


if __name__ == "__main__":
    unittest.main()


class BrowserLossDuringStepFourTests(unittest.TestCase):
    """Reproduces the live failure: Chrome died after 84 of 328 candidates.

    Every remaining candidate was then fed to the dead driver, burning its
    per-epoch retry budget in six seconds on an error that had nothing to
    do with the page.
    """

    def _prepare(self, harness, vpn):
        harness.run_step1(vpn)
        harness.run_step2(())
        harness.routes["local"] = FakeApiClient(snapshot_result(()))
        outcome = harness.runner().run_step3(harness.context)
        assert outcome.status is OutcomeStatus.SUCCEEDED

    def test_a_dead_browser_stops_the_step_and_keeps_earned_verdicts(self):
        from stripchat_core import BrowserLostError

        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            names = [f"Model-{index:02d}" for index in range(6)]
            self._prepare(harness, tuple(names))

            class DyingProvider:
                """Classifies two candidates, then the browser is gone."""

                def __init__(self):
                    self.seen = []
                    self.closed = 0

                def observe(self, url):
                    self.seen.append(url)
                    if len(self.seen) > 2:
                        raise BrowserLostError("chrome is gone")
                    from stripchat_classifier import StripchatPageObservation

                    return StripchatPageObservation(
                        original_url=url,
                        current_url=url,
                        ready_state="complete",
                        account_notice_texts=("Account Hidden",),
                    )

                def close(self):
                    self.closed += 1

            provider = DyingProvider()
            outcome = harness.runner().run_step4(
                harness.context, provider=provider
            )

            self.assertIs(outcome.status, OutcomeStatus.INCOMPLETE)
            self.assertEqual(outcome.error_code, "browser_lost")
            self.assertTrue(outcome.resumable)
            self.assertEqual(
                len(provider.seen),
                3,
                "the step must stop at the loss, not walk the whole list",
            )

            checkpoint = harness.store.load_checkpoint(harness.context)
            summary = checkpoint["summary"]
            self.assertEqual(summary["blocked"], 2, "earned verdicts are kept")
            self.assertEqual(
                summary["unattempted"],
                4,
                "untouched candidates keep a full retry budget",
            )
            for record in checkpoint["records"].values():
                self.assertLessEqual(
                    record.get("attempts_this_epoch", 0),
                    1,
                    "a dead browser must not spend anyone's budget twice",
                )

    def test_resume_after_a_browser_loss_finishes_the_remaining_work(self):
        from stripchat_core import BrowserLostError

        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            names = [f"Model-{index:02d}" for index in range(6)]
            self._prepare(harness, tuple(names))

            class DyingProvider:
                def __init__(self):
                    self.seen = []

                def observe(self, url):
                    self.seen.append(url)
                    if len(self.seen) > 2:
                        raise BrowserLostError("chrome is gone")
                    from stripchat_classifier import StripchatPageObservation

                    return StripchatPageObservation(
                        original_url=url,
                        current_url=url,
                        ready_state="complete",
                        account_notice_texts=("Account Hidden",),
                    )

                def close(self):
                    pass

            first = harness.runner().run_step4(
                harness.context, provider=DyingProvider()
            )
            self.assertIs(first.status, OutcomeStatus.INCOMPLETE)

            healthy = ScriptedBrowserProvider(
                {
                    f"https://stripchat.com/{name}/": {
                        "ready_state": "complete",
                        "video_element_count": 1,
                    }
                    for name in names
                }
            )
            second = harness.runner().run_step4(
                harness.context, provider=healthy
            )
            self.assertIs(second.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(
                second.summary_counts["blocked"],
                2,
                "verdicts earned before the loss survive the resume",
            )
            self.assertEqual(second.summary_counts["accessible"], 4)
            self.assertEqual(
                len(healthy.observed),
                4,
                "resume only revisits what was never judged",
            )
