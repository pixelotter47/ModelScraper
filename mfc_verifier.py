"""Tri-state MyFreeCams live verification.

Step 4 asks a single question about each candidate: with the VPN off, can we
still prove the model is broadcasting? A model that is provably streaming was
genuinely hidden from the local list, and a banned notice says so outright.
Anything else is either a plain negative (the model is simply not on air) or
no answer at all (throttled, tab died, navigation failed).

Keeping those last two apart is the whole point of this module. Treating "we
never got an answer" as "not blocked" silently loses models that are blocked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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

PLATFORM = "MyFreeCams"


# The page never produced a verdict. Retrying can still reach one, so these
# must not be counted as evidence that the model is reachable.
TRANSIENT_REASONS = frozenset(
    {
        "rate_limited",
        "navigation_error",
        "tab_failed",
        "browser_closed",
        "provider_error",
    }
)


def is_transient_unknown(value) -> bool:
    if not value:
        return False
    if value.get("verdict") != VerificationVerdict.UNKNOWN.value:
        return False
    return value.get("reason_code") in TRANSIENT_REASONS


@dataclass(frozen=True)
class MFCVerificationResult:
    """What the run proved, plus the outcome the caller reports upstream."""

    outcome: StepOutcome
    confirmed: tuple[str, ...] = ()
    unresolved: dict = field(default_factory=dict)
    # Every candidate counted by the reason it was judged on, so a run that
    # confirms nothing can still say what it saw.
    breakdown: dict = field(default_factory=dict)

    @property
    def publishable(self) -> bool:
        return self.outcome.status is OutcomeStatus.SUCCEEDED


class MFCLiveVerifier:
    def __init__(
        self,
        verdict_provider,
        *,
        run_id,
        generation_id,
        stop_token=None,
        batch_size=3,
        max_attempts=2,
        progress_callback=None,
    ):
        self.verdict_provider = verdict_provider
        self.run_id = run_id
        self.generation_id = generation_id
        self.stop_token = stop_token
        self.batch_size = max(1, int(batch_size))
        self.max_attempts = max(1, int(max_attempts))
        self.progress_callback = progress_callback

    def verify(self, candidates) -> MFCVerificationResult:
        started_at = utc_now_iso()
        canonical = tuple(dict.fromkeys(candidates))
        verdicts: dict[str, dict] = {}
        attempts: dict[str, int] = {}

        # Each round retries whatever is still unanswered. Because every round
        # increments the attempt count, this terminates after max_attempts even
        # when a page keeps failing.
        attempt = 0
        while True:
            pending = [
                url
                for url in canonical
                if self._is_pending(verdicts.get(url), attempts.get(url, 0))
            ]
            if not pending:
                break
            attempt += 1
            for offset in range(0, len(pending), self.batch_size):
                if self._is_cancelled():
                    return self._cancelled(started_at, canonical, verdicts, attempts)
                batch = pending[offset : offset + self.batch_size]
                for url in batch:
                    attempts[url] = attempt
                records = self._judge_batch(batch, attempt)
                for url in batch:
                    record = records.get(url) or self._unanswered(
                        url, attempt, "provider_error", "no verdict was returned"
                    )
                    verdicts[url] = record.to_dict()
                self._emit_progress(canonical, verdicts, attempts)

        return self._finish(started_at, canonical, verdicts)

    def _judge_batch(self, urls, attempt) -> dict[str, VerificationRecord]:
        """Hand a whole batch to the browser layer, which may check in parallel."""
        try:
            records = self.verdict_provider(list(urls), attempt)
        except Exception as exc:
            return {
                url: self._unanswered(url, attempt, "provider_error", str(exc))
                for url in urls
            }
        if isinstance(records, dict):
            return records
        try:
            return {record.original_url: record for record in records or ()}
        except (AttributeError, TypeError):
            return {}

    def _unanswered(self, url, attempt, reason, detail="") -> VerificationRecord:
        return VerificationRecord(
            original_url=url,
            verdict=VerificationVerdict.UNKNOWN,
            reason_code=reason,
            attempt=attempt,
            timestamp=utc_now_iso(),
            observed_url="",
            diagnostic_summary=str(detail)[:160],
            run_id=self.run_id,
        )

    def _is_pending(self, value, attempt_count) -> bool:
        if not value:
            return True
        if not is_transient_unknown(value):
            return False
        return attempt_count < self.max_attempts

    def _is_cancelled(self) -> bool:
        # A token that cannot answer the question is not an answer of "yes".
        return bool(
            self.stop_token
            and callable(getattr(self.stop_token, "is_cancelled", None))
            and self.stop_token.is_cancelled()
        )

    @staticmethod
    def _confirmed(canonical, verdicts) -> tuple[str, ...]:
        return tuple(
            url
            for url in canonical
            if verdicts.get(url, {}).get("verdict")
            == VerificationVerdict.BLOCKED.value
        )

    @staticmethod
    def _breakdown(canonical, verdicts) -> dict:
        counts: dict[str, int] = {}
        for url in canonical:
            reason = verdicts.get(url, {}).get("reason_code") or "unanswered"
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    @staticmethod
    def _unresolved(canonical, verdicts) -> dict:
        counts: dict[str, int] = {}
        for url in canonical:
            value = verdicts.get(url, {})
            if value.get("verdict") != VerificationVerdict.UNKNOWN.value:
                continue
            reason = value.get("reason_code") or "unknown"
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def _resolved_count(self, canonical, verdicts, attempts) -> int:
        return sum(
            not self._is_pending(verdicts.get(url), attempts.get(url, 0))
            for url in canonical
        )

    def _emit_progress(self, canonical, verdicts, attempts) -> None:
        if not self.progress_callback:
            return
        completed = self._resolved_count(canonical, verdicts, attempts)
        self.progress_callback(
            ProgressEvent(
                run_id=self.run_id,
                generation_id=self.generation_id,
                platform=PLATFORM,
                step=StepName.VERIFY,
                phase="verify",
                completed=completed,
                total=len(canonical),
                message=f"Verified {completed}/{len(canonical)} candidates.",
                status=ProgressStatus.RUNNING,
            )
        )

    def _base(self, started_at, canonical):
        return {
            "run_id": self.run_id,
            "generation_id": self.generation_id,
            "platform": PLATFORM,
            "step": StepName.VERIFY,
            "started_at": started_at,
            "input_count": len(canonical),
        }

    def _cancelled(self, started_at, canonical, verdicts, attempts):
        processed = self._resolved_count(canonical, verdicts, attempts)
        outcome = StepOutcome.cancelled(
            **self._base(started_at, canonical),
            processed_count=processed,
            remaining_count=len(canonical) - processed,
            error_code="cancelled_by_user",
        )
        return MFCVerificationResult(
            outcome=outcome,
            confirmed=self._confirmed(canonical, verdicts),
            unresolved=self._unresolved(canonical, verdicts),
            breakdown=self._breakdown(canonical, verdicts),
        )

    def _finish(self, started_at, canonical, verdicts):
        confirmed = self._confirmed(canonical, verdicts)
        unresolved = self._unresolved(canonical, verdicts)
        unanswered = [
            url for url in canonical if is_transient_unknown(verdicts.get(url))
        ]
        if unanswered:
            outcome = StepOutcome.incomplete(
                **self._base(started_at, canonical),
                output_count=len(confirmed),
                processed_count=len(canonical) - len(unanswered),
                remaining_count=len(unanswered),
                error_code="verification_unknown",
                error_message=(
                    f"{len(unanswered)} candidates could not be reached; "
                    "results were not published."
                ),
            )
        else:
            outcome = StepOutcome.succeeded(
                **self._base(started_at, canonical),
                output_count=len(confirmed),
                processed_count=len(canonical),
            )
        return MFCVerificationResult(
            outcome=outcome,
            confirmed=confirmed,
            unresolved=unresolved,
            breakdown=self._breakdown(canonical, verdicts),
        )
