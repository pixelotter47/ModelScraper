"""Stripchat API snapshot subsystem: schema, pagination, convergence."""

import json
import unittest

from stripchat_api import (
    StripchatApiClient,
    StripchatApiError,
    validate_envelope,
)
from workflow_types import OutcomeStatus


def body(models, total, count=None, requested=None, extra=None, ttl=60):
    value = {
        "models": [
            {"username": name, "id": index, "gender": "x"}
            for index, name in enumerate(models)
        ],
        "count": count if count is not None else len(models),
        "total": total,
        "ttl": ttl,
    }
    if requested is not None:
        value["requestedLimit"] = requested
    if extra:
        value.update(extra)
    return json.dumps(value).encode("utf-8")


class FakeResponse:
    def __init__(
        self,
        payload=b"{}",
        status_code=200,
        content_type="application/json",
        headers=None,
    ):
        self.content = payload
        self.status_code = status_code
        self.headers = dict(headers or {})
        if content_type is not None:
            self.headers.setdefault("content-type", content_type)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        if not self.responses:
            raise AssertionError("transport exhausted")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def pass_pages(models, page_size, total=None):
    """Full pages followed by the terminal empty page."""
    total = total if total is not None else len(models)
    pages = []
    for offset in range(0, len(models), page_size):
        chunk = models[offset : offset + page_size]
        pages.append(FakeResponse(body(chunk, total)))
    pages.append(FakeResponse(body([], total)))
    return pages


def client_for(responses, **overrides):
    values = dict(
        page_size=3,
        minimum_request_interval=0.0,
        sleep=lambda _seconds: None,
        jitter=lambda: 1.0,
        min_successful_passes=3,
        max_passes=6,
        required_stable_pairs=2,
        # Convergence fixtures serve scripted passes directly; cache-
        # generation pacing has its own tests below.
        max_generation_wait=0.0,
        jaccard_threshold=0.985,
        maximum_quarantine_ratio=0.02,
        duplicate_ratio_threshold=0.02,
    )
    values.update(overrides)
    return StripchatApiClient(FakeTransport(responses), **values)


