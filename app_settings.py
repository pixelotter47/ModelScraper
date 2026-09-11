"""User-editable GUI preferences and search-network configuration.

Preferences live in a single JSON file so the Python side stays the source of
truth: the search network is consumed by ``external_search`` when it builds
URLs, and the executable paths are consumed by ``mullvad_vpn`` when it resolves
Chrome and the Mullvad CLI. QML reads and writes them through ``AppController``.
"""

from __future__ import annotations

import json
import os
import re

from external_search import EXTERNAL_SEARCH_PROVIDERS, ExternalSearchProvider
from mullvad_vpn import MULLVAD_LOCATIONS
from storage_utils import atomic_write_json
from location_profiles import normalize_target_countries, normalize_target_location_terms


CONFIG_DIR_NAME = "config"
SETTINGS_FILE_NAME = "settings.json"

PLATFORM_CHOICES = ("Chaturbate", "MyFreeCams", "Stripchat", "XHamsterLive")
COPY_MODE_CHOICES = ("username", "url")
OPEN_TARGET_CHOICES = ("default", "chrome")
SORT_MODE_CHOICES = ("name", "date_newest", "date_oldest", "country")
VPN_PROVIDER_CHOICES = ("manual", "mullvad")

DEFAULTS = {
    # Model rows
    "copyOnOpen": True,
    "copyMode": "username",
    "openModelIn": "default",
    "openBatchSize": 5,
    "confirmDelete": True,
    "blockOnDelete": False,
    # Location profile discovery (empty means no extra profile matching).
    "targetCountries": "",
    "targetLocationTerms": "",
    # Engine
    "hideBrowserSteps12": True,
    "hideBrowserStep4": True,
    "vpnProvider": "manual",
    "vpnRelayLocation": "ie",
    "chromePath": "",
    "mullvadPath": "",
    "defaultPlatform": "Chaturbate",
    "autoLoadLatestSession": False,
    # Master list
    "rememberMasterListView": True,
    "masterListSortMode": "date_newest",
    "masterListCountryFilter": "All",
    # Interface
    "rememberWindowGeometry": True,
    "logAutoScroll": True,
}

_BOOL_KEYS = frozenset(
    key for key, value in DEFAULTS.items() if isinstance(value, bool)
)
_CHOICE_KEYS = {
    "copyMode": COPY_MODE_CHOICES,
    "openModelIn": OPEN_TARGET_CHOICES,
    "defaultPlatform": PLATFORM_CHOICES,
    "masterListSortMode": SORT_MODE_CHOICES,
    "vpnRelayLocation": tuple(MULLVAD_LOCATIONS),
    "vpnProvider": VPN_PROVIDER_CHOICES,
}
_INT_RANGES = {"openBatchSize": (1, 25)}
_PATH_KEYS = frozenset(("chromePath", "mullvadPath"))

_MAX_LABEL_LENGTH = 40
_MAX_TEMPLATE_LENGTH = 400
_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]*)\}")


def validate_url_template(template):
    """Return an error message for an unusable template, or None when valid."""
    value = str(template or "").strip()
    if not value:
        return "The address cannot be empty."
    if len(value) > _MAX_TEMPLATE_LENGTH:
        return "The address is too long."
    if not value.startswith(("http://", "https://")):
        return "The address must start with http:// or https://."
    placeholders = _PLACEHOLDER_PATTERN.findall(value)
    if "query" not in placeholders:
        return "The address must contain {query}."
    unknown = [name for name in placeholders if name != "query"]
    if unknown:
        return "{query} is the only supported placeholder."
    return None


def validate_provider_label(label):
    """Return an error message for an unusable label, or None when valid."""
    value = str(label or "").strip()
    if not value:
        return "The name cannot be empty."
    if len(value) > _MAX_LABEL_LENGTH:
        return f"The name cannot be longer than {_MAX_LABEL_LENGTH} characters."
    return None


def _slugify(label):
    slug = re.sub(r"[^a-z0-9]+", "-", str(label or "").lower()).strip("-")
    return slug or "source"


