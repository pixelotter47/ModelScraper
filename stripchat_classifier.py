"""Pure Stripchat URL canonicalization and identity policy.

No Selenium, no HTTP, no filesystem: everything here is a pure function so
the grammar and (in a later phase) the page decision table are exhaustively
testable. Version strings below participate in every config digest; bump
them whenever the observable behavior changes.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

CANONICALIZER_VERSION = "sc-url-1"
# sc-page-2: the offline/not-found evidence is read from the classes the
# live pages actually use, and an unfinished load no longer masks a page
# that already carries decisive evidence.
CLASSIFIER_VERSION = "sc-page-2"
API_CONTRACT_VERSION = "sc-api-2"

_HOSTS = {"stripchat.com", "www.stripchat.com"}
# Observed-safe ASCII handle characters with a deliberately generous bound.
_HANDLE = re.compile(r"^[A-Za-z0-9_@-]{1,64}$")


def canonical_model_url(raw: str) -> str:
    """Strict boundary validator for stored and source Stripchat URLs.

    Accepts exactly ``https://(www.)stripchat.com/<handle>[/]`` and returns
    ``https://stripchat.com/<handle>/`` with the handle's case preserved.
    Anything else raises ``ValueError("invalid_url")``.
    """
    text = str(raw).strip("\r\n\t ")
    if not text or any(ch.isspace() for ch in text) or "\\" in text:
        raise ValueError("invalid_url")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ValueError("invalid_url") from exc
    if parts.scheme.lower() != "https":
        raise ValueError("invalid_url")
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid_url") from exc
    if hostname is None or hostname.lower() not in _HOSTS:
        raise ValueError("invalid_url")
    if parts.username is not None or parts.password is not None:
        raise ValueError("invalid_url")
    if port is not None or ":" in (parts.netloc.split("@")[-1]):
        raise ValueError("invalid_url")
    if parts.query or parts.fragment:
        raise ValueError("invalid_url")
    # Percent-encoding can smuggle separators; the handle grammar is plain
    # ASCII with no '%', so reject any encoded byte outright.
    handle = _extract_handle(parts.path)
    return f"https://stripchat.com/{handle}/"


def _extract_handle(path: str) -> str:
    if "%" in path:
        raise ValueError("invalid_url")
    segments = path.split("/")
    # Expected shapes: ['', 'handle'] or ['', 'handle', ''].
    if len(segments) == 3 and segments[2] == "":
        segments = segments[:2]
    if len(segments) != 2 or segments[0] != "":
        raise ValueError("invalid_url")
    handle = segments[1]
    if not _HANDLE.fullmatch(handle):
        raise ValueError("invalid_url")
    return handle


def canonical_url_for_username(username: str) -> str:
    """Round-trip an API username through the URL builder and validator."""
    return canonical_model_url(f"https://stripchat.com/{username}/")


def model_lookup_key(canonical_url: str) -> str:
    """Case-insensitive identity key for set comparisons.

    Callers must prove there are no two distinct canonical URLs with one
    key before trusting case-folded comparisons; use
    :func:`ensure_no_lookup_collisions`.
    """
    return str(canonical_url).lower()


def ensure_no_lookup_collisions(urls) -> dict[str, str]:
    """Map lookup keys to canonical URLs; a collision is a hard failure."""
    mapping: dict[str, str] = {}
    for url in urls:
        key = model_lookup_key(url)
        existing = mapping.get(key)
        if existing is not None and existing != url:
            raise ValueError("identity_key_collision")
        mapping[key] = url
    return mapping


def observed_model_identity(current_url: str) -> str:
    """Identity of a browser-observed URL, tolerating query/fragment.

    The browser may append tracking parameters to the same profile; this
    helper validates scheme, host, authority, and path grammar, then drops
    only the query/fragment. It never replaces the strict persisted
    canonicalizer.
    """
    text = str(current_url).strip()
    if not text or any(ch.isspace() for ch in text) or "\\" in text:
        raise ValueError("invalid_observed_url")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ValueError("invalid_observed_url") from exc
    if parts.scheme.lower() != "https":
        raise ValueError("invalid_observed_url")
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid_observed_url") from exc
    if hostname is None or hostname.lower() not in _HOSTS:
        raise ValueError("invalid_observed_url")
    if parts.username is not None or parts.password is not None or port is not None:
        raise ValueError("invalid_observed_url")
    try:
        handle = _extract_handle(parts.path)
    except ValueError as exc:
        raise ValueError("invalid_observed_url") from exc
    return f"https://stripchat.com/{handle}/"


# ---------------------------------------------------------------------------
# Page evidence classification (pure; no Selenium imports)

from dataclasses import dataclass as _dataclass

from workflow_types import (
    VerificationRecord,
    VerificationVerdict,
    utc_now_iso,
)

# Displayed-text semantics. Class names are structural hints only; the
# decision table interprets what the page says, never what a CSS class is
# called. The live conflict probe (hidden text inside account-disabled-*
# classes) is why.
HIDDEN_TEXT_ALLOWLIST = (
    "account hidden",
    "account has been hidden",
    "account is hidden",
)
DISABLED_TEXT_ALLOWLIST = (
    "account disabled",
    "account has been disabled",
    "account is disabled",
    "account deleted",
    "model deleted",
)

# Transient unknown reasons: retrying a later epoch can still classify.
TRANSIENT_REASONS = frozenset(
    {
        "missing_positive_signal",
        "class_semantics_ambiguous",
        "class_player_conflict",
        "disabled_player_conflict",
        "hidden_player_conflict",
        "hidden_disabled_conflict",
        "homepage_redirect",
        "identity_mismatch",
        "unexpected_stripchat_redirect",
        "external_redirect",
        "invalid_observed_url",
        "page_not_ready",
        "blank_page",
        "browser_lost",
        "navigation_timeout",
        "selector_error",
        "captcha_required",
        "captcha_timeout",
        "consent_gate_blocked",
        "cancelled",
        "profile_in_use",
    }
)

# Non-target unknowns that end a candidate after bounded confirmation.
CONFIRMABLE_NONTARGET_REASONS = frozenset(
    {"room_offline", "not_found", "login_required", "room_not_viewable"}
)
# Non-target unknowns that are terminal on first sight.
IMMEDIATE_NONTARGET_REASONS = frozenset({"account_disabled_or_deleted"})
NONTARGET_CONFIRMATION_ATTEMPTS = 2


def normalize_notice_text(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _matches_allowlist(texts, allowlist) -> bool:
    for raw in texts:
        text = normalize_notice_text(raw)
        if any(phrase in text for phrase in allowlist):
            return True
    return False


@_dataclass(frozen=True)
class StripchatPageObservation:
    original_url: str
    current_url: str
    ready_state: str = ""
    account_notice_texts: tuple[str, ...] = ()
    visible_hidden_selector_count: int = 0
    visible_disabled_selector_count: int = 0
    video_element_count: int = 0
    player_container_count: int = 0
    captcha_detected: bool = False
    login_gate_detected: bool = False
    consent_gate_detected: bool = False
    offline_notice_detected: bool = False
    not_found_notice_detected: bool = False
    # What the player shutter says when it covers the stream instead of
    # playing it ("... is having fun in Private show"). Read for the
    # decision only; the wording never reaches the checkpoint.
    shutter_status_text: str = ""
    body_readable: bool = True
    navigation_error: str | None = None

    @property
    def has_hidden_text(self) -> bool:
        return _matches_allowlist(
            self.account_notice_texts, HIDDEN_TEXT_ALLOWLIST
        )

    @property
    def has_disabled_text(self) -> bool:
        return _matches_allowlist(
            self.account_notice_texts, DISABLED_TEXT_ALLOWLIST
        )

    @property
    def has_shutter_status(self) -> bool:
        return bool(normalize_notice_text(self.shutter_status_text))

    def diagnostic(self) -> str:
        return (
            f"ready={self.ready_state or 'none'};"
            f" hidden_notice={int(self.has_hidden_text)};"
            f" disabled_notice={int(self.has_disabled_text)};"
            f" hidden_classes={self.visible_hidden_selector_count};"
            f" disabled_classes={self.visible_disabled_selector_count};"
            f" video={self.video_element_count};"
            f" player={self.player_container_count};"
            f" shutter={int(self.has_shutter_status)}"
        )


def classify_stripchat_page(
    observation: StripchatPageObservation,
    *,
    attempt: int = 1,
    run_id: str = "",
    generation_id: str = "",
    epoch: int = 0,
    epoch_attempt: int = 1,
    timestamp: str | None = None,
) -> VerificationRecord:
    """Deterministic tri-state decision; there is no default-clean branch."""
    original_url = canonical_model_url(observation.original_url)

    def record(verdict, reason, *, terminal=False, retryable=None):
        if retryable is None:
            retryable = reason in TRANSIENT_REASONS and not terminal
        return VerificationRecord(
            original_url=original_url,
            verdict=verdict,
            reason_code=reason,
            attempt=max(1, int(attempt)),
            timestamp=timestamp or utc_now_iso(),
            observed_url=str(observation.current_url or "")[:300],
            diagnostic_summary=observation.diagnostic()[:240],
            run_id=run_id,
            generation_id=generation_id,
            epoch=epoch,
            epoch_attempt=epoch_attempt,
            retryable=bool(retryable),
            terminal=bool(terminal),
        )

    def confirmable(reason):
        terminal = attempt >= NONTARGET_CONFIRMATION_ATTEMPTS
        return record(
            VerificationVerdict.UNKNOWN,
            reason,
            terminal=terminal,
            retryable=not terminal,
        )

    if observation.navigation_error:
        reason = str(observation.navigation_error)[:60]
        return record(VerificationVerdict.UNKNOWN, reason)
    if observation.captcha_detected:
        return record(VerificationVerdict.UNKNOWN, "captcha_required")
    if observation.consent_gate_detected:
        return record(VerificationVerdict.UNKNOWN, "consent_gate_blocked")
    try:
        observed_identity = observed_model_identity(observation.current_url)
    except ValueError:
        stripped = str(observation.current_url or "").split("?")[0]
        stripped = stripped.split("#")[0].rstrip("/")
        if stripped.lower() in (
            "https://stripchat.com",
            "https://www.stripchat.com",
        ):
            return record(VerificationVerdict.UNKNOWN, "homepage_redirect")
        lowered = str(observation.current_url or "").lower()
        if lowered.startswith(
            ("https://stripchat.com/", "https://www.stripchat.com/")
        ):
            if "/login" in lowered:
                return confirmable("login_required")
            return record(
                VerificationVerdict.UNKNOWN, "unexpected_stripchat_redirect"
            )
        if lowered.startswith(("http://", "https://")):
            return record(VerificationVerdict.UNKNOWN, "external_redirect")
        return record(VerificationVerdict.UNKNOWN, "invalid_observed_url")
    if model_lookup_key(observed_identity) != model_lookup_key(original_url):
        if observed_identity.lower() == "https://stripchat.com/login/":
            return confirmable("login_required")
        return record(VerificationVerdict.UNKNOWN, "identity_mismatch")
    if observation.login_gate_detected:
        return confirmable("login_required")
    if not observation.body_readable:
        return record(VerificationVerdict.UNKNOWN, "blank_page")

    hidden = observation.has_hidden_text
    disabled = observation.has_disabled_text
    player = (
        observation.video_element_count > 0
        or observation.player_container_count > 0
    )
    visible_account_classes = (
        observation.visible_hidden_selector_count
        + observation.visible_disabled_selector_count
    )
    ambiguous_class = (
        visible_account_classes > 0 and not hidden and not disabled
    )

    if hidden and disabled:
        return record(
            VerificationVerdict.UNKNOWN, "hidden_disabled_conflict"
        )
    if hidden:
        if player:
            return record(
                VerificationVerdict.UNKNOWN, "hidden_player_conflict"
            )
        return record(
            VerificationVerdict.BLOCKED,
            "visible_hidden_notice",
            terminal=True,
            retryable=False,
        )
    if disabled:
        if player:
            return record(
                VerificationVerdict.UNKNOWN, "disabled_player_conflict"
            )
        return record(
            VerificationVerdict.UNKNOWN,
            "account_disabled_or_deleted",
            terminal=True,
            retryable=False,
        )
    if observation.offline_notice_detected and not player:
        return confirmable("room_offline")
    if observation.not_found_notice_detected and not player:
        return confirmable("not_found")
    if player:
        if ambiguous_class:
            return record(
                VerificationVerdict.UNKNOWN, "class_player_conflict"
            )
        return record(
            VerificationVerdict.ACCESSIBLE,
            "player_present",
            terminal=True,
            retryable=False,
        )
    if ambiguous_class:
        return record(
            VerificationVerdict.UNKNOWN, "class_semantics_ambiguous"
        )
    # A private or group show covers the stream with a shutter that says so.
    # The room is demonstrably not hidden - a hidden account renders the
    # account notice in place of the whole room - so this candidate is not a
    # target and must end instead of being retried until the run gives up.
    # A shutter with nothing to say still falls through below, which is what
    # keeps a Stripchat redesign loud rather than silently non-target.
    if observation.has_shutter_status:
        return confirmable("room_not_viewable")
    # Readiness is the *explanation* for a silent page, not a gate in front
    # of one that already spoke. A room renders its notice, its player or
    # its offline shutter about a second in and never revises it, while the
    # load event waits on media and telemetry for another twenty seconds;
    # gating on the load event only made the step slower, never safer.
    if observation.ready_state != "complete":
        return record(VerificationVerdict.UNKNOWN, "page_not_ready")
    return record(VerificationVerdict.UNKNOWN, "missing_positive_signal")
