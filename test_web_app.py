import queue
import tempfile
import unittest
import warnings
from unittest import mock

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated",
)
from fastapi.testclient import TestClient

from web_app import create_app
from app_settings import AppSettings, install_shared_settings
from modelscraper_service import ModelScraperService


class _FakeService:
    def __init__(self):
        self._settings_temp = tempfile.TemporaryDirectory()
        self.settings = AppSettings(self._settings_temp.name)
        self.settings._values["vpnProvider"] = "mullvad"
        self.calls = []
        self.state = {
            "platform": "Chaturbate",
            "platforms": ["Chaturbate", "MyFreeCams", "Stripchat", "XHamsterLive"],
            "status": "Idle",
            "busy": False,
            "currentTask": "",
            "sessionPath": "",
            "blockedModels": [],
            "masterListModels": [{"name": "alpha", "country": "US"}],
            "masterListSortMode": "name",
            "masterListCountryFilter": "All",
            "logs": [],
            "resume": {"available": False, "reasonCode": "no_active_run"},
            "resumable": False,
        }
        self.subscriber = queue.Queue()
        self.machine_policy_result = {"ok": True, "recovered": True}

    def get_state(self):
        return dict(self.state)

    def get_settings(self):
        return self.settings.as_dict()

    def resolve_headless(self, platform, requested):
        from modelscraper_service import ModelScraperService

        return ModelScraperService.resolve_headless(platform, requested)

    def restore_machine_policy(self):
        self.calls.append(("restore_machine_policy",))
        return dict(self.machine_policy_result)

    def reconcile_machine_policy_record(self):
        self.calls.append(("reconcile_machine_policy_record",))
        return dict(self.machine_policy_result)

    def subscribe(self):
        self.subscriber.put({"type": "state", "state": self.get_state()})
        return self.subscriber

    def unsubscribe(self, subscriber):
        self.calls.append(("unsubscribe", subscriber is self.subscriber))

    def set_platform(self, platform):
        self.calls.append(("set_platform", platform))
        if platform not in self.state["platforms"]:
            return False
        self.state["platform"] = platform
        return True

    def create_session(self):
        self.calls.append(("create_session",))
        self.state["sessionPath"] = "created-session"
        return "created-session"

    def use_latest_session(self):
        self.calls.append(("use_latest_session",))
        self.state["sessionPath"] = "latest-session"
        return "latest-session"

    def open_session_folder(self):
        self.calls.append(("open_session_folder",))
        return True

    def start_full_auto_flow(
        self,
        headless=True,
        start_minimized=True,
        run_selection="auto",
    ):
        self.calls.append(
            (
                "start_full_auto_flow",
                headless,
                start_minimized,
                run_selection,
            )
        )
        return True

    def query_master_list(self, **kwargs):
        self.calls.append(("query_master_list", kwargs))
        return {
            "rows": self.state["masterListModels"],
            "total": 1,
            "offset": kwargs.get("offset", 0),
            "limit": kwargs.get("limit", 200),
        }

    def start_vpn_list(self, headless=True, manual_network_confirmed=False):
        self.calls.append(("start_vpn_list", headless))
        return True

    def start_local_list(self, headless=True, manual_network_confirmed=False):
        self.calls.append(("start_local_list", headless))
        return True

    def start_compare(self):
        self.calls.append(("start_compare",))
        return True

    def start_verify(self, start_minimized=True, manual_network_confirmed=False):
        self.calls.append(("start_verify", start_minimized))
        return True

    def stop_current_task(self):
        self.calls.append(("stop_current_task",))
        return True

    def load_blocked_list_text(self, text):
        self.calls.append(("load_blocked_list_text", text))
        self.state["blockedModels"] = sorted(
            line.strip() for line in text.splitlines() if line.strip()
        )
        return self.state["blockedModels"]

    def compile_master_list(self):
        self.calls.append(("compile_master_list",))
        return True

    def verify_master_list(self, start_minimized=True, manual_network_confirmed=False):
        self.calls.append(("verify_master_list", start_minimized))
        return True

    def add_manual_to_master(self, username):
        self.calls.append(("add_manual_to_master", username))
        return True

    def delete_from_master_list(self, model_input, block=False):
        self.calls.append(("delete_from_master_list", model_input, block))
        return True

    def update_master_model_country(self, model_url, country):
        self.calls.append(("update_master_model_country", model_url, country))
        return True

    def sort_master_list(self, mode):
        self.calls.append(("sort_master_list", mode))
        return True

    def set_master_list_country_filter(self, country):
        self.calls.append(("set_master_list_country_filter", country))
        return True


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.service = _FakeService()
        self.client = TestClient(create_app(service=self.service))

    def tearDown(self):
        self.client.close()

    def test_health_and_state_endpoints_return_service_state(self):
        self.assertEqual(self.client.get("/api/health").json(), {"ok": True})

        response = self.client.get("/api/state")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["platform"], "Chaturbate")
        self.assertEqual(response.json()["masterListModels"][0]["name"], "alpha")

    def test_invalid_platform_returns_validation_error(self):
        response = self.client.post("/api/platform", json={"platform": "Invalid"})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.service.calls, [("set_platform", "Invalid")])

    def test_task_start_and_stop_endpoints_delegate_to_service(self):
        self.client.post(
            "/api/tasks/full-auto",
            json={"headless": False, "startMinimized": False},
        )
        self.client.post("/api/tasks/step1", json={"headless": True})
        self.client.post("/api/tasks/step2", json={"headless": False})
        self.client.post("/api/tasks/step3")
        self.client.post("/api/tasks/step4", json={"startMinimized": False})
        self.client.post("/api/tasks/stop")

        self.assertEqual(
            self.service.calls,
            [
                ("start_full_auto_flow", False, False, "auto"),
                ("start_vpn_list", True),
                ("start_local_list", False),
                ("start_compare",),
                ("start_verify", False),
                ("stop_current_task",),
            ],
        )

    def test_busy_task_start_returns_conflict(self):
        self.service.start_compare = lambda: False

        response = self.client.post("/api/tasks/step3")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"],
            "workflow_already_running",
        )

    def test_master_list_query_is_paged_separately(self):
        response = self.client.get(
            "/api/master-list?offset=0&limit=50&query=alp"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["rows"][0]["name"], "alpha")

    def test_blocked_list_upload_sorts_models_and_returns_state(self):
        response = self.client.post(
            "/api/blocked-list/load-text",
            json={"text": "zeta\nalpha\n\n"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["blockedModels"], ["alpha", "zeta"])
        self.assertEqual(
            self.service.calls,
            [("load_blocked_list_text", "zeta\nalpha\n\n")],
        )

    def test_master_list_endpoints_delegate_to_service(self):
        self.client.post("/api/master-list/compile")
        self.client.post("/api/master-list/verify", json={"startMinimized": False})
        self.client.post("/api/master-list/manual", json={"username": "new_model"})
        self.client.request(
            "DELETE",
            "/api/master-list",
            json={"model": "old_model", "block": True},
        )
        self.client.patch(
            "/api/master-list/country",
            json={"model": "old_model", "country": "RO"},
        )
        self.client.post("/api/master-list/sort", json={"mode": "date_newest"})
        self.client.post("/api/master-list/filter-country", json={"country": "RO"})

        self.assertEqual(
            self.service.calls,
            [
                ("compile_master_list",),
                ("verify_master_list", False),
                ("add_manual_to_master", "new_model"),
                ("delete_from_master_list", "old_model", True),
                ("update_master_model_country", "old_model", "RO"),
                ("sort_master_list", "date_newest"),
                ("set_master_list_country_filter", "RO"),
            ],
        )


class BrowserModeAndErrorEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.service = _FakeService()
        self.client = TestClient(create_app(service=self.service))

    def test_stripchat_omitted_headless_resolves_to_visible(self):
        self.service.state["platform"] = "Stripchat"
        response = self.client.post("/api/tasks/full-auto", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.service.calls[-1],
            ("start_full_auto_flow", False, True, "auto"),
        )

    def test_stripchat_explicit_false_is_accepted(self):
        self.service.state["platform"] = "Stripchat"
        response = self.client.post(
            "/api/tasks/full-auto", json={"headless": False}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls[-1][1], False)

    def test_stripchat_explicit_headless_is_rejected(self):
        self.service.state["platform"] = "Stripchat"
        response = self.client.post(
            "/api/tasks/full-auto", json={"headless": True}
        )
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "unsupported_browser_mode")
        self.assertIn("visible", detail["message"])
        self.assertEqual(self.service.calls, [])

    def test_other_platforms_keep_their_headless_default(self):
        response = self.client.post("/api/tasks/full-auto", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls[-1][1], True)

    def test_invalid_run_selection_uses_the_error_envelope(self):
        response = self.client.post(
            "/api/tasks/full-auto", json={"runSelection": "sideways"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["code"], "invalid_run_selection"
        )

    def test_busy_conflict_returns_a_structured_message(self):
        self.service.start_full_auto_flow = lambda **kwargs: False
        response = self.client.post("/api/tasks/full-auto", json={})
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "workflow_already_running")
        self.assertTrue(detail["message"])

    def test_resume_without_an_available_run_is_rejected(self):
        response = self.client.post("/api/tasks/resume", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "resume_unavailable"
        )
        self.assertEqual(self.service.calls, [])

    def test_resume_runs_when_available(self):
        self.service.state["resume"] = {"available": True}
        response = self.client.post("/api/tasks/resume", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls[-1][3], "resume")


