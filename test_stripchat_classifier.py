"""Stripchat URL grammar and identity policy."""

import unittest

from stripchat_classifier import (
    canonical_model_url,
    canonical_url_for_username,
    ensure_no_lookup_collisions,
    model_lookup_key,
    observed_model_identity,
)


class CanonicalModelUrlTests(unittest.TestCase):
    def test_accepts_observed_grammar_and_preserves_case(self):
        cases = {
            "https://stripchat.com/Synthetic-Model@1/": (
                "https://stripchat.com/Synthetic-Model@1/"
            ),
            "https://stripchat.com/Synthetic-Model@1": (
                "https://stripchat.com/Synthetic-Model@1/"
            ),
            "https://www.stripchat.com/UPPER_lower-123/": (
                "https://stripchat.com/UPPER_lower-123/"
            ),
            "https://WWW.STRIPCHAT.COM/MiXeD/": (
                "https://stripchat.com/MiXeD/"
            ),
            "https://stripchat.com/" + "a" * 64 + "/": (
                "https://stripchat.com/" + "a" * 64 + "/"
            ),
        }
        for raw, expected in cases.items():
            with self.subTest(url=raw):
                self.assertEqual(canonical_model_url(raw), expected)

    def test_rejections(self):
        cases = {
            "query": "https://stripchat.com/model?tab=bio",
            "fragment": "https://stripchat.com/model#top",
            "credentials": "https://user:pw@stripchat.com/model/",
            "userinfo": "https://user@stripchat.com/model/",
            "port": "https://stripchat.com:443/model/",
            "subdomain": "https://m.stripchat.com/model/",
            "foreign host": "https://example.com/model/",
            "http": "http://stripchat.com/model/",
            "two segments": "https://stripchat.com/model/extra/",
            "empty path": "https://stripchat.com/",
            "no path": "https://stripchat.com",
            "encoded slash": "https://stripchat.com/mo%2Fdel/",
            "encoded backslash": "https://stripchat.com/mo%5Cdel/",
            "any percent": "https://stripchat.com/mo%41del/",
            "backslash": "https://stripchat.com\\model/",
            "interior space": "https://stripchat.com/mo del/",
            "tab": "https://stripchat.com/mo\tdel/",
            "dot segment": "https://stripchat.com/../model/",
            "single dot": "https://stripchat.com/./",
            "plain dot handle": "https://stripchat.com/m.odel/",
            "unicode": "https://stripchat.com/modèl/",
            "confusable": "https://stripchat.com/mоdel/",
            "too long": "https://stripchat.com/" + "a" * 65 + "/",
            "empty": "",
            "not a url": "model",
        }
        for label, raw in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    canonical_model_url(raw)
                self.assertEqual(str(caught.exception), "invalid_url")

    def test_username_round_trip(self):
        self.assertEqual(
            canonical_url_for_username("Synthetic-Model@1"),
            "https://stripchat.com/Synthetic-Model@1/",
        )
        with self.assertRaises(ValueError):
            canonical_url_for_username("bad/name")


class LookupKeyTests(unittest.TestCase):
    def test_lookup_key_folds_case_only(self):
        self.assertEqual(
            model_lookup_key("https://stripchat.com/MiXeD/"),
            "https://stripchat.com/mixed/",
        )

    def test_collision_detection_is_a_hard_failure(self):
        ensure_no_lookup_collisions(
            [
                "https://stripchat.com/Model-A/",
                "https://stripchat.com/Model-B/",
                "https://stripchat.com/Model-A/",
            ]
        )
        with self.assertRaises(ValueError) as caught:
            ensure_no_lookup_collisions(
                [
                    "https://stripchat.com/Model-A/",
                    "https://stripchat.com/MODEL-a/",
                ]
            )
        self.assertEqual(str(caught.exception), "identity_key_collision")


class ObservedIdentityTests(unittest.TestCase):
    def test_same_identity_with_browser_noise_is_accepted(self):
        for observed in (
            "https://stripchat.com/Model-A/?utm=1",
            "https://www.stripchat.com/Model-A/#section",
            "https://stripchat.com/Model-A",
        ):
            with self.subTest(url=observed):
                self.assertEqual(
                    observed_model_identity(observed),
                    "https://stripchat.com/Model-A/",
                )

    def test_non_profile_routes_are_rejected(self):
        for observed in (
            "https://stripchat.com/",
            "https://stripchat.com/login",
            "https://stripchat.com/a/b",
            "https://example.com/Model-A/",
            "http://stripchat.com/Model-A/",
        ):
            with self.subTest(url=observed):
                if observed == "https://stripchat.com/login":
                    # /login parses as a one-segment path; identity says it
                    # is the login "handle", so the redirect table (not this
                    # helper) must classify login routes.
                    self.assertEqual(
                        observed_model_identity(observed),
                        "https://stripchat.com/login/",
                    )
                else:
                    with self.assertRaises(ValueError):
                        observed_model_identity(observed)


