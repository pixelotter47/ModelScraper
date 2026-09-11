"""Validated direct-HTTP Chaturbate snapshots with adaptive stability."""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass

import httpx

from ctb_classifier import canonical_model_url
from workflow_types import OutcomeStatus, utc_now_iso


DEFAULT_ENDPOINT = (
    "https://chaturbate.com/api/public/affiliates/onlinerooms/"
)


@dataclass(frozen=True)
class ModelObservation:
    url: str
    username: str
    seconds_online: int
    country: str
    location: str
    languages: str
    pass_index: int
    observed_at: str


@dataclass(frozen=True)
class SnapshotPass:
    pass_index: int
    status: OutcomeStatus
    reported_counts: tuple[int, ...]
    page_count: int
    models: tuple[ModelObservation, ...]
    started_at: str
    finished_at: str
    transport: str
    error_code: str | None = None


@dataclass(frozen=True)
class SnapshotResult:
    status: OutcomeStatus
    models: tuple[ModelObservation, ...]
    passes: tuple[SnapshotPass, ...]
    quarantined: tuple[ModelObservation, ...] = ()
    error_code: str | None = None


class ChaturbateApiClient:
    def __init__(
        self,
        client=None,
        *,
        endpoint=DEFAULT_ENDPOINT,
        clock=None,
        sleep=None,
        logger=None,
        stop_token=None,
        limit=500,
        max_pages=100,
        page_retries=2,
        jitter=None,
    ):
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
        )
        self.endpoint = endpoint
        self.clock = clock
        self.sleep = sleep or __import__("time").sleep
        self.logger = logger or (lambda _message: None)
        self.stop_token = stop_token
        self.limit = max(1, int(limit))
        self.max_pages = max(1, int(max_pages))
        self.page_retries = max(0, int(page_retries))
        self.jitter = jitter or (lambda: random.uniform(0, 0.15))

    def fetch_pass(self, pass_index):
        started_at = self._now()
        observations = {}
        reported = []
        fingerprints = set()
        offset = 0
        page_count = 0
        while page_count < self.max_pages:
            if self._cancelled():
                return self._pass(
                    pass_index,
                    OutcomeStatus.CANCELLED,
                    reported,
                    page_count,
                    observations,
                    started_at,
                    "cancelled",
                )
            response = None
            error_code = None
            for attempt in range(self.page_retries + 1):
                try:
                    response = self.client.get(
                        self.endpoint,
                        params=[
                            ("wm", "GXDW2"),
                            ("client_ip", "request_ip"),
                            ("gender", "f"),
                            ("gender", "c"),
                            ("format", "json"),
                            ("limit", str(self.limit)),
                            ("offset", str(offset)),
                        ],
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        error_code = f"http_{response.status_code}"
                        if attempt < self.page_retries:
                            self._backoff(response, attempt)
                            if self._cancelled():
                                return self._pass(
                                    pass_index,
                                    OutcomeStatus.CANCELLED,
                                    reported,
                                    page_count,
                                    observations,
                                    started_at,
                                    "cancelled",
                                )
                            continue
                    response.raise_for_status()
                    break
                except (httpx.HTTPError, ValueError):
                    response = None
                    error_code = error_code or "network_error"
                    if attempt < self.page_retries:
                        self._sleep_cancelable(2**attempt + self.jitter())
                        if self._cancelled():
                            return self._pass(
                                pass_index,
                                OutcomeStatus.CANCELLED,
                                reported,
                                page_count,
                                observations,
                                started_at,
                                "cancelled",
                            )
            if response is None:
                return self._pass(
                    pass_index,
                    OutcomeStatus.INCOMPLETE,
                    reported,
                    page_count,
                    observations,
                    started_at,
                    error_code or "network_error",
                )
            try:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("payload")
                results = payload.get("results")
                if not isinstance(results, list):
                    raise ValueError("results")
            except (ValueError, TypeError):
                return self._pass(
                    pass_index,
                    OutcomeStatus.INCOMPLETE,
                    reported,
                    page_count,
                    observations,
                    started_at,
                    "invalid_json_schema",
                )
            for key in ("count", "total_count", "total"):
                value = payload.get(key)
                if isinstance(value, int) and value >= 0:
                    reported.append(value)
            page_urls = []
            for raw in results:
                if not isinstance(raw, dict):
                    return self._pass(
                        pass_index,
                        OutcomeStatus.INCOMPLETE,
                        reported,
                        page_count,
                        observations,
                        started_at,
                        "invalid_model_schema",
                    )
                username = str(raw.get("username") or "").strip()
                if not username:
                    continue
                try:
                    url = canonical_model_url(
                        f"https://chaturbate.com/{username}/"
                    )
                except ValueError:
                    continue
                page_urls.append(url)
                observations[url] = ModelObservation(
                    url=url,
                    username=username,
                    seconds_online=max(
                        0, self._safe_int(raw.get("seconds_online"))
                    ),
                    country=str(raw.get("country") or "")[:8].upper(),
                    location=str(raw.get("location") or "")[:160],
                    languages=str(
                        raw.get("spoken_languages")
                        or raw.get("languages")
                        or ""
                    )[:160],
                    pass_index=pass_index,
                    observed_at=self._now(),
                )
            fingerprint = hashlib.sha256(
                "\n".join(sorted(page_urls)).encode("utf-8")
            ).hexdigest()
            if fingerprint in fingerprints and results:
                return self._pass(
                    pass_index,
                    OutcomeStatus.INCOMPLETE,
                    reported,
                    page_count,
                    observations,
                    started_at,
                    "repeated_page",
                )
            fingerprints.add(fingerprint)
            page_count += 1
            declared = max(reported) if reported else None
            if (
                not results
                or len(results) < self.limit
                or declared is not None
                and len(observations) >= declared
            ):
                return self._pass(
                    pass_index,
                    OutcomeStatus.SUCCEEDED,
                    reported,
                    page_count,
                    observations,
                    started_at,
                    None,
                )
            offset += self.limit
        return self._pass(
            pass_index,
            OutcomeStatus.INCOMPLETE,
            reported,
            page_count,
            observations,
            started_at,
            "max_pages_exceeded",
        )

    def collect_adaptive(
        self,
        *,
        min_passes=2,
        max_passes=5,
        jaccard_threshold=0.985,
        count_delta_threshold=0.02,
        required_stable_pairs=1,
    ):
        passes = []
        stable_pairs = 0
        seen = {}
        previous = None
        confirmation_due = False
        for index in range(1, max(1, int(max_passes)) + 1):
            current = self.fetch_pass(index)
            passes.append(current)
            if current.status is OutcomeStatus.CANCELLED:
                return SnapshotResult(
                    OutcomeStatus.CANCELLED,
                    (),
                    tuple(passes),
                    error_code=current.error_code,
                )
            if current.status is not OutcomeStatus.SUCCEEDED:
                stable_pairs = 0
                previous = None
                continue
            current_set = {item.url for item in current.models}
            if not current_set:
                previous = current
                stable_pairs = 0
                continue
            for item in current.models:
                seen.setdefault(item.url, []).append(item)
            if previous is not None:
                previous_set = {item.url for item in previous.models}
                union = current_set | previous_set
                jaccard = (
                    len(current_set & previous_set) / len(union)
                    if union
                    else 0.0
                )
                denominator = max(len(previous_set), len(current_set), 1)
                count_delta = (
                    abs(len(previous_set) - len(current_set)) / denominator
                )
                if (
                    jaccard >= jaccard_threshold
                    and count_delta <= count_delta_threshold
                ):
                    stable_pairs += 1
                else:
                    stable_pairs = 0
            previous = current
            complete_count = sum(
                item.status is OutcomeStatus.SUCCEEDED for item in passes
            )
            if (
                complete_count >= min_passes
                and stable_pairs >= required_stable_pairs
            ):
                singletons = [url for url, items in seen.items() if len(items) == 1]
                if singletons and index < max_passes and not confirmation_due:
                    confirmation_due = True
                    continue
                break
        if stable_pairs < required_stable_pairs:
            return SnapshotResult(
                OutcomeStatus.INCOMPLETE,
                (),
                tuple(passes),
                error_code="snapshot_not_stable",
            )
        stable = []
        quarantined = []
        for _url, items in sorted(seen.items()):
            latest = items[-1]
            if len(items) >= 2:
                stable.append(
                    ModelObservation(
                        **{
                            **latest.__dict__,
                            "seconds_online": max(
                                item.seconds_online for item in items
                            ),
                        }
                    )
                )
            else:
                quarantined.append(latest)
        if not stable:
            return SnapshotResult(
                OutcomeStatus.INCOMPLETE,
                (),
                tuple(passes),
                tuple(quarantined),
                "empty_stable_snapshot",
            )
        return SnapshotResult(
            OutcomeStatus.SUCCEEDED,
            tuple(stable),
            tuple(passes),
            tuple(quarantined),
        )

    def _pass(
        self,
        index,
        status,
        reported,
        page_count,
        observations,
        started_at,
        error_code,
    ):
        return SnapshotPass(
            pass_index=index,
            status=status,
            reported_counts=tuple(reported),
            page_count=page_count,
            models=tuple(
                observations[key] for key in sorted(observations)
            ),
            started_at=started_at,
            finished_at=self._now(),
            transport="http",
            error_code=error_code,
        )

    def _backoff(self, response, attempt):
        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = min(30.0, max(0.0, float(retry_after)))
        except ValueError:
            delay = 2**attempt + self.jitter()
        self._sleep_cancelable(delay)

    def _sleep_cancelable(self, seconds):
        remaining = max(0.0, float(seconds))
        while remaining > 0 and not self._cancelled():
            portion = min(0.25, remaining)
            self.sleep(portion)
            remaining -= portion

    def _cancelled(self):
        return bool(
            self.stop_token
            and self.stop_token.is_cancelled()
        )

    def _now(self):
        if self.clock and hasattr(self.clock, "now"):
            value = self.clock.now()
            return value.isoformat() if hasattr(value, "isoformat") else str(value)
        return utc_now_iso()

    @staticmethod
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
