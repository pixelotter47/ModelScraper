import datetime
import inspect
import json
import os
import re
import tempfile
import unittest
import warnings
from types import SimpleNamespace
from unittest import mock

import ctb_core
from ctb_classifier import PageObservation, classify_page
from ctb_store import ChaturbateRunStore, MasterRepository
from location_profiles import LocationPolicy

from ctb_core import (
    CTBRunner,
    active_session_display_priority,
    active_session_profile_priority,
    build_location_profile_match,
)
from workflow_types import (
    OutcomeStatus,
    StepName,
    StepOutcome,
    VerificationRecord,
    VerificationVerdict,
)
from windows_subprocess import hidden_subprocess_kwargs


class _DummySwitchTo:
    def __init__(self, driver):
        self._driver = driver

    def window(self, handle):
        self._driver.active_handle = handle


class _DummyDriver:
    def __init__(self):
        self.window_handles = ["main"]
        self.active_handle = "main"
        self.current_url = ""
        self.title = ""
        self.page_source = "this room is unavailable in your region or gender"
        self.get_calls = []
        self.switch_to = _DummySwitchTo(self)

    def get(self, url):
        self.get_calls.append(url)
        self.current_url = url.rstrip("/")

    def find_elements(self, by, selector):
        return []

    def close(self):
        return None

    def quit(self):
        return None


