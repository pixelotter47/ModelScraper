"""Platform specification, capabilities, and compound run identity.

This module is part of the platform-neutral safety kernel. It must never
import a platform module (``ctb_*``, ``stripchat_*``, ``mfc*``, ``xhl*``);
platform policy is injected through :class:`PlatformSpec`.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

CanonicalizeUrl = Callable[[str], str]

_PLATFORM_KEY = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_WINDOWS_RESERVED_TRAILERS = (".", " ")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class PlatformContractError(ValueError):
    """A platform contract violation with a stable snake_case code."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class PlatformArtifactNames:
    vpn: str
    local: str
    candidates: str
    metadata: str
    debug: str
    final: str
    master_json: str
    master_txt: str
    master_meta: str


@dataclass(frozen=True)
class PlatformCapabilities:
    persistent_runs: bool
    resumable_verification: bool
    adaptive_api_snapshots: bool
    machine_policy_required: bool
    typed_outcomes: bool
    master_verification: bool
    waiting_for_user_events: bool
    legacy_adoption_supported: bool = False


@dataclass(frozen=True)
class PlatformSpec:
    key: str
    display_name: str
    session_root_name: str
    url_canonicalizer: CanonicalizeUrl
    canonicalizer_version: str
    classifier_version: str
    api_contract_version: str
    artifacts: PlatformArtifactNames
    capabilities: PlatformCapabilities

    def __post_init__(self) -> None:
        if not _PLATFORM_KEY.fullmatch(self.key):
            raise PlatformContractError(
                "unsupported_platform",
                f"invalid platform key {self.key!r}",
            )


def _is_uuid(value: str) -> bool:
    try:
        # Exact match only: the canonical lowercase hyphenated form.
        return str(uuid.UUID(str(value))) == str(value)
    except (ValueError, AttributeError, TypeError):
        return False


def validate_session_id(session_id: str) -> None:
    """Reject session identifiers that cannot be a safe directory name."""
    if not session_id or not isinstance(session_id, str):
        raise PlatformContractError("invalid_request", "empty session_id")
    if _CONTROL_CHARS.search(session_id):
        raise PlatformContractError(
            "path_escape", "session_id contains control characters"
        )
    if session_id != session_id.strip() or session_id.endswith(
        _WINDOWS_RESERVED_TRAILERS
    ):
        raise PlatformContractError(
            "path_escape", "session_id has unsafe leading/trailing characters"
        )
    if any(sep in session_id for sep in ("/", "\\")):
        raise PlatformContractError(
            "path_escape", "session_id contains a path separator"
        )
    if ".." in session_id or ":" in session_id:
        raise PlatformContractError(
            "path_escape", "session_id contains a traversal or drive prefix"
        )


@dataclass(frozen=True)
class RunIdentity:
    """Compound identity binding every artifact and checkpoint."""

    platform_key: str
    session_id: str
    run_id: str
    generation_id: str

    def as_key(self) -> str:
        """In-memory map key and log correlation only; never a path."""
        return "|".join(
            (self.platform_key, self.session_id, self.run_id, self.generation_id)
        )

    def validate(self, *, known_platforms: tuple[str, ...] | None = None) -> None:
        if not _PLATFORM_KEY.fullmatch(self.platform_key or ""):
            raise PlatformContractError(
                "unsupported_platform",
                f"invalid platform key {self.platform_key!r}",
            )
        if known_platforms is not None and self.platform_key not in known_platforms:
            raise PlatformContractError(
                "unsupported_platform",
                f"unknown platform key {self.platform_key!r}",
            )
        validate_session_id(self.session_id)
        if not _is_uuid(self.run_id):
            raise PlatformContractError(
                "invalid_request", "run_id is not a canonical UUID"
            )
        if not _is_uuid(self.generation_id):
            raise PlatformContractError(
                "invalid_request", "generation_id is not a canonical UUID"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "platform_key": self.platform_key,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "generation_id": self.generation_id,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "RunIdentity":
        if not isinstance(value, dict):
            raise PlatformContractError(
                "invalid_request", "identity must be an object"
            )
        identity = cls(
            platform_key=str(value.get("platform_key") or ""),
            session_id=str(value.get("session_id") or ""),
            run_id=str(value.get("run_id") or ""),
            generation_id=str(value.get("generation_id") or ""),
        )
        identity.validate()
        return identity


def validate_session_containment(
    session_path: str | os.PathLike[str], session_root: str | os.PathLike[str]
) -> Path:
    """Resolve a session path and require it to stay inside its root."""
    resolved_root = Path(session_root).resolve()
    resolved = Path(session_path).resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise PlatformContractError(
            "path_escape",
            "session path escapes the configured platform session root",
        )
    return resolved


@runtime_checkable
class HardenedPlatformRunner(Protocol):
    """A runner whose steps may only return typed StepOutcome values."""

    spec: PlatformSpec

    def begin_or_resume(self, request):  # pragma: no cover - protocol
        ...

    def run_step1(self, context):  # pragma: no cover - protocol
        ...

    def run_step2(self, context):  # pragma: no cover - protocol
        ...

    def run_step3(self, context):  # pragma: no cover - protocol
        ...

    def run_step4(self, context):  # pragma: no cover - protocol
        ...

    def request_stop(self) -> None:  # pragma: no cover - protocol
        ...

    def close(self) -> None:  # pragma: no cover - protocol
        ...
