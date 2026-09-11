"""Resumable, checkpointed Chaturbate candidate verification."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable

from ctb_classifier import canonical_model_url
from ctb_store import ChaturbateRunStore, RunContext
from workflow_types import (
    OutcomeStatus,
    ProgressEvent,
    ProgressStatus,
    StepName,
    StepOutcome,
    VerificationRecord,
    VerificationVerdict,
    utc_now_iso,
)


def candidate_digest(urls: Iterable[str]) -> str:
    payload = "".join(f"{item}\n" for item in urls).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# Something went wrong with the browser or the network, so the page was never
# really judged. Retrying later can still succeed, and publishing now would
# claim a verdict that was never reached.
TRANSIENT_REASONS = frozenset(
    {
        "browser_closed",
        "selector_error",
        "navigation_timeout",
        "captcha_timeout",
        "age_gate_blocked",
        "cancelled",
    }
)


# The page was read successfully and simply cannot answer the question. No
# amount of retrying changes that, so the step must be allowed to finish.
def is_transient_unknown(value):
    if not value:
        return False
    if value.get("verdict") != VerificationVerdict.UNKNOWN.value:
        return False
    return value.get("reason_code") in TRANSIENT_REASONS


class ChaturbateVerifier:
    def __init__(
        self,
        store: ChaturbateRunStore,
        context: RunContext,
        verdict_provider: Callable[[str, int], VerificationRecord],
        *,
        stop_token=None,
        progress_callback=None,
        batch_size=5,
        max_attempts=2,
        known_blocked=(),
    ):
        self.store = store
        self.context = context
        self.verdict_provider = verdict_provider
        self.stop_token = stop_token
        self.progress_callback = progress_callback
        self.batch_size = max(1, int(batch_size))
        self.max_attempts = max(1, int(max_attempts))
        # URLs proven blocked by an earlier run (already in the master list).
        # They are not re-navigated, but must still appear in this run's
        # published output so the session result stays complete.
        self.known_blocked = tuple(
            dict.fromkeys(canonical_model_url(item) for item in known_blocked)
        )

    def verify(self, candidates, resume=True) -> StepOutcome:
        started_at = utc_now_iso()
        try:
            canonical = tuple(
                dict.fromkeys(canonical_model_url(item) for item in candidates)
            )
        except ValueError:
            return self._failed(
                started_at,
                "invalid_candidate_url",
                "Candidate list contains an invalid Chaturbate URL.",
            )
        digest = candidate_digest(canonical)
        try:
            checkpoint = (
                self.store.load_checkpoint(self.context) if resume else None
            )
        except (OSError, ValueError):
            return self._failed(
                started_at,
                "resume_checkpoint_mismatch",
                "Saved verification state is incompatible with this run.",
            )
        if checkpoint:
            if (
                checkpoint["candidate_sha256"] != digest
                or tuple(checkpoint["candidate_urls"]) != canonical
            ):
                return self._failed(
                    started_at,
                    "resume_checkpoint_mismatch",
                    "Candidate list changed after the checkpoint was created.",
                )
            verdicts = dict(checkpoint["verdicts"])
            attempts = {
                key: int(value)
                for key, value in checkpoint.get("attempts", {}).items()
            }
        else:
            verdicts = {}
            attempts = {}
            self._checkpoint(canonical, digest, verdicts, attempts)

        # Each round retries whatever is still unknown. Because every round
        # increments the attempt count, this terminates after max_attempts
        # even when a page is deterministically unclassifiable.
        while True:
            pending = [
                url
                for url in canonical
                if self._is_pending(verdicts.get(url), attempts.get(url, 0))
            ]
            if not pending:
                break
            for offset in range(0, len(pending), self.batch_size):
                if self._is_cancelled():
                    self._checkpoint(canonical, digest, verdicts, attempts)
                    return self._cancelled(
                        started_at, len(canonical), verdicts
                    )
                batch = pending[offset : offset + self.batch_size]
                for url in batch:
                    if self._is_cancelled():
                        self._checkpoint(
                            canonical, digest, verdicts, attempts
                        )
                        return self._cancelled(
                            started_at, len(canonical), verdicts
                        )
                    attempt = attempts.get(url, 0) + 1
                    attempts[url] = attempt
                    try:
                        record = self.verdict_provider(url, attempt)
                        if canonical_model_url(record.original_url) != url:
                            raise ValueError("verdict identity mismatch")
                    except Exception as exc:
                        record = VerificationRecord(
                            original_url=url,
                            verdict=VerificationVerdict.UNKNOWN,
                            reason_code="selector_error",
                            attempt=attempt,
                            timestamp=utc_now_iso(),
                            observed_url="",
                            diagnostic_summary=str(exc)[:160],
                            run_id=self.context.run_id,
                        )
                    verdicts[url] = record.to_dict()
                self._checkpoint(canonical, digest, verdicts, attempts)
                processed = sum(
                    not self._is_pending(
                        verdicts.get(url), attempts.get(url, 0)
                    )
                    for url in canonical
                )
                self._emit_progress(processed, len(canonical))

        blocked_by_failure = [
            url
            for url in canonical
            if is_transient_unknown(verdicts.get(url))
        ]
        if blocked_by_failure:
            self._checkpoint(canonical, digest, verdicts, attempts)
            outcome = StepOutcome.incomplete(
                run_id=self.context.run_id,
                generation_id=self.context.generation_id,
                platform=self.context.platform,
                step=StepName.VERIFY,
                started_at=started_at,
                input_count=len(canonical),
                processed_count=len(canonical) - len(blocked_by_failure),
                remaining_count=len(blocked_by_failure),
                error_code="verification_unknown",
                error_message=(
                    "Some pages could not be reached. Resume is available."
                ),
            )
            self.store.record_step(self.context, outcome)
            return outcome
        return self._publish_success(started_at, canonical, verdicts, digest)

    def _publish_success(self, started_at, candidates, verdicts, digest):
        blocked = [
            url
            for url in candidates
            if verdicts.get(url, {}).get("verdict")
            == VerificationVerdict.BLOCKED.value
        ]
        blocked = sorted(dict.fromkeys(blocked + list(self.known_blocked)))
        unresolved = {}
        for url in candidates:
            value = verdicts.get(url, {})
            if value.get("verdict") == VerificationVerdict.UNKNOWN.value:
                reason = value.get("reason_code") or "unknown"
                unresolved[reason] = unresolved.get(reason, 0) + 1
        if unresolved and self.progress_callback:
            summary = ", ".join(
                f"{reason}: {count}"
                for reason, count in sorted(unresolved.items())
            )
            total = sum(unresolved.values())
            self.progress_callback(
                ProgressEvent(
                    run_id=self.context.run_id,
                    generation_id=self.context.generation_id,
                    platform=self.context.platform,
                    step=StepName.VERIFY,
                    phase="verify",
                    completed=len(candidates) - total,
                    total=len(candidates),
                    message=(
                        f"{total} could not be classified ({summary}); "
                        "they were NOT added to the blocked list"
                    ),
                    status=ProgressStatus.RUNNING,
                )
            )
        record = self.store.artifact_lines(
            self.context,
            StepName.VERIFY,
            "FINAL_BLOCKED.txt",
            blocked,
            "verified_final",
        )
        outcome = StepOutcome.succeeded(
            run_id=self.context.run_id,
            generation_id=self.context.generation_id,
            platform=self.context.platform,
            step=StepName.VERIFY,
            started_at=started_at,
            input_count=len(candidates),
            output_count=len(blocked),
            processed_count=len(candidates),
            remaining_count=0,
            artifacts=(record,),
        )
        self.store.record_step(self.context, outcome)
        try:
            self.store.publish_lines(
                self.context, record, "FINAL_BLOCKED.txt"
            )
        except Exception as exc:
            failed = StepOutcome.failed(
                run_id=self.context.run_id,
                generation_id=self.context.generation_id,
                platform=self.context.platform,
                step=StepName.VERIFY,
                started_at=started_at,
                input_count=len(candidates),
                output_count=len(blocked),
                processed_count=len(candidates),
                remaining_count=0,
                error_code="final_publish_failed",
                error_message=str(exc)[:200],
                artifacts=(record,),
            )
            self.store.record_step(self.context, failed)
            return failed
        return outcome

    def _checkpoint(self, candidates, digest, verdicts, attempts):
        self.store.write_checkpoint(
            self.context,
            {
                "candidate_sha256": digest,
                "candidate_urls": list(candidates),
                "verdicts": dict(verdicts),
                "attempts": dict(attempts),
            },
        )

    def _is_pending(self, value, attempts_used=0):
        """Return True while a URL is still worth another navigation.

        An unknown verdict is retried, but only up to max_attempts. Some
        pages -- an offline room, a room demanding login -- can never be
        classified, and treating them as permanently pending made the whole
        step fail forever.
        """
        if not value:
            return True
        if value.get("verdict") != VerificationVerdict.UNKNOWN.value:
            return False
        return attempts_used < self.max_attempts

    def _is_cancelled(self):
        return bool(
            self.stop_token
            and callable(getattr(self.stop_token, "is_cancelled", None))
            and self.stop_token.is_cancelled()
        )

    def _emit_progress(self, completed, total):
        if not self.progress_callback:
            return
        self.progress_callback(
            ProgressEvent(
                run_id=self.context.run_id,
                generation_id=self.context.generation_id,
                platform=self.context.platform,
                step=StepName.VERIFY,
                phase="verify",
                completed=completed,
                total=total,
                message=f"Verified {completed} of {total}",
                status=ProgressStatus.RUNNING,
            )
        )

    def _cancelled(self, started_at, total, verdicts):
        processed = sum(
            item.get("verdict") != VerificationVerdict.UNKNOWN.value
            for item in verdicts.values()
        )
        outcome = StepOutcome.cancelled(
            run_id=self.context.run_id,
            generation_id=self.context.generation_id,
            platform=self.context.platform,
            step=StepName.VERIFY,
            started_at=started_at,
            input_count=total,
            processed_count=processed,
            remaining_count=total - processed,
            error_code="cancelled_by_user",
            error_message="Verification stopped safely. Resume is available.",
        )
        self.store.record_step(self.context, outcome)
        return outcome

    def _failed(self, started_at, code, message):
        outcome = StepOutcome.failed(
            run_id=self.context.run_id,
            generation_id=self.context.generation_id,
            platform=self.context.platform,
            step=StepName.VERIFY,
            started_at=started_at,
            error_code=code,
            error_message=message,
        )
        try:
            self.store.record_step(self.context, outcome)
        except Exception:
            pass
        return outcome
