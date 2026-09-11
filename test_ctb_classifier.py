import unittest

from ctb_classifier import (
    PageObservation,
    canonical_model_url,
    classify_page,
)
from workflow_types import VerificationVerdict


class ChaturbateClassifierTests(unittest.TestCase):
    def test_canonical_url_rejects_unsafe_identity(self):
        self.assertEqual(
            canonical_model_url(
                "https://www.chaturbate.com/Synthetic_Name/?source=test#x"
            ),
            "https://chaturbate.com/Synthetic_Name/",
        )
        for value in (
            "http://chaturbate.com/a/",
            "https://example.test/a/",
            "https://chaturbate.com/a/b/",
            "javascript:alert(1)",
        ):
            with self.assertRaises(ValueError):
                canonical_model_url(value)

    def test_accessible_requires_positive_signal(self):
        base = dict(
            original_url="https://chaturbate.com/synthetic_a/",
            current_url="https://chaturbate.com/synthetic_a/",
        )
        self.assertEqual(
            classify_page(
                PageObservation(**base, has_room_container=True)
            ).verdict,
            VerificationVerdict.ACCESSIBLE,
        )
        for observation in (
            PageObservation(**base),
            PageObservation(
                **base,
                navigation_error="navigation_timeout",
                has_room_container=True,
            ),
            PageObservation(
                original_url=base["original_url"],
                current_url="https://chaturbate.com/",
            ),
        ):
            self.assertEqual(
                classify_page(observation).verdict,
                VerificationVerdict.UNKNOWN,
            )

    def test_error_precedes_blocked_and_blocked_precedes_accessible(self):
        base = dict(
            original_url="https://chaturbate.com/synthetic_b/",
            current_url="https://chaturbate.com/synthetic_b/",
            has_denied_notice=True,
            has_room_container=True,
        )
        self.assertEqual(
            classify_page(PageObservation(**base)).verdict,
            VerificationVerdict.BLOCKED,
        )
        self.assertEqual(
            classify_page(
                PageObservation(**base, navigation_error="selector_error")
            ).verdict,
            VerificationVerdict.UNKNOWN,
        )
