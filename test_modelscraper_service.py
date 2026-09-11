import datetime
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from ctb_store import ChaturbateRunStore
from modelscraper_service import ModelScraperService
from workflow_types import RunOutcome, StepName, StepOutcome


class _FakeVpn:
    def __init__(self):
        self.calls = []

    def connect_ireland(self):
        self.calls.append("connect_ireland")

    def disconnect(self):
        self.calls.append("disconnect")

    def remove_chrome_from_split_tunnel(self):
        self.calls.append("remove_chrome_from_split_tunnel")

    def add_chrome_to_split_tunnel(self):
        self.calls.append("add_chrome_to_split_tunnel")


class _FakeRunner:
    def __init__(self, session_folder, name="runner"):
        self.name = name
        self.base_dir = session_folder
        self.session_folder = session_folder
        os.makedirs(self.session_folder, exist_ok=True)
        self.paths = SimpleNamespace(
            vpn_list=os.path.join(session_folder, f"{name}_vpn.txt"),
            local_list=os.path.join(session_folder, f"{name}_local.txt"),
            candidates=os.path.join(session_folder, f"{name}_candidates.txt"),
        )
        self.calls = []
        self.stop_requested = False
        self.master_models = [
            {"name": "beta", "date": "2026-01-02", "country": "RO"},
            {"name": "alpha", "date": "2026-01-01", "country": "US"},
        ]
        self.block_event = None

    def clear_stop(self):
        self.stop_requested = False
        self.calls.append(("clear_stop",))

    def request_stop(self):
        self.stop_requested = True
        self.calls.append(("request_stop",))

    def create_session(self):
        self.session_folder = os.path.join(self.session_folder, "created")
        os.makedirs(self.session_folder, exist_ok=True)
        return self.session_folder

    def use_latest_session(self):
        return self.session_folder

    def set_session(self, session_path):
        self.session_folder = session_path
        os.makedirs(self.session_folder, exist_ok=True)
        self.paths = SimpleNamespace(
            vpn_list=os.path.join(session_path, f"{self.name}_vpn.txt"),
            local_list=os.path.join(session_path, f"{self.name}_local.txt"),
            candidates=os.path.join(session_path, f"{self.name}_candidates.txt"),
        )
        self.calls.append(("set_session", session_path))

    def step_vpn_list(self, headless=True, mode="manual"):
        self.calls.append(("step_vpn_list", headless))
        if self.block_event is not None:
            self.block_event.wait(2)
        with open(self.paths.vpn_list, "w", encoding="utf-8") as handle:
            handle.write("vpn-model\n")

    def step_local_list(self, headless=True):
        self.calls.append(("step_local_list", headless))
        with open(self.paths.local_list, "w", encoding="utf-8") as handle:
            handle.write("local-model\n")

    def compare_lists(self):
        self.calls.append(("compare_lists",))
        with open(self.paths.candidates, "w", encoding="utf-8") as handle:
            handle.write("candidate-model\n")

    def verify_candidates(self, start_minimized=True, reveal_on_captcha=True):
        self.calls.append(("verify_candidates", start_minimized, reveal_on_captcha))

    def verify_live_models(self, start_minimized=False):
        self.calls.append(("verify_live_models", start_minimized))

    def capture_live_reference(self):
        self.calls.append(("capture_live_reference",))
        return 1

    def capture_local_reference(self):
        self.calls.append(("capture_local_reference",))
        return 1

    def verify_master_list(self, start_minimized=True):
        self.calls.append(("verify_master_list", start_minimized))

    def resolve_verification_discrepancy(self):
        self.calls.append(("resolve_verification_discrepancy",))

    def compile_master_list(self):
        self.calls.append(("compile_master_list",))
        return len(self.master_models)

    def get_master_list_models(self):
        return [dict(item) for item in self.master_models]

    def get_master_json_path(self):
        return os.path.join(self.base_dir, "MASTER_BLOCKED_DATA.json")

    def add_manual_to_master(self, username):
        self.calls.append(("add_manual_to_master", username))
        self.master_models.append({"name": username, "date": "Manual", "country": ""})

    def remove_from_master_list(self, model_input):
        self.calls.append(("remove_from_master_list", model_input))

    def block_model(self, model_input):
        self.calls.append(("block_model", model_input))

    def update_master_model_country(self, model_url, country):
        self.calls.append(("update_master_model_country", model_url, country))
        return True


