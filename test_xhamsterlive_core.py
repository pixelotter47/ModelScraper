import tempfile
import unittest
from unittest import mock

from xhamsterlive_core import XHamsterLiveRunner


class _DummyElement:
    def __init__(self, text):
        self.text = text


class _DummyDriver:
    def __init__(self, texts=None):
        self._texts = texts or []

    def find_elements(self, by, selector):
        return [_DummyElement(text) for text in self._texts]


class XHamsterLiveRunnerTests(unittest.TestCase):
    def test_set_session_uses_xhl_prefixed_paths(self):
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as session_dir:
            runner = XHamsterLiveRunner(base_dir)
            runner.set_session(session_dir)

            self.assertTrue(runner.paths.vpn_list.endswith("xhl_vpn_list.txt"))
            self.assertTrue(runner.paths.local_list.endswith("xhl_local_list.txt"))
            self.assertTrue(runner.paths.candidates.endswith("xhl_candidates.txt"))
            self.assertTrue(runner.paths.debug.endswith("xhl_debug_log.txt"))
            self.assertTrue(runner.paths.metadata.endswith("xhl_metadata.json"))

    def test_hidden_page_is_blocked_not_disabled(self):
        runner = XHamsterLiveRunner("")
        driver = _DummyDriver(
            ["Account Hidden", "SpoilM3Mor3's account has been hidden."]
        )
        page_src_lower = """
            <div class="account-disabled-page model-deleted-page">
              <div class="account-disabled-header">Account Hidden</div>
              <div class="account-disabled-description">
                SpoilM3Mor3's account has been hidden.
              </div>
            </div>
        """.lower()

        self.assertTrue(runner._is_blocked_page(driver, page_src_lower))
        self.assertFalse(runner._is_disabled_page(driver, page_src_lower))

    def test_disabled_page_stays_disabled(self):
        runner = XHamsterLiveRunner("")
        driver = _DummyDriver(
            ["Account Disabled", "SpoilM3Mor3's account has been disabled."]
        )
        page_src_lower = """
            <div class="account-disabled-page model-deleted-page">
              <div class="account-disabled-header">Account Disabled</div>
              <div class="account-disabled-description">
                SpoilM3Mor3's account has been disabled.
              </div>
            </div>
        """.lower()

        self.assertFalse(runner._is_blocked_page(driver, page_src_lower))
        self.assertTrue(runner._is_disabled_page(driver, page_src_lower))

    def test_platform_identity_and_url_construction_golden(self):
        runner = XHamsterLiveRunner("")
        self.assertEqual(XHamsterLiveRunner.platform_name, "XHamsterLive")
        self.assertEqual(XHamsterLiveRunner.site_domain, "xhamsterlive.com")
        self.assertEqual(XHamsterLiveRunner.api_host, "go.xhamsterlive.com")
        self.assertEqual(XHamsterLiveRunner.profile_dir_name, "uc_profile_xhl")
        self.assertEqual(
            runner._model_url("Synthetic-Model@1"),
            "https://xhamsterlive.com/Synthetic-Model@1/",
        )
        self.assertEqual(
            runner._api_models_url(500, 400),
            "https://go.xhamsterlive.com/api/models?limit=500&offset=400",
        )

    def test_step_one_and_two_fetch_call_order_golden(self):
        with tempfile.TemporaryDirectory() as base_dir:
            runner = XHamsterLiveRunner(base_dir)
            runner.set_session(base_dir)
            with mock.patch.object(
                runner, "fetch_models_all", return_value=set()
            ) as fetch:
                runner.step_vpn_list()
                runner.step_local_list()
        self.assertEqual(len(fetch.call_args_list), 2)
        step1_call, step2_call = fetch.call_args_list
        self.assertEqual(step1_call.args, ("VPN List",))
        self.assertEqual(
            step1_call.kwargs, {"robust": True, "save_metadata": True}
        )
        self.assertEqual(step2_call.args, ("Local List",))
        self.assertEqual(step2_call.kwargs, {"robust": True})

    def test_final_and_master_filenames_golden(self):
        with tempfile.TemporaryDirectory() as base_dir:
            runner = XHamsterLiveRunner(base_dir)
            runner.set_session(base_dir)
            self.assertTrue(
                runner.paths.verified.endswith("FINAL_BLOCKED.txt")
            )
            self.assertTrue(
                runner.get_master_list_path().endswith("MASTER_BLOCKED.txt")
            )
            self.assertTrue(
                runner.get_master_json_path().endswith(
                    "MASTER_BLOCKED_DATA.json"
                )
            )

    def test_compare_lists_saves_raw_difference_without_recheck(self):
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as session_dir:
            runner = XHamsterLiveRunner(base_dir)
            runner.set_session(session_dir)

            with open(runner.paths.vpn_list, "w", encoding="utf-8") as handle:
                handle.write("https://xhamsterlive.com/alice/\n")
                handle.write("https://xhamsterlive.com/bob/\n")
                handle.write("https://xhamsterlive.com/carla/\n")

            with open(runner.paths.local_list, "w", encoding="utf-8") as handle:
                handle.write("https://xhamsterlive.com/bob/\n")

            with open(runner.paths.metadata, "w", encoding="utf-8") as handle:
                handle.write('{"counts":{"alice":1,"bob":2,"carla":2},"passes":2}')

            def unexpected_fetch(*args, **kwargs):
                raise AssertionError("compare_lists should not re-fetch local data")

            runner.fetch_models_all = unexpected_fetch
            runner.compare_lists()

            with open(runner.paths.candidates, "r", encoding="utf-8") as handle:
                candidates = [line.strip() for line in handle if line.strip()]

            self.assertEqual(
                candidates,
                [
                    "https://xhamsterlive.com/alice/",
                    "https://xhamsterlive.com/carla/",
                ],
            )


if __name__ == "__main__":
    unittest.main()
