import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from app_settings import AppSettings, install_shared_settings
from ctb_core import CTBRunner
from location_profiles import (
    LocationPolicy,
    bind_session_location_policy,
    build_location_profile_match,
    normalize_target_countries,
    normalize_target_location_terms,
    read_session_location_policy,
)
from workflow_types import OutcomeStatus
from legacy_stripchat_family import LegacyStripchatFamilyRunner
from stripchat_core import StripchatRunner
from xhamsterlive_core import XHamsterLiveRunner


URL = "https://chaturbate.com/synthetic_location_model/"


@pytest.mark.parametrize("invalid", ["DE,", "ZZ", "USA", "US DE", "UK", None, ["US"]])
def test_countries_reject_invalid_codes_and_shapes(invalid):
    with pytest.raises(ValueError):
        normalize_target_countries(invalid)


def test_countries_accept_deduplicate_and_normalize_any_iso_country():
    assert normalize_target_countries("nz, ca,DE,ca") == "CA, DE, NZ"
    assert normalize_target_countries("  ") == ""


@pytest.mark.parametrize("invalid", ["Berlin,", "a", "*", "(Paris)", "x" * 81, "A/B", ["Paris"]])
def test_aliases_are_bounded_literal_text(invalid):
    with pytest.raises(ValueError):
        normalize_target_location_terms(invalid)


def test_policy_has_no_default_country_or_alias():
    assert not LocationPolicy().enabled
    assert build_location_profile_match({"country": "RO", "location": "Romania"}, URL) is None


def test_country_match_keeps_the_platform_country_and_source_fields():
    policy = LocationPolicy(("CA", "DE"))
    record = build_location_profile_match(
        {"country": "ca", "location": "Elsewhere", "spoken_languages": "German"}, URL, policy
    )
    assert record["country"] == "ca"
    assert record["languages"] == "German"
    assert record["match_reasons"] == ["country_code"]
    assert record["location_policy_fingerprint"] == policy.fingerprint


@pytest.mark.parametrize("country", ["JP", ""])
def test_alias_match_never_infers_or_overwrites_country(country):
    policy = LocationPolicy(("CA",), ("Montréal",))
    record = build_location_profile_match(
        {"country": country, "location": "Downtown Montreal", "spoken_languages": "French"}, URL, policy
    )
    assert record["country"] == country
    assert record["match_reasons"] == ["location_term"]
    assert record["matched_location_terms"] == ["Montréal"]


def test_aliases_match_whole_words_not_substrings_or_languages():
    policy = LocationPolicy(("FR",), ("Paris", "French"))
    assert build_location_profile_match({"country": "US", "location": "Parisian dream"}, URL, policy) is None
    assert build_location_profile_match({"spoken_languages": "French, Romanian"}, URL, policy) is None
    assert build_location_profile_match({"location": "Paris, France"}, URL, policy)["match_reasons"] == ["location_term"]


def test_same_effective_policy_has_same_fingerprint():
    first = LocationPolicy(("DE", "CA"), ("Montréal", "Berlin"))
    second = LocationPolicy(("ca", "de"), ("BERLIN", "montreal"))
    assert first.fingerprint == second.fingerprint


def test_settings_roundtrip_and_invalid_updates_leave_previous_value(tmp_path):
    settings = AppSettings(tmp_path)
    assert settings.set("targetCountries", "de,ca,de")
    assert settings.set("targetLocationTerms", "Berlin, Montréal")
    assert not settings.set("targetCountries", "ZZ")
    assert not settings.set("targetLocationTerms", "(.*)")
    reloaded = AppSettings(tmp_path)
    assert reloaded.get("targetCountries") == "CA, DE"
    assert reloaded.get("targetLocationTerms") == "Berlin, Montréal"