class EnvelopeValidationTests(unittest.TestCase):
    def _validate(self, payload, **overrides):
        values = dict(requested_limit=3, content_type="application/json")
        values.update(overrides)
        return validate_envelope(payload, **values)

    def test_valid_envelope_with_unknown_fields(self):
        envelope = self._validate(
            body(["Model-A"], 10, extra={"newField": 1})
        )
        self.assertEqual(envelope.models[0].username, "Model-A")
        self.assertEqual(envelope.total, 10)

    def test_schema_drift_cases(self):
        cases = {
            "not object": b"[]",
            "models missing": json.dumps({"count": 0, "total": 0}).encode(),
            "models not list": json.dumps(
                {"models": {}, "count": 0, "total": 0}
            ).encode(),
            "count mismatch": body(["Model-A"], 5, count=2),
            "count bool": json.dumps(
                {"models": [], "count": True, "total": 0}
            ).encode(),
            "total missing": json.dumps(
                {"models": [], "count": 0}
            ).encode(),
            "total negative": json.dumps(
                {"models": [], "count": 0, "total": -1}
            ).encode(),
            "requested mismatch": body(["Model-A"], 5, requested=99),
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(StripchatApiError) as caught:
                    self._validate(payload)
                self.assertEqual(caught.exception.code, "api_schema_drift")

    def test_invalid_username_and_duplicates(self):
        with self.assertRaises(StripchatApiError) as caught:
            self._validate(body(["bad name"], 5))
        self.assertEqual(caught.exception.code, "invalid_username")
        with self.assertRaises(StripchatApiError) as caught:
            self._validate(body(["Model-A", "MODEL-a"], 5))
        self.assertEqual(
            caught.exception.code, "duplicate_identity_in_page"
        )

    def test_transport_shape_rejections(self):
        with self.assertRaises(StripchatApiError) as caught:
            self._validate(b"not json")
        self.assertEqual(caught.exception.code, "invalid_json")
        with self.assertRaises(StripchatApiError) as caught:
            self._validate(body([], 0), content_type="text/html")
        self.assertEqual(caught.exception.code, "invalid_content_type")
        with self.assertRaises(StripchatApiError) as caught:
            validate_envelope(
                b"x" * 100,
                requested_limit=3,
                content_type="application/json",
                max_bytes=10,
            )
        self.assertEqual(caught.exception.code, "response_too_large")


class PaginationTests(unittest.TestCase):
    def test_offset_advances_by_actual_count_not_requested_size(self):
        transport_pages = [
            FakeResponse(body(["A1", "A2"], 4)),  # short page: tail candidate
            FakeResponse(body(["A3"], 4)),
            FakeResponse(body([], 4)),
        ]
        client = client_for(transport_pages)
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.SUCCEEDED)
        requests = client._client.requests
        self.assertIn("offset=0", requests[0])
        self.assertIn("offset=2", requests[1], "advance by count, not limit")
        self.assertIn("offset=3", requests[2])
        self.assertTrue(result.terminal_empty_page)
        self.assertEqual(len(result.models), 3)

    def test_moving_total_never_terminates_alone(self):
        pages = [
            FakeResponse(body(["A1", "A2", "A3"], 3)),
            FakeResponse(body(["A4"], 9954)),
            FakeResponse(body([], 4)),
        ]
        client = client_for(pages)
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(len(result.models), 4)
        self.assertEqual(result.reported_totals, (3, 9954, 4))

    def test_premature_empty_page_fails(self):
        client = client_for([FakeResponse(body([], 100))])
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "premature_empty_page")
        self.assertEqual(result.models, ())

    def test_repeated_page_content_is_a_loop(self):
        page = body(["A1", "A2", "A3"], 100)
        client = client_for(
            [FakeResponse(page), FakeResponse(page), FakeResponse(page)]
        )
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "api_page_loop")

    def test_a_repeated_short_tail_page_is_the_feed_moving_not_a_loop(self):
        """The end of the feed is re-served, and that is not a loop.

        The population grows and shrinks by a few dozen models between
        requests, so once pagination reaches the end it asks again from the
        new offset and gets heavily overlapping - sometimes identical -
        short pages. Treating that as a pagination loop failed whole passes
        against a perfectly healthy feed, and one failed pass is enough to
        cost a run its convergence budget.
        """
        client = client_for(
            [
                FakeResponse(body(["A1", "A2", "A3"], 8)),
                FakeResponse(body(["B1", "B2", "B3"], 8)),
                # The tail, re-served verbatim as the feed grows under it.
                FakeResponse(body(["C1", "C2"], 8)),
                FakeResponse(body(["C1", "C2"], 10)),
                FakeResponse(body([], 10)),
            ],
            duplicate_ratio_threshold=0.5,
        )
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.SUCCEEDED)
        self.assertIsNone(result.error_code)
        self.assertEqual(
            [model.username for model in result.models],
            ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2"],
        )

    def test_a_short_page_loop_still_ends_at_the_page_cap(self):
        """Muting the guard on short pages must not unbound the walk."""
        client = client_for(
            [FakeResponse(body(["C1", "C2"], 500)) for _ in range(10)],
            max_pages=4,
            duplicate_ratio_threshold=0.9,
        )
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "api_page_cap")

    def test_page_cap_is_enforced(self):
        pages = [
            FakeResponse(body([f"M{index}A", f"M{index}B", f"M{index}C"], 1000))
            for index in range(10)
        ]
        client = client_for(pages, max_pages=2)
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "api_page_cap")

    def test_model_cap_is_enforced(self):
        pages = [
            FakeResponse(body([f"M{index}A", f"M{index}B", f"M{index}C"], 1000))
            for index in range(10)
        ]
        client = client_for(pages, max_models=4)
        result = client.fetch_pass(1)
        self.assertEqual(result.error_code, "api_model_cap")

    def test_cross_page_case_variant_collision_fails(self):
        pages = [
            FakeResponse(body(["Model-A", "Model-B", "Model-C"], 5)),
            FakeResponse(body(["MODEL-a"], 5)),
        ]
        client = client_for(pages)
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "api_identity_collision")

    def test_exact_cross_page_repeat_counts_as_churn(self):
        pages = [
            FakeResponse(body(["Model-A", "Model-B", "Model-C"], 4)),
            FakeResponse(body(["Model-A"], 4)),
            FakeResponse(body([], 4)),
        ]
        client = client_for(pages, duplicate_ratio_threshold=0.5)
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(result.duplicate_count, 1)

    def test_duplicate_ratio_above_threshold_fails(self):
        pages = [
            FakeResponse(body(["Model-A", "Model-B", "Model-C"], 4)),
            FakeResponse(body(["Model-A"], 4)),
            FakeResponse(body([], 4)),
        ]
        client = client_for(pages, duplicate_ratio_threshold=0.02)
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "api_duplicate_ratio_exceeded")
        self.assertEqual(result.models, ())

    def test_empty_snapshot_is_incomplete(self):
        client = client_for([FakeResponse(body([], 0))])
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "empty_snapshot")


