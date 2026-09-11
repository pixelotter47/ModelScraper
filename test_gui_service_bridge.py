import inspect
import os
import tempfile
import unittest

from gui_app import AppController


class _BridgeService:
    def __init__(self):
        import queue

        self.events = queue.Queue()
        self.calls = []
        self.state = {
            "platform": "Chaturbate",
            "status": "Idle",
            "busy": False,
            "sessionPath": "",
            "blockedModels": [],
            "masterListModels": [],
            "progress": {},
            "lastOutcome": None,
            "resumable": False,
        }

    def get_state(self):
        return dict(self.state)

    def subscribe(self):
        return self.events

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return True

        return call


class GuiServiceBridgeTests(unittest.TestCase):
    def test_slots_delegate_to_shared_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _BridgeService()
            controller = AppController(tmp, service=service)
            controller.startVpnList(True)
            controller.startFullAutoFlow(True, False)
            controller.stopCurrentTask()
            self.assertEqual(
                [item[0] for item in service.calls],
                [
                    "start_vpn_list",
                    "start_full_auto_flow",
                    "stop_current_task",
                ],
            )

    def test_gui_contains_no_runner_vpn_or_full_auto_orchestration(self):
        source = inspect.getsource(AppController)
        for forbidden in (
            "CTBRunner(",
            "MFCRunner(",
            "MullvadVpnController(",
            "_run_full_auto_flow",
            "_start_task",
        ):
            self.assertNotIn(forbidden, source)

    def test_stripchat_gui_never_submits_headless_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _BridgeService()
            service.state["platform"] = "Stripchat"
            controller = AppController(tmp, service=service)

            controller.startVpnList(True)
            controller.startLocalList(True)
            controller.startFullAutoFlow(True, True)
            controller.resumeFullAutoFlow()

            vpn_call = next(
                item for item in service.calls
                if item[0] == "start_vpn_list"
            )
            local_call = next(
                item for item in service.calls
                if item[0] == "start_local_list"
            )
            full_auto_calls = [
                item for item in service.calls
                if item[0] == "start_full_auto_flow"
            ]
            ordinary_calls = [
                item for item in full_auto_calls
                if item[2].get("run_selection", "auto") == "auto"
            ]
            self.assertEqual(len(ordinary_calls), 1)
            self.assertFalse(vpn_call[2]["headless"])
            self.assertFalse(local_call[2]["headless"])
            self.assertFalse(ordinary_calls[0][2]["headless"])
            resume_calls = [
                item
                for item in full_auto_calls
                if item[2].get("run_selection") == "resume"
            ]
            self.assertEqual(len(resume_calls), 1)
            self.assertFalse(resume_calls[0][2]["headless"])
