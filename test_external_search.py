import unittest
from pathlib import Path
from unittest import mock

from external_search import (
    build_all_external_search_urls,
    build_external_search_url,
    get_external_search_menu_providers,
    launch_all_external_searches,
    launch_external_search,
)
from gui_app import AppController


class ExternalSearchTests(unittest.TestCase):
    def test_qml_opens_provider_menu_in_both_model_lists(self):
        qml = (Path(__file__).parent / "ui" / "Main.qml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            qml.count("searchProviderMenu.openFor("),
            2,
        )

    def test_qml_search_menu_exposes_single_and_all_actions(self):
        qml = (
            Path(__file__).parent
            / "ui"
            / "components"
            / "SearchProviderMenu.qml"
        ).read_text(encoding="utf-8")

        self.assertIn("root.providerSelected(", qml)
        self.assertIn("root.allProvidersSelected(", qml)
        self.assertIn("TOATE SITE-URILE", qml)

    def test_qml_exposes_camgirlfinder_search_in_both_model_lists(self):
        qml = (Path(__file__).parent / "ui" / "Main.qml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            qml.count('appController.openExternalSearch("camgirlfinder"'),
            2,
        )

    def test_builds_camwhores_search_url(self):
        self.assertEqual(
            build_external_search_url("camwhores", "  fern francis  "),
            "https://camwhores.tv/search/fern%20francis/",
        )

    def test_builds_camgirlfinder_search_url(self):
        self.assertEqual(
            build_external_search_url("camgirlfinder", "  yara crave  "),
            (
                "https://camgirlfinder.net/models"
                "?model=yara%20crave&platform=&gender="
            ),
        )

    def test_builds_recordbate_search_url(self):
        self.assertEqual(
            build_external_search_url("recordbate", "  fern francis  "),
            "https://recordbate.com/performer/fern%20francis",
        )

    def test_search_menu_provider_order_and_exclusions(self):
        providers = get_external_search_menu_providers()
        ids = [provider["id"] for provider in providers]

        self.assertEqual(ids[0], "camwhores")
        self.assertEqual(len(ids), 17)
        self.assertNotIn("camgirlfinder", ids)
        self.assertNotIn("vvebmgirls", ids)
        self.assertNotIn("weblove", ids)
        self.assertNotIn("camseek", ids)
        self.assertNotIn("camshows", ids)
        self.assertNotIn("camcaps", ids)
        self.assertIn("cumcams", ids)

    def test_every_menu_provider_builds_an_encoded_search_url(self):
        for provider in get_external_search_menu_providers():
            with self.subTest(provider=provider["id"]):
                url = build_external_search_url(
                    provider["id"],
                    "  fern francis  ",
                )
                self.assertIn("fern%20francis", url)
                self.assertTrue(url.startswith("https://"))
                self.assertNotIn("google.com/search", url)

    def test_uses_native_routes_for_reported_search_failures(self):
        expected_urls = {
            "curbate": "https://curbate.tv/search?q=roxellefox",
            "ecamrips": (
                "https://www.ecamrips.com/model/en/roxellefox/"
            ),
            "chaturflix": (
                "https://chaturflix.cam/performer/roxellefox"
            ),
            "webpussi": (
                "https://www.webpussi.com/search/?q=roxellefox"
            ),
            "cumcams": (
                "https://cumcams.cc/performer/roxellefox"
            ),
            "recurbate": (
                "https://recu.me/performer/roxellefox"
            ),
        }

        for provider_id, expected_url in expected_urls.items():
            with self.subTest(provider=provider_id):
                self.assertEqual(
                    build_external_search_url(
                        provider_id,
                        "roxellefox",
                    ),
                    expected_url,
                )

    def test_curbate_uses_search_route_and_preserves_username(self):
        self.assertEqual(
            build_external_search_url("curbate", "  _adelle  "),
            "https://curbate.tv/search?q=_adelle",
        )
        self.assertEqual(
            build_external_search_url("curbate", "eva_sweetyx"),
            "https://curbate.tv/search?q=eva_sweetyx",
        )

    def test_recurbate_preserves_leading_underscore(self):
        self.assertEqual(
            build_external_search_url("recurbate", "_adelle"),
            "https://recu.me/performer/_adelle",
        )

    def test_builds_all_menu_urls_in_display_order(self):
        urls = build_all_external_search_urls("fernfrancis")

        self.assertEqual(len(urls), 17)
        self.assertEqual(
            urls[0],
            "https://camwhores.tv/search/fernfrancis/",
        )
        self.assertEqual(
            urls[-1],
            "https://recordbate.com/performer/fernfrancis",
        )

    def test_rejects_unknown_provider(self):
        with self.assertRaisesRegex(
            ValueError,
            "Unknown external search provider",
        ):
            build_external_search_url("not-registered", "fernfrancis")

    def test_launches_search_in_new_chrome_tab(self):
        process_launcher = mock.Mock()

        with mock.patch("external_search.os.path.isfile", return_value=True):
            opened_url = launch_external_search(
                "camwhores",
                "fernfrancis",
                chrome_path=r"C:\Chrome\chrome.exe",
                process_launcher=process_launcher,
            )

        self.assertEqual(
            opened_url,
            "https://camwhores.tv/search/fernfrancis/",
        )
        process_launcher.assert_called_once_with(
            [
                r"C:\Chrome\chrome.exe",
                "--new-tab",
                "https://camwhores.tv/search/fernfrancis/",
            ],
            close_fds=True,
        )

    def test_launches_camgirlfinder_search_in_new_chrome_tab(self):
        process_launcher = mock.Mock()

        with mock.patch("external_search.os.path.isfile", return_value=True):
            opened_url = launch_external_search(
                "camgirlfinder",
                "yaracrave",
                chrome_path=r"C:\Chrome\chrome.exe",
                process_launcher=process_launcher,
            )

        self.assertEqual(
            opened_url,
            (
                "https://camgirlfinder.net/models"
                "?model=yaracrave&platform=&gender="
            ),
        )
        process_launcher.assert_called_once_with(
            [
                r"C:\Chrome\chrome.exe",
                "--new-tab",
                (
                    "https://camgirlfinder.net/models"
                    "?model=yaracrave&platform=&gender="
                ),
            ],
            close_fds=True,
        )

    def test_launches_all_searches_in_one_chrome_invocation(self):
        process_launcher = mock.Mock()

        with mock.patch("external_search.os.path.isfile", return_value=True):
            opened_urls = launch_all_external_searches(
                "fernfrancis",
                chrome_path=r"C:\Chrome\chrome.exe",
                process_launcher=process_launcher,
            )

        self.assertEqual(len(opened_urls), 17)
        process_launcher.assert_called_once_with(
            [
                r"C:\Chrome\chrome.exe",
                "--new-tab",
                *opened_urls,
            ],
            close_fds=True,
        )

    def test_controller_reports_launch_failure_without_crashing_gui(self):
        controller = AppController.__new__(AppController)
        controller._log = mock.Mock()

        with mock.patch(
            "gui_app.launch_external_search",
            side_effect=FileNotFoundError("Chrome missing"),
        ):
            result = controller.openExternalSearch("camwhores", "fernfrancis")

        self.assertFalse(result)
        controller._log.assert_called_once_with(
            "[ERROR] Could not open external search: Chrome missing"
        )

    def test_controller_reports_all_searches_without_crashing_gui(self):
        controller = AppController.__new__(AppController)
        controller._log = mock.Mock()

        with mock.patch(
            "gui_app.launch_all_external_searches",
            return_value=["https://example.test/1", "https://example.test/2"],
        ):
            result = controller.openAllExternalSearches("fernfrancis")

        self.assertTrue(result)
        controller._log.assert_called_once_with(
            "[INFO] Opened 2 external searches in Chrome for fernfrancis."
        )


if __name__ == "__main__":
    unittest.main()
