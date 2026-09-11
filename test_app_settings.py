import json
import os
import tempfile
import unittest

import app_settings
import mullvad_vpn
from app_settings import (
    DEFAULTS,
    AppSettings,
    install_shared_settings,
    validate_url_template,
)
from external_search import (
    EXTERNAL_SEARCH_PROVIDERS,
    build_all_external_search_urls,
    build_external_search_url,
    get_external_search_menu_providers,
)


class AppSettingsTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = os.path.join(self._temp.name, "config", "settings.json")

    def make(self, payload=None):
        if payload is not None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        return AppSettings(path=self.path)

    def test_defaults_apply_without_a_config_file(self):
        settings = self.make()

        self.assertEqual(settings.as_dict(), DEFAULTS)
        self.assertFalse(os.path.exists(self.path))

    def test_set_persists_across_instances(self):
        settings = self.make()

        self.assertTrue(settings.set("copyOnOpen", False))
        self.assertTrue(settings.set("openBatchSize", 12))

        reloaded = AppSettings(path=self.path)
        self.assertFalse(reloaded.get("copyOnOpen"))
        self.assertEqual(reloaded.get("openBatchSize"), 12)

    def test_vpn_defaults_to_manual_with_optional_mullvad_provider(self):
        settings = self.make()
        self.assertEqual(settings.get("vpnProvider"), "manual")
        self.assertEqual(settings.get("vpnRelayLocation"), "ie")
        self.assertTrue(settings.set("vpnProvider", "Mullvad"))
        self.assertEqual(AppSettings(path=self.path).get("vpnProvider"), "mullvad")
        self.assertTrue(settings.set("vpnProvider", "manual"))
        self.assertEqual(AppSettings(path=self.path).get("vpnProvider"), "manual")

    def test_invalid_vpn_provider_does_not_change_saved_preference(self):
        settings = self.make()
        self.assertTrue(settings.set("vpnProvider", "mullvad"))
        self.assertFalse(settings.set("vpnProvider", "unknown"))
        self.assertFalse(settings.set("vpnProvider", ""))
        self.assertEqual(AppSettings(path=self.path).get("vpnProvider"), "mullvad")

    def test_strict_operation_reload_rejects_invalid_stored_vpn_provider(self):
        settings = self.make({"preferences": {"vpnProvider": "unknown"}})
        self.assertEqual(settings.get("vpnProvider"), "manual")
        with self.assertRaisesRegex(ValueError, "Invalid stored VPN provider"):
            settings.load(strict=True)

    def test_rejects_unknown_key_and_invalid_choice(self):
        settings = self.make()

        self.assertFalse(settings.set("nope", True))
        self.assertFalse(settings.set("copyMode", "clipboard"))
        self.assertEqual(settings.get("copyMode"), "username")

    def test_clamps_and_coerces_numeric_and_boolean_values(self):
        settings = self.make()

        settings.set("openBatchSize", "99")
        self.assertEqual(settings.get("openBatchSize"), 25)
        settings.set("openBatchSize", 0)
        self.assertEqual(settings.get("openBatchSize"), 1)
        settings.set("logAutoScroll", "false")
        self.assertFalse(settings.get("logAutoScroll"))

    def test_country_filter_is_normalised(self):
        settings = self.make()

        settings.set("masterListCountryFilter", "ro")
        self.assertEqual(settings.get("masterListCountryFilter"), "RO")
        settings.set("masterListCountryFilter", "all")
        self.assertEqual(settings.get("masterListCountryFilter"), "All")

    def test_corrupt_file_falls_back_to_defaults(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        messages = []

        settings = AppSettings(path=self.path, logger=messages.append)

        self.assertEqual(settings.as_dict(), DEFAULTS)
        self.assertTrue(any("Could not read settings" in m for m in messages))

    def test_invalid_stored_value_falls_back_to_that_key_default(self):
        settings = self.make(
            {"preferences": {"copyMode": "nonsense", "openBatchSize": 7}}
        )

        self.assertEqual(settings.get("copyMode"), "username")
        self.assertEqual(settings.get("openBatchSize"), 7)


class SearchNetworkSettingsTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = os.path.join(self._temp.name, "settings.json")
        self.settings = AppSettings(path=self.path)

    def test_default_registry_matches_the_builtin_menu(self):
        rows = self.settings.providers()

        self.assertEqual(
            [row["id"] for row in rows],
            list(EXTERNAL_SEARCH_PROVIDERS),
        )
        self.assertTrue(all(row["enabled"] for row in rows))
        self.assertEqual(
            get_external_search_menu_providers(self.settings.provider_registry()),
            get_external_search_menu_providers(),
        )

    def test_disabled_source_leaves_the_menu_and_the_bulk_action(self):
        self.settings.set_provider_enabled("camwhores", False)
        registry = self.settings.provider_registry()

        ids = [row["id"] for row in get_external_search_menu_providers(registry)]
        self.assertNotIn("camwhores", ids)
        self.assertEqual(len(ids), len(get_external_search_menu_providers()) - 1)
        urls = build_all_external_search_urls("synthetic_model", registry)
        self.assertFalse(
            any("camwhores.tv" in url for url in urls),
        )

    def test_disabled_source_still_resolves_when_asked_directly(self):
        self.settings.set_provider_enabled("camgirlfinder", False)

        self.assertEqual(
            build_external_search_url(
                "camgirlfinder",
                "example_model",
                self.settings.provider_registry(),
            ),
            (
                "https://camgirlfinder.net/models"
                "?model=example_model&platform=&gender="
            ),
        )

    def test_added_source_appears_in_the_menu_and_builds_urls(self):
        provider_id, error = self.settings.add_provider(
            "My Archive", "https://my.archive/search?q={query}"
        )

        self.assertEqual(error, "")
        self.assertEqual(provider_id, "custom-my-archive")
        registry = self.settings.provider_registry()
        ids = [row["id"] for row in get_external_search_menu_providers(registry)]
        self.assertEqual(ids[-1], provider_id)
        self.assertEqual(
            build_external_search_url(provider_id, "example model", registry),
            "https://my.archive/search?q=example%20model",
        )

    def test_added_sources_get_unique_ids(self):
        first, _ = self.settings.add_provider(
            "Archive", "https://a.test/{query}"
        )
        second, _ = self.settings.add_provider(
            "archive!", "https://b.test/{query}"
        )

        self.assertEqual(first, "custom-archive")
        self.assertEqual(second, "custom-archive-2")

    def test_rejects_unusable_templates_and_labels(self):
        cases = [
            ("Site", "https://site.tv/search/"),
            ("Site", "site.tv/search/{query}"),
            ("Site", "https://site.tv/{query}/{page}"),
            ("", "https://site.tv/{query}"),
        ]
        for label, template in cases:
            with self.subTest(template=template):
                provider_id, error = self.settings.add_provider(label, template)
                self.assertEqual(provider_id, "")
                self.assertNotEqual(error, "")
        self.assertEqual(
            [row["id"] for row in self.settings.providers()],
            list(EXTERNAL_SEARCH_PROVIDERS),
        )

    def test_only_custom_sources_can_be_removed(self):
        provider_id, _ = self.settings.add_provider(
            "Temp", "https://temp.test/{query}"
        )

        self.assertFalse(self.settings.remove_provider("camwhores"))
        self.assertTrue(self.settings.remove_provider(provider_id))
        self.assertEqual(
            [row["id"] for row in self.settings.providers()],
            list(EXTERNAL_SEARCH_PROVIDERS),
        )

    def test_move_reorders_and_refuses_to_leave_the_list(self):
        self.assertTrue(self.settings.move_provider("archivebate", -1))
        self.assertEqual(
            [row["id"] for row in self.settings.providers()][:2],
            ["archivebate", "camwhores"],
        )
        self.assertFalse(self.settings.move_provider("archivebate", -1))
        self.assertFalse(self.settings.move_provider("archivebate", 0))

    def test_order_and_state_survive_a_reload(self):
        self.settings.move_provider("archivebate", -1)
        self.settings.set_provider_enabled("curbate", False)
        self.settings.add_provider("Extra", "https://extra.test/{query}")

        reloaded = AppSettings(path=self.path)
        rows = reloaded.providers()

        self.assertEqual(rows[0]["id"], "archivebate")
        self.assertFalse(reloaded.is_provider_enabled("curbate"))
        self.assertEqual(rows[-1]["label"], "Extra")

    def test_unknown_builtin_ids_are_dropped_and_new_ones_appended(self):
        settings = AppSettings(
            path=self.path,
        )
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "searchProviders": [
                        {"id": "retired-site", "enabled": True},
                        {"id": "curbate", "enabled": False},
                        {"id": "bad", "custom": True, "label": "Bad"},
                    ]
                },
                handle,
            )
        settings.load()
        ids = [row["id"] for row in settings.providers()]

        self.assertNotIn("retired-site", ids)
        self.assertNotIn("bad", ids)
        self.assertEqual(ids[0], "curbate")
        self.assertEqual(len(ids), len(EXTERNAL_SEARCH_PROVIDERS))
        self.assertFalse(settings.is_provider_enabled("curbate"))

    def test_reset_restores_the_builtin_network(self):
        self.settings.add_provider("Extra", "https://extra.test/{query}")
        self.settings.set_provider_enabled("camwhores", False)

        self.settings.reset_providers()

        self.assertEqual(
            [row["id"] for row in self.settings.providers()],
            list(EXTERNAL_SEARCH_PROVIDERS),
        )
        self.assertTrue(self.settings.is_provider_enabled("camwhores"))

    def test_template_validation_messages_are_specific(self):
        self.assertIn("{query}", validate_url_template("https://a.test/"))
        self.assertIn("http", validate_url_template("a.test/{query}"))
        self.assertIsNone(validate_url_template("https://a.test/{query}"))


class SharedSettingsTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.settings = AppSettings(
            path=os.path.join(self._temp.name, "settings.json")
        )
        install_shared_settings(self.settings)
        self.addCleanup(install_shared_settings, None)

    def test_executable_paths_come_from_settings_when_set(self):
        self.settings.set("chromePath", r"C:\Custom\chrome.exe")
        self.settings.set("mullvadPath", r"C:\Custom\mullvad.exe")

        self.assertEqual(
            mullvad_vpn.find_chrome_executable(), r"C:\Custom\chrome.exe"
        )
        self.assertEqual(
            mullvad_vpn.find_mullvad_executable(), r"C:\Custom\mullvad.exe"
        )

    def test_empty_paths_fall_back_to_detection(self):
        self.assertNotEqual(mullvad_vpn.find_chrome_executable(), "")
        self.assertNotEqual(mullvad_vpn.find_mullvad_executable(), "")

    def test_relay_location_follows_the_setting(self):
        self.assertEqual(mullvad_vpn.find_relay_location(), "ie")

        self.settings.set("vpnRelayLocation", "nl")

        self.assertEqual(mullvad_vpn.find_relay_location(), "nl")

    def test_controller_connects_to_the_configured_relay(self):
        self.settings.set("vpnRelayLocation", "nl")
        calls = []

        def runner(args, timeout):
            calls.append(args)
            if args == ["status"]:
                return "Connected\nRelay: nl-ams-wg-001\nVisible location: Netherlands."
            return ""

        vpn = mullvad_vpn.MullvadVpnController(
            command_path="mullvad.exe",
            chrome_path="chrome.exe",
            command_runner=runner,
            sleep=lambda _seconds: None,
        )
        vpn.connect_relay()

        self.assertEqual(vpn.relay_location, "nl")
        self.assertEqual(vpn.relay_display, "Netherlands")
        self.assertIn(["relay", "set", "location", "nl"], calls)

    def test_ireland_stays_the_default_relay(self):
        vpn = mullvad_vpn.MullvadVpnController(
            command_path="mullvad.exe",
            chrome_path="chrome.exe",
            command_runner=lambda args, timeout: "",
        )

        self.assertEqual(vpn.relay_location, "ie")
        self.assertEqual(vpn.relay_display, "Ireland")

    def test_settings_are_optional_for_low_level_helpers(self):
        install_shared_settings(None)

        self.assertEqual(app_settings.shared_setting("chromePath", "x"), "x")
        self.assertEqual(mullvad_vpn.find_relay_location(), "ie")


if __name__ == "__main__":
    unittest.main()
