from unittest.mock import patch

import pytest

from app_settings import AppSettings
from runtime_lock import WorkflowLease
from test_modelscraper_service import _make_service


def test_atomic_validation_does_not_persist_half_a_selection(tmp_path):
    service, _ = _make_service(str(tmp_path))
    with pytest.raises(ValueError):
        service.update_settings({"targetCountries": "BR", "targetLocationTerms": object()})
    assert AppSettings(tmp_path).get("targetCountries") == ""


def test_location_preferences_save_and_reload(tmp_path):
    service, _ = _make_service(str(tmp_path))
    assert service.update_settings({"targetCountries": "br,de", "targetLocationTerms": "Berlin"})
    assert AppSettings(tmp_path).get("targetCountries") == "BR, DE"
    assert AppSettings(tmp_path).get("targetLocationTerms") == "Berlin"


def test_busy_workflow_blocks_preference_changes(tmp_path):
    service, _ = _make_service(str(tmp_path))
    service._busy = True
    with pytest.raises(RuntimeError):
        service.update_settings({"targetCountries": "BR"})
    assert AppSettings(tmp_path).get("targetCountries") == ""


def test_cross_process_workflow_lease_blocks_preference_changes(tmp_path):
    service, _ = _make_service(str(tmp_path))
    with WorkflowLease(str(tmp_path), task="Synthetic running workflow"):
        with pytest.raises(RuntimeError):
            service.update_settings({"targetCountries": "BR"})


def test_failed_save_rolls_back_in_memory_preferences(tmp_path):
    service, _ = _make_service(str(tmp_path))
    with patch.object(service.settings, "save", return_value=False):
        with pytest.raises(OSError):
            service.update_settings({"targetCountries": "BR"})
    assert service.settings.get("targetCountries") == ""


def test_other_process_preferences_are_preserved(tmp_path):
    service, _ = _make_service(str(tmp_path))
    external = AppSettings(tmp_path)
    external.set("vpnRelayLocation", "de")
    service.update_settings({"targetCountries": "BR"})
    assert AppSettings(tmp_path).get("vpnRelayLocation") == "de"