def test_invalid_stored_location_preference_fails_closed(tmp_path):
    settings = AppSettings(tmp_path)
    Path(settings.path).parent.mkdir()
    Path(settings.path).write_text(json.dumps({"preferences": {"targetCountries": "ZZ"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid stored location"):
        AppSettings(tmp_path)


def test_session_binding_survives_reload_and_rejects_changed_policy(tmp_path):
    policy = LocationPolicy(("DE",))
    bind_session_location_policy(tmp_path, policy)
    (tmp_path / "cb_vpn_list.txt").write_text(URL, encoding="utf-8")
    assert read_session_location_policy(tmp_path) == policy
    assert bind_session_location_policy(tmp_path, policy) == policy
    with pytest.raises(ValueError, match="Create a new session"):
        bind_session_location_policy(tmp_path, LocationPolicy(("CA",)))
    assert read_session_location_policy(tmp_path) == policy


def test_unknown_policy_on_populated_session_is_not_adopted(tmp_path):
    (tmp_path / "cb_vpn_list.txt").write_text(URL, encoding="utf-8")
    with pytest.raises(ValueError, match="unknown location policy"):
        bind_session_location_policy(tmp_path, LocationPolicy())
    assert not (tmp_path / "location_policy.json").exists()


def test_fresh_empty_session_can_use_changed_preferences(tmp_path):
    bind_session_location_policy(tmp_path, LocationPolicy(("DE",)))
    bind_session_location_policy(tmp_path, LocationPolicy(("CA",)))
    assert read_session_location_policy(tmp_path).countries == ("CA",)


def test_tampered_session_policy_is_rejected(tmp_path):
    bind_session_location_policy(tmp_path, LocationPolicy(("DE",)))
    path = tmp_path / "location_policy.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["preferences"]["targetCountries"] = "CA"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        read_session_location_policy(tmp_path)


def test_runner_uses_its_checkout_settings_never_global_shared_settings(tmp_path):
    own_root = tmp_path / "public"
    private_root = tmp_path / "private"
    own = AppSettings(own_root)
    own.set("targetCountries", "CA")
    private = AppSettings(private_root)
    private.set("targetCountries", "DE")
    install_shared_settings(private)
    try:
        runner = CTBRunner(own_root / "cb sessions")
        assert runner.location_policy.countries == ("CA",)
    finally:
        install_shared_settings(None)


def test_runner_reloads_disk_before_resumed_step_and_refuses_mixed_policy(tmp_path):
    settings = AppSettings(tmp_path)
    settings.set("targetCountries", "DE")
    session = tmp_path / "cb sessions" / "session 1 01.01.2026"
    session.mkdir(parents=True)
    runner = CTBRunner(session.parent, settings=settings)
    runner.set_session(str(session))
    (session / "cb_vpn_list.txt").write_text(URL, encoding="utf-8")
    AppSettings(tmp_path).set("targetCountries", "CA")
    with mock.patch("ctb_core.ChaturbateApiClient") as http:
        with pytest.raises(ValueError, match="Create a new session"):
            runner.step_local_list()
        http.assert_not_called()


@pytest.mark.parametrize("invalid", ["{broken", "[]", '{"preferences": []}'])
def test_runner_rejects_corrupt_settings_before_fetch(tmp_path, invalid):
    settings = AppSettings(tmp_path)
    settings.set("targetCountries", "DE")
    session = tmp_path / "cb sessions" / "session 1 01.01.2026"
    session.mkdir(parents=True)
    runner = CTBRunner(session.parent, settings=settings)
    runner.set_session(str(session))
    Path(settings.path).write_text(invalid, encoding="utf-8")
    with mock.patch("ctb_core.ChaturbateApiClient") as http:
        with pytest.raises(ValueError):
            runner.fetch_models_via_api("Local")
        http.assert_not_called()


@pytest.mark.parametrize("transport", ["browser", "http_with_browser_fallback"])
@pytest.mark.parametrize("policy", [LocationPolicy(), LocationPolicy(("CA",))])
def test_both_transports_preserve_all_snapshot_urls_and_use_matching_policy(tmp_path, transport, policy):
    session = tmp_path / "session 1 01.01.2026"
    session.mkdir()
    runner = CTBRunner(tmp_path, location_policy=policy)
    runner.set_session(str(session))
    runner._http_mode = transport
    rooms = [
        {"username": "synthetic_country_match", "country": "CA", "location": "Toronto", "spoken_languages": "English"},
        {"username": "synthetic_other_country", "country": "JP", "location": "Tokyo", "spoken_languages": "French"},
    ]
    driver = mock.Mock()
    driver.find_element.return_value.text = json.dumps({"results": rooms})
    observations = tuple(SimpleNamespace(
        url=f"https://chaturbate.com/{room['username']}/", username=room["username"],
        country=room["country"], location=room["location"], languages=room["spoken_languages"], seconds_online=100,
    ) for room in rooms)
    api = mock.Mock()
    api.collect_adaptive.return_value = SimpleNamespace(
        status=OutcomeStatus.SUCCEEDED, models=observations, passes=(), quarantined=(),
    )
    with mock.patch("ctb_core._uc_chrome", return_value=driver), mock.patch("ctb_core.ChaturbateApiClient", return_value=api), mock.patch("ctb_core.time.sleep"):
        urls = runner.fetch_models_via_api("Local", save_profile_matches=True)
    assert urls == {observation.url for observation in observations}
    evidence = json.loads(Path(runner.paths.local_profile_matches).read_text(encoding="utf-8"))
    assert len(evidence) == int(policy.enabled)
    if policy.enabled:
        assert evidence[0]["name"] == observations[0].url
        assert evidence[0]["location_policy_fingerprint"] == policy.fingerprint


@pytest.mark.parametrize("binding", ["missing", "other_policy", "record_mismatch"])
def test_master_does_not_promote_profile_history_for_another_policy(tmp_path, binding):
    policy = LocationPolicy(("CA",))
    source = LocationPolicy(("DE",)) if binding == "other_policy" else policy
    session = tmp_path / "session 1 01.01.2026"
    session.mkdir()
    if binding != "missing":
        bind_session_location_policy(session, source)
    record = build_location_profile_match({"country": source.countries[0]}, URL, source)
    if binding == "record_mismatch":
        record["location_policy_fingerprint"] = "wrong"
    (session / "cb_local_profile_matches.json").write_text(json.dumps([record]), encoding="utf-8")
    (session / "FINAL_BLOCKED.txt").write_text(URL, encoding="utf-8")
    runner = CTBRunner(tmp_path, location_policy=policy)
    assert runner.compile_master_list() == 1
    entry = runner.get_master_list_models()[0]
    assert entry["name"] == URL
    assert "location_profile_match" not in entry
    assert "profile_match" not in entry


def test_vpn_location_evidence_requires_blocked_confirmation_in_same_session(tmp_path):
    policy = LocationPolicy(("CA",))
    source = tmp_path / "session 1 01.01.2026"
    confirmed = tmp_path / "session 2 02.01.2026"
    source.mkdir()
    confirmed.mkdir()
    bind_session_location_policy(source, policy)
    bind_session_location_policy(confirmed, policy)
    record = build_location_profile_match({"country": "CA"}, URL, policy)
    (source / "cb_vpn_profile_matches.json").write_text(json.dumps([record]), encoding="utf-8")
    (source / "FINAL_BLOCKED.txt").write_text("", encoding="utf-8")
    (confirmed / "FINAL_BLOCKED.txt").write_text(URL, encoding="utf-8")
    runner = CTBRunner(tmp_path, location_policy=policy)
    runner.compile_master_list()
    assert "vpn_location_profile_match" not in runner.get_master_list_models()[0]


@pytest.mark.parametrize("runner_type", [LegacyStripchatFamilyRunner, StripchatRunner, XHamsterLiveRunner])
def test_other_platforms_do_not_load_or_bind_chaturbate_location_policy(tmp_path, runner_type):
    session = tmp_path / "session 1 01.01.2026"
    session.mkdir()
    (session / "FINAL_BLOCKED.txt").write_text("synthetic_model", encoding="utf-8")
    with mock.patch("ctb_core.AppSettings", side_effect=AssertionError("CTB settings accessed")), mock.patch(
        "ctb_core.bind_session_location_policy", side_effect=AssertionError("CTB policy bound")
    ):
        runner = runner_type(tmp_path, location_policy=LocationPolicy(("CA",)))
        runner.set_session(str(session))
        runner._ensure_session()
    assert not runner.location_policy.enabled
    assert not (session / "location_policy.json").exists()


@pytest.mark.parametrize("runner_type", [LegacyStripchatFamilyRunner, StripchatRunner, XHamsterLiveRunner])
def test_other_platform_master_compilation_never_reads_chaturbate_profile_evidence(tmp_path, runner_type):
    session = tmp_path / "session 1 01.01.2026"
    session.mkdir()
    for filename in ("cb_local_profile_matches.json", "cb_vpn_profile_matches.json"):
        (session / filename).write_text("[]", encoding="utf-8")
    (session / "FINAL_BLOCKED.txt").write_text("", encoding="utf-8")
    runner = runner_type(tmp_path)
    with mock.patch("ctb_core.read_session_location_policy") as read_policy, mock.patch(
        "ctb_core.MasterRepository"
    ) as repository:
        assert runner.compile_master_list() == 0
        read_policy.assert_not_called()
        repository.return_value.publish.assert_called_once_with([])


@pytest.mark.parametrize("next_countries", ["CA", "", None])
def test_master_badges_follow_fresh_preferences_without_changing_history(tmp_path, next_countries):
    settings = AppSettings(tmp_path)
    settings.set("targetCountries", "DE")
    base = tmp_path / "cb sessions"
    base.mkdir()
    runner = CTBRunner(base, settings=settings)
    policy = LocationPolicy(("DE",))
    evidence = build_location_profile_match({"country": "DE", "location": "Berlin"}, URL, policy)
    row = {
        "name": URL, "country": "DE", "location_profile_match": True,
        "vpn_location_profile_match": True, "profile_match": evidence,
        "vpn_profile_match": dict(evidence),
    }
    master_path = Path(runner.get_master_json_path())
    master_path.write_text(json.dumps([row]), encoding="utf-8")
    original_bytes = master_path.read_bytes()
    assert runner.get_master_list_models()[0]["location_profile_match"]
    assert runner.get_master_list_models()[0]["vpn_location_profile_match"]
    if next_countries is None:
        Path(settings.path).write_text("{broken", encoding="utf-8")
    else:
        AppSettings(tmp_path).set("targetCountries", next_countries)
    current = runner.get_master_list_models()[0]
    assert "location_profile_match" not in current
    assert "vpn_location_profile_match" not in current
    assert current["country"] == "DE"
    assert current["profile_match"] == row["profile_match"]
    assert current["vpn_profile_match"] == row["vpn_profile_match"]
    assert master_path.read_bytes() == original_bytes
    restored = AppSettings(tmp_path)
    restored.set("targetCountries", "DE")
    assert runner.get_master_list_models()[0]["location_profile_match"]


@pytest.mark.parametrize("evidence", [{}, {"location_policy_fingerprint": "other"}, None])
def test_master_unversioned_or_malformed_evidence_has_no_current_badge(tmp_path, evidence):
    runner = CTBRunner(tmp_path, location_policy=LocationPolicy(("DE",)))
    row = {"name": URL, "location_profile_match": True, "profile_match": evidence}
    Path(runner.get_master_json_path()).write_text(json.dumps([row]), encoding="utf-8")
    current = runner.get_master_list_models()[0]
    assert "location_profile_match" not in current
    assert current["profile_match"] == evidence