class CTBRunnerTests(unittest.TestCase):
    def test_latest_session_uses_session_number_not_directory_mtime(self):
        with tempfile.TemporaryDirectory() as base_dir:
            older_number = os.path.join(base_dir, "session 9 24.07.2026")
            newer_number = os.path.join(base_dir, "session 10 24.07.2026")
            os.makedirs(older_number)
            os.makedirs(newer_number)
            os.utime(older_number, (2_000_000_000, 2_000_000_000))
            os.utime(newer_number, (1_000_000_000, 1_000_000_000))

            runner = CTBRunner(base_dir)

            self.assertEqual(runner.use_latest_session(), newer_number)

    def test_create_session_retries_a_concurrent_number_collision(self):
        frozen_now = datetime.datetime(2026, 7, 24, 12, 0, 0)
        with tempfile.TemporaryDirectory() as base_dir:
            runner = CTBRunner(base_dir)
            first = os.path.join(base_dir, "session 1 24.07.2026")
            os.makedirs(first)

            with mock.patch.object(
                runner, "_next_session_number", side_effect=[1, 2]
            ), mock.patch("ctb_core.datetime") as fake_datetime:
                fake_datetime.datetime.now.return_value = frozen_now
                created = runner.create_session()

            self.assertTrue(created.endswith("session 2 24.07.2026"))
            self.assertTrue(os.path.isdir(created))

    @unittest.skipUnless(os.name == "nt", "Windows-only process inspection")
    def test_chrome_process_lookup_hides_powershell_console(self):
        payload = json.dumps(
            {
                "ProcessId": 123,
                "CommandLine": "chrome.exe --user-data-dir=test",
            }
        )

        with mock.patch(
            "ctb_core.subprocess.check_output",
            return_value=payload,
        ) as check_output:
            processes = ctb_core._win_get_chrome_processes()

        self.assertEqual(
            processes,
            [(123, "chrome.exe --user-data-dir=test")],
        )
        expected_hidden = hidden_subprocess_kwargs()
        call_kwargs = check_output.call_args.kwargs
        self.assertEqual(
            call_kwargs["creationflags"],
            expected_hidden["creationflags"],
        )
        self.assertEqual(
            call_kwargs["startupinfo"].wShowWindow,
            expected_hidden["startupinfo"].wShowWindow,
        )

    def test_compile_master_list_includes_local_location_profile_matches(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 24.07.2026")
            os.makedirs(session_dir, exist_ok=True)
            policy = LocationPolicy(("DE",), ("Berlin",))
            runner = CTBRunner(base_dir, location_policy=policy)
            runner.set_session(session_dir)

            with open(
                os.path.join(session_dir, "FINAL_BLOCKED.txt"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("https://chaturbate.com/geo_blocked/\n")

            with open(
                os.path.join(session_dir, "cb_local_profile_matches.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(
                    [
                        {
                            "name": "https://chaturbate.com/local_location/",
                            "country": "DE",
                            "location": "Berlin, Germany",
                            "languages": "English",
                            "location_policy_fingerprint": policy.fingerprint,
                            "match_reasons": [
                                "country_code",
                                "location_term",
                            ],
                            "detected_at": "2026-07-24T12:00:00",
                        }
                    ],
                    handle,
                )

            self.assertEqual(runner.compile_master_list(), 2)
            models = {
                item["name"]: item for item in runner.get_master_list_models()
            }
            profile_model = models[
                "https://chaturbate.com/local_location/"
            ]

            self.assertTrue(profile_model["location_profile_match"])
            self.assertEqual(profile_model["country"], "DE")
            self.assertEqual(
                profile_model["profile_match"]["location"],
                "Berlin, Germany",
            )
            self.assertEqual(profile_model["profile_match"]["session"], 1)
            self.assertNotIn(
                "location_profile_match",
                models["https://chaturbate.com/geo_blocked/"],
            )

    def test_compile_promotes_only_confirmed_vpn_profile_matches(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 7 24.07.2026")
            os.makedirs(session_dir, exist_ok=True)
            policy = LocationPolicy(("DE",), ("Berlin",))
            runner = CTBRunner(base_dir, location_policy=policy)
            runner.set_session(session_dir)
            confirmed_url = "https://chaturbate.com/confirmed_location/"
            unconfirmed_url = "https://chaturbate.com/unconfirmed_location/"

            with open(
                os.path.join(session_dir, "FINAL_BLOCKED.txt"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(confirmed_url + "\n")
            with open(
                os.path.join(session_dir, "cb_vpn_profile_matches.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(
                    [
                        {
                            "name": confirmed_url,
                            "country": "DE",
                            "location": "Berlin",
                            "languages": "English",
                            "location_policy_fingerprint": policy.fingerprint,
                            "match_reasons": ["country_code", "location_term"],
                            "detected_at": "2026-07-24T16:40:00",
                        },
                        {
                            "name": unconfirmed_url,
                            "country": "DE",
                            "location": "Berlin",
                            "languages": "English",
                            "location_policy_fingerprint": policy.fingerprint,
                            "match_reasons": ["country_code", "location_term"],
                            "detected_at": "2026-07-24T16:40:00",
                        },
                    ],
                    handle,
                )

            runner.compile_master_list()
            models = {
                item["name"]: item for item in runner.get_master_list_models()
            }

            self.assertNotIn(unconfirmed_url, models)
            confirmed = models[confirmed_url]
            self.assertTrue(confirmed["vpn_location_profile_match"])
            self.assertEqual(confirmed["country"], "DE")
            self.assertEqual(confirmed["vpn_profile_match"]["session"], 7)

    def test_step_one_requests_vpn_profile_metadata(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 24.07.2026")
            os.makedirs(session_dir, exist_ok=True)
            runner = CTBRunner(base_dir)
            runner.set_session(session_dir)
            model_url = "https://chaturbate.com/test_model/"

            with mock.patch.object(
                runner,
                "fetch_models_via_api",
                return_value={model_url},
            ) as fetch_mock:
                runner.step_vpn_list(headless=True)

            fetch_mock.assert_called_once_with(
                "VPN List",
                robust=True,
                save_metadata=True,
                save_vpn_profile_matches=True,
                headless=True,
            )

    def test_partial_http_fetch_does_not_publish_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 24.07.2026")
            os.makedirs(session_dir)
            runner = CTBRunner(base_dir)
            runner.set_session(session_dir)
            runner._http_mode = "http_with_browser_fallback"
            with open(runner.paths.vpn_list, "wb") as handle:
                handle.write(
                    b"https://chaturbate.com/previous_snapshot/\n"
                )
            api = mock.Mock()
            api.collect_adaptive.return_value = SimpleNamespace(
                status=OutcomeStatus.INCOMPLETE,
                models=(),
                quarantined=(),
                passes=(),
            )
            with (
                mock.patch(
                    "ctb_core.ChaturbateApiClient", return_value=api
                ),
                mock.patch.object(
                    runner,
                    "_fetch_models_via_browser_api",
                    return_value=set(),
                ),
            ):
                outcome = runner.step_vpn_list(headless=True)
            self.assertEqual(outcome.status, OutcomeStatus.INCOMPLETE)
            with open(runner.paths.vpn_list, "rb") as handle:
                self.assertEqual(
                    handle.read(),
                    b"https://chaturbate.com/previous_snapshot/\n",
                )

    def test_healthy_http_snapshot_does_not_start_chrome(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 24.07.2026")
            os.makedirs(session_dir)
            runner = CTBRunner(base_dir)
            runner.set_session(session_dir)
            runner._http_mode = "http_with_browser_fallback"
            observation = SimpleNamespace(
                url="https://chaturbate.com/synthetic_http/",
                username="synthetic_http",
                seconds_online=120,
                country="",
                location="",
                languages="",
            )
            api = mock.Mock()
            api.collect_adaptive.return_value = SimpleNamespace(
                status=OutcomeStatus.SUCCEEDED,
                models=(observation,),
                quarantined=(),
                passes=(),
            )
            with (
                mock.patch(
                    "ctb_core.ChaturbateApiClient", return_value=api
                ),
                mock.patch.object(ctb_core, "_uc_chrome") as chrome,
            ):
                outcome = runner.step_vpn_list(headless=True)
            self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
            chrome.assert_not_called()

    def test_active_session_profile_priority(self):
        session_path = r"C:\sessions\session 7 24.07.2026"
        vpn_match = {
            "vpn_location_profile_match": True,
            "vpn_profile_match": {"session": 7},
        }
        local_match = {
            "location_profile_match": True,
            "profile_match": {"session": 7},
        }
        old_vpn_match = {
            "vpn_location_profile_match": True,
            "vpn_profile_match": {"session": 6},
        }

        self.assertEqual(active_session_profile_priority(vpn_match, session_path), 0)
        self.assertEqual(active_session_profile_priority(local_match, session_path), 1)
        self.assertEqual(
            active_session_profile_priority(old_vpn_match, session_path), 2
        )

    def test_display_priority_keeps_all_new_models_ahead_of_old_matches(self):
        session_path = r"C:\sessions\session 133 24.07.2026"
        new_vpn = {
            "name": "https://chaturbate.com/new_red/",
            "vpn_location_profile_match": True,
            "vpn_profile_match": {"session": 133},
        }
        new_local = {
            "name": "https://chaturbate.com/new_yellow/",
            "location_profile_match": True,
            "profile_match": {"session": 133},
        }
        new_generic = {
            "name": "https://chaturbate.com/new_generic/",
        }
        old_vpn = {
            "name": "https://chaturbate.com/old_red/",
            "vpn_location_profile_match": True,
            "vpn_profile_match": {"session": 133},
        }
        local_match = {
            "name": "https://chaturbate.com/yellow/",
            "location_profile_match": True,
            "profile_match": {"session": 133},
        }
        initial_models = {old_vpn["name"], local_match["name"]}

        self.assertEqual(
            active_session_display_priority(
                new_vpn,
                session_path,
                initial_models,
            ),
            0,
        )
        self.assertEqual(
            active_session_display_priority(
                new_local,
                session_path,
                initial_models,
            ),
            1,
        )
        self.assertEqual(
            active_session_display_priority(
                new_generic,
                session_path,
                initial_models,
            ),
            2,
        )
        self.assertEqual(
            active_session_display_priority(
                local_match,
                session_path,
                initial_models,
            ),
            3,
        )
        self.assertEqual(
            active_session_display_priority(
                old_vpn,
                session_path,
                initial_models,
            ),
            4,
        )

    def test_compile_master_list_timestamps_only_new_entries(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 24.07.2026")
            os.makedirs(session_dir, exist_ok=True)
            runner = CTBRunner(base_dir)

            with open(
                os.path.join(session_dir, "FINAL_BLOCKED.txt"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("https://chaturbate.com/legacy/\n")
                handle.write("https://chaturbate.com/preserved/\n")
                handle.write("https://chaturbate.com/new_model/\n")

            preserved_timestamp = "2026-07-23T14:35:12"
            with open(runner.get_master_json_path(), "w", encoding="utf-8") as handle:
                json.dump(
                    [
                        {
                            "name": "https://chaturbate.com/legacy/",
                            "date": "2026-07-24",
                            "session": 1,
                            "manual": False,
                        },
                        {
                            "name": "https://chaturbate.com/preserved/",
                            "date": "2026-07-23",
                            "session": 1,
                            "manual": False,
                            "timestamp": preserved_timestamp,
                        },
                    ],
                    handle,
                )

            runner.compile_master_list()
            models = {
                item["name"]: item
                for item in runner.get_master_list_models()
            }

            self.assertRegex(
                models["https://chaturbate.com/legacy/"]["timestamp"],
                r"^2026-07-24T\d{2}:\d{2}:\d{2}$",
            )
            self.assertEqual(
                models["https://chaturbate.com/preserved/"]["timestamp"],
                preserved_timestamp,
            )
            self.assertRegex(
                models["https://chaturbate.com/new_model/"]["timestamp"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$",
            )

    def test_master_verification_skips_local_location_profile_matches(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 24.07.2026")
            os.makedirs(session_dir, exist_ok=True)
            runner = CTBRunner(base_dir)
            runner.set_session(session_dir)
            model_url = "https://chaturbate.com/local_location/"

            with open(
                runner.get_master_list_path(), "w", encoding="utf-8"
            ) as handle:
                handle.write(model_url + "\n")
            with open(
                runner.get_master_json_path(), "w", encoding="utf-8"
            ) as handle:
                json.dump(
                    [
                        {
                            "name": model_url,
                            "date": "2026-07-24",
                            "session": 1,
                            "manual": False,
                            "location_profile_match": True,
                        }
                    ],
                    handle,
                )

            with mock.patch.object(ctb_core, "_uc_chrome") as chrome_mock:
                runner.verify_master_list(start_minimized=False)

            chrome_mock.assert_not_called()

    def test_unknown_master_verdict_never_removes_or_blacklists(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 24.07.2026")
            os.makedirs(session_dir, exist_ok=True)
            runner = CTBRunner(base_dir)
            runner.set_session(session_dir)
            url = "https://chaturbate.com/synthetic_unknown/"
            MasterRepository(base_dir).publish(
                [{"name": url, "date": "2026-07-24", "session": 1}]
            )
            blacklist = runner.get_global_blacklist_path()
            with open(blacklist, "wb") as handle:
                handle.write(
                    b"https://chaturbate.com/existing_blacklist/\n"
                )
            before = open(blacklist, "rb").read()

            def provider(model_url, attempt):
                return VerificationRecord(
                    original_url=model_url,
                    verdict=VerificationVerdict.UNKNOWN,
                    reason_code="navigation_timeout",
                    attempt=attempt,
                    timestamp="2026-07-24T12:00:00+00:00",
                    observed_url="",
                    run_id="safe-run",
                )

            outcome = runner.verify_master_list(
                start_minimized=False,
                verdict_provider=provider,
                run_id="safe-run",
            )
            self.assertEqual(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(open(blacklist, "rb").read(), before)
            models = MasterRepository(base_dir).load(required=True)
            self.assertEqual([item["name"] for item in models], [url])
            self.assertTrue(models[0]["pending_verification"])

    def test_corrupt_master_json_blocks_browser_and_mutation(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 24.07.2026")
            os.makedirs(session_dir, exist_ok=True)
            runner = CTBRunner(base_dir)
            runner.set_session(session_dir)
            with open(
                runner.get_master_json_path(), "wb"
            ) as handle:
                handle.write(b"{broken")
            with open(
                runner.get_master_list_path(), "wb"
            ) as handle:
                handle.write(
                    b"https://chaturbate.com/synthetic_a/\n"
                )
            with mock.patch.object(ctb_core, "_uc_chrome") as chrome_mock:
                outcome = runner.verify_master_list(start_minimized=False)
            chrome_mock.assert_not_called()
            self.assertEqual(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "master_metadata_invalid")

    def test_verify_candidates_skips_models_already_in_master(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 16.04.2026")
            os.makedirs(session_dir, exist_ok=True)

            runner = CTBRunner(base_dir)
            runner.set_session(session_dir)
            store = ChaturbateRunStore(session_dir)
            context = store.create_run()

            candidate_record = store.artifact_lines(
                context,
                StepName.COMPARE,
                "cb_candidates.txt",
                [
                    "https://chaturbate.com/alice/",
                    "https://chaturbate.com/bob/",
                ],
                "candidates",
            )
            store.record_step(
                context,
                StepOutcome.succeeded(
                    run_id=context.run_id,
                    generation_id=context.generation_id,
                    platform=context.platform,
                    step=StepName.COMPARE,
                    artifacts=(candidate_record,),
                ),
            )
            store.publish_lines(
                context, candidate_record, "cb_candidates.txt"
            )

            with open(runner.get_master_json_path(), "w", encoding="utf-8") as handle:
                json.dump(
                    [
                        {
                            "name": "https://chaturbate.com/alice/",
                            "date": "2026-04-15",
                            "session": 1,
                            "manual": False,
                        }
                    ],
                    handle,
                    indent=2,
                )

            driver = _DummyDriver()
            with (
                mock.patch.object(ctb_core, "_uc_chrome", return_value=driver),
                mock.patch.object(ctb_core, "IS_WINDOWS", False),
                mock.patch.object(runner, "compile_master_list") as compile_mock,
            ):
                runner.verify_candidates(
                    start_minimized=False,
                    reveal_on_captcha=False,
                )

            # alice is already confirmed in the master list, so the browser
            # must not spend a navigation on her. The leading homepage hit is
            # the one-off visit that accepts the terms splash.
            self.assertEqual(
                driver.get_calls,
                [
                    "https://chaturbate.com/",
                    "https://chaturbate.com/bob/",
                ],
            )
            # ...but she still belongs to this session's blocked output.
            with open(runner.paths.verified, "r", encoding="utf-8") as handle:
                verified = [line.strip() for line in handle if line.strip()]
            self.assertEqual(
                verified,
                [
                    "https://chaturbate.com/alice/",
                    "https://chaturbate.com/bob/",
                ],
            )
            self.assertTrue(os.path.exists(runner.paths.candidates))
            compile_mock.assert_called_once()

    def test_verify_candidates_records_a_browser_startup_crash(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 16.04.2026")
            os.makedirs(session_dir, exist_ok=True)

            runner = CTBRunner(base_dir)
            runner.set_session(session_dir)
            store = ChaturbateRunStore(session_dir)
            context = store.create_run()

            candidate_record = store.artifact_lines(
                context,
                StepName.COMPARE,
                "cb_candidates.txt",
                ["https://chaturbate.com/bob/"],
                "candidates",
            )
            store.record_step(
                context,
                StepOutcome.succeeded(
                    run_id=context.run_id,
                    generation_id=context.generation_id,
                    platform=context.platform,
                    step=StepName.COMPARE,
                    artifacts=(candidate_record,),
                ),
            )
            store.publish_lines(
                context, candidate_record, "cb_candidates.txt"
            )

            with mock.patch.object(
                ctb_core,
                "_uc_chrome",
                side_effect=RuntimeError("chrome would not start"),
            ):
                outcome = runner.verify_candidates(
                    start_minimized=False,
                    reveal_on_captcha=False,
                )

            # A crash while opening the browser has to leave the same trail as
            # every other Step 4 failure, otherwise the run log can only say
            # "workflow_step_failed" and the manifest keeps no step at all.
            self.assertEqual(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "verify_crashed")
            self.assertIn("chrome would not start", outcome.error_message)
            manifest = store.load_manifest(context)
            self.assertEqual(
                manifest["steps"]["step4_verify"]["error_code"],
                "verify_crashed",
            )
            self.assertIn(
                "chrome would not start",
                manifest["steps"]["step4_verify"]["error_message"],
            )

    def test_empty_candidate_verification_still_compiles_profile_matches(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 24.07.2026")
            os.makedirs(session_dir, exist_ok=True)
            runner = CTBRunner(base_dir)
            runner.set_session(session_dir)
            store = ChaturbateRunStore(session_dir)
            context = store.create_run()
            candidate_record = store.artifact_lines(
                context,
                StepName.COMPARE,
                "cb_candidates.txt",
                [],
                "candidates",
            )
            store.record_step(
                context,
                StepOutcome.succeeded(
                    run_id=context.run_id,
                    generation_id=context.generation_id,
                    platform=context.platform,
                    step=StepName.COMPARE,
                    artifacts=(candidate_record,),
                ),
            )
            store.publish_lines(
                context, candidate_record, "cb_candidates.txt"
            )

            with mock.patch.object(
                runner, "compile_master_list"
            ) as compile_mock:
                runner.verify_candidates(
                    start_minimized=False,
                    reveal_on_captcha=False,
                )

            compile_mock.assert_called_once()

    def test_legacy_candidate_verification_requires_explicit_adoption(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 16.04.2026")
            os.makedirs(session_dir, exist_ok=True)

            runner = CTBRunner(base_dir)
            runner.set_session(session_dir)

            with open(runner.paths.candidates, "w", encoding="utf-8") as handle:
                handle.write("https://chaturbate.com/alice/\n")

            with open(runner.get_master_json_path(), "w", encoding="utf-8") as handle:
                json.dump(
                    [
                        {
                            "name": "https://chaturbate.com/alice/",
                            "date": "2026-04-15",
                            "session": 1,
                            "manual": False,
                        }
                    ],
                    handle,
                    indent=2,
                )

            with mock.patch.object(ctb_core, "_uc_chrome") as chrome_mock:
                outcome = runner.verify_candidates(
                    start_minimized=False,
                    reveal_on_captcha=False,
                )

            chrome_mock.assert_not_called()
            self.assertEqual(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(
                outcome.error_code, "legacy_session_requires_adoption"
            )
            self.assertFalse(os.path.exists(runner.paths.verified))
            self.assertTrue(os.path.exists(runner.paths.candidates))


class MasterListEncodingTests(unittest.TestCase):
    """The master list is UTF-8; reading it as cp1252 silently degrades it."""

    ENTRY = {
        "name": "https://chaturbate.com/synthetic_unicode_model/",
        "date": "2026-01-24",
        "session": 42,
        "manual": False,
        "timestamp": "2026-01-24T22:28:55",
        "country": "CA",
        "location_profile_match": True,
        # Synthetic metadata exercises emoji and accented text.
        "profile_match": {
            "location": "Montréal ❤️", "languages": "Français",
            "location_policy_fingerprint": LocationPolicy(("CA",)).fingerprint,
        },
    }

    def _runner_with_master(self, base_dir):
        runner = CTBRunner(base_dir, location_policy=LocationPolicy(("CA",)))
        with open(
            runner.get_master_json_path(), "w", encoding="utf-8"
        ) as handle:
            json.dump([self.ENTRY], handle, ensure_ascii=False)
        with open(
            runner.get_master_list_path(), "w", encoding="utf-8"
        ) as handle:
            handle.write(self.ENTRY["name"] + "\n")
        return runner

    def test_non_ascii_master_metadata_survives(self):
        with tempfile.TemporaryDirectory() as base_dir:
            models = self._runner_with_master(base_dir).get_master_list_models()
        self.assertEqual(len(models), 1)
        entry = models[0]
        # The degraded text fallback keeps only name/date/session, which is
        # what stripped the flags, highlights and sorting from the UI.
        self.assertEqual(entry.get("country"), "CA")
        self.assertTrue(entry.get("location_profile_match"))
        self.assertEqual(entry.get("timestamp"), "2026-01-24T22:28:55")
        self.assertIn("❤️", entry["profile_match"]["location"])

    def test_master_list_is_never_read_with_the_platform_default_encoding(self):
        """Fails on Windows for any open() that omits encoding."""
        with tempfile.TemporaryDirectory() as base_dir:
            runner = self._runner_with_master(base_dir)
            with warnings.catch_warnings():
                warnings.simplefilter("error", EncodingWarning)
                models = runner.get_master_list_models()
        self.assertEqual(models[0].get("country"), "CA")

    def test_unreadable_metadata_is_reported_not_swallowed(self):
        logs = []
        with tempfile.TemporaryDirectory() as base_dir:
            runner = CTBRunner(base_dir, location_policy=LocationPolicy(("CA",)))
            runner.set_logger(logs.append)
            with open(
                runner.get_master_json_path(), "w", encoding="utf-8"
            ) as handle:
                handle.write("{ this is not json")
            with open(
                runner.get_master_list_path(), "w", encoding="utf-8"
            ) as handle:
                handle.write("https://chaturbate.com/synthetic_unicode_model/\n")
            models = runner.get_master_list_models()
        self.assertEqual(len(models), 1)
        self.assertTrue(
            any("Could not read master metadata" in line for line in logs),
            logs,
        )


class AdaptivePassTests(unittest.TestCase):
    @staticmethod
    def _models(count, offset=0):
        return {
            f"https://chaturbate.com/model{index}/"
            for index in range(offset, offset + count)
        }

    def test_identical_passes_are_stable(self):
        snapshot = self._models(1000)
        self.assertTrue(ctb_core.passes_are_stable(snapshot, set(snapshot)))

    def test_small_churn_is_still_stable(self):
        previous = self._models(1000)
        current = set(previous)
        for url in list(previous)[:5]:
            current.discard(url)
        current |= self._models(5, offset=5000)
        self.assertTrue(ctb_core.passes_are_stable(previous, current))

    def test_large_churn_is_not_stable(self):
        previous = self._models(1000)
        current = self._models(1000, offset=500)
        self.assertFalse(ctb_core.passes_are_stable(previous, current))

    def test_big_count_change_is_not_stable(self):
        previous = self._models(1000)
        self.assertFalse(
            ctb_core.passes_are_stable(previous, self._models(900))
        )

    def test_empty_passes_are_never_stable(self):
        self.assertFalse(ctb_core.passes_are_stable(set(), set()))
        self.assertFalse(
            ctb_core.passes_are_stable(self._models(10), set())
        )

    def test_adaptive_is_the_default_mode(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MODEL_SCRAPER_CTB_PASSES_MODE", None)
            runner = ctb_core.CTBRunner(tempfile.gettempdir())
        self.assertEqual(runner._passes_mode, "adaptive")

    def test_fixed_mode_remains_available(self):
        with mock.patch.dict(
            os.environ, {"MODEL_SCRAPER_CTB_PASSES_MODE": "fixed"}
        ):
            runner = ctb_core.CTBRunner(tempfile.gettempdir())
        self.assertEqual(runner._passes_mode, "fixed")

    def test_invalid_mode_is_rejected(self):
        with mock.patch.dict(
            os.environ, {"MODEL_SCRAPER_CTB_PASSES_MODE": "nonsense"}
        ):
            with self.assertRaises(ValueError):
                ctb_core.CTBRunner(tempfile.gettempdir())

    def _run_snapshot(self, mode, pass_payloads):
        """Drive the browser snapshot loop and report how many passes ran."""

        class FakeBody:
            def __init__(self, text):
                self.text = text

        class FakeDriver:
            def __init__(self):
                self.pass_index = -1
                self.payload = "{}"

            def get(self, url):
                if "offset=0" in url:
                    self.pass_index += 1
                index = min(self.pass_index, len(pass_payloads) - 1)
                self.payload = json.dumps(
                    {
                        "results": [
                            {"username": name, "seconds_online": 3600}
                            for name in pass_payloads[index]
                        ]
                    }
                )

            def find_element(self, *_args):
                return FakeBody(self.payload)

            def quit(self):
                return None

        logs = []
        with tempfile.TemporaryDirectory() as base_dir:
            session_dir = os.path.join(base_dir, "session 1 16.04.2026")
            os.makedirs(session_dir, exist_ok=True)
            with mock.patch.dict(
                os.environ, {"MODEL_SCRAPER_CTB_PASSES_MODE": mode}
            ):
                runner = ctb_core.CTBRunner(base_dir)
            runner.set_logger(logs.append)
            runner.set_session(session_dir)
            driver = FakeDriver()
            with (
                mock.patch.object(ctb_core, "_uc_chrome", return_value=driver),
                mock.patch.object(ctb_core.time, "sleep", lambda *_: None),
            ):
                models = runner._fetch_models_via_browser_api(
                    "Test", robust=True
                )
        started = sum(1 for line in logs if re.search(r"\] Pass \d+/\d+", line))
        return started, models, logs

    def test_adaptive_stops_once_two_passes_agree(self):
        steady = [f"model{index}" for index in range(200)]
        started, models, logs = self._run_snapshot(
            "adaptive", [steady, steady, steady, steady, steady]
        )
        self.assertEqual(started, 2)
        self.assertEqual(len(models), 200)
        self.assertTrue(any("settled after 2 passes" in line for line in logs))

    def test_adaptive_uses_every_pass_while_the_list_churns(self):
        churning = [
            [f"model{index}" for index in range(start, start + 200)]
            for start in (0, 150, 300, 450, 600)
        ]
        started, _models, _logs = self._run_snapshot("adaptive", churning)
        self.assertEqual(started, 5)

    def test_fixed_mode_always_runs_five_passes(self):
        steady = [f"model{index}" for index in range(200)]
        started, _models, _logs = self._run_snapshot(
            "fixed", [steady, steady, steady, steady, steady]
        )
        self.assertEqual(started, 5)

    def test_adaptive_still_unions_models_seen_in_any_pass(self):
        first = [f"model{index}" for index in range(200)]
        second = first + ["late_arrival"]
        started, models, _logs = self._run_snapshot(
            "adaptive", [first, second, second, second, second]
        )
        # One extra model out of 200 is well inside the stability thresholds,
        # so the run stops early -- but the union still keeps that model.
        self.assertEqual(started, 2)
        self.assertIn("https://chaturbate.com/late_arrival/", models)


class CaptchaHandlingTests(unittest.TestCase):
    def test_detects_cloudflare_interstitials(self):
        self.assertTrue(ctb_core.page_has_captcha("Just a moment...", ""))
        self.assertTrue(ctb_core.page_has_captcha("", "<div id=cf-challenge>"))
        self.assertTrue(
            ctb_core.page_has_captcha("", "Checking your browser before access")
        )
        self.assertTrue(ctb_core.page_has_captcha("", "Verify you are human"))

    def test_ignores_normal_room_pages(self):
        self.assertFalse(
            ctb_core.page_has_captcha("alice - Chaturbate", "<div id=room_view_container>")
        )
        self.assertFalse(ctb_core.page_has_captcha("", ""))

    def test_verification_profile_is_stable_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ, {"MODELSCRAPER_VERIFY_PROFILE": os.path.join(tmp, "p")}
            ):
                first = ctb_core.persistent_verify_profile_dir()
                second = ctb_core.persistent_verify_profile_dir()
        self.assertEqual(first, second)

    def test_verification_never_runs_headless(self):
        """Headless Chrome is fingerprinted by Cloudflare and cannot be solved."""
        source = inspect.getsource(ctb_core.CTBRunner.verify_candidates)
        self.assertNotIn("--headless", source)
        source = inspect.getsource(ctb_core.CTBRunner.verify_master_list)
        self.assertNotIn("--headless", source)

    def test_await_captcha_clear_reveals_then_rehides_window(self):
        class FakeHider:
            def __init__(self):
                self.calls = []

            def show(self):
                self.calls.append("show")
                return True

            def hide(self):
                self.calls.append("hide")
                return True

        class FakeDriver:
            def __init__(self, pages):
                self._pages = list(pages)
                self.title = ""

            @property
            def page_source(self):
                return self._pages.pop(0) if self._pages else ""

        runner = ctb_core.CTBRunner.__new__(ctb_core.CTBRunner)
        runner.log = lambda *_: None
        runner._should_stop = lambda: False
        hider = FakeHider()
        driver = FakeDriver(["Just a moment", "Just a moment", "<div id=room_view_container>"])

        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            cleared = runner._await_captcha_clear(driver, hider, rehide=True)

        self.assertTrue(cleared)
        self.assertEqual(hider.calls, ["show", "hide"])

    def test_block_excerpt_uses_visible_text_not_html_head(self):
        """page_source[:240] is only <head> boilerplate, so notices were missed."""

        class FakeElement:
            text = "Sorry, this model is not available in your region or gender."

        class FakeDriver:
            page_source = (
                '<html xmlns="http://www.w3.org/1999/xhtml" lang="en" '
                'style="--vh: 9.93px;"><head><meta http-equiv="origin-trial" '
                'content="' + "A" * 400 + '"></head><body>'
                "Not available in your region or gender.</body></html>"
            )

            def find_element(self, *_args):
                return FakeElement()

        excerpt = ctb_core._visible_block_excerpt(FakeDriver())
        self.assertIn("region or gender", excerpt.lower())

    def test_block_excerpt_falls_back_to_marker_slice_when_body_empty(self):
        class FakeDriver:
            page_source = (
                "<head>" + "x" * 500 + "</head><body>blocked by region or gender rules</body>"
            )

            def find_element(self, *_args):
                raise RuntimeError("no body")

        excerpt = ctb_core._visible_block_excerpt(FakeDriver())
        self.assertIn("region or gender", excerpt.lower())

    def test_offline_room_is_not_positive_proof_of_access(self):
        """A rendered but offline room says nothing about the region."""
        record = classify_page(
            PageObservation(
                original_url="https://chaturbate.com/synthetic_blocked_model/",
                current_url="https://chaturbate.com/synthetic_blocked_model/",
                has_room_container=True,
                has_offline_notice=True,
            )
        )
        self.assertEqual(record.verdict, VerificationVerdict.UNKNOWN)
        self.assertEqual(record.reason_code, "room_offline")

    def test_live_room_is_accessible(self):
        record = classify_page(
            PageObservation(
                original_url="https://chaturbate.com/synthetic_blocked_model/",
                current_url="https://chaturbate.com/synthetic_blocked_model/",
                has_room_container=True,
                has_offline_notice=False,
            )
        )
        self.assertEqual(record.verdict, VerificationVerdict.ACCESSIBLE)

    def test_offline_wording_is_detected(self):
        self.assertTrue(ctb_core.page_is_offline("Room is currently offline"))
        self.assertTrue(
            ctb_core.page_is_offline(
                "The member you are trying to view is currently offline."
            )
        )
        self.assertFalse(ctb_core.page_is_offline("synthetic_blocked_model make me cum"))
        self.assertFalse(ctb_core.page_is_offline(""))

    def test_room_selector_covers_the_live_markup(self):
        """room_view_container is gone; the real containers must be matched."""
        for marker in (
            "[data-testid='room-subject']",
            "[data-testid='room-tabs']",
            "div.chat_room",
            "div.roomPage",
        ):
            self.assertIn(marker, ctb_core.ROOM_CONTAINER_SELECTOR)
        self.assertIn("#room_view_container", ctb_core.ROOM_CONTAINER_SELECTOR)

    def test_login_redirect_is_named_rather_than_unexpected(self):
        record = classify_page(
            PageObservation(
                original_url="https://chaturbate.com/synthetic_offline_model/",
                current_url=(
                    "https://chaturbate.com/auth/login/"
                    "?next=/roomlogin/synthetic_offline_model/"
                ),
            )
        )
        self.assertEqual(record.verdict, VerificationVerdict.UNKNOWN)
        self.assertEqual(record.reason_code, "login_required")

    def test_block_notice_now_classifies_as_blocked(self):
        record = classify_page(
            PageObservation(
                original_url="https://chaturbate.com/alice/",
                current_url="https://chaturbate.com/alice/",
                has_denied_notice=False,
                has_room_container=False,
                page_text_excerpt=(
                    "Not available in your region or gender."
                ),
            )
        )
        self.assertEqual(record.verdict, VerificationVerdict.BLOCKED)
        self.assertEqual(record.reason_code, "region_or_gender_notice")

    def test_age_gate_is_detected_from_visible_text(self):
        self.assertTrue(
            ctb_core.page_has_age_gate(
                "YOU MUST BE OVER 18 AND AGREE TO THE TERMS BELOW BEFORE CONTINUING"
            )
        )
        self.assertFalse(ctb_core.page_has_age_gate("alice is offline"))
        self.assertFalse(ctb_core.page_has_age_gate(""))

    def test_age_gate_is_dismissed_by_clicking_the_entry_control(self):
        gate_text = "YOU MUST BE OVER 18 AND AGREE TO THE TERMS BELOW BEFORE CONTINUING"

        class FakeButton:
            def __init__(self, driver):
                self.driver = driver

            def is_displayed(self):
                return True

            def click(self):
                self.driver.accepted = True

        class FakeBody:
            def __init__(self, driver):
                self.driver = driver

            @property
            def text(self):
                return "room content" if self.driver.accepted else gate_text

        class FakeDriver:
            def __init__(self):
                self.accepted = False

            def find_element(self, *_args):
                return FakeBody(self)

            def find_elements(self, *_args):
                return [FakeButton(self)] if not self.accepted else []

        driver = FakeDriver()
        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            self.assertTrue(ctb_core.dismiss_age_gate(driver))
        self.assertTrue(driver.accepted)

    def test_age_gate_dismissal_reports_failure_when_no_control_exists(self):
        class FakeBody:
            text = "YOU MUST BE OVER 18 AND AGREE TO THE TERMS BELOW"

        class FakeDriver:
            def find_element(self, *_args):
                return FakeBody()

            def find_elements(self, *_args):
                return []

        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            self.assertFalse(ctb_core.dismiss_age_gate(FakeDriver()))

    def test_age_gate_dismissal_is_a_noop_when_already_accepted(self):
        class FakeBody:
            text = "alice's room"

        class FakeDriver:
            def find_element(self, *_args):
                return FakeBody()

            def find_elements(self, *_args):
                raise AssertionError("must not look for the entry control")

        self.assertTrue(ctb_core.dismiss_age_gate(FakeDriver()))

    def test_wait_for_age_gate_clear_returns_when_a_human_accepts(self):
        class FakeBody:
            def __init__(self, driver):
                self.driver = driver

            @property
            def text(self):
                self.driver.polls += 1
                if self.driver.polls >= 3:
                    return "room content"
                return "YOU MUST BE OVER 18 AND AGREE TO THE TERMS BELOW"

        class FakeDriver:
            polls = 0

            def find_element(self, *_args):
                return FakeBody(self)

        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            self.assertTrue(ctb_core.wait_for_age_gate_clear(FakeDriver()))

    def test_wait_for_age_gate_clear_honours_stop_requests(self):
        class FakeBody:
            text = "YOU MUST BE OVER 18 AND AGREE TO THE TERMS BELOW"

        class FakeDriver:
            def find_element(self, *_args):
                return FakeBody()

        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            self.assertFalse(
                ctb_core.wait_for_age_gate_clear(
                    FakeDriver(), should_stop=lambda: True
                )
            )

    def test_blank_page_is_not_reported_as_accepted(self):
        """An unreadable page must never pass as a clean one."""

        class FakeDriver:
            def find_element(self, *_args):
                raise RuntimeError("nothing rendered")

            @property
            def page_source(self):
                return ""

            def execute_script(self, *_args):
                return "loading"

            def find_elements(self, *_args):
                return []

        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            self.assertFalse(
                ctb_core.dismiss_age_gate(FakeDriver(), page_timeout=0.0)
            )

    def test_blank_page_does_not_end_the_manual_wait(self):
        class FakeDriver:
            def find_element(self, *_args):
                raise RuntimeError("nothing rendered")

            @property
            def page_source(self):
                return ""

        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            self.assertFalse(
                ctb_core.wait_for_age_gate_clear(FakeDriver(), timeout=0.0)
            )

    def test_wait_for_page_text_returns_once_the_document_renders(self):
        class FakeBody:
            def __init__(self, driver):
                self.driver = driver

            @property
            def text(self):
                self.driver.polls += 1
                return "room content" if self.driver.polls >= 3 else ""

        class FakeDriver:
            polls = 0
            page_source = ""

            def find_element(self, *_args):
                return FakeBody(self)

            def execute_script(self, *_args):
                return "complete"

        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            self.assertEqual(
                ctb_core.wait_for_page_text(FakeDriver()), "room content"
            )

    def test_age_gate_state_distinguishes_blank_from_clear(self):
        def driver_with(text, source=""):
            class FakeBody:
                def __init__(self):
                    self.text = text

            class FakeDriver:
                page_source = source

                def find_element(self, *_args):
                    return FakeBody()

                def execute_script(self, *_args):
                    return "complete"

            return FakeDriver()

        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            self.assertEqual(
                ctb_core.age_gate_state(driver_with(""), timeout=0.0),
                "unreadable",
            )
            self.assertEqual(
                ctb_core.age_gate_state(
                    driver_with("YOU MUST BE OVER 18 AND AGREE TO THE TERMS"),
                    timeout=0.0,
                ),
                "present",
            )
            self.assertEqual(
                ctb_core.age_gate_state(
                    driver_with("HOME DISCOVER TAGS PRIVATE SHOWS"), timeout=0.0
                ),
                "clear",
            )

    def test_age_gate_markup_is_saved_for_diagnosis(self):
        class FakeDriver:
            page_source = "<html><body>splash markup</body></html>"

        with tempfile.TemporaryDirectory() as tmp:
            path = ctb_core.dump_age_gate_markup(FakeDriver(), tmp)
            self.assertTrue(path)
            with open(path, encoding="utf-8") as handle:
                self.assertIn("splash markup", handle.read())

    def test_hidden_controls_are_not_clicked(self):
        class HiddenButton:
            clicked = False

            def is_displayed(self):
                return False

            def click(self):
                HiddenButton.clicked = True

        class FakeBody:
            text = "YOU MUST BE OVER 18 AND AGREE TO THE TERMS BELOW"

        class FakeDriver:
            def find_element(self, *_args):
                return FakeBody()

            def find_elements(self, *_args):
                return [HiddenButton()]

        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            self.assertFalse(ctb_core.dismiss_age_gate(FakeDriver()))
        self.assertFalse(HiddenButton.clicked)

    def test_await_captcha_clear_times_out_without_hanging(self):
        class FakeDriver:
            title = "Just a moment"
            page_source = "cf-challenge"

        runner = ctb_core.CTBRunner.__new__(ctb_core.CTBRunner)
        runner.log = lambda *_: None
        runner._should_stop = lambda: False

        with mock.patch.object(ctb_core.time, "sleep", lambda *_: None):
            cleared = runner._await_captcha_clear(
                FakeDriver(), hider=None, rehide=False, timeout=0.0
            )

        self.assertFalse(cleared)


if __name__ == "__main__":
    unittest.main()
