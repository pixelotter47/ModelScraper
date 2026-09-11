import os
import queue
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gui_app
from app_settings import DEFAULTS, AppSettings, install_shared_settings
from gui_app import AppController

UI_DIR = Path(__file__).parent / "ui"


class _BridgeService:
    def __init__(self):
        self.events = queue.Queue()
        self.calls = []
        self.state = {
            "platform": "Chaturbate",
            "status": "Idle",
            "busy": False,
            "sessionPath": "",
            "blockedModels": [],
            "masterListModels": [],
            "masterListSortMode": "date_newest",
            "masterListCountryFilter": "All",
            "progress": {},
            "lastOutcome": None,
            "resumable": False,
        }

    def get_state(self):
        return dict(self.state)

    def subscribe(self):
        return self.events

    def update_settings(self, updates):
        if self.state["busy"]:
            raise RuntimeError("A task is running.")
        validated = {key: self.settings._coerce(key, value) for key, value in updates.items()}
        if any(value is None for value in validated.values()):
            raise ValueError("Invalid preference")
        self.settings._values.update(validated)
        return self.settings.save()

    def edit_search_providers(self, operation, *args):
        if self.state["busy"]:
            raise RuntimeError("A task is running.")
        return getattr(self.settings, operation)(*args)

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return True

        return call


class SettingsBridgeTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.addCleanup(install_shared_settings, None)
        self.service = _BridgeService()
        self.settings = AppSettings(
            path=os.path.join(self._temp.name, "settings.json")
        )
        self.controller = AppController(
            self._temp.name,
            service=self.service,
            settings=self.settings,
        )

    def service_calls(self):
        return [item[0] for item in self.service.calls]

    def test_settings_property_exposes_every_default(self):
        self.assertEqual(set(self.controller.settings), set(DEFAULTS))

    def test_set_setting_persists_and_notifies(self):
        seen = []
        self.controller.settingsChanged.connect(lambda: seen.append(True))

        self.assertTrue(self.controller.setSetting("copyOnOpen", False))

        self.assertEqual(len(seen), 1)
        self.assertFalse(self.controller.settings["copyOnOpen"])
        self.assertFalse(AppSettings(path=self.settings.path).get("copyOnOpen"))

    def test_rejected_setting_does_not_notify(self):
        seen = []
        self.controller.settingsChanged.connect(lambda: seen.append(True))

        self.assertFalse(self.controller.setSetting("copyMode", "sms"))

        self.assertEqual(seen, [])

    def test_location_preferences_save_together_and_accept_any_country(self):
        self.assertTrue(self.controller.setLocationPreferences("jp, ca", "Tokyo, Canada"))
        self.assertEqual(self.settings.get("targetCountries"), "CA, JP")
        self.assertEqual(set(self.settings.get("targetLocationTerms").split(", ")), {"Tokyo", "Canada"})
        self.assertEqual(len(self.controller.countryChoices), 250)
        self.assertIn("jp", {item["code"] for item in self.controller.countryChoices})

    def test_busy_workflow_rejects_settings_and_reset(self):
        self.assertTrue(self.controller.setSetting("openBatchSize", 10))
        self.service.state["busy"] = True
        self.assertFalse(self.controller.setLocationPreferences("JP", "Tokyo"))
        self.assertFalse(self.controller.setSetting("vpnRelayLocation", "de"))
        self.controller.resetPreferences()
        self.assertEqual(self.settings.get("openBatchSize"), 10)
        self.assertEqual(self.settings.get("targetCountries"), "")

    def test_invalid_location_pair_does_not_save_partially(self):
        self.assertFalse(self.controller.setLocationPreferences("JP", "!"))
        self.assertEqual(self.settings.get("targetCountries"), "")

    def test_manual_vpn_steps_pass_the_users_network_confirmation(self):
        self.assertEqual(self.settings.get("vpnProvider"), "manual")
        self.controller.startVpnList(True)
        self.controller.startLocalList(True)
        self.controller.startVerify(True)
        self.controller.verifyMasterList(True)
        for name in ("start_vpn_list", "start_local_list", "start_verify", "verify_master_list"):
            call = next(item for item in self.service.calls if item[0] == name)
            self.assertIs(call[2]["manual_network_confirmed"], True)

    def test_busy_workflow_rejects_search_provider_mutations(self):
        original = self.settings.providers()
        self.service.state["busy"] = True
        self.assertFalse(self.controller.setSearchProviderEnabled("camwhores", False))
        self.assertTrue(self.controller.addSearchProvider("Mine", "https://mine.test/{query}"))
        self.assertFalse(self.controller.removeSearchProvider("camwhores"))
        self.assertFalse(self.controller.moveSearchProvider("archivebate", -1))
        self.controller.resetSearchProviders()
        self.assertEqual(self.settings.providers(), original)

    def test_mullvad_steps_do_not_claim_manual_confirmation(self):
        self.assertTrue(self.controller.setSetting("vpnProvider", "mullvad"))
        self.controller.startVpnList(True)
        self.assertIs(self.service.calls[-1][2]["manual_network_confirmed"], False)

    def test_startup_restores_a_saved_master_list_view(self):
        self.settings.set("masterListSortMode", "country")
        self.settings.set("masterListCountryFilter", "RO")
        service = _BridgeService()

        AppController(self._temp.name, service=service, settings=self.settings)

        self.assertEqual(
            [item[0] for item in service.calls],
            ["sort_master_list", "set_master_list_country_filter"],
        )

    def test_startup_stays_quiet_when_nothing_was_customised(self):
        service = _BridgeService()

        AppController(self._temp.name, service=service, settings=self.settings)

        self.assertEqual(service.calls, [])

    def test_sorting_the_master_list_remembers_the_choice(self):
        self.controller.sortMasterList("country")

        self.assertIn("sort_master_list", self.service_calls())
        self.assertEqual(self.settings.get("masterListSortMode"), "country")

    def test_sorting_is_not_remembered_when_the_toggle_is_off(self):
        self.controller.setSetting("rememberMasterListView", False)

        self.controller.sortMasterList("country")

        self.assertEqual(self.settings.get("masterListSortMode"), "date_newest")

    def test_copy_on_open_honours_the_toggle_and_the_mode(self):
        clipboard = mock.Mock()
        with mock.patch.object(
            gui_app.QtGui.QGuiApplication, "clipboard", return_value=clipboard
        ):
            self.assertTrue(
                self.controller.copyModelReference(
                    "sarah_blake_", "https://chaturbate.com/sarah_blake_"
                )
            )
            clipboard.setText.assert_called_once_with("sarah_blake_")

            self.controller.setSetting("copyMode", "url")
            self.controller.copyModelReference(
                "sarah_blake_", "https://chaturbate.com/sarah_blake_"
            )
            clipboard.setText.assert_called_with(
                "https://chaturbate.com/sarah_blake_"
            )

            clipboard.reset_mock()
            self.controller.setSetting("copyOnOpen", False)
            self.assertFalse(
                self.controller.copyModelReference(
                    "sarah_blake_", "https://chaturbate.com/sarah_blake_"
                )
            )
            clipboard.setText.assert_not_called()

    def test_model_pages_open_in_the_default_browser(self):
        with mock.patch.object(
            gui_app.QtGui.QDesktopServices, "openUrl", return_value=True
        ) as open_url:
            self.assertTrue(
                self.controller.openModelUrl("https://chaturbate.com/x")
            )

        self.assertEqual(open_url.call_count, 1)

    def test_model_pages_open_in_one_chrome_batch_when_configured(self):
        self.controller.setSetting("openModelIn", "chrome")
        urls = ["https://chaturbate.com/a", "https://chaturbate.com/b"]

        with mock.patch("gui_app.launch_urls_in_chrome") as launcher:
            self.assertTrue(self.controller.openModelUrls(urls))

        launcher.assert_called_once_with(urls)

    def test_chrome_failure_falls_back_to_the_default_browser(self):
        self.controller.setSetting("openModelIn", "chrome")
        self.controller._log = mock.Mock()

        with mock.patch(
            "gui_app.launch_urls_in_chrome",
            side_effect=FileNotFoundError("Chrome missing"),
        ):
            with mock.patch.object(
                gui_app.QtGui.QDesktopServices, "openUrl", return_value=True
            ) as open_url:
                self.assertTrue(
                    self.controller.openModelUrl("https://chaturbate.com/x")
                )

        open_url.assert_called_once()
        self.assertIn("Chrome could not be launched", self.controller._log.call_args[0][0])

    def test_empty_url_list_is_ignored(self):
        self.assertFalse(self.controller.openModelUrls([]))
        self.assertFalse(self.controller.openModelUrl("   "))

    def test_provider_rows_expose_editing_metadata(self):
        rows = self.controller.searchProviderRows

        self.assertEqual(rows[0]["id"], "camwhores")
        self.assertTrue(rows[0]["enabled"])
        self.assertFalse(rows[0]["custom"])
        camgirlfinder = [row for row in rows if row["id"] == "camgirlfinder"][0]
        self.assertFalse(camgirlfinder["inMenu"])

    def test_disabling_a_source_updates_the_popover_list(self):
        seen = []
        self.controller.searchProvidersChanged.connect(lambda: seen.append(True))
        before = len(self.controller.externalSearchProviders)

        self.assertTrue(
            self.controller.setSearchProviderEnabled("camwhores", False)
        )

        self.assertEqual(len(seen), 1)
        self.assertEqual(len(self.controller.externalSearchProviders), before - 1)
        self.assertFalse(self.controller.isSearchProviderEnabled("camwhores"))

    def test_adding_a_source_returns_an_empty_error_and_notifies(self):
        seen = []
        self.controller.searchProvidersChanged.connect(lambda: seen.append(True))

        error = self.controller.addSearchProvider(
            "My Archive", "https://my.archive/{query}"
        )

        self.assertEqual(error, "")
        self.assertEqual(len(seen), 1)
        self.assertEqual(
            self.controller.externalSearchProviders[-1]["label"], "My Archive"
        )

    def test_adding_an_invalid_source_reports_the_reason(self):
        error = self.controller.addSearchProvider("Broken", "my.archive")

        self.assertIn("http", error)
        self.assertEqual(self.controller.searchProviderRows[-1]["custom"], False)

    def test_removing_and_reordering_sources(self):
        self.controller.addSearchProvider("Temp", "https://temp.test/{query}")

        self.assertFalse(self.controller.removeSearchProvider("camwhores"))
        self.assertTrue(self.controller.moveSearchProvider("archivebate", -1))
        self.assertEqual(
            self.controller.searchProviderRows[0]["id"], "archivebate"
        )
        self.assertTrue(self.controller.removeSearchProvider("custom-temp"))

        self.controller.resetSearchProviders()
        self.assertEqual(
            self.controller.searchProviderRows[0]["id"], "camwhores"
        )

    def test_external_search_uses_the_configured_registry(self):
        self.controller.addSearchProvider("Mine", "https://mine.test/{query}")

        with mock.patch("gui_app.launch_external_search") as launcher:
            self.controller.openExternalSearch("custom-mine", "fernfrancis")

        registry = launcher.call_args.kwargs["providers"]
        self.assertIn("custom-mine", registry)

    def test_reset_restores_defaults_and_notifies(self):
        self.controller.setSetting("openBatchSize", 20)
        seen = []
        self.controller.settingsChanged.connect(lambda: seen.append(True))

        self.controller.resetPreferences()

        self.assertEqual(len(seen), 1)
        self.assertEqual(
            self.controller.settings["openBatchSize"],
            DEFAULTS["openBatchSize"],
        )

    def test_vpn_locations_include_the_tested_default(self):
        codes = [item["code"] for item in self.controller.vpnLocations]

        self.assertEqual(codes[0], "ie")


class SettingsQmlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_qml = (UI_DIR / "Main.qml").read_text(encoding="utf-8")
        cls.settings_qml = (UI_DIR / "SettingsView.qml").read_text(
            encoding="utf-8"
        )

    def test_main_mounts_the_settings_page_and_a_rail_entry(self):
        self.assertIn("SettingsView {", self.main_qml)
        self.assertIn('text: "Preferences"', self.main_qml)
        self.assertIn("mainStack.currentIndex = 2", self.main_qml)

    def test_model_rows_route_through_the_controller(self):
        self.assertNotIn("Qt.openUrlExternally", self.main_qml)
        self.assertEqual(self.main_qml.count("appController.copyModelReference("), 2)
        self.assertEqual(self.main_qml.count("appController.openModelUrl("), 2)
        self.assertIn("appController.openModelUrls(", self.main_qml)

    def test_every_preference_is_reachable_from_the_ui(self):
        combined = self.settings_qml + self.main_qml
        for key in DEFAULTS:
            with self.subTest(key=key):
                self.assertIn(key, combined)

    def test_settings_view_does_not_reach_for_the_main_window_id(self):
        self.assertNotIn("window.", self.settings_qml)

    def test_settings_view_uses_escaped_icon_glyphs(self):
        for source in (self.settings_qml, self.main_qml):
            self.assertFalse(
                [char for char in source if 0xE000 <= ord(char) <= 0xF8FF],
                "literal private-use glyphs must be written as \\uXXXX",
            )
        self.assertTrue(re.search(r'"\\uf0ac"', self.settings_qml))


if __name__ == "__main__":
    unittest.main()
