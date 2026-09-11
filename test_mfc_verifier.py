"""Regression tests for MyFreeCams live verification verdict handling."""

import unittest

from mfc_verifier import MFCLiveVerifier
from test_support import FakeStopToken
from workflow_types import (
    OutcomeStatus,
    StepName,
    VerificationRecord,
    VerificationVerdict,
    utc_now_iso,
)

URL_A = "https://www.myfreecams.com/#synthetic_a"
URL_B = "https://www.myfreecams.com/#synthetic_b"


def verdict(url, value, reason, attempt=1):
    return VerificationRecord(
        original_url=url,
        verdict=value,
        reason_code=reason,
        attempt=attempt,
        timestamp=utc_now_iso(),
        observed_url=url,
        run_id="synthetic",
    )


def per_url(judge):
    """Adapt a per-URL fake to the batch interface the verifier really calls."""

    def provider(urls, attempt):
        return {url: judge(url, attempt) for url in urls}

    return provider


def build(provider, **kwargs):
    kwargs.setdefault("run_id", "run-synthetic")
    kwargs.setdefault("generation_id", "gen-synthetic")
    return MFCLiveVerifier(provider, **kwargs)


class MFCVerificationBreakdownTests(unittest.TestCase):
    """A run that rejects everything has to be able to say on what grounds."""

    def test_breakdown_counts_every_candidate_by_reason(self):
        reasons = {
            URL_A: (VerificationVerdict.BLOCKED, "bounced_while_live"),
            URL_B: (VerificationVerdict.ACCESSIBLE, "handle_mismatch"),
        }
        provider = per_url(lambda url, attempt: verdict(url, *reasons[url]))

        result = build(provider).verify([URL_A, URL_B])

        self.assertEqual(
            result.breakdown,
            {"bounced_while_live": 1, "handle_mismatch": 1},
        )

    def test_breakdown_groups_candidates_that_share_a_reason(self):
        provider = per_url(
            lambda url, attempt: verdict(
                url, VerificationVerdict.ACCESSIBLE, "handle_mismatch"
            )
        )

        result = build(provider).verify([URL_A, URL_B])

        self.assertEqual(result.breakdown, {"handle_mismatch": 2})