class SyntheticMasterInventoryTests(unittest.TestCase):
    """Representative handle grammar without consulting local model data."""

    def test_synthetic_master_entries_round_trip_byte_identically(self):
        handles = ("A", "Synthetic-Model@1", "synthetic_model_2", "S" * 64)
        urls = [f"https://stripchat.com/{handle}/" for handle in handles]
        mismatches = sum(
            1 for url in urls if canonical_model_url(url) != url
        )
        self.assertEqual(
            mismatches, 0, f"{mismatches} of {len(urls)} entries changed"
        )
        ensure_no_lookup_collisions(urls)


class SemanticDecisionTableTests(unittest.TestCase):
    """Every row of the H/D/P/C matrix, plus precedence and extras."""

    URL = "https://stripchat.com/Model-A/"

    def observe(self, **overrides):
        from stripchat_classifier import StripchatPageObservation

        values = dict(
            original_url=self.URL,
            current_url=self.URL,
            ready_state="complete",
        )
        values.update(overrides)
        return StripchatPageObservation(**values)

    def classify(self, observation, attempt=1):
        from stripchat_classifier import classify_stripchat_page

        return classify_stripchat_page(observation, attempt=attempt)

    def test_matrix_rows(self):
        hidden = ("Account Hidden",)
        disabled = ("Account Disabled",)
        both = ("Account Hidden", "Account Disabled")
        rows = [
            # (notice_texts, player, ambiguous_class, verdict, reason)
            ((), 0, 0, "unknown", "missing_positive_signal"),
            ((), 0, 1, "unknown", "class_semantics_ambiguous"),
            ((), 1, 0, "accessible", "player_present"),
            ((), 1, 1, "unknown", "class_player_conflict"),
            (disabled, 0, 0, "unknown", "account_disabled_or_deleted"),
            (disabled, 0, 1, "unknown", "account_disabled_or_deleted"),
            (disabled, 1, 0, "unknown", "disabled_player_conflict"),
            (disabled, 1, 1, "unknown", "disabled_player_conflict"),
            (hidden, 0, 0, "blocked", "visible_hidden_notice"),
            (hidden, 0, 1, "blocked", "visible_hidden_notice"),
            (hidden, 1, 0, "unknown", "hidden_player_conflict"),
            (hidden, 1, 1, "unknown", "hidden_player_conflict"),
            (both, 0, 0, "unknown", "hidden_disabled_conflict"),
            (both, 1, 1, "unknown", "hidden_disabled_conflict"),
        ]
        for texts, player, ambiguous, verdict, reason in rows:
            with self.subTest(
                texts=texts, player=player, ambiguous=ambiguous
            ):
                record = self.classify(
                    self.observe(
                        account_notice_texts=texts,
                        video_element_count=player,
                        visible_disabled_selector_count=ambiguous,
                    )
                )
                self.assertEqual(record.verdict.value, verdict)
                self.assertEqual(record.reason_code, reason)

    def test_live_conflict_hidden_text_in_disabled_classes_is_blocked(self):
        record = self.classify(
            self.observe(
                account_notice_texts=(
                    "Account Hidden",
                    "Synthetic-Model@1's account has been hidden.",
                ),
                visible_disabled_selector_count=2,
            )
        )
        self.assertEqual(record.verdict.value, "blocked")
        self.assertEqual(record.reason_code, "visible_hidden_notice")
        self.assertTrue(record.terminal)

    def test_class_only_evidence_is_never_clean_or_blocked(self):
        record = self.classify(
            self.observe(visible_hidden_selector_count=2)
        )
        self.assertEqual(record.verdict.value, "unknown")
        self.assertEqual(record.reason_code, "class_semantics_ambiguous")

    def test_plain_complete_page_is_never_clean(self):
        record = self.classify(self.observe())
        self.assertEqual(record.verdict.value, "unknown")
        self.assertEqual(record.reason_code, "missing_positive_signal")

    def test_arbitrary_body_text_cannot_create_a_blocked_verdict(self):
        record = self.classify(
            self.observe(
                account_notice_texts=(
                    "this room may hide the account settings menu",
                )
            )
        )
        self.assertNotEqual(record.verdict.value, "blocked")

    def test_undisplayed_video_still_counts_as_player(self):
        record = self.classify(self.observe(video_element_count=1))
        self.assertEqual(record.verdict.value, "accessible")

    def test_precedence_of_gates_and_errors_over_signals(self):
        hidden_kwargs = dict(
            account_notice_texts=("Account Hidden",),
            video_element_count=1,
        )
        cases = [
            (
                dict(navigation_error="navigation_timeout"),
                "navigation_timeout",
            ),
            (dict(captcha_detected=True), "captcha_required"),
            (dict(consent_gate_detected=True), "consent_gate_blocked"),
            (
                dict(current_url="https://stripchat.com/"),
                "homepage_redirect",
            ),
            (
                dict(current_url="https://stripchat.com/Other-Model/"),
                "identity_mismatch",
            ),
            (
                dict(current_url="https://stripchat.com/a/b"),
                "unexpected_stripchat_redirect",
            ),
            (
                dict(current_url="https://example.com/Model-A/"),
                "external_redirect",
            ),
            (dict(current_url="::bad::"), "invalid_observed_url"),
            (dict(body_readable=False), "blank_page"),
        ]
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                record = self.classify(
                    self.observe(**{**hidden_kwargs, **overrides})
                )
                self.assertEqual(record.verdict.value, "unknown")
                self.assertEqual(record.reason_code, reason)

    def test_a_room_whose_shutter_explains_itself_is_a_confirmed_nontarget(
        self,
    ):
        """A private or group show renders a shutter instead of a stream.

        The room is demonstrably not hidden - a hidden account renders the
        account notice instead - so the candidate is simply not watchable
        from here and must end rather than be retried until the run gives
        up on it.
        """
        first = self.classify(
            self.observe(
                shutter_status_text=(
                    "anchan is having fun in Private show LIVE"
                )
            ),
            attempt=1,
        )
        self.assertEqual(first.verdict.value, "unknown")
        self.assertEqual(first.reason_code, "room_not_viewable")
        self.assertFalse(first.terminal)
        second = self.classify(
            self.observe(shutter_status_text="having fun in Private show"),
            attempt=2,
        )
        self.assertTrue(second.terminal)

    def test_a_watchable_room_never_reaches_the_shutter_rule(self):
        record = self.classify(
            self.observe(
                video_element_count=1,
                shutter_status_text="having fun in Private show",
            )
        )
        self.assertEqual(record.verdict.value, "accessible")

    def test_an_empty_shutter_leaves_the_page_loudly_unexplained(self):
        """A silent page must stay loud; that is the DOM-drift alarm."""
        record = self.classify(self.observe(shutter_status_text="   "))
        self.assertEqual(record.reason_code, "missing_positive_signal")

    def test_conflicting_account_classes_outrank_the_shutter(self):
        record = self.classify(
            self.observe(
                visible_hidden_selector_count=1,
                shutter_status_text="having fun in Private show",
            )
        )
        self.assertEqual(record.reason_code, "class_semantics_ambiguous")

    def test_an_unfinished_load_only_explains_a_page_with_no_signal(self):
        record = self.classify(self.observe(ready_state="interactive"))
        self.assertEqual(record.verdict.value, "unknown")
        self.assertEqual(record.reason_code, "page_not_ready")

    def test_a_decided_page_is_not_reopened_by_an_unfinished_load(self):
        """The load event is media and telemetry, not evidence.

        Live pages render their verdict about a second into the load and
        never change it, but the load event lands ~20s later. Waiting for
        it costs the whole step its time budget and settles nothing, so a
        page that already says what it is stays said.
        """
        cases = [
            (dict(account_notice_texts=("Account Hidden",)), "blocked"),
            (dict(video_element_count=1), "accessible"),
            (dict(offline_notice_detected=True), "unknown"),
        ]
        for overrides, verdict in cases:
            with self.subTest(**overrides):
                record = self.classify(
                    self.observe(ready_state="interactive", **overrides)
                )
                self.assertEqual(record.verdict.value, verdict)
                self.assertNotEqual(record.reason_code, "page_not_ready")

    def test_same_identity_with_query_reaches_semantics(self):
        record = self.classify(
            self.observe(
                current_url="https://www.stripchat.com/Model-A/?tab=bio",
                account_notice_texts=("Account Hidden",),
            )
        )
        self.assertEqual(record.verdict.value, "blocked")

    def test_login_route_is_confirmable_nontarget(self):
        first = self.classify(
            self.observe(current_url="https://stripchat.com/login/"),
            attempt=1,
        )
        self.assertEqual(first.reason_code, "login_required")
        self.assertFalse(first.terminal)
        second = self.classify(
            self.observe(current_url="https://stripchat.com/login/"),
            attempt=2,
        )
        self.assertTrue(second.terminal)

    def test_offline_and_not_found_confirm_after_two_attempts(self):
        for flag, reason in (
            ("offline_notice_detected", "room_offline"),
            ("not_found_notice_detected", "not_found"),
        ):
            with self.subTest(reason=reason):
                first = self.classify(
                    self.observe(**{flag: True}), attempt=1
                )
                self.assertEqual(first.reason_code, reason)
                self.assertFalse(first.terminal)
                self.assertTrue(first.retryable)
                second = self.classify(
                    self.observe(**{flag: True}), attempt=2
                )
                self.assertTrue(second.terminal)

    def test_gated_routes_are_never_accessible(self):
        for overrides in (
            dict(current_url="https://stripchat.com/"),
            dict(current_url="https://stripchat.com/login/"),
            dict(captcha_detected=True),
            dict(consent_gate_detected=True),
            dict(current_url="https://example.com/Model-A/"),
        ):
            with self.subTest(overrides=overrides):
                record = self.classify(
                    self.observe(video_element_count=1, **overrides)
                )
                self.assertNotEqual(record.verdict.value, "accessible")


if __name__ == "__main__":
    unittest.main()
