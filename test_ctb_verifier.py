import tempfile
import unittest
from pathlib import Path

from ctb_store import ChaturbateRunStore
from ctb_verifier import ChaturbateVerifier
from test_support import FakeStopToken
from workflow_types import (
    OutcomeStatus,
    VerificationRecord,
    VerificationVerdict,
    utc_now_iso,
)


def record(url, verdict, attempt=1, reason=None):
    return VerificationRecord(
        original_url=url,
        verdict=verdict,
        reason_code=reason or verdict.value,
        attempt=attempt,
        timestamp=utc_now_iso(),
        observed_url=url,
        run_id="synthetic",
    )


class ChaturbateVerifierTests(unittest.TestCase):
    def test_step4_browser_crash_keeps_previous_final_and_pending_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp, "FINAL_BLOCKED.txt")
            old.write_text(
                "https://chaturbate.com/previous_final/\n",
                encoding="utf-8",
            )
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            urls = [
                "https://chaturbate.com/synthetic_a/",
                "https://chaturbate.com/synthetic_b/",
            ]

            def provider(url, attempt):
                if url.endswith("synthetic_b/"):
                    raise RuntimeError("browser closed")
                return record(url, VerificationVerdict.BLOCKED, attempt)

            outcome = ChaturbateVerifier(
                store, context, provider, max_attempts=1
            ).verify(urls)
            self.assertEqual(outcome.status, OutcomeStatus.INCOMPLETE)
            self.assertEqual(
                old.read_text(encoding="utf-8"),
                "https://chaturbate.com/previous_final/\n",
            )
            checkpoint = store.load_checkpoint(context)
            self.assertEqual(
                checkpoint["verdicts"][urls[0]]["verdict"], "blocked"
            )
            self.assertEqual(
                checkpoint["verdicts"][urls[1]]["verdict"], "unknown"
            )
            self.assertTrue(Path(context.run_dir, "step4").is_dir())

    def test_step4_stop_after_batch_is_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            stop = FakeStopToken()
            urls = [
                "https://chaturbate.com/synthetic_a/",
                "https://chaturbate.com/synthetic_b/",
            ]
            first_calls = []

            def first_provider(url, attempt):
                first_calls.append(url)
                stop.cancel()
                return record(url, VerificationVerdict.BLOCKED, attempt)

            first = ChaturbateVerifier(
                store,
                context,
                first_provider,
                stop_token=stop,
                batch_size=1,
            ).verify(urls)
            self.assertEqual(first.status, OutcomeStatus.CANCELLED)
            self.assertEqual((first.processed_count, first.remaining_count), (1, 1))
            resumed_calls = []

            def resumed_provider(url, attempt):
                resumed_calls.append(url)
                return record(url, VerificationVerdict.ACCESSIBLE, attempt)

            second = ChaturbateVerifier(
                store,
                context,
                resumed_provider,
                stop_token=FakeStopToken(),
                batch_size=1,
            ).verify(urls)
            self.assertEqual(second.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(resumed_calls, [urls[1]])
            self.assertEqual(
                Path(tmp, "FINAL_BLOCKED.txt").read_text(encoding="utf-8"),
                f"{urls[0]}\n",
            )

    def test_empty_current_generation_publishes_empty_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp, "FINAL_BLOCKED.txt")
            final.write_text(
                "https://chaturbate.com/old_entry/\n", encoding="utf-8"
            )
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            outcome = ChaturbateVerifier(
                store, context, lambda *_: None
            ).verify([])
            self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(final.read_bytes(), b"")

    def test_known_blocked_urls_are_published_without_being_navigated(self):
        """Models already confirmed in master are not re-checked in the browser."""
        alice = "https://chaturbate.com/alice/"
        bob = "https://chaturbate.com/bob/"
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            visited = []

            def provider(url, attempt):
                visited.append(url)
                return record(url, VerificationVerdict.BLOCKED)

            outcome = ChaturbateVerifier(
                store, context, provider, known_blocked=[alice]
            ).verify([bob])

            self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(visited, [bob])
            published = Path(tmp, "FINAL_BLOCKED.txt").read_text(
                encoding="utf-8"
            ).split()
            self.assertEqual(published, [alice, bob])

    def test_known_blocked_urls_are_not_duplicated(self):
        bob = "https://chaturbate.com/bob/"
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            outcome = ChaturbateVerifier(
                store,
                context,
                lambda url, attempt: record(url, VerificationVerdict.BLOCKED),
                known_blocked=[bob],
            ).verify([bob])
            self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(
                Path(tmp, "FINAL_BLOCKED.txt").read_text(encoding="utf-8").split(),
                [bob],
            )

    def test_unclassifiable_pages_stop_blocking_completion(self):
        """An offline room can never resolve, so it must not fail forever."""
        offline = "https://chaturbate.com/offline_model/"
        blocked = "https://chaturbate.com/blocked_model/"
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            attempts = []

            def provider(url, attempt):
                attempts.append((url, attempt))
                if url == offline:
                    return record(
                        url,
                        VerificationVerdict.UNKNOWN,
                        attempt,
                        reason="room_offline",
                    )
                return record(url, VerificationVerdict.BLOCKED, attempt)

            outcome = ChaturbateVerifier(
                store, context, provider, max_attempts=2
            ).verify([offline, blocked])

            self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
            # Retried up to the cap, then accepted as unresolvable.
            self.assertEqual(
                sum(1 for url, _ in attempts if url == offline), 2
            )
            published = Path(tmp, "FINAL_BLOCKED.txt").read_text(
                encoding="utf-8"
            ).split()
            self.assertEqual(published, [blocked])

    def test_unclassifiable_pages_are_reported_not_silently_dropped(self):
        offline = "https://chaturbate.com/offline_model/"
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            messages = []
            ChaturbateVerifier(
                store,
                context,
                lambda url, attempt: record(
                    url,
                    VerificationVerdict.UNKNOWN,
                    attempt,
                    reason="room_offline",
                ),
                progress_callback=lambda event: messages.append(event.message),
                max_attempts=1,
            ).verify([offline])
            self.assertTrue(
                any("could not be classified" in item for item in messages),
                messages,
            )
            self.assertTrue(
                any("room_offline: 1" in item for item in messages), messages
            )

    def test_browser_failures_still_keep_the_step_incomplete(self):
        """A crash is not a verdict, so resume must stay available."""
        url = "https://chaturbate.com/synthetic_a/"
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            outcome = ChaturbateVerifier(
                store,
                context,
                lambda u, attempt: record(
                    u,
                    VerificationVerdict.UNKNOWN,
                    attempt,
                    reason="browser_closed",
                ),
                max_attempts=2,
            ).verify([url])
            self.assertEqual(outcome.status, OutcomeStatus.INCOMPLETE)
            self.assertEqual(outcome.error_code, "verification_unknown")

    def test_resume_rejects_changed_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            first = "https://chaturbate.com/synthetic_a/"
            stop = FakeStopToken(True)
            ChaturbateVerifier(
                store, context, lambda *_: None, stop_token=stop
            ).verify([first])
            outcome = ChaturbateVerifier(
                store, context, lambda *_: None
            ).verify(["https://chaturbate.com/synthetic_b/"])
            self.assertEqual(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(
                outcome.error_code, "resume_checkpoint_mismatch"
            )


class ProgressStatusMigrationTests(unittest.TestCase):
    def test_progress_events_are_running_with_unchanged_messages(self):
        from workflow_types import ProgressStatus

        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run(mode="manual", source="test")
            events = []

            def provider(url, attempt):
                return VerificationRecord(
                    original_url=url,
                    verdict=VerificationVerdict.BLOCKED,
                    reason_code="denied_notice",
                    attempt=attempt,
                    timestamp="2026-07-28T00:00:00+00:00",
                    observed_url=url,
                    run_id=context.run_id,
                )

            verifier = ChaturbateVerifier(
                store,
                context,
                provider,
                progress_callback=events.append,
            )
            outcome = verifier.verify(
                ["https://chaturbate.com/model_a/",
                 "https://chaturbate.com/model_b/"]
            )
            self.assertEqual(outcome.status.value, "succeeded")
            self.assertTrue(events)
            self.assertEqual(
                [event.message for event in events],
                ["Verified 2 of 2"],
                "one event per batch; message text must not change",
            )
            for event in events:
                self.assertIs(event.status, ProgressStatus.RUNNING)
