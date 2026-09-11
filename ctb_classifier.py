"""Pure Chaturbate URL validation and page verdict classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from workflow_types import VerificationRecord, VerificationVerdict, utc_now_iso


_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_HOSTS = {"chaturbate.com", "www.chaturbate.com"}


def canonical_model_url(value: str) -> str:
    try:
        parts = urlsplit(str(value).strip())
    except ValueError as exc:
        raise ValueError("invalid_url") from exc
    if parts.scheme.lower() != "https" or parts.hostname not in _HOSTS:
        raise ValueError("invalid_url")
    segments = [item for item in parts.path.split("/") if item]
    if len(segments) != 1 or not _USERNAME.fullmatch(segments[0]):
        raise ValueError("invalid_url")
    return urlunsplit(("https", "chaturbate.com", f"/{segments[0]}/", "", ""))


@dataclass(frozen=True)
class PageObservation:
    original_url: str
    current_url: str
    title: str = ""
    has_denied_notice: bool = False
    has_room_container: bool = False
    has_offline_notice: bool = False
    page_text_excerpt: str = ""
    navigation_error: str | None = None


def classify_page(
    observation: PageObservation,
    *,
    attempt: int = 1,
    run_id: str = "",
    timestamp: str | None = None,
) -> VerificationRecord:
    original_url = canonical_model_url(observation.original_url)
    current_url = str(observation.current_url or "")[:300]
    excerpt = " ".join(
        str(observation.page_text_excerpt or "").split()
    )[:240]
    if observation.navigation_error:
        verdict = VerificationVerdict.UNKNOWN
        reason = str(observation.navigation_error)[:80]
    elif observation.has_denied_notice:
        verdict = VerificationVerdict.BLOCKED
        reason = "denied_notice"
    elif "region or gender" in excerpt.lower():
        verdict = VerificationVerdict.BLOCKED
        reason = "region_or_gender_notice"
    elif observation.has_room_container and observation.has_offline_notice:
        # The room page rendered, so nothing was denied, but an offline room
        # is no proof the region is allowed either. Never treat it as
        # positive evidence for removing a master entry.
        verdict = VerificationVerdict.UNKNOWN
        reason = "room_offline"
    elif observation.has_room_container:
        verdict = VerificationVerdict.ACCESSIBLE
        reason = "room_container"
    elif current_url.rstrip("/").lower() in {
        "https://chaturbate.com",
        "https://www.chaturbate.com",
    }:
        verdict = VerificationVerdict.UNKNOWN
        reason = "homepage_redirect_only"
    elif "/auth/login" in current_url.lower():
        # The room exists but demands an account, so nothing about the
        # region can be concluded from it.
        verdict = VerificationVerdict.UNKNOWN
        reason = "login_required"
    else:
        verdict = VerificationVerdict.UNKNOWN
        reason = "unexpected_page"
    return VerificationRecord(
        original_url=original_url,
        verdict=verdict,
        reason_code=reason,
        attempt=max(1, int(attempt)),
        timestamp=timestamp or utc_now_iso(),
        observed_url=current_url,
        diagnostic_summary=excerpt,
        run_id=run_id,
    )