class MFCLiveVerifierTests(unittest.TestCase):
    def test_rate_limited_candidate_is_retried_instead_of_dropped(self):
        seen = []

        def provider(url, attempt):
            seen.append(attempt)
            if attempt == 1:
                return verdict(url, VerificationVerdict.UNKNOWN, "rate_limited", attempt)
            return verdict(url, VerificationVerdict.BLOCKED, "live_video", attempt)

        result = build(per_url(provider)).verify([URL_A])

        self.assertEqual(seen, [1, 2])
        self.assertEqual(result.confirmed, (URL_A,))
        self.assertEqual(result.outcome.status, OutcomeStatus.SUCCEEDED)

    def test_persistent_rate_limit_leaves_the_step_incomplete(self):
        def provider(url, attempt):
            return verdict(url, VerificationVerdict.UNKNOWN, "rate_limited", attempt)

        result = build(per_url(provider), max_attempts=2).verify([URL_A])

        self.assertEqual(result.outcome.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.outcome.error_code, "verification_unknown")
        self.assertEqual(result.outcome.remaining_count, 1)
        self.assertEqual(result.confirmed, ())

    def test_offline_candidate_is_excluded_without_blocking_completion(self):
        def provider(url, attempt):
            if url == URL_A:
                return verdict(url, VerificationVerdict.BLOCKED, "live_video", attempt)
            return verdict(url, VerificationVerdict.ACCESSIBLE, "no_video", attempt)

        result = build(per_url(provider)).verify([URL_A, URL_B])

        self.assertEqual(result.outcome.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(result.confirmed, (URL_A,))
        self.assertEqual(result.outcome.input_count, 2)
        self.assertEqual(result.outcome.output_count, 1)

    def test_deterministic_negative_is_not_retried(self):
        seen = []

        def provider(url, attempt):
            seen.append(attempt)
            return verdict(url, VerificationVerdict.ACCESSIBLE, "no_video", attempt)

        build(per_url(provider), max_attempts=3).verify([URL_A])

        self.assertEqual(seen, [1])

    def test_banned_notice_counts_as_confirmed(self):
        def provider(url, attempt):
            return verdict(url, VerificationVerdict.BLOCKED, "banned_notice", attempt)

        result = build(per_url(provider)).verify([URL_A])

        self.assertEqual(result.confirmed, (URL_A,))
        self.assertEqual(result.outcome.status, OutcomeStatus.SUCCEEDED)

    def test_provider_crash_is_transient_and_retried(self):
        seen = []

        def provider(url, attempt):
            seen.append(attempt)
            if attempt == 1:
                raise RuntimeError("tab closed unexpectedly")
            return verdict(url, VerificationVerdict.BLOCKED, "live_video", attempt)

        result = build(per_url(provider)).verify([URL_A])

        self.assertEqual(seen, [1, 2])
        self.assertEqual(result.outcome.status, OutcomeStatus.SUCCEEDED)

    def test_stop_request_cancels_without_confirming_the_rest(self):
        stop = FakeStopToken()

        def provider(url, attempt):
            stop.cancel()
            return verdict(url, VerificationVerdict.BLOCKED, "live_video", attempt)

        result = build(per_url(provider), stop_token=stop, batch_size=1).verify([URL_A, URL_B])

        self.assertEqual(result.outcome.status, OutcomeStatus.CANCELLED)
        self.assertEqual(result.outcome.processed_count, 1)
        self.assertEqual(result.outcome.remaining_count, 1)

    def test_unresolved_reasons_are_reported(self):
        def provider(url, attempt):
            return verdict(url, VerificationVerdict.UNKNOWN, "rate_limited", attempt)

        result = build(per_url(provider), max_attempts=1).verify([URL_A, URL_B])

        self.assertEqual(result.unresolved, {"rate_limited": 2})

    def test_outcome_identifies_the_myfreecams_verify_step(self):
        def provider(url, attempt):
            return verdict(url, VerificationVerdict.BLOCKED, "live_video", attempt)

        result = build(per_url(provider)).verify([URL_A])

        self.assertEqual(result.outcome.step, StepName.VERIFY)
        self.assertEqual(result.outcome.platform, "MyFreeCams")

    def test_duplicate_candidates_are_verified_once(self):
        seen = []

        def provider(url, attempt):
            seen.append(url)
            return verdict(url, VerificationVerdict.BLOCKED, "live_video", attempt)

        result = build(per_url(provider)).verify([URL_A, URL_A])

        self.assertEqual(seen, [URL_A])
        self.assertEqual(result.confirmed, (URL_A,))

    def test_candidates_are_offered_to_the_provider_in_batches(self):
        batches = []

        def provider(urls, attempt):
            batches.append(list(urls))
            return {
                url: verdict(url, VerificationVerdict.BLOCKED, "live_video", attempt)
                for url in urls
            }

        build(provider, batch_size=2).verify([URL_A, URL_B, URL_A + "_c"])

        self.assertEqual([len(item) for item in batches], [2, 1])

    def test_candidate_missing_from_the_batch_response_is_not_dropped(self):
        def provider(urls, attempt):
            # The browser layer answered for one tab and lost the other.
            return {
                urls[0]: verdict(
                    urls[0], VerificationVerdict.BLOCKED, "live_video", attempt
                )
            }

        result = build(provider, max_attempts=1).verify([URL_A, URL_B])

        self.assertEqual(result.outcome.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.unresolved, {"provider_error": 1})
        self.assertEqual(result.confirmed, (URL_A,))

    def test_object_without_a_cancel_check_does_not_cancel_the_run(self):
        class PlainToken:
            """A stop token that forgot to implement the interface."""

        def provider(url, attempt):
            return verdict(url, VerificationVerdict.BLOCKED, "live_video", attempt)

        result = build(per_url(provider), stop_token=PlainToken()).verify([URL_A])

        self.assertEqual(result.outcome.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(result.confirmed, (URL_A,))

    def test_whole_batch_failure_is_transient(self):
        calls = []

        def provider(urls, attempt):
            calls.append(attempt)
            if attempt == 1:
                raise RuntimeError("browser window disappeared")
            return {
                url: verdict(url, VerificationVerdict.BLOCKED, "live_video", attempt)
                for url in urls
            }

        result = build(provider).verify([URL_A, URL_B])

        self.assertEqual(calls, [1, 2])
        self.assertEqual(result.outcome.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(len(result.confirmed), 2)


if __name__ == "__main__":
    unittest.main()


class MFCProgressStatusMigrationTests(unittest.TestCase):
    def test_progress_events_are_running_and_messages_unchanged(self):
        from workflow_types import ProgressStatus

        events = []

        def provider(urls, attempt):
            return {
                url: VerificationRecord(
                    original_url=url,
                    verdict=VerificationVerdict.ACCESSIBLE,
                    reason_code="model_listed",
                    attempt=attempt,
                    timestamp="2026-07-28T00:00:00+00:00",
                    observed_url=url,
                )
                for url in urls
            }

        verifier = MFCLiveVerifier(
            provider,
            run_id="run",
            generation_id="generation",
            progress_callback=events.append,
        )
        result = verifier.verify(["model_a", "model_b"])
        self.assertTrue(result.outcome.status.value in ("succeeded", "incomplete"))
        self.assertTrue(events)
        for event in events:
            self.assertIs(event.status, ProgressStatus.RUNNING)
            self.assertIn("candidates.", event.message)
