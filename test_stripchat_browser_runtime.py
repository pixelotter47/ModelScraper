"""The Stripchat Chrome runtime owns one persistent, leased profile."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from public_runtime import public_state_root
from runtime_lock import WorkflowBusyError
from stripchat_browser_runtime import (
    OWNER_MARKER,
    PROFILE_NAME,
    ProfileInUseError,
    StripchatChromeRuntime,
    persistent_profile_dir,
)


class _Options:
    def __init__(self):
        self.arguments = []
        self.capabilities = {}

    def add_argument(self, value):
        self.arguments.append(value)

    def set_capability(self, name, value):
        self.capabilities[name] = value


class _Driver:
    def __init__(self):
        self.calls = []

    def quit(self):
        self.calls.append("quit")

    def minimize_window(self):
        self.calls.append("minimize")

    def set_window_rect(self, **kwargs):
        self.calls.append(("rect", kwargs))


class _Lease:
    """Records acquire/release; can simulate another process holding it."""

    def __init__(self, busy=False):
        self.busy = busy
        self.acquired = 0
        self.released = 0

    def acquire(self):
        if self.busy:
            raise WorkflowBusyError({"pid": 4321})
        self.acquired += 1
        return self

    def release(self):
        self.released += 1


def runtime_for(profile_dir, driver=None, lease=None, options=None, chrome=None):
    opened = []

    def chrome_factory(**kwargs):
        opened.append(kwargs)
        if chrome is not None:
            return chrome(**kwargs)
        return driver

    runtime = StripchatChromeRuntime(
        options_factory=(lambda: options) if options else _Options,
        profile_dir_factory=lambda: profile_dir,
        lease_factory=lambda _path: lease or _Lease(),
        chrome_factory=chrome_factory,
    )
    return runtime, opened


class PersistentProfileTests(unittest.TestCase):
    def test_profile_dir_is_stable_and_marked(self):
        with tempfile.TemporaryDirectory() as root:
            os.environ["MODELSCRAPER_STRIPCHAT_PROFILE"] = os.path.join(
                root, "profile"
            )
            try:
                first = persistent_profile_dir()
                second = persistent_profile_dir()
            finally:
                del os.environ["MODELSCRAPER_STRIPCHAT_PROFILE"]
            self.assertEqual(first, second, "the profile must be reused")
            marker = json.loads(
                Path(first, OWNER_MARKER).read_text(encoding="utf-8")
            )
            self.assertEqual(marker["application"], "ModelScraper")
            self.assertEqual(marker["profile"], PROFILE_NAME)

    def test_default_location_uses_the_documented_name(self):
        with tempfile.TemporaryDirectory() as root:
            previous = os.environ.get("LOCALAPPDATA")
            os.environ["LOCALAPPDATA"] = root
            try:
                path = persistent_profile_dir()
                expected = public_state_root() / "chrome_profiles" / PROFILE_NAME
            finally:
                if previous is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = previous
            self.assertEqual(
                Path(path),
                expected,
            )
            self.assertFalse(Path(root, "ModelScraper").exists())

    def test_navigation_does_not_wait_for_the_load_event(self):
        """A room page fires its load event ~20s after it is decidable.

        The provider polls for its own evidence, so ``get`` only has to
        hand back a parsed document; waiting for the last media byte would
        cost the step twenty minutes it does not need to spend.
        """
        with tempfile.TemporaryDirectory() as root:
            options = _Options()
            runtime, _ = runtime_for(
                root, driver=_Driver(), options=options
            )
            runtime()
            self.assertEqual(
                options.capabilities.get("pageLoadStrategy"), "eager"
            )

    def test_the_profile_survives_quit_and_is_reused(self):
        with tempfile.TemporaryDirectory() as root:
            profile_dir = os.path.join(root, "stripchat-verify-v1")
            os.makedirs(profile_dir)
            cookie = Path(profile_dir, "Cookies")
            cookie.write_text("cf_clearance", encoding="utf-8")

            options = _Options()
            raw_driver = _Driver()
            lease = _Lease()
            runtime, opened = runtime_for(
                profile_dir, driver=raw_driver, lease=lease, options=options
            )
            runtime.configure(start_minimized=True)
            driver = runtime()

            self.assertEqual(opened[0]["user_data_dir"], profile_dir)
            self.assertIn("--start-minimized", options.arguments)
            self.assertNotIn("--headless", " ".join(options.arguments))
            self.assertEqual(lease.acquired, 1)

            self.assertTrue(runtime.hide())
            self.assertTrue(runtime.reveal())
            driver.quit()

            self.assertEqual(raw_driver.calls[-1], "quit")
            self.assertTrue(
                os.path.isdir(profile_dir),
                "the managed profile must never be deleted on quit",
            )
            self.assertEqual(
                cookie.read_text(encoding="utf-8"),
                "cf_clearance",
                "a solved challenge must survive for the next run",
            )
            self.assertEqual(lease.released, 1)
            self.assertIsNone(runtime._active)

            # A second run reuses the same directory.
            runtime2, opened2 = runtime_for(
                profile_dir, driver=_Driver(), lease=_Lease()
            )
            runtime2()
            self.assertEqual(opened2[0]["user_data_dir"], profile_dir)


class ProfileLeaseTests(unittest.TestCase):
    def test_a_busy_profile_refuses_instead_of_killing_chrome(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, opened = runtime_for(
                root, driver=_Driver(), lease=_Lease(busy=True)
            )
            with self.assertRaises(ProfileInUseError) as caught:
                runtime()
            self.assertEqual(caught.exception.code, "profile_in_use")
            self.assertEqual(
                opened, [], "Chrome must not be launched when the lease is busy"
            )
            self.assertIsNone(runtime._active)

    def test_a_failed_launch_releases_the_lease(self):
        with tempfile.TemporaryDirectory() as root:
            lease = _Lease()

            def fail(**_kwargs):
                raise RuntimeError("chrome failed")

            runtime, _opened = runtime_for(root, lease=lease, chrome=fail)
            with self.assertRaisesRegex(RuntimeError, "chrome failed"):
                runtime()
            self.assertEqual(lease.released, 1)
            self.assertIsNone(runtime._active)
            self.assertTrue(
                os.path.isdir(root), "a failed launch must not delete it"
            )

    def test_quit_is_idempotent_and_releases_once(self):
        with tempfile.TemporaryDirectory() as root:
            lease = _Lease()
            runtime, _opened = runtime_for(
                root, driver=_Driver(), lease=lease
            )
            driver = runtime()
            driver.quit()
            driver.quit()
            self.assertEqual(lease.released, 1)

    def test_real_lease_blocks_a_second_holder(self):
        with tempfile.TemporaryDirectory() as root:
            profile_dir = os.path.join(root, "stripchat-verify-v1")
            os.makedirs(profile_dir)
            first = StripchatChromeRuntime._default_lease(profile_dir)
            first.acquire()
            try:
                second = StripchatChromeRuntime._default_lease(profile_dir)
                with self.assertRaises(WorkflowBusyError):
                    second.acquire()
            finally:
                first.release()


class LegacyProfileTests(unittest.TestCase):
    def test_the_legacy_session_profile_is_never_touched(self):
        with tempfile.TemporaryDirectory() as root:
            legacy = Path(root, "sc sessions", "session 1", "uc_profile_sc")
            legacy.mkdir(parents=True)
            marker = legacy / "Cookies"
            marker.write_text("legacy", encoding="utf-8")

            profile_dir = os.path.join(root, "stripchat-verify-v1")
            os.makedirs(profile_dir)
            runtime, opened = runtime_for(profile_dir, driver=_Driver())
            runtime().quit()

            self.assertTrue(legacy.is_dir())
            self.assertEqual(marker.read_text(encoding="utf-8"), "legacy")
            self.assertNotIn("uc_profile_sc", opened[0]["user_data_dir"])


if __name__ == "__main__":
    unittest.main()