class AppSettings:
    """JSON-backed preferences with validation and atomic persistence."""

    def __init__(self, base_dir=None, path=None, logger=None):
        if path is None:
            base = os.path.abspath(
                base_dir or os.path.dirname(os.path.abspath(__file__))
            )
            path = os.path.join(base, CONFIG_DIR_NAME, SETTINGS_FILE_NAME)
        self.path = os.path.abspath(path)
        self._logger = logger
        self._values = dict(DEFAULTS)
        self._providers = []
        self.load()

    # ------------------------------------------------------------------ io

    def _log(self, message):
        if self._logger:
            self._logger(message)

    def load(self, strict=False):
        raw = {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            if strict:
                raise ValueError(f"Could not read settings: {exc}") from exc
            self._log(f"[WARN] Could not read settings: {exc}. Using defaults.")
            raw = {}
        if not isinstance(raw, dict):
            if strict:
                raise ValueError("Settings must contain a JSON object.")
            raw = {}
        preferences = raw.get("preferences")
        if strict and preferences is not None and not isinstance(preferences, dict):
            raise ValueError("Settings preferences must contain a JSON object.")
        self._values = self._sanitize_preferences(
            preferences if isinstance(preferences, dict) else {}, strict=strict
        )
        self._providers = self._sanitize_providers(raw.get("searchProviders"))

    def save(self):
        payload = {
            "preferences": dict(self._values),
            "searchProviders": [dict(entry) for entry in self._providers],
        }
        try:
            atomic_write_json(self.path, payload)
            return True
        except OSError as exc:
            self._log(f"[ERROR] Could not save settings: {exc}")
            return False

    # --------------------------------------------------------- preferences

    def _sanitize_preferences(self, raw, strict=False):
        values = dict(DEFAULTS)
        for key, default in DEFAULTS.items():
            if key not in raw:
                continue
            coerced = self._coerce(key, raw[key])
            if coerced is None and key in ("targetCountries", "targetLocationTerms"):
                raise ValueError(f"Invalid stored location preference: {key}")
            if coerced is None and strict and key == "vpnProvider":
                raise ValueError("Invalid stored VPN provider preference: vpnProvider")
            values[key] = default if coerced is None else coerced
        return values

    def _coerce(self, key, value):
        """Return a valid value for ``key``, or None when it cannot be used."""
        if key not in DEFAULTS:
            return None
        if key in ("targetCountries", "targetLocationTerms"):
            normalizer = (normalize_target_countries if key == "targetCountries"
                          else normalize_target_location_terms)
            try:
                return normalizer(value)
            except ValueError:
                return None
        if key in _BOOL_KEYS:
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "1", "yes"):
                    return True
                if lowered in ("false", "0", "no"):
                    return False
                return None
            return bool(value)
        if key in _INT_RANGES:
            minimum, maximum = _INT_RANGES[key]
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                return None
            return max(minimum, min(maximum, number))
        if key in _CHOICE_KEYS:
            text = str(value or "").strip()
            for choice in _CHOICE_KEYS[key]:
                if text.lower() == choice.lower():
                    return choice
            return None
        if key in _PATH_KEYS:
            return str(value or "").strip()
        if key == "masterListCountryFilter":
            text = str(value or "").strip()
            if not text or text.lower() == "all":
                return "All"
            return text.upper()
        return str(value or "").strip()

    def get(self, key, default=None):
        if key in self._values:
            return self._values[key]
        return DEFAULTS.get(key, default)

    def as_dict(self):
        return dict(self._values)

    def set(self, key, value):
        if key not in DEFAULTS:
            self._log(f"[WARN] Unknown setting '{key}'.")
            return False
        coerced = self._coerce(key, value)
        if coerced is None:
            self._log(f"[WARN] Rejected value for setting '{key}': {value!r}")
            return False
        if self._values.get(key) == coerced:
            return True
        self._values[key] = coerced
        self.save()
        return True

    def reset_preferences(self):
        self._values = dict(DEFAULTS)
        self.save()

    # ------------------------------------------------------ search network

    def _sanitize_providers(self, raw):
        entries = []
        seen = set()
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("id") or "").strip()
            if not provider_id or provider_id in seen:
                continue
            enabled = bool(item.get("enabled", True))
            if item.get("custom"):
                label = str(item.get("label") or "").strip()
                template = str(item.get("urlTemplate") or "").strip()
                if validate_provider_label(label) or validate_url_template(
                    template
                ):
                    continue
                entries.append(
                    {
                        "id": provider_id,
                        "label": label,
                        "urlTemplate": template,
                        "enabled": enabled,
                        "custom": True,
                    }
                )
                seen.add(provider_id)
                continue
            if provider_id not in EXTERNAL_SEARCH_PROVIDERS:
                continue
            entries.append(
                {"id": provider_id, "enabled": enabled, "custom": False}
            )
            seen.add(provider_id)

        for provider_id in EXTERNAL_SEARCH_PROVIDERS:
            if provider_id not in seen:
                entries.append(
                    {"id": provider_id, "enabled": True, "custom": False}
                )
        return entries

    def providers(self):
        """Return ordered provider rows, merged with the built-in registry."""
        rows = []
        for entry in self._providers:
            if entry.get("custom"):
                rows.append(
                    {
                        "id": entry["id"],
                        "label": entry["label"],
                        "urlTemplate": entry["urlTemplate"],
                        "enabled": bool(entry["enabled"]),
                        "custom": True,
                        "inMenu": True,
                    }
                )
                continue
            builtin = EXTERNAL_SEARCH_PROVIDERS[entry["id"]]
            rows.append(
                {
                    "id": entry["id"],
                    "label": builtin.label,
                    "urlTemplate": builtin.url_template,
                    "enabled": bool(entry["enabled"]),
                    "custom": False,
                    "inMenu": builtin.menu_enabled,
                }
            )
        return rows

    def provider_registry(self):
        """Return an id -> ExternalSearchProvider mapping in display order."""
        return {
            row["id"]: ExternalSearchProvider(
                label=row["label"],
                url_template=row["urlTemplate"],
                menu_enabled=row["inMenu"],
                enabled=row["enabled"],
            )
            for row in self.providers()
        }

    def is_provider_enabled(self, provider_id):
        key = str(provider_id or "").strip()
        for entry in self._providers:
            if entry["id"] == key:
                return bool(entry["enabled"])
        return False

    def _index_of(self, provider_id):
        key = str(provider_id or "").strip()
        for index, entry in enumerate(self._providers):
            if entry["id"] == key:
                return index
        return -1

    def set_provider_enabled(self, provider_id, enabled):
        index = self._index_of(provider_id)
        if index == -1:
            return False
        if bool(self._providers[index]["enabled"]) == bool(enabled):
            return True
        self._providers[index]["enabled"] = bool(enabled)
        self.save()
        return True

    def add_provider(self, label, url_template):
        """Add a custom source. Returns ``(provider_id, error_message)``."""
        label_error = validate_provider_label(label)
        if label_error:
            return "", label_error
        template_error = validate_url_template(url_template)
        if template_error:
            return "", template_error

        clean_label = str(label).strip()
        clean_template = str(url_template).strip()
        base_id = f"custom-{_slugify(clean_label)}"
        taken = {entry["id"] for entry in self._providers}
        taken.update(EXTERNAL_SEARCH_PROVIDERS)
        provider_id = base_id
        suffix = 2
        while provider_id in taken:
            provider_id = f"{base_id}-{suffix}"
            suffix += 1

        self._providers.append(
            {
                "id": provider_id,
                "label": clean_label,
                "urlTemplate": clean_template,
                "enabled": True,
                "custom": True,
            }
        )
        self.save()
        return provider_id, ""

    def remove_provider(self, provider_id):
        """Delete a custom source. Built-in sources can only be disabled."""
        index = self._index_of(provider_id)
        if index == -1 or not self._providers[index].get("custom"):
            return False
        self._providers.pop(index)
        self.save()
        return True

    def move_provider(self, provider_id, delta):
        index = self._index_of(provider_id)
        if index == -1:
            return False
        try:
            step = int(delta)
        except (TypeError, ValueError):
            return False
        target = index + step
        if step == 0 or target < 0 or target >= len(self._providers):
            return False
        entry = self._providers.pop(index)
        self._providers.insert(target, entry)
        self.save()
        return True

    def reset_providers(self):
        self._providers = self._sanitize_providers([])
        self.save()


_shared_settings = None


def install_shared_settings(settings):
    """Publish the GUI's settings so low-level helpers can read them."""
    global _shared_settings
    _shared_settings = settings
    return settings


def shared_settings():
    """Return the installed settings, or None outside a running GUI."""
    return _shared_settings


def shared_setting(key, default=""):
    settings = _shared_settings
    if settings is None:
        return default
    try:
        return settings.get(key, default)
    except Exception:
        return default
