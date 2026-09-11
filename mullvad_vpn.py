import os
import shutil
import subprocess
import time

from windows_subprocess import hidden_subprocess_kwargs


DEFAULT_MULLVAD_EXE = r"C:\Program Files\Mullvad VPN\resources\mullvad.exe"
DEFAULT_CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

DEFAULT_RELAY_LOCATION = "ie"

# Mullvad relay countries offered in the GUI, keyed by the CLI location code.
MULLVAD_LOCATIONS = {
    "ie": "Ireland",
    "gb": "UK",
    "nl": "Netherlands",
    "de": "Germany",
    "fr": "France",
    "es": "Spain",
    "it": "Italy",
    "se": "Sweden",
    "ch": "Switzerland",
    "at": "Austria",
    "pl": "Poland",
    "cz": "Czech Republic",
    "us": "USA",
    "ca": "Canada",
}


class MullvadError(RuntimeError):
    pass


def _configured_setting(key):
    """Read a GUI setting without importing settings at module load time."""
    try:
        from app_settings import shared_setting
    except Exception:
        return ""
    try:
        return str(shared_setting(key, "") or "").strip()
    except Exception:
        return ""


def find_mullvad_executable():
    configured = _configured_setting("mullvadPath") or os.environ.get(
        "MODEL_SCRAPER_MULLVAD_PATH"
    )
    if configured:
        return configured

    for name in ("mullvad", "mullvad.exe"):
        found = shutil.which(name)
        if found:
            return found

    return DEFAULT_MULLVAD_EXE


def find_chrome_executable():
    configured = _configured_setting("chromePath") or os.environ.get(
        "MODEL_SCRAPER_CHROME_PATH"
    )
    if configured:
        return configured

    for name in ("chrome", "chrome.exe"):
        found = shutil.which(name)
        if found:
            return found

    return DEFAULT_CHROME_EXE


def find_relay_location():
    configured = _configured_setting("vpnRelayLocation").lower()
    if configured in MULLVAD_LOCATIONS:
        return configured
    return DEFAULT_RELAY_LOCATION


