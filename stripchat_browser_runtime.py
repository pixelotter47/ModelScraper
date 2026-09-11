"""Production Chrome runtime owned by the hardened Stripchat adapter."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from public_runtime import public_state_root
from runtime_lock import WorkflowBusyError, WorkflowLease
from workflow_types import utc_now_iso

PROFILE_NAME = "stripchat-verify-v1"
PROFILE_SCHEMA_VERSION = 1
OWNER_MARKER = ".modelscraper-stripchat-profile.json"


class ProfileInUseError(RuntimeError):
    """Another process already owns the Stripchat verification profile."""

    code = "profile_in_use"

    def __init__(self, owner=None):
        self.owner = owner or {}
        super().__init__("profile_in_use")


def persistent_profile_dir() -> str:
    """The one managed Stripchat verification profile.

    Reused across runs and resumes so a solved Cloudflare challenge keeps
    its ``cf_clearance`` cookie. Never deleted automatically; a future
    maintenance action must verify ownership and ask for explicit intent.
    """
    override = os.environ.get("MODELSCRAPER_STRIPCHAT_PROFILE")
    if override:
        path = Path(override).resolve()
    else:
        path = public_state_root() / "chrome_profiles" / PROFILE_NAME
    path.mkdir(parents=True, exist_ok=True)
    marker = path / OWNER_MARKER
    if not marker.exists():
        marker.write_text(
            json.dumps(
                {
                    "application": "ModelScraper",
                    "profile": PROFILE_NAME,
                    "schema_version": PROFILE_SCHEMA_VERSION,
                    "created_at": utc_now_iso(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return str(path)


class _OwnedChromeDriver:
    """Proxy that releases the profile lease on quit, deleting nothing.

    The managed profile is persistent by design, so ``quit`` must never
    remove it; only the cross-process lease is given back.
    """

    def __init__(self, driver, release, owner):
        self._driver = driver
        self._release = release
        self._owner = owner
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._driver, name)

    def quit(self):
        if self._closed:
            return
        self._closed = True
        error = None
        try:
            self._driver.quit()
        except Exception as exc:  # provider close is best-effort
            error = exc
        finally:
            try:
                self._release()
            finally:
                self._owner._released(self)
        if error is not None:
            raise error


class StripchatChromeRuntime:
    """Creates one visible UC Chrome with an owned temporary profile.

    The object is both the zero-argument driver factory expected by
    ``StripchatBrowserProvider`` and its reveal/hide window manager. It never
    touches the legacy ``uc_profile_sc`` directory.
    """

    def __init__(
        self,
        *,
        logger=None,
        chrome_factory=None,
        options_factory=None,
        profile_dir_factory=None,
        lease_factory=None,
    ):
        self.logger = logger or (lambda _message: None)
        self._chrome_factory = chrome_factory
        self._options_factory = options_factory
        self._profile_dir_factory = profile_dir_factory or persistent_profile_dir
        self._lease_factory = lease_factory or self._default_lease
        self._start_minimized = False
        self._active = None
        self._lock = threading.RLock()

    @staticmethod
    def _default_lease(profile_dir):
        """One cross-process lease per profile directory."""
        return WorkflowLease(
            profile_dir,
            platform="stripchat",
            task="stripchat-verify-profile",
            runtime_dir=os.path.join(
                os.path.dirname(os.path.normpath(profile_dir)), ".locks"
            ),
        )

    def configure(self, *, start_minimized=False):
        with self._lock:
            if self._active is not None:
                raise RuntimeError("stripchat browser is already active")
            self._start_minimized = bool(start_minimized)

    def _new_options(self):
        if self._options_factory is not None:
            return self._options_factory()
        from ctb_core import uc

        return uc.ChromeOptions()

    def _open_chrome(self, *, options, user_data_dir):
        if self._chrome_factory is not None:
            return self._chrome_factory(
                options=options, user_data_dir=user_data_dir
            )
        from ctb_core import _uc_chrome

        return _uc_chrome(options=options, user_data_dir=user_data_dir)

    def __call__(self):
        with self._lock:
            if self._active is not None:
                raise RuntimeError("stripchat browser is already active")
            start_minimized = self._start_minimized
        options = self._new_options()
        # The provider polls for its own evidence, so navigation only has to
        # hand back a parsed document. Waiting for the load event added ~20s
        # of media and telemetry to every candidate and settled nothing.
        try:
            options.set_capability("pageLoadStrategy", "eager")
        except Exception:
            pass
        for argument in (
            "--mute-audio",
            "--disable-popup-blocking",
            "--window-size=1200,900",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
        ):
            options.add_argument(argument)
        # Never use headless Chrome for Stripchat. A minimized real window can
        # be revealed when a challenge requires user interaction.
        if start_minimized:
            options.add_argument("--start-minimized")
            options.add_argument("--window-position=-32000,-32000")
        profile_path = self._profile_dir_factory()
        lease = self._lease_factory(profile_path)
        try:
            lease.acquire()
        except WorkflowBusyError as exc:
            # Another ModelScraper process owns the profile. Refuse the
            # step; never kill the other Chrome.
            self.logger(
                "[WARN] The Stripchat verification profile is in use by "
                "another process."
            )
            raise ProfileInUseError(exc.owner) from exc
        released = False

        def release():
            nonlocal released
            if released:
                return
            released = True
            try:
                lease.release()
            except Exception:
                pass

        try:
            driver = self._open_chrome(
                options=options, user_data_dir=profile_path
            )
        except Exception:
            release()
            raise
        owned = _OwnedChromeDriver(driver, release, self)
        collision = False
        with self._lock:
            if self._active is not None:
                collision = True
            else:
                self._active = owned
        if collision:
            try:
                owned.quit()
            except Exception:
                release()
            raise RuntimeError("stripchat browser ownership collision")
        return owned

    def _released(self, driver):
        with self._lock:
            if self._active is driver:
                self._active = None

    def reveal(self):
        with self._lock:
            driver = self._active
        if driver is None:
            return False
        try:
            driver.set_window_rect(x=120, y=80, width=1200, height=900)
        except Exception:
            try:
                driver.maximize_window()
            except Exception:
                return False
        return True

    def hide(self):
        with self._lock:
            driver = self._active
            enabled = self._start_minimized
        if driver is None or not enabled:
            return False
        try:
            driver.minimize_window()
        except Exception:
            try:
                driver.set_window_rect(x=-32000, y=-32000, width=1, height=1)
            except Exception:
                return False
        return True
