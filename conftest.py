"""Tests never inherit a developer's browser/VPN state or local overrides."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_application_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for key in tuple(os.environ):
        if key.startswith(("MODEL_SCRAPER_", "MODELSCRAPER_")):
            monkeypatch.delenv(key, raising=False)
