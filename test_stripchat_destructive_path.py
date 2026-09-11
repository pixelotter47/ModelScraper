"""The legacy destructive Stripchat Step 4 must be unreachable from the UI.

This is the single most important data-safety guarantee of the hardening
work: the old `verify_candidates` rewrote the session FINAL_BLOCKED.txt and
deleted every sc_* intermediate even when the browser had crashed or the
user pressed Stop. These tests fail loudly if any service entry point can
reach it again while Stripchat is selected.
"""

import hashlib
import os
import tempfile
import unittest
from unittest import mock

import legacy_stripchat_family
from modelscraper_service import ModelScraperService


SESSION_FILES = (
    "FINAL_BLOCKED.txt",
    "sc_vpn_list.txt",
    "sc_local_list.txt",
    "sc_candidates.txt",
    "sc_debug_log.txt",
    "sc_metadata.json",
)


def _digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class _Workspace:
    def __init__(self, base):
        self.base = base
        self.session = os.path.join(
            base, "sc sessions", "session 1 22.01.2026"
        )
        os.makedirs(self.session)
        self.before = {}
        for name in SESSION_FILES:
            path = os.path.join(self.session, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("https://stripchat.com/Precious-Data/\n")
            self.before[name] = _digest(path)

    def assert_intact(self, case):
        for name, digest in self.before.items():
            path = os.path.join(self.session, name)
            case.assertTrue(os.path.exists(path), f"{name} was deleted")
            case.assertEqual(_digest(path), digest, f"{name} was modified")


class LegacyDestructivePathTests(unittest.TestCase):
    def _run(self, action):
        """Run one service action with Stripchat selected, spying on legacy."""
        calls = []
        original = (
            legacy_stripchat_family.LegacyStripchatFamilyRunner.verify_candidates
        )

        def spy(runner_self, *args, **kwargs):
            calls.append((args, kwargs))
            return original(runner_self, *args, **kwargs)

        with tempfile.TemporaryDirectory() as base:
            workspace = _Workspace(base)
            with mock.patch.object(
                legacy_stripchat_family.LegacyStripchatFamilyRunner,
                "verify_candidates",
                spy,
            ):
                service = ModelScraperService(base)
                service.set_platform("Stripchat")
                service._set_session(workspace.session, "Stripchat")
                action(service)
                service.wait_for_current_task(timeout=30)
            workspace.assert_intact(self)
        return calls

    def test_step4_never_reaches_the_destructive_verifier(self):
        calls = self._run(lambda service: service.start_verify())
        self.assertEqual(
            calls, [], "manual Step 4 reached the legacy destructive path"
        )

    def test_full_auto_never_reaches_the_destructive_verifier(self):
        calls = self._run(lambda service: service.start_full_auto_flow())
        self.assertEqual(
            calls, [], "Full Auto reached the legacy destructive path"
        )

    def test_resume_never_reaches_the_destructive_verifier(self):
        calls = self._run(
            lambda service: service.start_full_auto_flow(
                run_selection="resume"
            )
        )
        self.assertEqual(
            calls, [], "Resume reached the legacy destructive path"
        )

    def test_manual_steps_1_to_3_leave_the_session_untouched(self):
        for name, action in (
            ("step1", lambda service: service.start_vpn_list()),
            ("step2", lambda service: service.start_local_list()),
            ("step3", lambda service: service.start_compare()),
        ):
            with self.subTest(step=name):
                calls = self._run(action)
                self.assertEqual(calls, [])


class FailClosedWithoutAdapterTests(unittest.TestCase):
    """With no hardened adapter, Stripchat must refuse - never fall back."""

    def _service(self, base):
        service = ModelScraperService(base, adapters={})
        service.set_platform("Stripchat")
        return service

    def test_every_stripchat_entry_point_refuses(self):
        with tempfile.TemporaryDirectory() as base:
            session = os.path.join(base, "sc sessions", "session 1 22.01.2026")
            os.makedirs(session)
            calls = []
            original = (
                legacy_stripchat_family.LegacyStripchatFamilyRunner.verify_candidates
            )

            def spy(runner_self, *args, **kwargs):
                calls.append(True)
                return original(runner_self, *args, **kwargs)

            with mock.patch.object(
                legacy_stripchat_family.LegacyStripchatFamilyRunner,
                "verify_candidates",
                spy,
            ):
                service = self._service(base)
                service._set_session(session, "Stripchat")
                for label, action in (
                    ("step1", service.start_vpn_list),
                    ("step2", service.start_local_list),
                    ("step3", service.start_compare),
                    ("step4", service.start_verify),
                    ("full_auto", service.start_full_auto_flow),
                ):
                    with self.subTest(action=label):
                        self.assertFalse(
                            action(),
                            f"{label} started without a hardened adapter",
                        )
                        self.assertFalse(service.get_state()["busy"])
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