class MachinePolicyEndpointTests(unittest.TestCase):
    def setUp(self):
        self.service = _FakeService()
        self.client = TestClient(create_app(service=self.service))

    def test_restore_returns_result_and_state(self):
        response = self.client.post("/api/machine-policy/restore")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["result"]["ok"])
        self.assertIn("platform", payload["state"])

    def test_restore_failure_uses_the_error_envelope(self):
        self.service.machine_policy_result = {
            "ok": False,
            "error_code": "machine_lease_busy",
        }
        response = self.client.post("/api/machine-policy/restore")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "machine_lease_busy"
        )

    def test_reconcile_endpoint_exists(self):
        response = self.client.post("/api/machine-policy/reconcile")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            ("reconcile_machine_policy_record",), self.service.calls
        )


class LocationPreferencesEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(install_shared_settings, None)
        self.settings = AppSettings(self.temp.name)
        self.service = ModelScraperService(self.temp.name, settings=self.settings)
        self.client = TestClient(create_app(service=self.service, base_dir=self.temp.name))
        self.addCleanup(self.client.close)

    def test_settings_are_shared_and_do_not_expose_local_paths(self):
        self.settings.set("chromePath", "PRIVATE_EXECUTABLE_PATH")
        payload = self.client.get("/api/settings").json()
        self.assertEqual(set(payload["settings"]), {"targetCountries", "targetLocationTerms", "vpnRelayLocation", "vpnProvider"})
        self.assertEqual(payload["settings"]["targetCountries"], "")
        self.assertEqual(len(payload["countryChoices"]), 249)
        self.assertNotIn("PRIVATE_EXECUTABLE_PATH", str(payload))
        self.assertIs(self.client.app.state.settings, self.service.settings)

    def test_country_and_alias_preferences_persist_without_changing_relay(self):
        response = self.client.put("/api/settings", json={"targetCountries": "jp, ca", "targetLocationTerms": "Tokyo, Canada"})
        self.assertEqual(response.status_code, 200, response.text)
        saved = AppSettings(self.temp.name)
        self.assertEqual(saved.get("targetCountries"), "CA, JP")
        self.assertEqual(saved.get("vpnRelayLocation"), "ie")
        cleared = self.client.put("/api/settings", json={"targetCountries": "", "targetLocationTerms": ""})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["settings"]["targetCountries"], "")

    def test_invalid_or_private_fields_are_rejected_atomically(self):
        for payload in (
            {"targetCountries": "ZZ"}, {"targetCountries": ["JP"]},
            {"targetCountries": None}, {"chromePath": "private"}, {},
            {"targetCountries": "JP", "targetLocationTerms": "!"},
        ):
            with self.subTest(payload=payload):
                response = self.client.put("/api/settings", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.settings.get("targetCountries"), "")

    def test_busy_update_returns_conflict_and_does_not_change_settings(self):
        self.service._busy = True
        response = self.client.put("/api/settings", json={"targetCountries": "JP"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.settings.get("targetCountries"), "")

    def test_disk_error_does_not_leak_paths(self):
        with mock.patch.object(self.service, "update_settings", side_effect=OSError("PRIVATE_PATH")):
            response = self.client.put("/api/settings", json={"targetCountries": "JP"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("PRIVATE_PATH", response.text)

    def test_manual_vpn_is_default_and_rejects_full_auto_and_resume(self):
        self.assertEqual(self.settings.get("vpnProvider"), "manual")
        with mock.patch.object(self.service, "start_full_auto_flow") as start:
            for path in ("/api/tasks/full-auto", "/api/tasks/resume"):
                response = self.client.post(path, json={})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["detail"]["code"], "automatic_vpn_required")
            start.assert_not_called()

    def test_manual_steps_require_explicit_network_confirmation(self):
        for step, method in (("step1", "start_vpn_list"), ("step2", "start_local_list"), ("step4", "start_verify")):
            with self.subTest(step=step), mock.patch.object(self.service, method, return_value=True) as start:
                response = self.client.post(f"/api/tasks/{step}", json={})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"]["code"], "manual_network_confirmation_required")
                start.assert_not_called()
                response = self.client.post(f"/api/tasks/{step}", json={"manualNetworkConfirmed": True})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIs(start.call_args.kwargs["manual_network_confirmed"], True)

    def test_manual_master_verification_requires_local_network_confirmation(self):
        with mock.patch.object(self.service, "verify_master_list", return_value=True) as start:
            response = self.client.post("/api/master-list/verify", json={})
            self.assertEqual(response.status_code, 422)
            start.assert_not_called()
            response = self.client.post("/api/master-list/verify", json={"manualNetworkConfirmed": True})
            self.assertEqual(response.status_code, 200)
            self.assertIs(start.call_args.kwargs["manual_network_confirmed"], True)

    def test_vpn_provider_is_validated_and_persisted(self):
        invalid = self.client.put("/api/settings", json={"vpnProvider": "unmanaged-guess"})
        self.assertEqual(invalid.status_code, 422)
        response = self.client.put("/api/settings", json={"vpnProvider": "mullvad"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AppSettings(self.temp.name).get("vpnProvider"), "mullvad")

    def test_settings_and_vpn_gate_follow_changes_from_another_ui(self):
        other_ui = AppSettings(self.temp.name)
        other_ui.set("vpnProvider", "mullvad")
        other_ui.set("targetCountries", "JP")
        payload = self.client.get("/api/settings").json()["settings"]
        self.assertEqual(payload["vpnProvider"], "mullvad")
        self.assertEqual(payload["targetCountries"], "JP")
        with mock.patch.object(self.service, "start_full_auto_flow", return_value=True) as start:
            response = self.client.post("/api/tasks/full-auto", json={})
            self.assertEqual(response.status_code, 200, response.text)
            start.assert_called_once()
            other_ui.set("vpnProvider", "manual")
            response = self.client.post("/api/tasks/full-auto", json={})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(start.call_count, 1)

    def test_invalid_preferences_fail_closed_without_exposing_paths(self):
        with mock.patch.object(self.service, "get_settings", side_effect=ValueError("PRIVATE_PATH")):
            response = self.client.get("/api/settings")
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("PRIVATE_PATH", response.text)


if __name__ == "__main__":
    unittest.main()