class RetryPolicyTests(unittest.TestCase):
    def test_retryable_status_honors_bounded_retry_after(self):
        sleeps = []
        pages = [
            FakeResponse(
                b"", status_code=429, headers={"retry-after": "1000"}
            ),
            FakeResponse(body(["Model-A"], 1)),
            FakeResponse(body([], 1)),
        ]
        client = client_for(pages, sleep=sleeps.append)
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.SUCCEEDED)
        self.assertAlmostEqual(sum(sleeps), 30.0, places=2)

    def test_transport_errors_use_jittered_backoff_then_succeed(self):
        sleeps = []
        pages = [
            ConnectionError("down"),
            ConnectionError("down"),
            FakeResponse(body(["Model-A"], 1)),
            FakeResponse(body([], 1)),
        ]
        client = client_for(pages, sleep=sleeps.append)
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.SUCCEEDED)
        self.assertAlmostEqual(sum(sleeps), 0.75 + 1.5, places=2)

    def test_retry_budget_exhaustion_fails_the_pass(self):
        pages = [ConnectionError("down")] * 5
        client = client_for(pages, page_retries=2)
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "api_http_error")

    def test_non_retryable_status_fails_immediately(self):
        client = client_for([FakeResponse(b"", status_code=404)])
        result = client.fetch_pass(1)
        self.assertEqual(result.error_code, "api_http_error")
        self.assertEqual(len(client._client.requests), 1)

    def test_redirects_are_never_followed(self):
        client = client_for(
            [FakeResponse(b"", status_code=302, headers={"location": "x"})]
        )
        result = client.fetch_pass(1)
        self.assertEqual(result.error_code, "unexpected_redirect")


class StopTests(unittest.TestCase):
    class StopAfter:
        def __init__(self, calls):
            self.calls = calls

        def is_cancelled(self):
            self.calls -= 1
            return self.calls < 0

    def test_stop_before_first_request(self):
        client = client_for(
            [FakeResponse(body(["Model-A"], 1))],
            stop_token=self.StopAfter(0),
        )
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.CANCELLED)
        self.assertEqual(result.error_code, "api_cancelled")
        self.assertEqual(len(client._client.requests), 0)

    def test_stop_between_pages(self):
        client = client_for(
            [
                FakeResponse(body(["Model-A", "Model-B", "Model-C"], 6)),
                FakeResponse(body(["Model-D"], 6)),
            ],
            stop_token=self.StopAfter(2),
        )
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.CANCELLED)
        self.assertEqual(result.models, ())

    def test_stop_during_backoff(self):
        client = client_for(
            [
                FakeResponse(
                    b"", status_code=503, headers={"retry-after": "30"}
                ),
                FakeResponse(body(["Model-A"], 1)),
            ],
            stop_token=self.StopAfter(2),
        )
        result = client.fetch_pass(1)
        self.assertIs(result.status, OutcomeStatus.CANCELLED)


