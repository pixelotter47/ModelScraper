"""Explicit, portable location preferences for optional profile discovery.

Countries refer to platform-reported country codes. Location terms are user
chosen aliases matched against the profile's free-text location, never against
languages. Neither source proves nationality or a person's physical location.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from storage_utils import atomic_write_json


from country_choices import COUNTRY_CODES

POLICY_FILE_NAME = "location_policy.json"


def normalized_location_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.casefold().split())


def normalize_target_countries(value):
    """Return canonical comma-separated codes, raising for unsupported values."""
    if not isinstance(value, str):
        raise ValueError("Target countries must be comma-separated ISO country codes.")
    if not value.strip():
        return ""
    parts = [part.strip().upper() for part in value.split(",")]
    if len(parts) > len(COUNTRY_CODES) or any(part not in COUNTRY_CODES for part in parts):
        raise ValueError("Use ISO alpha-2 country codes, for example DE, FR, or leave empty.")
    return ", ".join(sorted(set(parts)))


def normalize_target_location_terms(value):
    """Return bounded literal aliases; regex syntax and blank entries are invalid."""
    if not isinstance(value, str):
        raise ValueError("Location aliases must be comma-separated text.")
    if not value.strip():
        return ""
    if len(value) > 4096:
        raise ValueError("Location aliases must total at most 4096 characters.")
    parts = [" ".join(part.split()) for part in value.split(",")]
    if len(parts) > 50:
        raise ValueError("Use at most 50 location aliases.")
    for part in parts:
        if not 2 <= len(part) <= 80 or not part[0].isalnum() or not part[-1].isalnum():
            raise ValueError("Each location alias needs 2-80 characters and must start/end with a letter or number.")
        if any(not (char.isalnum() or char in " -.'’") for char in part):
            raise ValueError("Location aliases allow letters, numbers, spaces, hyphens, periods and apostrophes.")
    unique = {normalized_location_text(part): part for part in reversed(parts)}
    return ", ".join(unique[key] for key in sorted(unique))


@dataclass(frozen=True)
class LocationPolicy:
    countries: tuple[str, ...] = ()
    location_terms: tuple[str, ...] = ()

    def __post_init__(self):
        countries = normalize_target_countries(",".join(self.countries))
        terms = normalize_target_location_terms(",".join(self.location_terms))
        object.__setattr__(self, "countries", tuple(countries.split(", ")) if countries else ())
        object.__setattr__(self, "location_terms", tuple(terms.split(", ")) if terms else ())

    @classmethod
    def from_settings(cls, settings):
        countries = normalize_target_countries(settings.get("targetCountries", ""))
        terms = normalize_target_location_terms(settings.get("targetLocationTerms", ""))
        return cls(
            tuple(countries.split(", ")) if countries else (),
            tuple(terms.split(", ")) if terms else (),
        )

    @property
    def enabled(self):
        return bool(self.countries or self.location_terms)

    def as_dict(self):
        return {
            "targetCountries": ", ".join(self.countries),
            "targetLocationTerms": ", ".join(self.location_terms),
        }

    @property
    def fingerprint(self):
        payload = {
            "schema_version": 1,
            "countries": self.countries,
            "location_terms": sorted(normalized_location_text(term) for term in self.location_terms),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def build_location_profile_match(room, model_url, policy=None, detected_at=None):
    """Return bounded source evidence for configured matches, or None.

    All-country operation (empty preferences) disables this extra discovery
    channel. It does not remove any model from a geo-access snapshot.
    """
    policy = policy or LocationPolicy()
    if not isinstance(room, dict) or not policy.enabled:
        return None
    country = str(room.get("country") or "")[:8]
    location = str(room.get("location") or "").strip()[:160]
    languages = str(room.get("spoken_languages") or "").strip()[:160]
    normalized = normalized_location_text(location)
    reasons = []
    if country.strip().upper() in policy.countries:
        reasons.append("country_code")
    matched_terms = [
        term for term in policy.location_terms
        if re.search(r"(?<!\w)" + re.escape(normalized_location_text(term)) + r"(?!\w)", normalized)
    ]
    if matched_terms:
        reasons.append("location_term")
    if not reasons:
        return None
    return {
        "name": str(model_url or "").strip(),
        "country": country,
        "location": location,
        "languages": languages,
        "match_reasons": reasons,
        "matched_location_terms": matched_terms,
        "location_policy_fingerprint": policy.fingerprint,
        "detected_at": detected_at or datetime.datetime.now().isoformat(timespec="seconds"),
    }


def read_session_location_policy(session_path):
    """Read and validate the session binding; missing policy is unknown history."""
    path = Path(session_path, POLICY_FILE_NAME)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Invalid session location policy.")
    if not isinstance(payload.get("preferences"), dict):
        raise ValueError("Invalid session location preferences.")
    policy = LocationPolicy.from_settings(payload["preferences"])
    if payload.get("fingerprint") != policy.fingerprint:
        raise ValueError("Session location policy fingerprint does not match its preferences.")
    return policy


def session_has_location_artifacts(session_path):
    path = Path(session_path)
    names = (
        "cb_vpn_list.txt", "cb_local_list.txt", "cb_candidates.txt", "FINAL_BLOCKED.txt",
        "vpn_metadata.json", "cb_local_profile_matches.json", "cb_vpn_profile_matches.json",
        "run_state.json",
    )
    return any((path / name).exists() for name in names) or any((path / "runs").glob("*/manifest.json"))


def bind_session_location_policy(session_path, policy):
    """Prevent resumed runs from mixing old snapshots with new preferences."""
    previous = read_session_location_policy(session_path)
    if previous is not None and previous.fingerprint == policy.fingerprint:
        return previous
    if session_has_location_artifacts(session_path):
        raise ValueError(
            "This session contains snapshots with a different or unknown location policy. "
            "Create a new session to use the current location preferences."
        )
    atomic_write_json(Path(session_path, POLICY_FILE_NAME), {
        "schema_version": 1,
        "preferences": policy.as_dict(),
        "fingerprint": policy.fingerprint,
    })
    return policy