def _make_service(base_dir, vpn=None):
    from app_settings import AppSettings

    settings = AppSettings(base_dir)
    settings.set("vpnProvider", "mullvad")
    runners = {
        name: _FakeRunner(os.path.join(base_dir, name), name=name[:3].lower())
        for name in ("Chaturbate", "MyFreeCams", "Stripchat", "XHamsterLive")
    }
    service = ModelScraperService(
        base_dir,
        runners=runners,
        vpn_factory=(lambda: vpn) if vpn else _FakeVpn,
        adapters={},
        settings=settings,
    )
    return service, runners


class _DispatchAdapter:
    """Small typed adapter used to prove the service dispatch boundary."""

    spec = SimpleNamespace(key="stripchat")

    def __init__(self, root):
        self.root = root
        self.session_path = None
        self.calls = []
        os.makedirs(root, exist_ok=True)

    @property
    def session_root(self):
        return self.root

    def bind_session(self, path):
        self.session_path = str(path)
        self.calls.append(("bind_session", self.session_path))

    def create_session(self):
        path = os.path.join(self.root, "session 1 28.07.2026")
        os.makedirs(path, exist_ok=True)
        self.bind_session(path)
        return path

    def use_latest_session(self):
        return self.create_session()

    def clear_stop(self):
        self.calls.append(("clear_stop",))

    def request_stop(self):
        self.calls.append(("request_stop",))

    def get_master_models(self):
        return []

    def candidate_projection_path(self):
        return os.path.join(self.session_path or self.root, "sc_candidates.txt")

    def blocked_projection_path(self):
        return os.path.join(self.session_path or self.root, "FINAL_BLOCKED.txt")

    def inspect_resume(self):
        return SimpleNamespace(
            available=False,
            run_id=None,
            generation_id=None,
            next_step=None,
            processed_count=0,
            remaining_count=0,
            candidate_count=0,
            reason_code="nothing_to_resume",
            message="",
        )


class _RecordingHardenedCoordinator:
    calls = []

    def __init__(self, *_args, **_kwargs):
        pass

    @staticmethod
    def _step(adapter, step):
        return StepOutcome.succeeded(
            run_id="run-stripchat",
            generation_id="generation-stripchat",
            platform="stripchat",
            step=step,
            session_id=os.path.basename(adapter.session_path),
        )

    def run_step(self, adapter, *, step, options):
        self.calls.append(("manual", step, options.headless))
        result = self._step(adapter, step)
        return RunOutcome.calculate(
            run_id=result.run_id,
            generation_id=result.generation_id,
            platform="stripchat",
            mode="manual",
            steps=[result],
            restoration=StepOutcome.succeeded(
                run_id=result.run_id,
                generation_id=result.generation_id,
                platform="stripchat",
                step=StepName.RESTORE,
                session_id=result.session_id,
            ),
            started_at="2026-07-28T00:00:00+00:00",
            session_id=result.session_id,
            required_steps=(step,),
        )

    def run(self, adapter, *, options):
        self.calls.append(("full_auto", options.run_selection, options.headless))
        steps = [
            self._step(adapter, step)
            for step in (
                StepName.VPN_SNAPSHOT,
                StepName.LOCAL_SNAPSHOT,
                StepName.COMPARE,
                StepName.VERIFY,
            )
        ]
        return RunOutcome.calculate(
            run_id=steps[0].run_id,
            generation_id=steps[0].generation_id,
            platform="stripchat",
            mode="full_auto",
            steps=steps,
            restoration=StepOutcome.succeeded(
                run_id=steps[0].run_id,
                generation_id=steps[0].generation_id,
                platform="stripchat",
                step=StepName.RESTORE,
                session_id=steps[0].session_id,
            ),
            started_at="2026-07-28T00:00:00+00:00",
            session_id=steps[0].session_id,
            required_steps=tuple(item.step for item in steps),
        )


class MFCStandaloneVerifyTests(unittest.TestCase):
    """Step 4 on its own still needs a snapshot from each side of the VPN,
    and only the service can move the VPN to get them."""

    def test_step_four_alone_takes_both_snapshots(self):
        with tempfile.TemporaryDirectory() as base_dir:
            vpn = _FakeVpn()
            service, runners = _make_service(base_dir, vpn=vpn)
            service.set_platform("MyFreeCams")
            service.create_session()
            runners["MyFreeCams"].calls.clear()

            service.start_verify()
            service.wait_for_current_task(timeout=10)

            wanted = {
                "capture_live_reference",
                "capture_local_reference",
                "verify_live_models",
            }
            names = [
                call[0]
                for call in runners["MyFreeCams"].calls
                if call[0] in wanted
            ]
            self.assertEqual(
                names,
                ["capture_live_reference", "capture_local_reference",
                 "verify_live_models"],
            )

    def test_the_on_air_snapshot_is_taken_with_the_vpn_up(self):
        with tempfile.TemporaryDirectory() as base_dir:
            vpn = _FakeVpn()
            service, _ = _make_service(base_dir, vpn=vpn)
            service.set_platform("MyFreeCams")
            service.create_session()
            vpn.calls.clear()

            service.start_verify()
            service.wait_for_current_task(timeout=10)

            self.assertEqual(vpn.calls[:3], ["remove_chrome_from_split_tunnel", "connect_ireland", "disconnect"])