class MullvadVpnController:
    def __init__(
        self,
        command_path=None,
        chrome_path=None,
        command_runner=None,
        sleep=time.sleep,
        connect_timeout=90,
        command_timeout=30,
        verify_attempts=30,
        verify_interval=1.0,
        relay_location=None,
    ):
        self.command_path = command_path or find_mullvad_executable()
        self.chrome_path = chrome_path or find_chrome_executable()
        self._command_runner = command_runner or self._run_subprocess
        self._sleep = sleep
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.verify_attempts = max(1, int(verify_attempts))
        self.verify_interval = verify_interval
        self.relay_location = (
            str(relay_location).strip().lower()
            if relay_location
            else find_relay_location()
        )
        self.relay_display = MULLVAD_LOCATIONS.get(
            self.relay_location, self.relay_location.upper()
        )

    def connect_relay(self):
        self._run("relay", "set", "location", self.relay_location)
        current_status = self.status()
        if self.is_connected_to_relay(current_status):
            return

        if self.is_connected(current_status):
            self._run("reconnect", "--wait", timeout=self.connect_timeout)
        else:
            self._run("connect", "--wait", timeout=self.connect_timeout)

        self._wait_for_status(
            self.is_connected_to_relay,
            f"Connected to {self.relay_display}",
        )

    def connect_ireland(self):
        """Kept as the historical name used by the workflow coordinator."""
        return self.connect_relay()

    def disconnect(self):
        current_status = self.status()
        if self.is_disconnected(current_status):
            return

        self._run("disconnect", "--wait", timeout=self.connect_timeout)
        self._wait_for_status(self.is_disconnected, "Disconnected")

    def status(self):
        return self._run("status")

    def split_tunnel_apps(self):
        output = self._run("split-tunnel", "get")
        apps = []
        in_apps = False
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.lower().startswith("excluded applications"):
                in_apps = True
                continue
            if in_apps:
                apps.append(line)
        return apps

    def remove_chrome_from_split_tunnel(self):
        self.ensure_chrome_not_in_split_tunnel()

    def add_chrome_to_split_tunnel(self):
        self.ensure_chrome_in_split_tunnel()

    def ensure_chrome_not_in_split_tunnel(self):
        if self._path_in_list(self.chrome_path, self.split_tunnel_apps()):
            self._run("split-tunnel", "app", "remove", self.chrome_path)
        if self._path_in_list(self.chrome_path, self.split_tunnel_apps()):
            raise MullvadError("Chrome is still excluded after removal.")

    def ensure_chrome_in_split_tunnel(self):
        if not self._path_in_list(self.chrome_path, self.split_tunnel_apps()):
            self._run("split-tunnel", "app", "add", self.chrome_path)
        if not self._path_in_list(self.chrome_path, self.split_tunnel_apps()):
            raise MullvadError("Chrome split-tunnel exclusion was not verified.")

    def ensure_connected_relay(self):
        self.connect_relay()
        status = self.status()
        if not self.is_connected_to_relay(status):
            raise MullvadError(
                f"Mullvad {self.relay_display} connection was not verified."
            )

    def ensure_connected_ireland(self):
        """Kept as the historical name used by the workflow coordinator."""
        return self.ensure_connected_relay()

    @staticmethod
    def _normalize_path(path):
        return os.path.normcase(os.path.normpath((path or "").strip()))

    @classmethod
    def _path_in_list(cls, target_path, paths):
        target = cls._normalize_path(target_path)
        return any(cls._normalize_path(path) == target for path in paths)

    @staticmethod
    def is_connected(status_text):
        return (status_text or "").strip().lower().startswith("connected")

    @staticmethod
    def is_disconnected(status_text):
        return (status_text or "").strip().lower().startswith("disconnected")

    @classmethod
    def matches_relay(cls, status_text, location, display_name):
        text = status_text or ""
        lower = text.lower()
        if not cls.is_connected(text):
            return False
        code = f"{str(location or '').lower()}-"
        return (
            str(display_name or "").lower() in lower
            or "relay:" in lower
            and code in lower
        )

    def is_connected_to_relay(self, status_text):
        return self.matches_relay(
            status_text, self.relay_location, self.relay_display
        )

    @classmethod
    def is_connected_to_ireland(cls, status_text):
        return cls.matches_relay(status_text, "ie", "Ireland")

    def _wait_for_status(self, predicate, description):
        last_status = ""
        for attempt in range(self.verify_attempts):
            last_status = self.status()
            if predicate(last_status):
                return
            if attempt < self.verify_attempts - 1:
                self._sleep(self.verify_interval)

        cleaned = " ".join((last_status or "").split())
        raise MullvadError(
            f"Mullvad did not reach state: {description}. Last status: {cleaned}"
        )

    def _run(self, *args, timeout=None):
        try:
            return self._command_runner(list(args), timeout or self.command_timeout)
        except MullvadError:
            raise
        except Exception as exc:
            raise MullvadError(f"Mullvad command failed: {' '.join(args)} ({exc})")

    def _run_subprocess(self, args, timeout):
        command = [self.command_path, *args]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                **hidden_subprocess_kwargs(),
            )
        except FileNotFoundError as exc:
            raise MullvadError(
                f"Mullvad CLI not found at {self.command_path}. "
                "Set MODEL_SCRAPER_MULLVAD_PATH if it is installed elsewhere."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise MullvadError(
                f"Mullvad command timed out after {timeout}s: {' '.join(args)}"
            ) from exc

        output = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part and part.strip()
        )
        if completed.returncode != 0:
            raise MullvadError(
                f"Mullvad command failed: {' '.join(args)}. Output: {output}"
            )
        return output
