"""Regression tests for MyFreeCams session publication rules."""

import json
import os
import tempfile
import unittest

from mfc_verifier import MFCVerificationResult
from mfcscrape import MFCRunner
from workflow_types import OutcomeStatus, StepName, StepOutcome

URL_A = "https://www.myfreecams.com/#synthetic_a"
URL_B = "https://www.myfreecams.com/#synthetic_b"


def outcome(status, **values):
    values.setdefault("run_id", "run-synthetic")
    values.setdefault("generation_id", "gen-synthetic")
    values.setdefault("platform", "MyFreeCams")
    values.setdefault("step", StepName.VERIFY)
    factory = {
        OutcomeStatus.SUCCEEDED: StepOutcome.succeeded,
        OutcomeStatus.INCOMPLETE: StepOutcome.incomplete,
        OutcomeStatus.CANCELLED: StepOutcome.cancelled,
    }[status]
    return factory(**values)


class MFCPublicationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.runner = MFCRunner(self.base, logger=lambda message: None)
        self.runner.create_session()
        # Step 3 leaves these behind; step 4 may only clear them once it has
        # actually finished.
        self._write(self.runner.paths.vpn_list, [URL_A, URL_B])
        self._write(self.runner.paths.local_list, [URL_B])
        self._write(self.runner.paths.blocked, [URL_A])

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _write(path, lines):
        with open(path, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def _finalize(self, status, confirmed=(URL_A,), **values):
        result = MFCVerificationResult(
            outcome=outcome(status, **values), confirmed=tuple(confirmed)
        )
        return self.runner._finalize_verification(result)

    def test_complete_run_publishes_and_clears_intermediates(self):
        self._finalize(OutcomeStatus.SUCCEEDED)

        with open(self.runner.paths.verified_live, encoding="utf-8") as handle:
            self.assertEqual(handle.read().split(), [URL_A])
        self.assertFalse(os.path.exists(self.runner.paths.vpn_list))
        self.assertFalse(os.path.exists(self.runner.paths.local_list))
        self.assertTrue(os.path.exists(self.runner.get_master_json_path()))

    def test_incomplete_run_does_not_publish_the_verified_list(self):
        self._finalize(OutcomeStatus.INCOMPLETE, remaining_count=1)

        self.assertFalse(os.path.exists(self.runner.paths.verified_live))

    def test_incomplete_run_keeps_intermediates_for_a_retry(self):
        self._finalize(OutcomeStatus.INCOMPLETE, remaining_count=1)

        self.assertTrue(os.path.exists(self.runner.paths.vpn_list))
        self.assertTrue(os.path.exists(self.runner.paths.local_list))

    def test_incomplete_run_does_not_compile_the_master_list(self):
        self._finalize(OutcomeStatus.INCOMPLETE, remaining_count=1)

        self.assertFalse(os.path.exists(self.runner.get_master_json_path()))

    def test_incomplete_run_preserves_partial_work_separately(self):
        self._finalize(OutcomeStatus.INCOMPLETE, remaining_count=1)

        with open(self.runner.paths.partial_verified, encoding="utf-8") as handle:
            self.assertEqual(handle.read().split(), [URL_A])

    def test_cancelled_run_does_not_publish(self):
        self._finalize(OutcomeStatus.CANCELLED)

        self.assertFalse(os.path.exists(self.runner.paths.verified_live))
        self.assertTrue(os.path.exists(self.runner.paths.vpn_list))

    def test_incomplete_run_leaves_an_earlier_result_intact(self):
        self._write(self.runner.paths.verified_live, [URL_B])

        self._finalize(OutcomeStatus.INCOMPLETE, confirmed=(URL_A,), remaining_count=1)

        with open(self.runner.paths.verified_live, encoding="utf-8") as handle:
            self.assertEqual(handle.read().split(), [URL_B])

    def test_complete_run_clears_a_stale_partial_file(self):
        self._write(self.runner.paths.partial_verified, [URL_B])

        self._finalize(OutcomeStatus.SUCCEEDED)

        self.assertFalse(os.path.exists(self.runner.paths.partial_verified))

    def test_missing_candidate_list_reports_failure_instead_of_success(self):
        os.remove(self.runner.paths.blocked)

        returned = self.runner.verify_live_models()

        self.assertEqual(returned.status, OutcomeStatus.FAILED)
        self.assertEqual(returned.error_code, "missing_candidates")

    def test_empty_candidate_list_reports_failure(self):
        self._write(self.runner.paths.blocked, [])

        returned = self.runner.verify_live_models()

        self.assertEqual(returned.status, OutcomeStatus.FAILED)
        self.assertEqual(returned.error_code, "empty_candidates")

    def test_stop_before_verification_reports_cancellation(self):
        self.runner.request_stop()

        returned = self.runner.verify_live_models()

        self.assertEqual(returned.status, OutcomeStatus.CANCELLED)

    def test_runner_works_as_a_stop_token(self):
        self.assertFalse(self.runner.is_cancelled())

        self.runner.request_stop()

        self.assertTrue(self.runner.is_cancelled())

    def test_finalize_returns_the_step_outcome(self):
        returned = self._finalize(OutcomeStatus.INCOMPLETE, remaining_count=1)

        self.assertEqual(returned.status, OutcomeStatus.INCOMPLETE)


class MFCSnapshotVerificationTests(unittest.TestCase):
    """Step 4 asks two questions of the API instead of one of the browser:
    is she still on air, and is she still hidden from this connection?"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runner = MFCRunner(self._tmp.name, logger=lambda message: None)
        self.runner.create_session()
        with open(self.runner.paths.blocked, "w", encoding="utf-8") as handle:
            handle.write(URL_A + "\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _verify(self, on_air, viewable_here):
        self.runner.set_live_reference(on_air)
        self.runner.set_local_reference(viewable_here)
        return self.runner.verify_live_models()

    def _published(self):
        with open(self.runner.paths.verified_live, encoding="utf-8") as handle:
            return handle.read().split()

    def test_a_candidate_still_on_air_and_still_hidden_is_confirmed(self):
        outcome = self._verify(on_air={URL_A}, viewable_here={URL_B})

        self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(self._published(), [URL_A])

    def test_a_candidate_that_went_off_air_is_rejected(self):
        outcome = self._verify(on_air={URL_B}, viewable_here={URL_B})

        self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(self._published(), [])

    def test_a_candidate_this_connection_can_watch_is_rejected(self):
        # Present in both lists, so nothing was hidden from us at all.
        outcome = self._verify(on_air={URL_A}, viewable_here={URL_A})

        self.assertEqual(self._published(), [])

    def test_a_missing_snapshot_publishes_nothing(self):
        self.runner.set_live_reference({URL_A})

        outcome = self.runner.verify_live_models()

        self.assertEqual(outcome.status, OutcomeStatus.FAILED)
        self.assertFalse(os.path.exists(self.runner.paths.verified_live))

    def test_a_missing_snapshot_keeps_the_candidates_for_a_retry(self):
        self.runner.set_live_reference({URL_A})

        self.runner.verify_live_models()

        self.assertTrue(os.path.exists(self.runner.paths.blocked))


class MFCLiveReferenceTests(unittest.TestCase):
    """A liveness reference describes one moment and must not outlive its run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runner = MFCRunner(self._tmp.name, logger=lambda message: None)
        self.runner.create_session()

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_captured_reference_is_consumed_by_the_run_that_uses_it(self):
        self.runner.set_live_reference({URL_A})
        self.runner.request_stop()

        self.runner.verify_live_models()

        self.assertIsNone(self.runner._live_reference)

    def test_an_empty_capture_leaves_no_reference_behind(self):
        self.runner.set_live_reference(set())

        self.assertIsNone(self.runner._live_reference)


class MFCMasterListTests(unittest.TestCase):
    """The master list must reflect what step 4 proved, not what step 3 guessed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.runner = MFCRunner(self.base, logger=lambda message: None)

    def tearDown(self):
        self._tmp.cleanup()

    def _session(self, name, candidates=None, verified=None):
        """Build a session folder; verified=None means step 4 never ran."""
        folder = os.path.join(self.base, name)
        os.makedirs(folder, exist_ok=True)
        if candidates is not None:
            self._write(os.path.join(folder, "BLOCKED_MODELS.txt"), candidates)
        if verified is not None:
            self._write(os.path.join(folder, "mfc_verified_live.txt"), verified)
        return folder

    @staticmethod
    def _write(path, lines):
        with open(path, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def _compile(self):
        self.runner.compile_master_list()
        return {item["name"]: item for item in self.runner.get_master_list_models()}

    def test_rejected_candidates_do_not_reach_the_master_list(self):
        self._session("session 1 01.02.2026", candidates=[URL_A, URL_B], verified=[URL_A])

        entries = self._compile()

        self.assertIn(URL_A, entries)
        self.assertNotIn(URL_B, entries)

    def test_candidates_from_an_unverified_session_are_kept_as_pending(self):
        self._session("session 1 01.02.2026", candidates=[URL_B], verified=None)

        entries = self._compile()

        self.assertIn(URL_B, entries)
        self.assertTrue(entries[URL_B]["pending_verification"])

    def test_confirmed_models_are_not_marked_pending(self):
        self._session("session 1 01.02.2026", candidates=[URL_A], verified=[URL_A])

        entries = self._compile()

        self.assertFalse(entries[URL_A].get("pending_verification"))

    def test_a_confirmation_anywhere_outweighs_a_rejection_elsewhere(self):
        self._session("session 1 01.02.2026", candidates=[URL_A], verified=[])
        self._session("session 2 02.02.2026", candidates=[URL_A], verified=[URL_A])

        entries = self._compile()

        self.assertIn(URL_A, entries)
        self.assertFalse(entries[URL_A].get("pending_verification"))

    def test_verified_model_is_kept_even_without_a_candidate_file(self):
        self._session("session 1 01.02.2026", candidates=None, verified=[URL_A])

        entries = self._compile()

        self.assertIn(URL_A, entries)

    def test_manual_additions_survive_without_verification(self):
        self._session("session 1 01.02.2026", candidates=[URL_A], verified=[URL_A])
        self._write(self.runner.get_manual_list_path(), [URL_B])

        entries = self._compile()

        self.assertIn(URL_B, entries)
        self.assertTrue(entries[URL_B]["manual"])

    def test_blacklisted_models_stay_out(self):
        self._session("session 1 01.02.2026", candidates=[URL_A], verified=[URL_A])
        self._write(self.runner.get_global_blacklist_path(), [URL_A])

        entries = self._compile()

        self.assertNotIn(URL_A, entries)

    def test_country_set_by_hand_survives_a_recompile(self):
        self._session("session 1 01.02.2026", candidates=[URL_A], verified=[URL_A])
        self._compile()
        self.runner.update_master_model_country(URL_A, "Romania")

        entries = self._compile()

        self.assertEqual(entries[URL_A]["country"], "Romania")

    def test_a_shrinking_recompile_keeps_a_dated_backup_of_its_own(self):
        self._session(
            "session 1 01.02.2026", candidates=[URL_A, URL_B], verified=[URL_A, URL_B]
        )
        self._compile()

        # Re-verification finds URL_B was a false positive.
        self._write(
            os.path.join(self.base, "session 1 01.02.2026", "mfc_verified_live.txt"),
            [URL_A],
        )
        self._compile()
        # A second, non-shrinking compile must not overwrite that snapshot.
        self._compile()

        dated = [
            name
            for name in os.listdir(self.base)
            if name.startswith("MASTER_BLOCKED_DATA.json.")
            and name.endswith(".bak")
            and name != "MASTER_BLOCKED_DATA.json.bak"
        ]
        self.assertEqual(len(dated), 1)
        with open(os.path.join(self.base, dated[0]), encoding="utf-8") as handle:
            self.assertIn(URL_B, {item["name"] for item in json.load(handle)})

    def test_previous_master_is_backed_up_before_being_rewritten(self):
        self._session("session 1 01.02.2026", candidates=[URL_A, URL_B], verified=[URL_A, URL_B])
        self._compile()

        # A later session proves URL_B was a false positive.
        self._session("session 2 02.02.2026", candidates=[URL_B], verified=[])
        self._write(
            os.path.join(self.base, "session 1 01.02.2026", "mfc_verified_live.txt"),
            [URL_A],
        )
        entries = self._compile()

        self.assertNotIn(URL_B, entries)
        backup = self.runner.get_master_json_path() + ".bak"
        with open(backup, encoding="utf-8") as handle:
            previous = {item["name"] for item in json.load(handle)}
        self.assertIn(URL_B, previous)


if __name__ == "__main__":
    unittest.main()