class ModelScraperServiceTests(unittest.TestCase):
    def test_state_discovers_checkpoint_after_process_restart(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            session = runners["Chaturbate"].session_folder
            store = ChaturbateRunStore(session)
            context = store.create_run(mode="full_auto")
            store.record_step(
                context,
                StepOutcome.succeeded(
                    run_id=context.run_id,
                    generation_id=context.generation_id,
                    platform=context.platform,
                    step=StepName.COMPARE,
                ),
            )
            store.write_checkpoint(
                context,
                {
                    "candidate_sha256": "synthetic",
                    "candidate_urls": [
                        "https://chaturbate.com/synthetic/"
                    ],
                    "verdicts": {},
                },
            )
            service._session_path = session

            self.assertTrue(service.get_state()["resumable"])

    def test_failed_flow_logs_why_it_failed(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            runner = runners["Chaturbate"]

            def boom(**_kwargs):
                raise RuntimeError("chrome would not start")

            runner.verify_candidates = boom

            outcome = service.run_full_auto_flow_sync(
                runner,
                platform="Chaturbate",
                headless=False,
                start_minimized=True,
                vpn=_FakeVpn(),
            )

            self.assertEqual(outcome.status.value, "failed")
            warning = next(
                line
                for line in service._logs
                if "Full auto flow ended" in line
            )
            # The error code alone names no cause; the run log has to carry
            # the exception text or the failure cannot be diagnosed later.
            self.assertIn("chrome would not start", warning)

    def test_full_auto_flow_runs_selected_platform_steps_around_vpn(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            runner = runners["Chaturbate"]
            vpn = _FakeVpn()

            service.run_full_auto_flow_sync(
                runner,
                platform="Chaturbate",
                headless=False,
                start_minimized=True,
                vpn=vpn,
            )

        self.assertEqual(
            vpn.calls,
            [
                "remove_chrome_from_split_tunnel",
                "connect_ireland",
                "disconnect",
                "connect_ireland",
                "add_chrome_to_split_tunnel",
            ],
        )
        self.assertEqual(
            runner.calls[-4:],
            [
                ("step_vpn_list", False),
                ("step_local_list", False),
                ("compare_lists",),
                ("verify_candidates", True, True),
            ],
        )

    def test_background_task_rejects_second_task_while_busy(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            release = threading.Event()
            runners["Chaturbate"].block_event = release

            self.assertTrue(service.start_vpn_list(headless=True))
            self.assertFalse(service.start_local_list(headless=True))
            self.assertTrue(service.get_state()["busy"])

            release.set()
            self.assertTrue(service.wait_for_current_task(timeout=3))
            self.assertFalse(service.get_state()["busy"])

    def test_two_simultaneous_start_requests_reserve_only_one_task(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            release = threading.Event()
            runners["Chaturbate"].block_event = release
            barrier = threading.Barrier(3)
            results = []

            def start():
                barrier.wait()
                results.append(service.start_vpn_list(headless=True))

            first = threading.Thread(target=start)
            second = threading.Thread(target=start)
            first.start()
            second.start()
            barrier.wait()
            first.join(3)
            second.join(3)
            self.assertCountEqual(results, [True, False])
            release.set()
            self.assertTrue(service.wait_for_current_task(timeout=3))
            calls = runners["Chaturbate"].calls
            self.assertEqual(
                len([call for call in calls if call[0] == "step_vpn_list"]),
                1,
            )

    def test_stop_request_sets_only_the_active_runner_stop_flag(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            with service._lock:
                service._busy = True
                service._task_platform = "Chaturbate"

            service.stop_current_task()

            self.assertTrue(runners["Chaturbate"].stop_requested)
            self.assertFalse(
                any(
                    runner.stop_requested
                    for platform, runner in runners.items()
                    if platform != "Chaturbate"
                )
            )

    def test_platform_session_and_state_shape(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)

            self.assertTrue(service.set_platform("Stripchat"))
            service.create_session()
            service.load_blocked_list_text("zeta\nalpha\n\n")
            state = service.get_state()

            self.assertEqual(state["platform"], "Stripchat")
            self.assertEqual(state["status"], "Idle")
            self.assertFalse(state["busy"])
            self.assertEqual(state["blockedModels"], ["alpha", "zeta"])
            self.assertEqual([m["name"] for m in state["masterListModels"]], ["beta", "alpha"])
            self.assertEqual(state["masterListSortMode"], "date_newest")
            self.assertTrue(state["sessionPath"].endswith("created"))
            self.assertIn(("set_session", state["sessionPath"]), runners["Stripchat"].calls)

    def test_platform_switch_preserves_independent_session_roots(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            ctb_session = os.path.join(
                base_dir, "Chaturbate", "session 1 24.07.2026"
            )
            mfc_session = os.path.join(
                base_dir, "MyFreeCams", "session 1 24.07.2026"
            )
            os.makedirs(ctb_session)
            os.makedirs(mfc_session)
            self.assertTrue(service._set_session(ctb_session))
            self.assertTrue(service.set_platform("MyFreeCams"))
            self.assertTrue(service._set_session(mfc_session))
            self.assertTrue(service.set_platform("Stripchat"))
            self.assertTrue(service.set_platform("Chaturbate"))
            self.assertEqual(service.get_state()["sessionPath"], ctb_session)
            self.assertEqual(
                runners["Chaturbate"].session_folder, ctb_session
            )
            self.assertEqual(
                runners["MyFreeCams"].session_folder, mfc_session
            )

    def test_master_list_actions_delegate_to_active_runner(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            runner = runners["Chaturbate"]

            service.add_manual_to_master("new_model")
            service.delete_from_master_list("old_model", block=False)
            service.delete_from_master_list("bad_model", block=True)
            service.update_master_model_country("new_model", "RO")
            count = service.compile_master_list_sync()

            self.assertEqual(count, 3)
            self.assertIn(("add_manual_to_master", "new_model"), runner.calls)
            self.assertIn(("remove_from_master_list", "old_model"), runner.calls)
            self.assertIn(("block_model", "bad_model"), runner.calls)
            self.assertIn(("update_master_model_country", "new_model", "RO"), runner.calls)
            self.assertIn(("resolve_verification_discrepancy",), runner.calls)
            self.assertIn(("compile_master_list",), runner.calls)

    def test_missing_stripchat_adapter_never_falls_back_to_legacy_workflow(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            service.set_platform("Stripchat")
            legacy = runners["Stripchat"]
            legacy.calls.clear()

            self.assertFalse(service.start_vpn_list(headless=False))
            self.assertFalse(service.start_local_list(headless=False))
            self.assertFalse(service.start_compare())
            self.assertFalse(service.start_verify())
            self.assertFalse(
                service.start_full_auto_flow(
                    headless=False,
                    start_minimized=True,
                )
            )

            outcome = service.run_full_auto_flow_sync(
                legacy,
                platform="Stripchat",
                headless=False,
                start_minimized=True,
                vpn=_FakeVpn(),
            )
            self.assertEqual(outcome.status.value, "failed")
            self.assertEqual(
                outcome.error_code,
                "hardened_adapter_unavailable",
            )

            self.assertFalse(service.compile_master_list_sync())
            self.assertFalse(service.add_manual_to_master("new_model"))
            self.assertFalse(
                service.delete_from_master_list("old_model", block=False)
            )
            self.assertFalse(
                service.delete_from_master_list("bad_model", block=True)
            )
            self.assertFalse(
                service.update_master_model_country("new_model", "RO")
            )

            with service._lock:
                service._busy = True
                service._task_platform = "Stripchat"
            self.assertFalse(service.stop_current_task())

            forbidden = {
                "step_vpn_list",
                "step_local_list",
                "compare_lists",
                "verify_candidates",
                "compile_master_list",
                "add_manual_to_master",
                "remove_from_master_list",
                "block_model",
                "update_master_model_country",
                "request_stop",
            }
            self.assertFalse(
                [call for call in legacy.calls if call[0] in forbidden]
            )

    def test_current_session_profile_matches_are_temporarily_prioritized(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            runner = runners["Chaturbate"]
            service._session_path = os.path.join(
                base_dir, "session 7 24.07.2026"
            )
            runner.master_models = [
                {
                    "name": "normal",
                    "timestamp": "2026-07-24T17:00:00",
                },
                {
                    "name": "local-ro",
                    "timestamp": "2026-07-24T16:59:00",
                    "location_profile_match": True,
                    "profile_match": {"session": 7},
                },
                {
                    "name": "blocked-ro",
                    "timestamp": "2026-07-24T16:58:00",
                    "vpn_location_profile_match": True,
                    "vpn_profile_match": {"session": 7},
                },
                {
                    "name": "old-blocked-ro",
                    "timestamp": "2026-07-24T17:01:00",
                    "vpn_location_profile_match": True,
                    "vpn_profile_match": {"session": 6},
                },
            ]

            names = [
                item["name"]
                for item in service._master_list_models()
            ]

            self.assertEqual(names[:2], ["blocked-ro", "local-ro"])
            self.assertEqual(names[2:], ["old-blocked-ro", "normal"])

    def test_all_new_models_stay_ahead_of_older_session_profile_matches(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            runner = runners["Chaturbate"]
            service._session_path = os.path.join(
                base_dir, "session 163 09.08.2026"
            )
            service._initial_master_models["Chaturbate"] = {
                "old-local-ro",
                "old-normal",
            }
            runner.master_models = [
                {
                    "name": "old-local-ro",
                    "timestamp": "2026-08-09T16:40:00",
                    "location_profile_match": True,
                    "profile_match": {"session": 163},
                },
                {
                    "name": "new-generic",
                    "timestamp": "2026-08-09T16:36:48",
                },
                {
                    "name": "new-local-ro",
                    "timestamp": "2026-08-09T16:36:48",
                    "location_profile_match": True,
                    "profile_match": {"session": 163},
                },
                {
                    "name": "new-blocked-ro",
                    "timestamp": "2026-08-09T16:36:48",
                    "vpn_location_profile_match": True,
                    "vpn_profile_match": {"session": 163},
                },
                {
                    "name": "old-normal",
                    "timestamp": "2026-08-09T16:39:00",
                },
            ]

            models = service._master_list_models()

            self.assertEqual(
                [item["name"] for item in models],
                [
                    "new-blocked-ro",
                    "new-local-ro",
                    "new-generic",
                    "old-local-ro",
                    "old-normal",
                ],
            )
            self.assertEqual(
                [item["is_new"] for item in models],
                [True, True, True, False, False],
            )

    def test_use_latest_restores_persisted_new_models_after_restart(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            runner = runners["Chaturbate"]
            session_path = os.path.join(
                runner.base_dir, "session 163 09.08.2026"
            )
            os.makedirs(session_path)
            runner.master_models = [
                {
                    "name": "old-local-ro",
                    "timestamp": "2026-08-07T14:58:00",
                    "location_profile_match": True,
                    "profile_match": {"session": 163},
                },
                {
                    "name": "new-generic",
                    "timestamp": "2026-08-09T16:36:48",
                },
                {
                    "name": "new-local-ro",
                    "timestamp": "2026-08-09T16:36:48",
                    "location_profile_match": True,
                    "profile_match": {"session": 163},
                },
                {
                    "name": "new-blocked-ro",
                    "timestamp": "2026-08-09T16:36:48",
                    "vpn_location_profile_match": True,
                    "vpn_profile_match": {"session": 163},
                },
            ]
            service._initial_master_models["Chaturbate"] = {
                item["name"] for item in runner.master_models
            }
            with open(
                os.path.join(session_path, "master_additions.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(
                    {
                        "schema_version": 1,
                        "platform": "Chaturbate",
                        "session": "session 163 09.08.2026",
                        "models": [
                            "new-blocked-ro",
                            "new-local-ro",
                            "new-generic",
                        ],
                    },
                    handle,
                )

            self.assertTrue(service._set_session(session_path))
            models = service._master_list_models()

            self.assertEqual(
                [item["name"] for item in models],
                [
                    "new-blocked-ro",
                    "new-local-ro",
                    "new-generic",
                    "old-local-ro",
                ],
            )
            self.assertEqual(
                [item["is_new"] for item in models],
                [True, True, True, False],
            )

    def test_use_latest_recovers_additions_from_successful_master_publication(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            runner = runners["Chaturbate"]
            session_path = os.path.join(
                runner.base_dir, "session 163 09.08.2026"
            )
            os.makedirs(session_path)
            current = [
                {
                    "name": "old-model",
                    "date": "2026-08-07",
                    "session": 161,
                    "manual": False,
                },
                {
                    "name": "new-one",
                    "date": "2026-08-09",
                    "session": 163,
                    "manual": False,
                },
                {
                    "name": "new-two",
                    "date": "2026-08-09",
                    "session": 163,
                    "manual": False,
                },
            ]
            runner.master_models = [dict(item) for item in current]
            master_path = runner.get_master_json_path()
            with open(master_path, "w", encoding="utf-8") as handle:
                json.dump(current, handle)
            with open(master_path + ".bak", "w", encoding="utf-8") as handle:
                json.dump(current[:1], handle)
            with open(
                os.path.join(session_path, "run_state.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(
                    {
                        "status": "succeeded",
                        "updated_at": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                    },
                    handle,
                )

            self.assertTrue(service._set_session(session_path))
            models = service._master_list_models()

            self.assertEqual(
                [item["name"] for item in models[:2]],
                ["new-one", "new-two"],
            )
            self.assertTrue(all(item["is_new"] for item in models[:2]))
            self.assertTrue(
                os.path.exists(
                    os.path.join(session_path, "master_additions.json")
                )
            )

    def test_successful_scan_additions_are_persisted_for_future_restarts(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            runner = runners["Chaturbate"]
            session_path = os.path.join(
                runner.base_dir, "session 164 10.08.2026"
            )
            os.makedirs(session_path)
            self.assertTrue(service._set_session(session_path))

            additions = service._record_session_master_additions(
                "Chaturbate",
                [{"name": "old-model"}],
                [{"name": "old-model"}, {"name": "new-model"}],
                source="full_auto",
            )

            self.assertEqual(additions, {"new-model"})
            with open(
                os.path.join(session_path, "master_additions.json"),
                "r",
                encoding="utf-8",
            ) as handle:
                record = json.load(handle)
            self.assertEqual(record["models"], ["new-model"])


class FullAutoResultPropagationTests(unittest.TestCase):
    def test_worker_result_reaches_last_outcome_and_task_finished(self):
        from workflow_types import OutcomeStatus, RunOutcome

        with tempfile.TemporaryDirectory() as base_dir:
            service, _runners = _make_service(base_dir)
            expected = RunOutcome.calculate(
                run_id="run-1",
                generation_id="gen-1",
                platform="Chaturbate",
                mode="full_auto",
                steps=[
                    StepOutcome.succeeded(
                        run_id="run-1",
                        generation_id="gen-1",
                        platform="Chaturbate",
                        step=step,
                    )
                    for step in (
                        StepName.VPN_SNAPSHOT,
                        StepName.LOCAL_SNAPSHOT,
                        StepName.COMPARE,
                        StepName.VERIFY,
                    )
                ],
                restoration=StepOutcome.succeeded(
                    run_id="run-1",
                    generation_id="gen-1",
                    platform="Chaturbate",
                    step=StepName.RESTORE,
                ),
                started_at="2026-07-28T00:00:00+00:00",
            )
            service.run_full_auto_flow_sync = (
                lambda *args, **kwargs: expected
            )
            events = []
            subscriber = service.subscribe()
            self.assertTrue(service.start_full_auto_flow())
            service.wait_for_current_task(timeout=10)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    event = subscriber.get(timeout=0.25)
                except queue.Empty:
                    continue
                events.append(event)
                if event.get("type") == "taskFinished":
                    break
            state = service.get_state()
            self.assertIsNotNone(state["lastOutcome"])
            self.assertEqual(state["lastOutcome"]["run_id"], "run-1")
            self.assertEqual(state["lastOutcome"]["status"], "succeeded")
            finished = [
                item for item in events if item.get("type") == "taskFinished"
            ]
            self.assertTrue(finished)
            self.assertEqual(finished[-1]["outcome"]["run_id"], "run-1")

    def test_dropped_worker_result_becomes_a_failure_not_success(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, _runners = _make_service(base_dir)
            service.run_full_auto_flow_sync = lambda *args, **kwargs: None
            self.assertTrue(service.start_full_auto_flow())
            service.wait_for_current_task(timeout=10)
            state = service.get_state()
            self.assertIsNotNone(state["lastOutcome"])
            self.assertEqual(state["lastOutcome"]["status"], "failed")
            self.assertEqual(
                state["lastOutcome"]["error_code"], "missing_run_outcome"
            )


class TruthfulStateTests(unittest.TestCase):
    def test_outcomes_do_not_leak_across_platform_switch(self):
        from workflow_types import RunOutcome

        with tempfile.TemporaryDirectory() as base_dir:
            service, _runners = _make_service(base_dir)
            outcome = RunOutcome.calculate(
                run_id="run-ctb",
                generation_id="gen-ctb",
                platform="Chaturbate",
                mode="full_auto",
                steps=[
                    StepOutcome.succeeded(
                        run_id="run-ctb",
                        generation_id="gen-ctb",
                        platform="Chaturbate",
                        step=StepName.VERIFY,
                    )
                ],
                restoration=None,
                started_at="2026-07-28T00:00:00+00:00",
            )
            service.run_full_auto_flow_sync = lambda *a, **k: outcome
            service.start_full_auto_flow()
            service.wait_for_current_task(timeout=10)
            self.assertIsNotNone(service.get_state()["lastOutcome"])
            service.set_platform("Stripchat")
            self.assertIsNone(
                service.get_state()["lastOutcome"],
                "another platform's outcome must not be shown here",
            )
            service.set_platform("Chaturbate")
            self.assertEqual(
                service.get_state()["lastOutcome"]["run_id"], "run-ctb"
            )

    def test_resume_is_disabled_while_a_task_is_active(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, _runners = _make_service(base_dir)
            release = threading.Event()

            def slow(*args, **kwargs):
                release.wait(5)
                return None

            service.run_full_auto_flow_sync = slow
            service.start_full_auto_flow()
            try:
                state = service.get_state()
                self.assertTrue(state["busy"])
                self.assertFalse(state["resume"]["available"])
                self.assertFalse(state["resumable"])
                self.assertEqual(
                    state["resume"]["reasonCode"], "task_active"
                )
                self.assertIsNone(state["resume"]["nextStep"])
            finally:
                release.set()
                service.wait_for_current_task(timeout=10)

    def test_state_reports_schema_version_and_effective_options(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, _runners = _make_service(base_dir)
            state = service.get_state()
            self.assertEqual(state["schemaVersion"], 2)
            self.assertIn("effectiveOptions", state)
            self.assertIn("machinePolicy", state)

    def test_stripchat_headless_request_is_rejected(self):
        from modelscraper_service import (
            ModelScraperService,
            UnsupportedBrowserMode,
        )

        self.assertFalse(
            ModelScraperService.resolve_headless("Stripchat", None)
        )
        self.assertFalse(
            ModelScraperService.resolve_headless("Stripchat", False)
        )
        with self.assertRaises(UnsupportedBrowserMode):
            ModelScraperService.resolve_headless("Stripchat", True)
        self.assertTrue(
            ModelScraperService.resolve_headless("Chaturbate", None)
        )

    def test_stripchat_full_auto_refuses_an_explicit_headless_request(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, _runners = _make_service(base_dir)
            service.set_platform("Stripchat")
            self.assertFalse(service.start_full_auto_flow(headless=True))
            self.assertFalse(service.get_state()["busy"])


class HardenedStripchatDispatchTests(unittest.TestCase):
    def setUp(self):
        _RecordingHardenedCoordinator.calls = []

    def test_all_stripchat_gui_actions_bypass_the_legacy_runner(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            adapter = _DispatchAdapter(runners["Stripchat"].session_folder)
            service._adapters = {"Stripchat": adapter}
            service._hardened_coordinator_factory = (
                _RecordingHardenedCoordinator
            )
            service.set_platform("Stripchat")
            service._set_session(adapter.create_session(), "Stripchat")
            runners["Stripchat"].calls.clear()

            actions = (
                lambda: service.start_vpn_list(headless=False),
                lambda: service.start_local_list(headless=False),
                service.start_compare,
                lambda: service.start_verify(start_minimized=False),
                lambda: service.start_full_auto_flow(
                    headless=False, start_minimized=False
                ),
            )
            for action in actions:
                self.assertTrue(action())
                self.assertTrue(service.wait_for_current_task(timeout=10))

            self.assertEqual(
                [item[:2] for item in _RecordingHardenedCoordinator.calls],
                [
                    ("manual", StepName.VPN_SNAPSHOT),
                    ("manual", StepName.LOCAL_SNAPSHOT),
                    ("manual", StepName.COMPARE),
                    ("manual", StepName.VERIFY),
                    ("full_auto", "auto"),
                ],
            )
            forbidden = {
                "step_vpn_list",
                "step_local_list",
                "compare_lists",
                "verify_candidates",
            }
            self.assertFalse(
                forbidden.intersection(call[0] for call in runners["Stripchat"].calls)
            )

    def test_stop_targets_only_the_active_stripchat_adapter(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, runners = _make_service(base_dir)
            adapter = _DispatchAdapter(runners["Stripchat"].session_folder)
            service._adapters = {"Stripchat": adapter}
            service.set_platform("Stripchat")
            with service._lock:
                service._busy = True
                service._task_platform = "Stripchat"

            self.assertTrue(service.stop_current_task())

            self.assertIn(("request_stop",), adapter.calls)
            self.assertFalse(
                any(
                    call[0] == "request_stop"
                    for runner in runners.values()
                    for call in runner.calls
                )
            )

    def test_default_service_injects_the_owned_browser_runtime(self):
        with tempfile.TemporaryDirectory() as base_dir:
            runtimes = []

            class Runtime:
                def __init__(self, **_kwargs):
                    runtimes.append(self)

                def __call__(self):
                    raise AssertionError("read-only wiring test must not open Chrome")

                def configure(self, **_kwargs):
                    pass

                def reveal(self):
                    return True

                def hide(self):
                    return True

            service = ModelScraperService(
                base_dir, stripchat_runtime_factory=Runtime
            )
            adapter = service._adapters["Stripchat"]

            self.assertEqual(len(runtimes), 1)
            self.assertIs(adapter.driver_factory, runtimes[0])
            self.assertIs(adapter.window_manager, runtimes[0])
            self.assertTrue(callable(adapter.policy_observer))


if __name__ == "__main__":
    unittest.main()


class HardenedTaskAlwaysEndsTypedTests(unittest.TestCase):
    """A crashed or silent hardened task must never leave a stale success."""

    def _service_with_prior_success(self, base_dir):
        service, _runners = _make_service(base_dir)
        earlier = RunOutcome.calculate(
            run_id="earlier-run",
            generation_id="earlier-generation",
            platform="Chaturbate",
            mode="full_auto",
            steps=[
                StepOutcome.succeeded(
                    run_id="earlier-run",
                    generation_id="earlier-generation",
                    platform="Chaturbate",
                    step=StepName.VERIFY,
                )
            ],
            restoration=None,
            started_at="2026-07-28T00:00:00+00:00",
        )
        service.run_full_auto_flow_sync = lambda *a, **k: earlier
        service.start_full_auto_flow()
        service.wait_for_current_task(timeout=10)
        self.assertEqual(
            service.get_state()["lastOutcome"]["status"], "succeeded"
        )
        return service

    def test_a_raising_hardened_step_replaces_the_stale_outcome(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service = self._service_with_prior_success(base_dir)

            def boom():
                raise RuntimeError("chrome vanished")

            service._start_task("Step 4", boom, managed_leases=True)
            service.wait_for_current_task(timeout=10)
            outcome = service.get_state()["lastOutcome"]
            self.assertEqual(outcome["status"], "failed")
            self.assertEqual(outcome["error_code"], "workflow_task_failed")
            self.assertEqual(outcome["mode"], "manual")

    def test_a_silent_hardened_step_becomes_missing_run_outcome(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service = self._service_with_prior_success(base_dir)
            service._start_task(
                "Step 1", lambda: None, managed_leases=True
            )
            service.wait_for_current_task(timeout=10)
            outcome = service.get_state()["lastOutcome"]
            self.assertEqual(outcome["status"], "failed")
            self.assertEqual(outcome["error_code"], "missing_run_outcome")
            self.assertEqual(
                outcome["steps"][0]["step"], StepName.VPN_SNAPSHOT.value
            )

    def test_legacy_tasks_keep_their_untyped_returns(self):
        with tempfile.TemporaryDirectory() as base_dir:
            service, _runners = _make_service(base_dir)
            service._start_task("Step 1", lambda: None)
            service.wait_for_current_task(timeout=10)
            self.assertIsNone(
                service.get_state()["lastOutcome"],
                "legacy platforms legitimately return None",
            )


class StripchatPublicationModeTests(unittest.TestCase):
    """A run publishes unless the operator asks for a rehearsal.

    Shadow mode stages the final list and the master revision and then
    stops before the commit witness, so a run can verify hundreds of
    blocked models and change nothing an operator can see. That was the
    right default while the hardened flow was unproven; as the shipped
    behavior it means the workflow never finishes its job.
    """

    def _shadow_for(self, value):
        from unittest import mock

        environment = dict(os.environ)
        environment.pop("MODEL_SCRAPER_STRIPCHAT_WORKFLOW", None)
        if value is not None:
            environment["MODEL_SCRAPER_STRIPCHAT_WORKFLOW"] = value
        with mock.patch.dict(os.environ, environment, clear=True):
            return ModelScraperService._stripchat_shadow_mode()

    def test_publication_is_live_by_default(self):
        self.assertFalse(self._shadow_for(None))
        self.assertFalse(self._shadow_for("hardened"))

    def test_shadow_stays_available_as_an_explicit_opt_out(self):
        for value in ("shadow", "SHADOW", "  Shadow  "):
            with self.subTest(value=value):
                self.assertTrue(self._shadow_for(value))
