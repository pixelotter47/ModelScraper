"""Validated, bounded, convergence-proven Stripchat API snapshots.

Treats ``https://go.stripchat.com/api/models`` as an undocumented,
versionless, untrusted external source: every page is schema-validated,
pagination completeness needs a terminal empty page, and only a stable
multi-pass convergence window may publish a model set. A non-success
result exposes no publishable models.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field

from stripchat_classifier import (
    canonical_url_for_username,
    model_lookup_key,
)
from workflow_types import OutcomeStatus, utc_now_iso

DEFAULT_ENDPOINT = "https://go.stripchat.com/api/models"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class StripchatApiError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


class _Cancelled(Exception):
    pass


@dataclass(frozen=True)
class StripchatApiModel:
    username: str

    @property
    def canonical_url(self) -> str:
        return canonical_url_for_username(self.username)

    @property
    def lookup_key(self) -> str:
        return model_lookup_key(self.canonical_url)


@dataclass(frozen=True)
class StripchatApiEnvelope:
    models: tuple[StripchatApiModel, ...]
    count: int
    total: int
    requested_limit: int | None
    ttl: int | float | None


@dataclass(frozen=True)
class StripchatSnapshotPass:
    pass_index: int
    status: OutcomeStatus
    models: tuple[StripchatApiModel, ...]
    page_count: int
    raw_record_count: int
    duplicate_count: int
    reported_totals: tuple[int, ...]
    terminal_empty_page: bool
    started_at: str
    finished_at: str
    error_code: str | None = None

    def metrics(self) -> dict:
        """Sanitized per-pass metrics; never includes usernames."""
        return {
            "transport": "http",
            "endpoint_host": "go.stripchat.com",
            "pass_index": self.pass_index,
            "pages": self.page_count,
            "raw_records": self.raw_record_count,
            "unique_models": len(self.models),
            "duplicate_records": self.duplicate_count,
            "terminal_empty_page": self.terminal_empty_page,
            "reported_total_min": (
                min(self.reported_totals) if self.reported_totals else 0
            ),
            "reported_total_max": (
                max(self.reported_totals) if self.reported_totals else 0
            ),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status.value,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class StripchatSnapshotResult:
    status: OutcomeStatus
    models: tuple[StripchatApiModel, ...]
    quarantined: tuple[StripchatApiModel, ...]
    passes: tuple[StripchatSnapshotPass, ...]
    convergence_window: tuple[int, ...]
    error_code: str | None = None
    warnings: tuple[str, ...] = ()

    def metrics(self) -> dict:
        return {
            "status": self.status.value,
            "error_code": self.error_code,
            "accepted_models": len(self.models),
            "quarantined_models": len(self.quarantined),
            "convergence_window": list(self.convergence_window),
            "passes": [item.metrics() for item in self.passes],
            "warnings": list(self.warnings),
        }


def validate_envelope(
    payload: bytes,
    *,
    requested_limit: int,
    content_type: str | None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> StripchatApiEnvelope:
    if len(payload) > max_bytes:
        raise StripchatApiError("response_too_large")
    if content_type is not None and "json" not in content_type.lower():
        raise StripchatApiError("invalid_content_type", content_type[:60])
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StripchatApiError("invalid_json") from exc
    if not isinstance(value, dict):
        raise StripchatApiError("api_schema_drift", "payload is not an object")
    models_raw = value.get("models")
    if not isinstance(models_raw, list):
        raise StripchatApiError("api_schema_drift", "models missing or not a list")
    count = value.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise StripchatApiError("api_schema_drift", "invalid count")
    if count != len(models_raw):
        raise StripchatApiError(
            "api_schema_drift", "count does not equal len(models)"
        )
    total = value.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise StripchatApiError("api_schema_drift", "invalid total")
    requested = value.get("requestedLimit")
    if requested is not None:
        if (
            isinstance(requested, bool)
            or not isinstance(requested, int)
            or requested < 0
        ):
            raise StripchatApiError("api_schema_drift", "invalid requestedLimit")
        if requested != requested_limit:
            raise StripchatApiError(
                "api_schema_drift", "requestedLimit does not echo the request"
            )
    ttl = value.get("ttl")
    if ttl is not None and (
        isinstance(ttl, bool) or not isinstance(ttl, (int, float))
    ):
        raise StripchatApiError("api_schema_drift", "invalid ttl")
    models = []
    seen_keys: dict[str, str] = {}
    for item in models_raw:
        if not isinstance(item, dict):
            raise StripchatApiError("api_schema_drift", "model is not an object")
        username = item.get("username")
        if not isinstance(username, str) or not username:
            raise StripchatApiError("invalid_username", "missing username")
        try:
            model = StripchatApiModel(username=username)
            key = model.lookup_key
        except ValueError as exc:
            raise StripchatApiError("invalid_username") from exc
        existing = seen_keys.get(key)
        if existing is not None:
            raise StripchatApiError(
                "duplicate_identity_in_page",
                "one page contains a duplicate case-folded identity",
            )
        seen_keys[key] = username
        models.append(model)
    return StripchatApiEnvelope(
        models=tuple(models),
        count=count,
        total=total,
        requested_limit=requested,
        ttl=ttl,
    )


class StripchatApiClient:
    def __init__(
        self,
        client=None,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        clock=None,
        sleep=None,
        jitter=None,
        logger=None,
        stop_token=None,
        page_size: int = 400,
        max_pages: int = 64,
        max_models: int = 50_000,
        page_retries: int = 3,
        minimum_request_interval: float = 0.25,
        max_retry_delay: float = 30.0,
        min_successful_passes: int = 3,
        max_passes: int = 6,
        required_stable_pairs: int = 2,
        jaccard_threshold: float = 0.85,
        count_delta_threshold: float = 0.02,
        maximum_quarantine_ratio: float = 0.12,
        duplicate_ratio_threshold: float = 0.10,
        max_pass_seconds: float = 900.0,
        generation_poll_interval: float = 2.0,
        max_generation_wait: float = 60.0,
    ):
        self._client = client
        self.endpoint = endpoint
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.jitter = jitter or random.random
        self.logger = logger or (lambda _message: None)
        self.stop_token = stop_token
        self.page_size = int(page_size)
        self.max_pages = int(max_pages)
        self.max_models = int(max_models)
        self.page_retries = int(page_retries)
        self.minimum_request_interval = float(minimum_request_interval)
        self.max_retry_delay = float(max_retry_delay)
        self.min_successful_passes = int(min_successful_passes)
        self.max_passes = int(max_passes)
        self.required_stable_pairs = int(required_stable_pairs)
        self.jaccard_threshold = float(jaccard_threshold)
        self.count_delta_threshold = float(count_delta_threshold)
        self.maximum_quarantine_ratio = float(maximum_quarantine_ratio)
        self.duplicate_ratio_threshold = float(duplicate_ratio_threshold)
        self.max_pass_seconds = float(max_pass_seconds)
        self.generation_poll_interval = float(generation_poll_interval)
        self.max_generation_wait = float(max_generation_wait)
        self._last_request_at: float | None = None
        self._last_first_page: tuple | None = None

    # ------------------------------------------------------------------
    # Transport

    def _transport(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=10.0, read=30.0, write=10.0, pool=10.0
                ),
                follow_redirects=False,
                headers={
                    "User-Agent": "ModelScraper/2.0",
                    "Accept": "application/json",
                },
            )
        return self._client

    def _check_stop(self):
        if self.stop_token and self.stop_token.is_cancelled():
            raise _Cancelled()

    def _cancellable_sleep(self, seconds: float):
        remaining = float(seconds)
        while remaining > 0:
            self._check_stop()
            chunk = min(0.25, remaining)
            self.sleep(chunk)
            remaining -= chunk
        self._check_stop()

    def _throttle(self):
        if self._last_request_at is not None:
            elapsed = self.clock() - self._last_request_at
            wait = self.minimum_request_interval - elapsed
            if wait > 0:
                self._cancellable_sleep(wait)
        self._last_request_at = self.clock()

    def _fetch_page(self, offset: int) -> StripchatApiEnvelope:
        """One page with bounded retries; failures raise StripchatApiError."""
        url = f"{self.endpoint}?limit={self.page_size}&offset={offset}"
        attempt = 0
        while True:
            self._check_stop()
            self._throttle()
            attempt += 1
            try:
                response = self._transport().get(url)
            except Exception as exc:
                if attempt > self.page_retries:
                    raise StripchatApiError(
                        "api_http_error", type(exc).__name__
                    ) from exc
                self._backoff(attempt, None)
                continue
            status = int(getattr(response, "status_code", 0))
            if 300 <= status < 400:
                raise StripchatApiError("unexpected_redirect", str(status))
            if status in RETRYABLE_STATUS:
                if attempt > self.page_retries:
                    raise StripchatApiError("api_http_error", str(status))
                self._backoff(attempt, response)
                continue
            if status != 200:
                raise StripchatApiError("api_http_error", str(status))
            self._check_stop()
            headers = getattr(response, "headers", {}) or {}
            content_type = headers.get("content-type") or headers.get(
                "Content-Type"
            )
            return validate_envelope(
                bytes(getattr(response, "content", b"")),
                requested_limit=self.page_size,
                content_type=content_type,
            )

    def _backoff(self, attempt: int, response) -> None:
        delay = None
        if response is not None:
            headers = getattr(response, "headers", {}) or {}
            retry_after = headers.get("retry-after") or headers.get(
                "Retry-After"
            )
            if retry_after:
                try:
                    delay = min(float(retry_after), self.max_retry_delay)
                except ValueError:
                    try:
                        from email.utils import parsedate_to_datetime
                        import datetime as _datetime

                        target = parsedate_to_datetime(retry_after)
                        now = _datetime.datetime.now(
                            _datetime.timezone.utc
                        )
                        delay = min(
                            max(0.0, (target - now).total_seconds()),
                            self.max_retry_delay,
                        )
                    except (TypeError, ValueError):
                        delay = None
        if delay is None:
            ceiling = min(self.max_retry_delay, 0.75 * (2 ** (attempt - 1)))
            delay = ceiling * self.jitter()
        self.logger(
            f"[INFO] API retry {attempt} in {delay:.2f}s"
        )
        self._cancellable_sleep(delay)

    # ------------------------------------------------------------------
    # One complete bounded pass

    def fetch_pass(self, pass_index: int) -> StripchatSnapshotPass:
        started_at = utc_now_iso()
        started_clock = self.clock()
        offsets_seen: set[int] = set()
        page_fingerprints: dict[tuple, int] = {}
        accepted: dict[str, StripchatApiModel] = {}
        totals: list[int] = []
        pages = 0
        raw_records = 0
        duplicates = 0
        terminal_empty = False
        error_code = None
        status = OutcomeStatus.INCOMPLETE
        offset = 0
        try:
            while True:
                self._check_stop()
                if pages >= self.max_pages:
                    error_code = "api_page_cap"
                    break
                if self.clock() - started_clock > self.max_pass_seconds:
                    error_code = "api_time_budget"
                    break
                if offset in offsets_seen:
                    error_code = "pagination_no_progress"
                    break
                offsets_seen.add(offset)
                envelope = self._fetch_page(offset)
                pages += 1
                if offset == 0:
                    # Identifies the cache generation this pass sampled.
                    self._last_first_page = tuple(
                        model.lookup_key for model in envelope.models
                    )
                totals.append(envelope.total)
                if envelope.count == 0:
                    if offset + self.page_size < envelope.total:
                        error_code = "premature_empty_page"
                        break
                    terminal_empty = True
                    status = OutcomeStatus.SUCCEEDED
                    break
                keys = tuple(model.lookup_key for model in envelope.models)
                # Only a repeated *full* page proves the server ignored the
                # offset. A short page means the feed ended inside this
                # window, and its population moves by a few dozen models
                # between requests, so the tail gets asked again from the
                # new offset and comes back overlapping - sometimes
                # identical. Calling that a loop failed whole passes against
                # a healthy feed, and one failed pass costs a run its
                # convergence budget. A short-page loop stays bounded by the
                # page cap.
                if envelope.count >= self.page_size:
                    ordered_fp = ("ordered",) + keys
                    set_fp = ("set",) + tuple(sorted(keys))
                    for fingerprint in (ordered_fp, set_fp):
                        previous = page_fingerprints.get(fingerprint)
                        if previous is not None and previous != offset:
                            raise StripchatApiError("api_page_loop")
                        page_fingerprints[fingerprint] = offset
                for model in envelope.models:
                    raw_records += 1
                    existing = accepted.get(model.lookup_key)
                    if existing is not None:
                        if existing.username != model.username:
                            raise StripchatApiError("api_identity_collision")
                        duplicates += 1
                    else:
                        accepted[model.lookup_key] = model
                if len(accepted) > self.max_models:
                    error_code = "api_model_cap"
                    break
                offset += envelope.count
        except StripchatApiError as exc:
            status = OutcomeStatus.INCOMPLETE
            error_code = exc.code
            self.logger(f"[WARN] API pass {pass_index} failed: {exc.code}")
        except _Cancelled:
            status = OutcomeStatus.CANCELLED
            error_code = "api_cancelled"
        if status is OutcomeStatus.SUCCEEDED:
            if not accepted:
                status = OutcomeStatus.INCOMPLETE
                error_code = "empty_snapshot"
            elif raw_records and duplicates / raw_records > (
                self.duplicate_ratio_threshold
            ):
                status = OutcomeStatus.INCOMPLETE
                error_code = "api_duplicate_ratio_exceeded"
        models = (
            tuple(accepted[key] for key in sorted(accepted))
            if status is OutcomeStatus.SUCCEEDED
            else ()
        )
        return StripchatSnapshotPass(
            pass_index=pass_index,
            status=status,
            models=models,
            page_count=pages,
            raw_record_count=raw_records,
            duplicate_count=duplicates,
            reported_totals=tuple(totals),
            terminal_empty_page=terminal_empty,
            started_at=started_at,
            finished_at=utc_now_iso(),
            error_code=error_code,
        )

    # ------------------------------------------------------------------
    # Multi-pass convergence

    def _first_page_fingerprint(self):
        """Cheap probe of the feed's current cache generation."""
        envelope = self._fetch_page(0)
        return tuple(model.lookup_key for model in envelope.models)

    def _await_new_generation(self, previous) -> bool:
        """Block until the feed serves a different cache generation.

        The endpoint answers from a short-lived cache, so two passes taken
        inside one generation return byte-identical data. Counting that as
        two agreeing observations would be a false stability proof, so a
        pass only starts once the generation has actually rolled.
        """
        if previous is None or self.max_generation_wait <= 0:
            return True
        started = self.clock()
        while self.clock() - started <= self.max_generation_wait:
            self._check_stop()
            try:
                current = self._first_page_fingerprint()
            except StripchatApiError:
                # Let the full pass surface the error with its own code.
                return True
            if current != previous:
                return True
            self._cancellable_sleep(self.generation_poll_interval)
        return False

    def collect_snapshot(self) -> StripchatSnapshotResult:
        passes: list[StripchatSnapshotPass] = []
        consecutive: list[StripchatSnapshotPass] = []
        warnings: list[str] = []
        extra_confirmation_used = False
        pass_index = 0
        generation = None
        while pass_index < self.max_passes:
            pass_index += 1
            if pass_index > 1:
                try:
                    if not self._await_new_generation(generation):
                        warnings.append(
                            "the feed did not refresh within the generation "
                            "wait; passes may not be independent"
                        )
                except _Cancelled:
                    return StripchatSnapshotResult(
                        status=OutcomeStatus.CANCELLED,
                        models=(),
                        quarantined=(),
                        passes=tuple(passes),
                        convergence_window=(),
                        error_code="api_cancelled",
                    )
            result = self.fetch_pass(pass_index)
            passes.append(result)
            generation = self._last_first_page or generation
            if result.status is OutcomeStatus.CANCELLED:
                return StripchatSnapshotResult(
                    status=OutcomeStatus.CANCELLED,
                    models=(),
                    quarantined=(),
                    passes=tuple(passes),
                    convergence_window=(),
                    error_code="api_cancelled",
                )
            if result.status is not OutcomeStatus.SUCCEEDED:
                consecutive = []
                continue
            consecutive.append(result)
            if len(consecutive) < self.min_successful_passes:
                continue
            window = consecutive[-self.min_successful_passes :]
            if not self._window_is_stable(window):
                continue
            accepted, quarantined, drift_warnings = self._accepted_from_window(
                window
            )
            warnings.extend(drift_warnings)
            union_count = len(accepted) + len(quarantined)
            if union_count and (
                len(quarantined) / union_count
                > self.maximum_quarantine_ratio
            ):
                if (
                    not extra_confirmation_used
                    and pass_index < self.max_passes
                ):
                    extra_confirmation_used = True
                    self.logger(
                        "[INFO] Quarantine ratio high; collecting one "
                        "confirmation pass."
                    )
                    continue
                return StripchatSnapshotResult(
                    status=OutcomeStatus.INCOMPLETE,
                    models=(),
                    quarantined=tuple(quarantined),
                    passes=tuple(passes),
                    convergence_window=tuple(
                        item.pass_index for item in window
                    ),
                    error_code="excessive_quarantine",
                    warnings=tuple(warnings),
                )
            return StripchatSnapshotResult(
                status=OutcomeStatus.SUCCEEDED,
                models=tuple(accepted),
                quarantined=tuple(quarantined),
                passes=tuple(passes),
                convergence_window=tuple(item.pass_index for item in window),
                warnings=tuple(warnings),
            )
        return StripchatSnapshotResult(
            status=OutcomeStatus.INCOMPLETE,
            models=(),
            quarantined=(),
            passes=tuple(passes),
            convergence_window=(),
            error_code="api_snapshot_unstable",
        )

    def _window_is_stable(self, window) -> bool:
        pairs = 0
        for first, second in zip(window, window[1:]):
            first_keys = {model.lookup_key for model in first.models}
            second_keys = {model.lookup_key for model in second.models}
            if not first_keys or not second_keys:
                return False
            union = len(first_keys | second_keys)
            intersection = len(first_keys & second_keys)
            jaccard = intersection / union if union else 0.0
            larger = max(len(first_keys), len(second_keys))
            delta = (
                abs(len(first_keys) - len(second_keys)) / larger
                if larger
                else 1.0
            )
            if jaccard < self.jaccard_threshold or delta > (
                self.count_delta_threshold
            ):
                return False
            pairs += 1
        return pairs >= self.required_stable_pairs

    def _accepted_from_window(self, window):
        counts: dict[str, int] = {}
        latest_spelling: dict[str, str] = {}
        spelling_drift = 0
        for item in window:
            for model in item.models:
                counts[model.lookup_key] = counts.get(model.lookup_key, 0) + 1
                previous = latest_spelling.get(model.lookup_key)
                if previous is not None and previous != model.username:
                    spelling_drift += 1
                latest_spelling[model.lookup_key] = model.username
        accepted = []
        quarantined = []
        for key in sorted(counts):
            model = StripchatApiModel(username=latest_spelling[key])
            if counts[key] >= 2:
                accepted.append(model)
            else:
                quarantined.append(model)
        warnings = []
        if spelling_drift:
            warnings.append(
                f"{spelling_drift} identities changed spelling between passes"
            )
        return accepted, quarantined, warnings
