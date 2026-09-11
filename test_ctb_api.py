import json
import unittest

import httpx

from ctb_api import ChaturbateApiClient
from test_support import FakeClock, FakeStopToken
from workflow_types import OutcomeStatus


def response(payload, status=200, headers=None):
    request = httpx.Request("GET", "https://example.test/api")
    return httpx.Response(
        status,
        request=request,
        content=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )


class _Client:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def get(self, url, params):
        self.calls.append((url, list(params)))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class ChaturbateApiTests(unittest.TestCase):
    def test_complete_pagination_preserves_repeated_gender_keys(self):
        client = _Client(
            [
                response(
                    {
                        "count": 2,
                        "results": [
                            {
                                "username": "synthetic_a",
                                "seconds_online": 120,
                            },
                            {
                                "username": "synthetic_b",
                                "seconds_online": 80,
                            },
                        ],
                    }
                )
            ]
        )
        result = ChaturbateApiClient(
            client=client, limit=500
        ).fetch_pass(1)
        self.assertEqual(result.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(len(result.models), 2)
        genders = [
            value
            for key, value in client.calls[0][1]
            if key == "gender"
        ]
        self.assertEqual(genders, ["f", "c"])

    def test_partial_api_fetch_is_incomplete_and_discards_publication_value(self):
        client = _Client(
            [
                response(
                    {
                        "count": 3,
                        "results": [
                            {"username": "synthetic_a"},
                            {"username": "synthetic_b"},
                        ],
                    }
                ),
                httpx.ReadTimeout("synthetic timeout"),
            ]
        )
        result = ChaturbateApiClient(
            client=client,
            limit=2,
            page_retries=0,
        ).fetch_pass(1)
        self.assertEqual(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(result.error_code, "network_error")

    def test_adaptive_stops_after_stable_pair(self):
        pages = []
        for _ in range(2):
            pages.append(
                response(
                    {
                        "count": 2,
                        "results": [
                            {"username": "synthetic_a"},
                            {"username": "synthetic_b"},
                        ],
                    }
                )
            )
        client = ChaturbateApiClient(client=_Client(pages))
        result = client.collect_adaptive()
        self.assertEqual(result.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(len(result.passes), 2)
        self.assertEqual(len(result.models), 2)

    def test_empty_equal_passes_never_become_stable_success(self):
        client = ChaturbateApiClient(
            client=_Client(
                [response({"count": 0, "results": []}) for _ in range(5)]
            )
        )
        result = client.collect_adaptive()
        self.assertEqual(result.status, OutcomeStatus.INCOMPLETE)
        self.assertEqual(len(result.passes), 5)

    def test_stop_during_backoff_is_bounded(self):
        stop = FakeStopToken()
        clock = FakeClock()

        def sleep(_seconds):
            stop.cancel()

        client = ChaturbateApiClient(
            client=_Client(
                [response({}, status=429, headers={"Retry-After": "10"})]
            ),
            clock=clock,
            sleep=sleep,
            stop_token=stop,
        )
        result = client.fetch_pass(1)
        self.assertIn(
            result.status,
            (OutcomeStatus.CANCELLED, OutcomeStatus.INCOMPLETE),
        )