class ConvergenceTests(unittest.TestCase):
    def _passes(self, *model_sets, page_size=3):
        responses = []
        for models in model_sets:
            responses.extend(pass_pages(list(models), page_size))
        return responses

    def test_three_stable_passes_converge(self):
        stable = ["Model-A", "Model-B", "Model-C", "Model-D"]
        client = client_for(self._passes(stable, stable, stable))
        result = client.collect_snapshot()
        self.assertIs(result.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(len(result.models), 4)
        self.assertEqual(result.convergence_window, (1, 2, 3))
        self.assertEqual(result.quarantined, ())

    def test_incomplete_pass_resets_the_window(self):
        stable = ["Model-A", "Model-B", "Model-C"]
        responses = self._passes(stable, stable)
        responses.append(FakeResponse(body([], 100)))  # premature empty
        responses.extend(self._passes(stable, stable, stable))
        client = client_for(responses)
        result = client.collect_snapshot()
        self.assertIs(result.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(result.convergence_window, (4, 5, 6))

    def test_one_failed_pass_does_not_exhaust_the_default_budget(self):
        """A single lost pass must not cost the run its whole snapshot.

        A failed pass resets the consecutive chain, so with too small a
        budget one transient failure late in the sequence leaves no room to
        build another window and the step ends api_snapshot_unstable with
        five healthy passes in hand. That is exactly how session 10 failed:
        passes 1-3 did not converge, pass 4 raised api_page_loop, and
        passes 5-6 could no longer form a window of three.
        """
        from stripchat_store import DEFAULT_STRIPCHAT_CONFIG

        churn = [
            [f"Early-{tag}-{index}" for index in range(3)]
            for tag in "abc"
        ]
        settled = ["Model-A", "Model-B", "Model-C"]
        responses = self._passes(*churn)
        responses.append(FakeResponse(body([], 100)))  # premature empty
        responses.extend(self._passes(settled, settled, settled))
        client = client_for(
            responses,
            max_passes=DEFAULT_STRIPCHAT_CONFIG["max_passes"],
        )
        result = client.collect_snapshot()
        self.assertIs(result.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(result.convergence_window, (5, 6, 7))

    def test_singletons_are_quarantined_not_accepted(self):
        base = [f"Model-{index}" for index in range(100)]
        with_extra = base + ["Flicker-1"]
        client = client_for(
            self._passes(base, with_extra, base), page_size=100
        )
        result = client.collect_snapshot()
        self.assertIs(result.status, OutcomeStatus.SUCCEEDED)
        accepted = {model.username for model in result.models}
        self.assertNotIn("Flicker-1", accepted)
        self.assertEqual(
            [model.username for model in result.quarantined], ["Flicker-1"]
        )

    def test_excessive_quarantine_fails_after_confirmation_pass(self):
        # Each pass carries 7 unique flickers on a 1000-model base: adjacent
        # Jaccard stays >= 0.985 while window singletons exceed the 2%
        # quarantine ceiling even after the one confirmation pass.
        base = [f"Model-{index}" for index in range(1000)]
        def flickers(tag):
            return base + [f"Flicker-{tag}-{index}" for index in range(7)]

        client = client_for(
            self._passes(
                flickers("a"),
                flickers("b"),
                flickers("c"),
                flickers("d"),
                page_size=400,
            ),
            page_size=400,
        )
        result = client.collect_snapshot()
        self.assertIs(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "excessive_quarantine")
        self.assertEqual(result.models, ())

    def test_never_converges_by_pass_six_is_unstable(self):
        sets = [
            [f"Model-{index}-{round_}" for index in range(5)]
            for round_ in range(6)
        ]
        client = client_for(self._passes(*sets), page_size=100)
        result = client.collect_snapshot()
        self.assertIs(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "api_snapshot_unstable")
        self.assertEqual(result.models, ())

    def test_metrics_contain_no_usernames(self):
        stable = ["Secret-Handle@9", "Other-Handle@8", "Third-Handle@7"]
        client = client_for(self._passes(stable, stable, stable))
        result = client.collect_snapshot()
        dumped = json.dumps(result.metrics())
        for name in stable:
            self.assertNotIn(name, dumped)


if __name__ == "__main__":
    unittest.main()


class CacheGenerationPacingTests(unittest.TestCase):
    """Two reads of one cached generation are not two observations.

    The live endpoint answers from a cache that rolls every ~10s while a
    full pass takes ~8s, so back-to-back passes returned byte-identical
    data. Counting that as agreement would manufacture stability.
    """

    def _client(self, responses, **overrides):
        values = dict(
            page_size=3,
            minimum_request_interval=0.0,
            jitter=lambda: 1.0,
            min_successful_passes=3,
            max_passes=6,
            required_stable_pairs=2,
            generation_poll_interval=1.0,
            max_generation_wait=30.0,
        )
        values.update(overrides)
        return StripchatApiClient(FakeTransport(responses), **values)

    def test_a_pass_waits_for_the_cache_to_roll(self):
        first = ["A1", "A2", "A3"]
        second = ["B1", "B2", "B3"]
        responses = pass_pages(first, 3)
        # Two probes still serving the old generation, then a fresh one.
        responses.append(FakeResponse(body(first, 3)))
        responses.append(FakeResponse(body(first, 3)))
        responses.append(FakeResponse(body(second, 3)))
        responses.extend(pass_pages(second, 3))
        slept = []
        client = self._client(responses, sleep=slept.append)

        self.assertIs(client.fetch_pass(1).status, OutcomeStatus.SUCCEEDED)
        self.assertTrue(client._await_new_generation(client._last_first_page))
        self.assertGreaterEqual(
            len(slept), 1, "it must wait rather than re-read the same cache"
        )
        self.assertIs(client.fetch_pass(2).status, OutcomeStatus.SUCCEEDED)

    def test_waiting_is_cancellable(self):
        class StopAfter:
            def __init__(self, calls):
                self.calls = calls

            def is_cancelled(self):
                self.calls -= 1
                return self.calls < 0

        page = body(["A1", "A2", "A3"], 3)
        responses = [FakeResponse(page) for _ in range(20)]
        client = self._client(responses, sleep=lambda _s: None)
        # Take the real fingerprint, then arm the stop token so the wait
        # loop is what gets cancelled.
        stuck = client._first_page_fingerprint()
        client.stop_token = StopAfter(2)
        with self.assertRaises(Exception):
            client._await_new_generation(stuck)

    def test_a_stuck_feed_is_reported_but_does_not_hang(self):
        page_bytes = body(["A1", "A2", "A3"], 3)
        responses = [FakeResponse(page_bytes) for _ in range(60)]
        clock = {"now": 0.0}

        def tick():
            clock["now"] += 5.0
            return clock["now"]

        client = self._client(
            responses,
            sleep=lambda _s: None,
            clock=tick,
            max_generation_wait=20.0,
        )
        stuck = client._first_page_fingerprint()
        self.assertFalse(client._await_new_generation(stuck))

    def test_pacing_is_skipped_when_disabled(self):
        responses = pass_pages(["A1", "A2", "A3"], 3)
        client = self._client(
            responses, sleep=lambda _s: None, max_generation_wait=0.0
        )
        client.fetch_pass(1)
        before = len(client._client.requests)
        self.assertTrue(client._await_new_generation(client._last_first_page))
        self.assertEqual(
            len(client._client.requests),
            before,
            "disabled pacing must not issue probe requests",
        )
