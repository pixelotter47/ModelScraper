import datetime
import json
import os
import random
import re
import time
import subprocess
import threading
import shutil
import tempfile
from pathlib import Path
import uuid

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

from app_settings import AppSettings
from location_profiles import (
    LocationPolicy, bind_session_location_policy, build_location_profile_match,
    read_session_location_policy, session_has_location_artifacts,
)
from public_runtime import public_state_root
from ctb_classifier import PageObservation, classify_page
from ctb_api import ChaturbateApiClient
from ctb_store import ChaturbateRunStore, MasterRepository
from ctb_verifier import ChaturbateVerifier
from storage_utils import sha256_file
from temp_profile import TemporaryChromeProfile
from workflow_types import (
    ArtifactRecord,
    OutcomeStatus,
    StepName,
    StepOutcome,
    VerificationRecord,
)
from windows_subprocess import hidden_subprocess_kwargs


def safe_quit(driver):
    """
    Safely quit the driver, handling the known undetected-chromedriver cleanup error.
    This suppresses the [WinError 6] 'The handle is invalid' which is harmless
    but noisy in the logs.
    """
    if not driver:
        return
    try:
        driver.quit()
    except Exception:
        # Suppress harmless deallocation errors
        pass


def _parse_chrome_major(version_text):
    if not version_text:
        return None
    match = re.search(r"(\d+)\.", version_text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _read_windows_chrome_major():
    try:
        import winreg
    except Exception:
        return None

    reg_paths = [
        (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Wow6432Node\Google\Chrome\BLBeacon"),
    ]
    for root, path in reg_paths:
        try:
            with winreg.OpenKey(root, path) as key:
                version, _ = winreg.QueryValueEx(key, "version")
            major = _parse_chrome_major(str(version))
            if major:
                return major
        except OSError:
            continue
    return None


def _version_from_command(cmd):
    try:
        output = subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        ).strip()
    except Exception:
        return None
    return _parse_chrome_major(output)


def _detect_chrome_version_main():
    env_value = os.environ.get("CHROME_VERSION_MAIN")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    if os.name == "nt":
        major = _read_windows_chrome_major()
        if major:
            return major

    finder = getattr(uc, "find_chrome_executable", None)
    if callable(finder):
        try:
            path = finder()
        except Exception:
            path = None
        if path:
            major = _version_from_command([path, "--version"])
            if major:
                return major

    for exe in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(exe)
        if path:
            major = _version_from_command([path, "--version"])
            if major:
                return major

    return None


def _uc_chrome(options=None, user_data_dir=None):
    kwargs = {}
    if options is not None:
        kwargs["options"] = options
    if user_data_dir is not None:
        kwargs["user_data_dir"] = user_data_dir
    version_main = _detect_chrome_version_main()
    if version_main:
        kwargs["version_main"] = version_main
    return uc.Chrome(**kwargs)


IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _KERNEL32 = ctypes.windll.kernel32
    _USER32 = ctypes.windll.user32

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    SW_HIDE = 0
    SW_SHOW = 5
    SW_RESTORE = 9
    GWL_EXSTYLE = -20
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_FRAMECHANGED = 0x0020
    SWP_NOACTIVATE = 0x0010
    ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_size_t)

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ULONG_PTR),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
        ]

    def _win_list_processes():
        snapshot = _KERNEL32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return []

        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        processes = []

        if not _KERNEL32.Process32FirstW(snapshot, ctypes.byref(entry)):
            _KERNEL32.CloseHandle(snapshot)
            return processes

        while True:
            processes.append(
                (entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile)
            )
            if not _KERNEL32.Process32NextW(snapshot, ctypes.byref(entry)):
                break

        _KERNEL32.CloseHandle(snapshot)
        return processes

    def _win_get_chrome_pids():
        pids = set()
        for pid, _, exe in _win_list_processes():
            if exe and exe.lower() == "chrome.exe":
                pids.add(pid)
        return pids

    def _win_get_chrome_processes():
        try:
            cmd = (
                "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
            )
            output = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", cmd],
                text=True,
                stderr=subprocess.DEVNULL,
                **hidden_subprocess_kwargs(),
            ).strip()
            if not output:
                return []
            data = json.loads(output)
            if isinstance(data, dict):
                data = [data]
            processes = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                pid = item.get("ProcessId")
                cmdline = item.get("CommandLine") or ""
                if pid is None:
                    continue
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    continue
                processes.append((pid, cmdline))
            return processes
        except Exception:
            return []

    def _win_get_chrome_pids_for_profile(profile_dir):
        target = profile_dir.lower()
        pids = set()
        for pid, cmdline in _win_get_chrome_processes():
            if cmdline and target in cmdline.lower():
                pids.add(pid)
        return pids

    def _win_get_chrome_pids_for_parent(parent_pid):
        if not parent_pid:
            return set()
        processes = _win_list_processes()
        children_map = {}
        chrome_pids = set()
        for pid, ppid, exe in processes:
            children_map.setdefault(ppid, set()).add(pid)
            if exe and exe.lower() == "chrome.exe":
                chrome_pids.add(pid)

        descendants = set()
        stack = [parent_pid]
        while stack:
            current = stack.pop()
            for child in children_map.get(current, set()):
                if child not in descendants:
                    descendants.add(child)
                    stack.append(child)
        return chrome_pids.intersection(descendants)

    def _win_find_chrome_windows(pids):
        if not pids:
            return []

        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_proc(hwnd, _):
            pid = wintypes.DWORD()
            _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                class_name = ctypes.create_unicode_buffer(256)
                _USER32.GetClassNameW(hwnd, class_name, 256)
                if class_name.value.startswith("Chrome_WidgetWin"):
                    found.append(hwnd)
            return True

        _USER32.EnumWindows(enum_proc, 0)
        return found

    def _win_set_toolwindow(hwnd, enabled):
        if not hwnd:
            return
        style = _USER32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        else:
            style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
        _USER32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        _USER32.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE,
        )

    def _win_hide_windows_for_pids(pids):
        hwnds = _win_find_chrome_windows(pids)
        for hwnd in hwnds:
            _win_set_toolwindow(hwnd, True)
            _USER32.ShowWindow(hwnd, SW_HIDE)
        return len(hwnds)

    def _win_show_windows_for_pids(pids):
        hwnds = _win_find_chrome_windows(pids)
        for hwnd in hwnds:
            _win_set_toolwindow(hwnd, False)
            _USER32.ShowWindow(hwnd, SW_RESTORE)
            _USER32.SetForegroundWindow(hwnd)
        return len(hwnds)

    def _win_hide_windows_for_profile(profile_dir):
        pids = _win_get_chrome_pids_for_profile(profile_dir)
        if not pids:
            return 0
        hwnds = _win_find_chrome_windows(pids)
        for hwnd in hwnds:
            _win_set_toolwindow(hwnd, True)
            _USER32.ShowWindow(hwnd, SW_HIDE)
        return len(hwnds)

    def _win_show_windows_for_profile(profile_dir):
        pids = _win_get_chrome_pids_for_profile(profile_dir)
        if not pids:
            return 0
        hwnds = _win_find_chrome_windows(pids)
        for hwnd in hwnds:
            _win_set_toolwindow(hwnd, False)
            _USER32.ShowWindow(hwnd, SW_RESTORE)
            _USER32.SetForegroundWindow(hwnd)
        return len(hwnds)

    class _ChromeWindowHider:
        def __init__(self, profile_dir, parent_pid=None):
            self.profile_dir = profile_dir
            self.parent_pid = parent_pid
            self._hide_event = threading.Event()
            self._stop_event = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._pids = set()
            self._last_pid_scan = 0.0

        def start(self):
            if not self._thread.is_alive():
                self._thread.start()

        def stop(self):
            self._stop_event.set()

        def hide(self):
            self._hide_event.set()
            return self._apply_hide(force=True)

        def show(self):
            self._hide_event.clear()
            return self._apply_show(force=True)

        def _refresh_pids(self, force=False):
            now = time.time()
            if force or not self._pids or now - self._last_pid_scan > 1.0:
                self._pids = set()
                if self.parent_pid:
                    self._pids = _win_get_chrome_pids_for_parent(self.parent_pid)
                if not self._pids:
                    self._pids = _win_get_chrome_pids_for_profile(self.profile_dir)
                self._last_pid_scan = now

        def _apply_hide(self, force=False):
            self._refresh_pids(force=force)
            if not self._pids:
                return False
            return _win_hide_windows_for_pids(self._pids) > 0

        def _apply_show(self, force=False):
            self._refresh_pids(force=force)
            if not self._pids:
                return False
            return _win_show_windows_for_pids(self._pids) > 0

        def _run(self):
            while not self._stop_event.is_set():
                if self._hide_event.is_set():
                    self._apply_hide()
                    time.sleep(0.2)
                else:
                    time.sleep(0.1)


else:

    class _ChromeWindowHider:
        """No-op window hider for non-Windows platforms."""

        def __init__(self, profile_dir, parent_pid=None):
            self.profile_dir = profile_dir
            self.parent_pid = parent_pid

        def start(self):
            return None

        def stop(self):
            return None

        def hide(self):
            return False

        def show(self):
            return False


CAPTCHA_MARKERS = (
    "just a moment",
    "cf-challenge",
    "checking your browser",
    "verify you are human",
    "cf_chl_opt",
)


def page_has_captcha(title, source):
    """Return True when the page is a Cloudflare interstitial."""
    haystack = f"{title or ''}\n{source or ''}".lower()
    return any(marker in haystack for marker in CAPTCHA_MARKERS)


# Matches ChaturbateApiClient.collect_adaptive so both transports agree on
# what "the online list has settled" means.
SNAPSHOT_JACCARD_THRESHOLD = 0.985
SNAPSHOT_COUNT_DELTA_THRESHOLD = 0.02


def snapshot_pass_metrics(previous, current):
    """Return (overlap, count_delta) between two snapshot passes."""
    if not previous or not current:
        return 0.0, 1.0
    union = previous | current
    if not union:
        return 0.0, 1.0
    jaccard = len(previous & current) / len(union)
    denominator = max(len(previous), len(current), 1)
    count_delta = abs(len(previous) - len(current)) / denominator
    return jaccard, count_delta


def passes_are_stable(
    previous,
    current,
    *,
    jaccard_threshold=SNAPSHOT_JACCARD_THRESHOLD,
    count_delta_threshold=SNAPSHOT_COUNT_DELTA_THRESHOLD,
):
    """Return True when two consecutive snapshot passes agree.

    The online list churns constantly, so a single pass invents candidates
    that were merely blinking offline. Once two passes overlap almost
    entirely there is nothing left for further passes to discover.
    """
    if not previous or not current:
        return False
    jaccard, count_delta = snapshot_pass_metrics(previous, current)
    return (
        jaccard >= jaccard_threshold
        and count_delta <= count_delta_threshold
    )


BLOCK_NOTICE_MARKERS = ("region or gender",)

# Confirmed against live markup: room_view_container no longer exists, so a
# rendered room has to be recognised by what the page actually ships.
ROOM_CONTAINER_SELECTOR = ", ".join(
    (
        "#room_view_container",
        "[data-testid='room-subject']",
        "[data-testid='room-tabs']",
        "div.chat_room",
        "div.roomPage",
    )
)

OFFLINE_MARKERS = (
    "room is currently offline",
    "is currently offline",
    "member you are trying to view is currently offline",
)


def page_is_offline(text):
    lowered = (text or "").lower()
    return any(marker in lowered for marker in OFFLINE_MARKERS)

AGE_GATE_MARKERS = (
    "you must be over 18",
    "agree to the terms below before continuing",
)

AGE_GATE_TEXT_PATTERNS = (
    "i am 18",
    "or older",
    "i agree",
    "enter",
    "continue",
    "accept",
)

_LOWER = (
    "translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
)


def _age_gate_candidates(driver):
    """Yield anything plausibly clickable on the terms splash.

    The splash markup is not stable across site changes, so cast a wide net
    over roles and labels rather than betting on one selector.
    """
    selectors = [
        (By.CSS_SELECTOR, "#entrance_terms_agree"),
        (By.CSS_SELECTOR, "[data-testid*='terms']"),
        (By.CSS_SELECTOR, "[data-testid*='age']"),
        (By.CSS_SELECTOR, "[id*='entrance'] a, [id*='entrance'] button"),
        (By.CSS_SELECTOR, "[class*='entrance'] a, [class*='entrance'] button"),
        (By.CSS_SELECTOR, "[role='button']"),
        (By.CSS_SELECTOR, "input[type='submit'], input[type='button']"),
    ]
    for pattern in AGE_GATE_TEXT_PATTERNS:
        selectors.append(
            (
                By.XPATH,
                "//*[self::a or self::button or self::div or self::span]"
                f"[contains({_LOWER}, '{pattern}')]",
            )
        )
    seen = set()
    for how, selector in selectors:
        try:
            elements = driver.find_elements(how, selector)
        except Exception:
            continue
        for element in elements:
            key = id(element)
            if key in seen:
                continue
            seen.add(key)
            yield element


def dump_age_gate_markup(driver, folder):
    """Persist the splash markup so unmatched controls can be diagnosed."""
    try:
        source = driver.page_source or ""
    except Exception:
        return ""
    if not source:
        return ""
    path = os.path.join(folder, "age_gate_debug.html")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
    except OSError:
        return ""
    return path


def page_has_age_gate(text):
    lowered = (text or "").lower()
    return any(marker in lowered for marker in AGE_GATE_MARKERS)


def wait_for_page_text(driver, timeout=30.0, should_stop=None):
    """Return readable page text, waiting for the document to render.

    An unreadable page must never be mistaken for a clean one: empty text
    would otherwise satisfy every "is the splash gone?" check and report
    success for a page that never loaded.
    """
    deadline = time.monotonic() + float(timeout)
    while True:
        if should_stop is not None and should_stop():
            return ""
        try:
            ready = driver.execute_script("return document.readyState")
        except Exception:
            ready = None
        text = _visible_block_excerpt(driver, limit=400)
        if text.strip() and ready in (None, "interactive", "complete"):
            return text
        if time.monotonic() >= deadline:
            return text
        time.sleep(0.5)


def dismiss_age_gate(driver, timeout=8.0, page_timeout=30.0):
    """Accept Chaturbate's terms splash so room content can render.

    Until the splash is accepted the room markup never loads, so pages come
    back with neither a denied notice nor a room container and get filed as
    ``unexpected_page``. Accepting once stores the consent in the persistent
    profile and covers every later navigation.
    """
    text = wait_for_page_text(driver, timeout=page_timeout)
    if not text.strip():
        # Blank page: cannot claim the splash is gone.
        return False
    if not page_has_age_gate(text):
        return True
    for element in _age_gate_candidates(driver):
        try:
            if not element.is_displayed():
                continue
            element.click()
        except Exception:
            continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not page_has_age_gate(
                _visible_block_excerpt(driver, limit=400)
            ):
                return True
            time.sleep(0.25)
    return not page_has_age_gate(_visible_block_excerpt(driver, limit=400))


def age_gate_state(driver, timeout=30.0):
    """Return "clear", "present" or "unreadable" for the current page."""
    text = wait_for_page_text(driver, timeout=timeout)
    if not text.strip():
        return "unreadable"
    return "present" if page_has_age_gate(text) else "clear"


def wait_for_age_gate_clear(driver, timeout=300.0, should_stop=None):
    """Wait for a human to accept the splash when automation cannot."""
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        if should_stop is not None and should_stop():
            return False
        text = _visible_block_excerpt(driver, limit=400)
        if text.strip() and not page_has_age_gate(text):
            return True
        time.sleep(0.5)
    return False


def _visible_block_excerpt(driver, limit=240):
    """Return page text the classifier can actually match block notices in.

    ``classify_page`` looks for human-visible wording such as "region or
    gender". Handing it ``page_source[:limit]`` only ever yields the ``<head>``
    boilerplate, so the check could never fire. Prefer the rendered body text,
    and fall back to a marker-anchored slice of the source.
    """
    try:
        body = driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        body = ""
    if body.strip():
        return " ".join(body.split())[:limit]
    try:
        source = driver.page_source or ""
    except Exception:
        return ""
    lowered = source.lower()
    for marker in BLOCK_NOTICE_MARKERS:
        index = lowered.find(marker)
        if index != -1:
            return " ".join(source[index : index + limit].split())[:limit]
    return " ".join(source.split())[:limit]


VERIFY_PROFILE_NAME = "cb-verify-persistent"


def persistent_verify_profile_dir():
    """Return a stable Chrome profile dir reused across verification runs.

    Cloudflare issues a ``cf_clearance`` cookie after a solved challenge.
    Keeping the profile between runs means the challenge is solved once per
    expiry window instead of once per run (and often once per page).
    """
    root = os.environ.get("MODELSCRAPER_VERIFY_PROFILE")
    if root:
        path = os.path.abspath(root)
    else:
        path = str(public_state_root() / "chrome_profiles" / VERIFY_PROFILE_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def active_session_profile_priority(item, session_path):
    """Prioritize current-session VPN matches, then current local matches."""
    parts = os.path.basename(os.path.normpath(session_path or "")).split()
    if len(parts) < 2 or parts[0].lower() != "session":
        return 2
    session_num = _safe_int(parts[1], -1)
    if session_num < 0:
        return 2

    vpn_evidence = item.get("vpn_profile_match") or {}
    if (
        item.get("vpn_location_profile_match")
        and _safe_int(vpn_evidence.get("session"), -2) == session_num
    ):
        return 0

    local_evidence = item.get("profile_match") or {}
    if (
        item.get("location_profile_match")
        and _safe_int(local_evidence.get("session"), -2) == session_num
    ):
        return 1
    return 2


def active_session_display_priority(item, session_path, initial_models):
    """Keep every new model ahead of older current-session profile matches."""
    profile_priority = active_session_profile_priority(item, session_path)
    name = item.get("name")
    is_new = bool(name and name not in initial_models)

    if is_new:
        # Preserve the useful LOCATION BLOCKED -> LOCATION PROFILE -> other ordering, but
        # never let an older profile match split the visible block of new
        # models produced by the current app session.
        return profile_priority

    if profile_priority == 1:
        return 3
    return 4


class CTBPaths:
    def __init__(self, session_folder):
        self.session_folder = session_folder
        self.vpn_list = os.path.join(session_folder, "cb_vpn_list.txt")
        self.local_list = os.path.join(session_folder, "cb_local_list.txt")
        self.candidates = os.path.join(session_folder, "cb_candidates.txt")
        self.verified = os.path.join(session_folder, "FINAL_BLOCKED.txt")
        self.debug = os.path.join(session_folder, "debug_log.txt")
        self.metadata = os.path.join(session_folder, "vpn_metadata.json")
        self.local_profile_matches = os.path.join(
            session_folder, "cb_local_profile_matches.json"
        )
        self.vpn_profile_matches = os.path.join(
            session_folder, "cb_vpn_profile_matches.json"
        )


class CTBRunner:
    supports_location_profiles = True

    def __init__(self, base_dir, logger=None, settings=None, location_policy=None):
        self.base_dir = base_dir
        self.settings = settings
        if self.supports_location_profiles and self.settings is None:
            self.settings = AppSettings(base_dir=Path(base_dir).parent)
        self._injected_location_policy = location_policy
        self.location_policy = self._configured_location_policy()
        self.logger = logger
        self.session_folder = ""
        self.paths = None
        self._stop_requested = False
        self._active_driver = None
        self._last_snapshot_metrics = None
        self._http_mode = os.environ.get(
            "MODEL_SCRAPER_CTB_HTTP_MODE", "browser"
        )
        # "adaptive" stops once two consecutive passes agree (usually 2-3)
        # instead of always paying for 5. Set the variable to "fixed" to get
        # the old always-five behaviour back.
        self._passes_mode = os.environ.get(
            "MODEL_SCRAPER_CTB_PASSES_MODE", "adaptive"
        )
        if self._http_mode not in (
            "browser",
            "http_canary",
            "http_with_browser_fallback",
        ):
            raise ValueError("Invalid MODEL_SCRAPER_CTB_HTTP_MODE")
        if self._passes_mode not in (
            "fixed",
            "adaptive_canary",
            "adaptive",
        ):
            raise ValueError("Invalid MODEL_SCRAPER_CTB_PASSES_MODE")
        self.log(
            f"[INFO] CTB modes: transport={self._http_mode}, "
            f"passes={self._passes_mode}"
        )

    def set_logger(self, logger):
        self.logger = logger

    def log(self, message):
        if self.logger:
            self.logger(message)

    def clear_stop(self):
        self._stop_requested = False

    def _should_stop(self):
        return bool(self._stop_requested)

    def _set_active_driver(self, driver):
        self._active_driver = driver

    def request_stop(self):
        self._stop_requested = True
        driver = self._active_driver
        if driver is not None:
            try:
                safe_quit(driver)
            except Exception:
                pass

    def _list_session_dirs(self):
        try:
            entries = os.listdir(self.base_dir)
        except FileNotFoundError:
            return []

        session_dirs = []
        for name in entries:
            if not name.lower().startswith("session "):
                continue
            full_path = os.path.join(self.base_dir, name)
            if os.path.isdir(full_path):
                session_dirs.append(full_path)
        return session_dirs

    def _next_session_number(self):
        max_num = 0
        for folder in self._list_session_dirs():
            base = os.path.basename(folder)
            parts = base.split()
            if len(parts) >= 2:
                max_num = max(max_num, _safe_int(parts[1], 0))
        return max_num + 1

    def create_session(self):
        date_str = datetime.datetime.now().strftime("%d.%m.%Y")
        session_path = None
        for _attempt in range(10):
            session_num = self._next_session_number()
            folder_name = f"session {session_num} {date_str}"
            candidate = os.path.join(self.base_dir, folder_name)
            try:
                os.makedirs(candidate, exist_ok=False)
                session_path = candidate
                break
            except FileExistsError:
                continue
        if session_path is None:
            raise RuntimeError("Could not reserve a unique session number.")
        self.set_session(session_path)
        self.log(f"[INFO] New session created: {session_path}")
        return session_path

    def use_latest_session(self):
        session_dirs = self._list_session_dirs()
        if not session_dirs:
            self.log("[WARN] No session folders found. Creating a new session.")
            return self.create_session()

        session_dirs.sort(key=self._session_sort_key)
        latest = session_dirs[-1]
        self.set_session(latest)
        self.log(f"[INFO] Using latest session: {latest}")
        return latest

    def _session_sort_key(self, path):
        session_num = 0
        parts = os.path.basename(os.path.normpath(path)).split()
        if len(parts) >= 2:
            session_num = _safe_int(parts[1], 0)
        state_path = os.path.join(path, "run_state.json")
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
                run_id = state.get("active_run_id")
                manifest_path = os.path.join(
                    path, "runs", str(run_id), "manifest.json"
                )
                with open(manifest_path, "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
                return (2, str(manifest.get("created_at") or ""), session_num)
            except (OSError, ValueError, AttributeError):
                return (-1, "", session_num)
        return (1, "", session_num)

    def _configured_location_policy(self):
        if not self.supports_location_profiles:
            return LocationPolicy()
        if self._injected_location_policy is not None:
            return self._injected_location_policy
        self.settings.load(strict=True)
        return LocationPolicy.from_settings(self.settings)

    def set_session(self, session_path):
        self.session_folder = session_path
        self.paths = CTBPaths(session_path)
        # Selecting historical sessions stays read-only. Bind fresh sessions
        # before a workflow creates manifests or snapshots in them.
        if (self.supports_location_profiles and os.path.isdir(session_path)
                and not session_has_location_artifacts(session_path)):
            self.location_policy = bind_session_location_policy(
                session_path, self._configured_location_policy()
            )

    def _ensure_session(self):
        if not self.session_folder:
            self.create_session()
        if self.supports_location_profiles:
            self.location_policy = bind_session_location_policy(
                self.session_folder, self._configured_location_policy()
            )

    def _write_list(self, path, items):
        with open(path, "w", encoding="utf-8") as handle:
            for item in sorted(items):
                handle.write(item + "\n")

    def _write_profile_matches(self, matches, path, label):
        records = sorted(matches.values(), key=lambda item: item.get("name", ""))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2, ensure_ascii=False)
        self.log(
            f"[INFO] {label} location profile matches saved: "
            f"{path} ({len(records)} models)"
        )

    def _fetch_models_via_browser_api(
        self,
        mode_name,
        robust=False,
        save_metadata=False,
        save_profile_matches=False,
        save_vpn_profile_matches=False,
        headless=True,
    ):
        self._ensure_session()
        if self._should_stop():
            self.log("[INFO] Stop requested before API fetch started.")
            return set()
        self.log(f"[INFO] Contacting API for {mode_name} list...")
        if robust:
            self.log("[INFO] Robust mode enabled (5 passes).")

        online_set = set()
        metadata_map = {}
        profile_matches = {}

        options = uc.ChromeOptions()
        options.add_argument("--mute-audio")
        options.add_argument("--disable-gpu")
        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1280,720")

        driver = None
        limit = 500
        base_api_url = (
            "https://chaturbate.com/api/public/affiliates/onlinerooms/"
            "?wm=GXDW2&client_ip=request_ip&gender=f&gender=c&format=json&limit=500"
        )

        max_passes = 5 if robust else 1
        min_passes = 2 if robust else 1
        adaptive = robust and self._passes_mode in (
            "adaptive",
            "adaptive_canary",
        )
        previous_pass = None
        stable_pairs = 0

        try:
            driver = _uc_chrome(options=options)
            self._set_active_driver(driver)
            for pass_num in range(max_passes):
                if self._should_stop():
                    self.log("[INFO] Stop requested. Ending API fetch.")
                    break
                offset = 0
                if max_passes > 1:
                    self.log(f"[INFO] Pass {pass_num + 1}/{max_passes}...")
                current_pass = set()
                pass_failed = False

                while True:
                    if self._should_stop():
                        self.log("[INFO] Stop requested. Ending API fetch.")
                        break
                    cache_bust = random.randint(1000, 99999)
                    current_url = (
                        f"{base_api_url}&offset={offset}&_nocache={cache_bust}"
                    )
                    self.log(f"[INFO] Fetching batch offset {offset}...")
                    driver.get(current_url)
                    time.sleep(0.5)

                    try:
                        json_text = driver.find_element(By.TAG_NAME, "body").text
                        data = json.loads(json_text)
                        results = data.get("results", [])

                        if not results:
                            break

                        for room in results:
                            username = room.get("username")
                            if username:
                                link = f"https://chaturbate.com/{username}/"
                                online_set.add(link)
                                current_pass.add(link)
                                if save_metadata:
                                    metadata_map[username] = room.get(
                                        "seconds_online", 0
                                    )
                                if save_profile_matches or save_vpn_profile_matches:
                                    profile_match = build_location_profile_match(
                                        room,
                                        link,
                                        policy=self.location_policy,
                                    )
                                    if profile_match:
                                        profile_matches[link] = profile_match

                        if len(results) < limit:
                            break
                        offset += limit
                    except Exception as exc:
                        self.log(f"[ERROR] API parsing error: {exc}")
                        pass_failed = True
                        break

                if self._should_stop():
                    break

                if not adaptive:
                    continue
                if pass_failed or not current_pass:
                    previous_pass = None
                    stable_pairs = 0
                    continue
                if previous_pass is not None:
                    overlap, delta = snapshot_pass_metrics(
                        previous_pass, current_pass
                    )
                    self.log(
                        f"[INFO] Pass {pass_num + 1} overlap {overlap:.2%} "
                        f"(needs {SNAPSHOT_JACCARD_THRESHOLD:.2%}), count "
                        f"change {delta:.2%} (allows "
                        f"{SNAPSHOT_COUNT_DELTA_THRESHOLD:.2%})."
                    )
                if previous_pass is not None and passes_are_stable(
                    previous_pass, current_pass
                ):
                    stable_pairs += 1
                else:
                    stable_pairs = 0
                previous_pass = current_pass
                if pass_num + 1 >= min_passes and stable_pairs >= 1:
                    self.log(
                        f"[INFO] Snapshot settled after {pass_num + 1} "
                        f"passes; skipping the remaining "
                        f"{max_passes - pass_num - 1}."
                    )
                    break

            self.log(
                f"[INFO] {mode_name} fetch complete. Unique models: {len(online_set)}"
            )
            if save_metadata and metadata_map:
                with open(self.paths.metadata, "w", encoding="utf-8") as handle:
                    json.dump(metadata_map, handle)
                self.log(
                    f"[INFO] Metadata snapshot saved ({len(metadata_map)} records)."
                )
        except Exception as exc:
            self.log(f"[ERROR] Fetch failed: {exc}")
        finally:
            self._set_active_driver(None)
            safe_quit(driver)

        if save_profile_matches:
            self._write_profile_matches(
                profile_matches,
                self.paths.local_profile_matches,
                "Local",
            )
        if save_vpn_profile_matches:
            self._write_profile_matches(
                profile_matches,
                self.paths.vpn_profile_matches,
                "VPN",
            )

        return online_set

    def fetch_models_via_api(
        self,
        mode_name,
        robust=False,
        save_metadata=False,
        save_profile_matches=False,
        save_vpn_profile_matches=False,
        headless=True,
    ):
        self._ensure_session()
        self._last_snapshot_metrics = None
        if self._http_mode == "browser":
            return self._fetch_models_via_browser_api(
                mode_name,
                robust=robust,
                save_metadata=save_metadata,
                save_profile_matches=save_profile_matches,
                save_vpn_profile_matches=save_vpn_profile_matches,
                headless=headless,
            )

        class RunnerStopToken:
            def __init__(self, runner):
                self.runner = runner

            def is_cancelled(self):
                return self.runner._should_stop()

        client = ChaturbateApiClient(
            logger=self.log,
            stop_token=RunnerStopToken(self),
        )
        result = client.collect_adaptive(
            min_passes=2,
            max_passes=5 if robust else 2,
        )
        self._last_snapshot_metrics = {
            "transport": "http",
            "status": result.status.value,
            "accepted_count": len(result.models),
            "quarantined_count": len(result.quarantined),
            "passes": [
                {
                    "index": item.pass_index,
                    "status": item.status.value,
                    "page_count": item.page_count,
                    "model_count": len(item.models),
                    "reported_counts": list(item.reported_counts),
                    "error_code": item.error_code,
                }
                for item in result.passes
            ],
        }
        self.log(
            f"[INFO] HTTP snapshot {result.status.value}: "
            f"{len(result.models)} accepted, "
            f"{len(result.quarantined)} quarantined."
        )
        if self._http_mode == "http_canary":
            browser = self._fetch_models_via_browser_api(
                mode_name,
                robust=robust,
                save_metadata=save_metadata,
                save_profile_matches=save_profile_matches,
                save_vpn_profile_matches=save_vpn_profile_matches,
                headless=headless,
            )
            http_urls = {item.url for item in result.models}
            overlap = (
                len(http_urls & browser) / len(http_urls | browser)
                if http_urls or browser
                else 0.0
            )
            self.log(
                f"[CANARY] HTTP/browser overlap={overlap:.4f}; "
                f"http={len(http_urls)} browser={len(browser)}."
            )
            return browser
        if result.status is not OutcomeStatus.SUCCEEDED:
            self.log(
                "[WARN] Direct HTTP snapshot was not complete; restarting "
                "the browser fallback from offset zero."
            )
            return self._fetch_models_via_browser_api(
                mode_name,
                robust=robust,
                save_metadata=save_metadata,
                save_profile_matches=save_profile_matches,
                save_vpn_profile_matches=save_vpn_profile_matches,
                headless=headless,
            )

        urls = {item.url for item in result.models}
        metadata = {
            item.username: item.seconds_online for item in result.models
        }
        matches = {}
        for item in result.models:
            match = build_location_profile_match(
                {
                    "username": item.username,
                    "country": item.country,
                    "location": item.location,
                    "spoken_languages": item.languages,
                },
                item.url,
                policy=self.location_policy,
            )
            if match:
                matches[item.url] = match
        if save_metadata:
            with open(self.paths.metadata, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle)
        if save_profile_matches:
            self._write_profile_matches(
                matches, self.paths.local_profile_matches, "Local"
            )
        if save_vpn_profile_matches:
            self._write_profile_matches(
                matches, self.paths.vpn_profile_matches, "VPN"
            )
        return urls

    def _normalize_url(self, url):
        """Ensure URL has a trailing slash for consistency."""
        url = url.strip()
        if not url:
            return ""
        if not url.endswith("/"):
            return url + "/"
        return url

    def get_master_list_path(self):
        """Return the path to the master blocked list file (Legacy TXT)."""
        return os.path.join(self.base_dir, "MASTER_BLOCKED.txt")

    def get_master_json_path(self):
        """Return the path to the master blocked list data file (JSON)."""
        return os.path.join(self.base_dir, "MASTER_BLOCKED_DATA.json")

    def get_global_blacklist_path(self):
        """Return the path to the global blacklist file."""
        return os.path.join(self.base_dir, "GLOBAL_BLACKLIST.txt")

    def get_manual_list_path(self):
        """Return the path to the manual additions file."""
        return os.path.join(self.base_dir, "MANUAL_MODELS.txt")

    def get_blacklisted_models(self):
        """Return set of globally blacklisted models."""
        path = self.get_global_blacklist_path()
        if not os.path.exists(path):
            return set()
        models = set()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    norm = self._normalize_url(line)
                    if norm:
                        models.add(norm)
            return models
        except Exception:
            return set()

    def compile_master_list(self):
        """
        Scan all session folders for FINAL_BLOCKED.txt.
        Extract dates from folder names (e.g. 'session 1 04.01.2026').
        Compile into MASTER_BLOCKED.txt (names only) and MASTER_BLOCKED_DATA.json (names + dates).
        """
        self.log("[INFO] Compiling master blocked list...")

        # Load manual additions
        manual_models = set()
        manual_path = self.get_manual_list_path()
        if os.path.exists(manual_path):
            try:
                with open(manual_path, "r", encoding="utf-8") as f:
                    for line in f:
                        norm = self._normalize_url(line)
                        if norm:
                            manual_models.add(norm)
            except Exception:
                pass

        # map: model_url -> {"date": date_str, "session": session_num}
        model_info = {}

        blacklist = self.get_blacklisted_models()
        self.log(f"[INFO] Loaded {len(blacklist)} blacklisted models.")

        # Load existing JSON to preserve metadata like country
        existing_info = {}
        json_path = self.get_master_json_path()
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        name = item.get("name")
                        if name:
                            existing_info[self._normalize_url(name)] = item
            except Exception:
                pass

        session_dirs = self._list_session_dirs()
        compiled_at = datetime.datetime.now().isoformat(timespec="seconds")

        for session_dir in session_dirs:
            folder_name = os.path.basename(os.path.normpath(session_dir))
            parts = folder_name.split()
            date_str = "Unknown"
            session_num = 0
            if parts and len(parts) >= 2:
                session_num = _safe_int(parts[1], 0)

            if parts:
                raw_date = parts[-1]  # Try last part
                try:
                    dt = datetime.datetime.strptime(raw_date, "%d.%m.%Y")
                    date_str = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass

            blocked_path = os.path.join(session_dir, "FINAL_BLOCKED.txt")
            if os.path.exists(blocked_path):
                try:
                    file_dt = datetime.datetime.fromtimestamp(
                        os.path.getmtime(blocked_path)
                    )
                    timestamp_date = (
                        date_str
                        if date_str != "Unknown"
                        else file_dt.strftime("%Y-%m-%d")
                    )
                    source_timestamp = (
                        f"{timestamp_date}T{file_dt.strftime('%H:%M:%S')}"
                    )
                    with open(blocked_path, "r", encoding="utf-8") as handle:
                        for line in handle:
                            norm = self._normalize_url(line)
                            if norm and norm not in blacklist:
                                if norm not in model_info:
                                    model_info[norm] = {
                                        "date": date_str,
                                        "session": session_num,
                                        "timestamp": source_timestamp,
                                    }
                                else:
                                    # Keep the earliest discovery
                                    # Compare date first, then session
                                    curr = model_info[norm]
                                    is_earlier = False
                                    if date_str != "Unknown":
                                        if curr["date"] == "Unknown":
                                            is_earlier = True
                                        elif date_str < curr["date"]:
                                            is_earlier = True
                                        elif (
                                            date_str == curr["date"]
                                            and session_num < curr["session"]
                                        ):
                                            is_earlier = True

                                    if is_earlier:
                                        model_info[norm] = {
                                            "date": date_str,
                                            "session": session_num,
                                            "timestamp": source_timestamp,
                                        }

                except Exception as exc:
                    self.log(f"[WARN] Failed to read {blocked_path}: {exc}")

        # Configured location profile matches are intentionally separate from
        # geo-blocked verification. They are visible locally and therefore
        # must not be fed through the blocked-candidate verifier.
        policy = self._configured_location_policy()
        profile_session_dirs = session_dirs if self.supports_location_profiles else ()
        for session_dir in profile_session_dirs:
            profile_path = os.path.join(
                session_dir, "cb_local_profile_matches.json"
            )
            if not os.path.exists(profile_path):
                continue

            folder_name = os.path.basename(os.path.normpath(session_dir))
            parts = folder_name.split()
            date_str = "Unknown"
            session_num = _safe_int(parts[1], 0) if len(parts) >= 2 else 0
            if parts:
                try:
                    date_str = datetime.datetime.strptime(
                        parts[-1], "%d.%m.%Y"
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    pass

            try:
                source_policy = read_session_location_policy(session_dir)
                if source_policy is None or source_policy.fingerprint != policy.fingerprint:
                    continue
                file_dt = datetime.datetime.fromtimestamp(
                    os.path.getmtime(profile_path)
                )
                timestamp_date = (
                    date_str
                    if date_str != "Unknown"
                    else file_dt.strftime("%Y-%m-%d")
                )
                source_timestamp = (
                    f"{timestamp_date}T{file_dt.strftime('%H:%M:%S')}"
                )
                with open(profile_path, "r", encoding="utf-8") as handle:
                    records = json.load(handle)
                if not isinstance(records, list):
                    raise ValueError("expected a JSON list")

                for record in records:
                    if not isinstance(record, dict):
                        continue
                    norm = self._normalize_url(record.get("name"))
                    if not norm or norm in blacklist:
                        continue

                    if record.get("location_policy_fingerprint") != policy.fingerprint:
                        continue
                    checked = build_location_profile_match(
                        {"country": record.get("country"), "location": record.get("location"),
                         "spoken_languages": record.get("languages")},
                        norm, policy=policy,
                    )
                    if not checked:
                        continue
                    reasons = checked["match_reasons"]
                    country = checked["country"]
                    evidence = {
                        "country": country,
                        "location": str(record.get("location") or "")[:160],
                        "languages": str(record.get("languages") or "")[:160],
                        "match_reasons": reasons,
                        "matched_location_terms": checked["matched_location_terms"],
                        "location_policy_fingerprint": policy.fingerprint,
                        "session": session_num,
                        "detected_at": str(
                            record.get("detected_at") or source_timestamp
                        )[:32],
                    }

                    if norm not in model_info:
                        model_info[norm] = {
                            "date": date_str,
                            "session": session_num,
                            "timestamp": source_timestamp,
                        }
                    info = model_info[norm]
                    current_evidence = info.get("profile_match") or {}
                    if evidence["detected_at"] >= current_evidence.get(
                        "detected_at", ""
                    ):
                        info["profile_match"] = evidence
                    info["location_profile_match"] = True
            except Exception as exc:
                self.log(f"[WARN] Failed to read {profile_path}: {exc}")

        # A VPN-side location profile match is promoted only when Step 4
        # confirmed that same model as blocked in the same session.
        for session_dir in profile_session_dirs:
            profile_path = os.path.join(
                session_dir, "cb_vpn_profile_matches.json"
            )
            blocked_path = os.path.join(session_dir, "FINAL_BLOCKED.txt")
            if not os.path.exists(profile_path) or not os.path.exists(blocked_path):
                continue

            folder_name = os.path.basename(os.path.normpath(session_dir))
            parts = folder_name.split()
            date_str = "Unknown"
            session_num = _safe_int(parts[1], 0) if len(parts) >= 2 else 0
            if parts:
                try:
                    date_str = datetime.datetime.strptime(
                        parts[-1], "%d.%m.%Y"
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    pass

            try:
                source_policy = read_session_location_policy(session_dir)
                if source_policy is None or source_policy.fingerprint != policy.fingerprint:
                    continue
                with open(blocked_path, "r", encoding="utf-8") as handle:
                    confirmed_blocked = {
                        self._normalize_url(line)
                        for line in handle
                        if line.strip()
                    }
                file_dt = datetime.datetime.fromtimestamp(
                    os.path.getmtime(profile_path)
                )
                timestamp_date = (
                    date_str
                    if date_str != "Unknown"
                    else file_dt.strftime("%Y-%m-%d")
                )
                source_timestamp = (
                    f"{timestamp_date}T{file_dt.strftime('%H:%M:%S')}"
                )
                with open(profile_path, "r", encoding="utf-8") as handle:
                    records = json.load(handle)
                if not isinstance(records, list):
                    raise ValueError("expected a JSON list")

                for record in records:
                    if not isinstance(record, dict):
                        continue
                    norm = self._normalize_url(record.get("name"))
                    if (
                        not norm
                        or norm in blacklist
                        or norm not in confirmed_blocked
                    ):
                        continue

                    if record.get("location_policy_fingerprint") != policy.fingerprint:
                        continue
                    checked = build_location_profile_match(
                        {"country": record.get("country"), "location": record.get("location"),
                         "spoken_languages": record.get("languages")},
                        norm, policy=policy,
                    )
                    if not checked:
                        continue
                    reasons = checked["match_reasons"]
                    country = checked["country"]
                    evidence = {
                        "country": country,
                        "location": str(record.get("location") or "")[:160],
                        "languages": str(record.get("languages") or "")[:160],
                        "match_reasons": reasons,
                        "matched_location_terms": checked["matched_location_terms"],
                        "location_policy_fingerprint": policy.fingerprint,
                        "session": session_num,
                        "detected_at": str(
                            record.get("detected_at") or source_timestamp
                        )[:32],
                    }

                    if norm not in model_info:
                        model_info[norm] = {
                            "date": date_str,
                            "session": session_num,
                            "timestamp": source_timestamp,
                        }
                    info = model_info[norm]
                    current_evidence = info.get("vpn_profile_match") or {}
                    if evidence["detected_at"] >= current_evidence.get(
                        "detected_at", ""
                    ):
                        info["vpn_profile_match"] = evidence
                    info["vpn_location_profile_match"] = True
            except Exception as exc:
                self.log(f"[WARN] Failed to read {profile_path}: {exc}")

        # Preserve an exact insertion timestamp when one already exists. New
        # entries use this compilation time; legacy date-only entries retain
        # the time of the FINAL_BLOCKED file from their original session.
        for model_url, info in model_info.items():
            previous = existing_info.get(model_url)
            if previous and previous.get("timestamp"):
                info["timestamp"] = previous["timestamp"]
            elif previous is None:
                info["timestamp"] = compiled_at

        # Ensure manual models are included
        # Track today's info for new manual models
        today_dt = datetime.datetime.now()
        today_date = today_dt.strftime("%Y-%m-%d")
        today_iso = today_dt.isoformat()

        for m in manual_models:
            if m not in blacklist:
                if m not in model_info:
                    # Check if we have existing info for this manual model
                    if m in existing_info:
                        info = existing_info[m]
                        d = info.get("date", "Manual")
                        s = info.get("session", 0)
                        ts = info.get("timestamp")

                        # If it was still "Manual", upgrade it to today
                        if d == "Manual":
                            d = today_date
                            ts = today_iso

                        model_info[m] = {"date": d, "session": s, "timestamp": ts}
                    else:
                        model_info[m] = {
                            "date": today_date,
                            "session": 0,
                            "timestamp": today_iso,
                        }

        sorted_models = sorted(model_info.keys())
        # Build the canonical JSON in memory. MasterRepository publishes JSON,
        # its TXT compatibility view, backup, and revision metadata atomically.
        master_json = self.get_master_json_path()
        json_data = []
        preserved_optional_fields = {
            "last_verdict",
            "last_verified_at",
            "accessible_confirmations",
            "accessible_run_ids",
            "verification_reason",
            "verification_history",
            "pending_verification",
        }
        for m in sorted_models:
            info = model_info[m]
            entry = {
                "name": m,
                "date": info["date"],
                "session": info["session"],
                "manual": (m in manual_models),
            }
            # Include hidden timestamp if present
            if "timestamp" in info and info["timestamp"]:
                entry["timestamp"] = info["timestamp"]

            # Preserve country if it exists
            existing_country = existing_info.get(m, {}).get("country")
            if existing_country:
                entry["country"] = existing_country
            elif info.get("profile_match", {}).get("country"):
                entry["country"] = info["profile_match"]["country"]
            elif info.get("vpn_profile_match", {}).get("country"):
                entry["country"] = info["vpn_profile_match"]["country"]

            if info.get("location_profile_match"):
                entry["location_profile_match"] = True
                entry["profile_match"] = info["profile_match"]
            if info.get("vpn_location_profile_match"):
                entry["vpn_location_profile_match"] = True
                entry["vpn_profile_match"] = info["vpn_profile_match"]
            for field in preserved_optional_fields:
                if field in existing_info.get(m, {}):
                    entry[field] = existing_info[m][field]

            json_data.append(entry)

        try:
            MasterRepository(self.base_dir).publish(json_data)
            self.log(f"[INFO] Master list JSON saved: {master_json}")
        except Exception as exc:
            self.log(f"[ERROR] Failed to publish canonical master: {exc}")
            raise

        return len(sorted_models)

    def get_master_list_models(self):
        """
        Return list of models in the master blocked list.
        Returns list of dicts: [{'name': 'url', 'date': 'YYYY-MM-DD'}, ...]
        """
        json_path = self.get_master_json_path()
        if os.path.exists(json_path):
            # The file is UTF-8; without saying so Windows reads it as cp1252
            # and any accented or emoji character in a profile match makes the
            # whole list fall back to the bare text file, losing countries,
            # profile matches and timestamps.
            try:
                with open(json_path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, list):
                    return self._current_profile_badges(data)
                self.log(
                    "[WARN] Master metadata is not a list; falling back to "
                    "the text list. Countries and profile matches will be "
                    "missing."
                )
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                # Never swallow this silently: the fallback looks like a
                # working list while quietly dropping most of the data.
                self.log(
                    f"[ERROR] Could not read master metadata ({exc}). "
                    "Falling back to the text list; countries, profile "
                    "matches and dates will be missing."
                )

        # Fallback to text file
        master_path = self.get_master_list_path()
        if not os.path.exists(master_path):
            return []
        try:
            with open(master_path, "r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
                return [
                    {"name": x, "date": "Unknown", "session": 0} for x in sorted(lines)
                ]
        except (OSError, UnicodeDecodeError) as exc:
            self.log(f"[ERROR] Could not read the master text list: {exc}")
            return []

    def _current_profile_badges(self, rows):
        """Present current matches without rewriting historical source evidence."""
        try:
            policy = self._configured_location_policy()
            fingerprint = policy.fingerprint if policy.enabled else None
        except (OSError, ValueError) as exc:
            self.log(
                f"[WARN] Could not validate current location preferences ({exc}). "
                "Location match badges are hidden until the preferences are valid."
            )
            fingerprint = None
        visible = []
        for item in rows:
            if not isinstance(item, dict):
                visible.append(item)
                continue
            entry = dict(item)
            for flag, evidence_key in (
                ("location_profile_match", "profile_match"),
                ("vpn_location_profile_match", "vpn_profile_match"),
            ):
                evidence = entry.get(evidence_key)
                current = (
                    fingerprint is not None
                    and isinstance(evidence, dict)
                    and evidence.get("location_policy_fingerprint") == fingerprint
                )
                if not current:
                    entry.pop(flag, None)
            visible.append(entry)
        return visible

    def remove_from_master_list(self, model_url):
        """Remove one URL through the canonical atomic repository."""
        target = self._normalize_url(model_url)
        if not target:
            return False
        repository = MasterRepository(self.base_dir)
        try:
            data = repository.load(required=True)
            updated = [
                item
                for item in data
                if self._normalize_url(item.get("name", "")) != target
            ]
            if len(updated) == len(data):
                return False
            manual = repository.read_url_set(repository.manual_path)
            manual.discard(target)
            repository.rewrite_url_set(repository.manual_path, manual)
            repository.publish(updated)
            self.log(f"[INFO] Removed {target} from the canonical master.")
            return True
        except Exception as exc:
            self.log(f"[ERROR] Atomic master removal failed: {exc}")
            return False

    def block_model(self, model_url):
        """
        Add the given model URL to GLOBAL_BLACKLIST.txt so it doesn't appear in future compilations.
        """
        blacklist_path = self.get_global_blacklist_path()
        target = self._normalize_url(model_url)
        if not target:
            return

        try:
            repository = MasterRepository(self.base_dir)
            current_blacklist = repository.read_url_set(
                repository.blacklist_path
            )
            if target in current_blacklist:
                return

            self.log(f"[INFO] Blocking {target} globally.")
            current_blacklist.add(target)
            repository.rewrite_url_set(
                repository.blacklist_path, current_blacklist
            )

            # Also ensure it's removed from Master List if present
            self.remove_from_master_list(target)

        except Exception as exc:
            self.log(f"[ERROR] Failed to block model: {exc}")

    def add_manual_to_master(self, username):
        """Construct CTB URL and add to manual list."""
        if not username:
            return
        username = username.strip().lower()
        url = f"https://chaturbate.com/{username}/"
        norm = self._normalize_url(url)
        manual_path = self.get_manual_list_path()

        try:
            repository = MasterRepository(self.base_dir)
            existing = repository.read_url_set(manual_path)

            if norm not in existing:
                existing.add(norm)
                repository.rewrite_url_set(manual_path, existing)
                self.log(f"[INFO] Manually added {norm} to CTB manual list.")

            self.compile_master_list()
        except Exception as exc:
            self.log(f"[ERROR] Failed to add manual model: {exc}")

    def update_master_model_country(self, model_url, country):
        """Update the country for a given model in the master list JSON."""
        target = self._normalize_url(model_url)
        if not target:
            return False

        repository = MasterRepository(self.base_dir)
        try:
            data = repository.load(required=True)

            updated = False
            for item in data:
                if self._normalize_url(item.get("name", "")) == target:
                    item["country"] = country
                    updated = True
                    break

            if updated:
                repository.publish(data)
                return True
        except Exception as exc:
            self.log(f"[ERROR] Failed to update country for {target}: {exc}")

        return False

    def step_vpn_list(self, headless=True, mode="manual"):
        self.log("[INFO] Step 1: VPN list (VPN ON).")
        self._ensure_session()
        store = ChaturbateRunStore(self.session_folder)
        context = store.create_run(mode=mode)
        published_record = None
        models = self.fetch_models_via_api(
            "VPN List",
            robust=True,
            save_metadata=True,
            save_vpn_profile_matches=True,
            headless=headless,
        )
        if self._should_stop():
            outcome = StepOutcome.cancelled(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.VPN_SNAPSHOT,
                input_count=len(models),
                remaining_count=len(models),
                error_code="cancelled_by_user",
            )
        elif not models:
            self.log("[WARN] VPN list empty. Nothing saved.")
            outcome = StepOutcome.incomplete(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.VPN_SNAPSHOT,
                error_code="empty_snapshot",
                error_message="Step 1 did not produce a complete snapshot.",
            )
        else:
            record = store.artifact_lines(
                context,
                StepName.VPN_SNAPSHOT,
                "cb_vpn_list.txt",
                sorted(models),
                "vpn_snapshot",
            )
            artifacts = [record]
            if self._last_snapshot_metrics:
                artifacts.append(
                    store.artifact_json(
                        context,
                        StepName.VPN_SNAPSHOT,
                        "snapshot_metrics.json",
                        self._last_snapshot_metrics,
                        "vpn_snapshot_metrics",
                    )
                )
            outcome = StepOutcome.succeeded(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.VPN_SNAPSHOT,
                output_count=len(models),
                artifacts=tuple(artifacts),
            )
            published_record = record
        store.record_step(context, outcome)
        if published_record:
            store.publish_lines(
                context, published_record, "cb_vpn_list.txt"
            )
            self.log(f"[INFO] VPN list saved: {self.paths.vpn_list}")
        return outcome

    def step_local_list(self, headless=True):
        self.log("[INFO] Step 2: Local list (VPN OFF).")
        self._ensure_session()
        store = ChaturbateRunStore(self.session_folder)
        context = store.load_active()
        if context is None:
            return StepOutcome.failed(
                run_id="",
                generation_id="",
                platform="Chaturbate",
                step=StepName.LOCAL_SNAPSHOT,
                error_code="unmet_step1_prerequisite",
            )
        manifest = store.load_manifest(context)
        published_record = None
        if (
            manifest.get("steps", {})
            .get(StepName.VPN_SNAPSHOT.value, {})
            .get("status")
            != "succeeded"
        ):
            outcome = StepOutcome.failed(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.LOCAL_SNAPSHOT,
                error_code="unmet_step1_prerequisite",
            )
            store.record_step(context, outcome)
            return outcome
        models = self.fetch_models_via_api(
            "Local List",
            robust=True,
            save_metadata=False,
            save_profile_matches=True,
            headless=headless,
        )
        if self._should_stop():
            outcome = StepOutcome.cancelled(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.LOCAL_SNAPSHOT,
                input_count=len(models),
                remaining_count=len(models),
                error_code="cancelled_by_user",
            )
        elif not models:
            self.log("[WARN] Local list empty. Nothing saved.")
            outcome = StepOutcome.incomplete(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.LOCAL_SNAPSHOT,
                error_code="empty_snapshot",
                error_message="Step 2 did not produce a complete snapshot.",
            )
        else:
            record = store.artifact_lines(
                context,
                StepName.LOCAL_SNAPSHOT,
                "cb_local_list.txt",
                sorted(models),
                "local_snapshot",
            )
            artifacts = [record]
            if self._last_snapshot_metrics:
                artifacts.append(
                    store.artifact_json(
                        context,
                        StepName.LOCAL_SNAPSHOT,
                        "snapshot_metrics.json",
                        self._last_snapshot_metrics,
                        "local_snapshot_metrics",
                    )
                )
            outcome = StepOutcome.succeeded(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.LOCAL_SNAPSHOT,
                output_count=len(models),
                artifacts=tuple(artifacts),
            )
            published_record = record
        store.record_step(context, outcome)
        if published_record:
            store.publish_lines(
                context, published_record, "cb_local_list.txt"
            )
            self.log(f"[INFO] Local list saved: {self.paths.local_list}")
        return outcome

    def _verify_master_list_legacy(self, start_minimized=True):
        self._ensure_session()
        if self._should_stop():
            self.log("[INFO] Stop requested before verification started.")
            return
        master_path = self.get_master_list_path()
        if not os.path.exists(master_path):
            self.log("[WARN] Master list not found.")
            return

        self.log("[INFO] Loading master list for verification...")

        # Load JSON to check for manual flag
        manual_urls = set()
        profile_match_urls = set()
        master_json = self.get_master_json_path()
        if os.path.exists(master_json):
            try:
                with open(master_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        if item.get("manual"):
                            manual_urls.add(item.get("name"))
                        if item.get("location_profile_match"):
                            profile_match_urls.add(item.get("name"))
            except Exception:
                pass

        with open(master_path, "r", encoding="utf-8") as handle:
            raw_urls = [line.strip() for line in handle if line.strip()]

        protected_urls = manual_urls | profile_match_urls
        urls = [u for u in raw_urls if u not in protected_urls]
        skipped_manual_count = len([u for u in raw_urls if u in manual_urls])
        skipped_profile_count = len(
            [u for u in raw_urls if u in profile_match_urls]
        )

        if skipped_manual_count > 0:
            self.log(
                f"[INFO] Skipping {skipped_manual_count} manually added models from verification."
            )
        if skipped_profile_count > 0:
            self.log(
                "[INFO] Skipping "
                f"{skipped_profile_count} location profile matches from verification."
            )

        if not urls:
            self.log("[WARN] No geo-blocked Master List models require verification.")
            return

        self.log(f"[INFO] Master List Verification ({len(urls)} models).")
        self.log(
            "[INFO] VPN should be OFF. Removing False Positives (Accessible Models)."
        )

        prefs = {
            "profile.managed_default_content_settings.images": 1,
            "profile.default_content_setting_values.popups": 1,
            "profile.managed_default_content_settings.popups": 1,
            "profile.content_settings.exceptions.popups.*.setting": 1,
        }

        profile_dir = os.path.join(self.session_folder, "uc_profile_master_verify")
        os.makedirs(profile_dir, exist_ok=True)

        options = uc.ChromeOptions()
        options.add_argument("--mute-audio")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--window-size=1200,900")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-extensions")

        # User requested to SEE the page for this temporary step, so we force visibility
        # and remove hiding logic.
        # if start_minimized:
        #     options.add_argument("--start-minimized") ...

        options.add_experimental_option("prefs", prefs)
        options.page_load_strategy = "normal"

        # Models to KEEP (Confirmed Blocked)
        kept_models = []
        # Models to REMOVE (Accessible / False Positive)
        removed_models = []

        all_batches = []
        if urls:
            all_batches.append([urls[0]])
            remaining = urls[1:]
            batch_size = 5
            for idx in range(0, len(remaining), batch_size):
                all_batches.append(remaining[idx : idx + batch_size])

        driver = None

        try:
            driver = _uc_chrome(options=options, user_data_dir=profile_dir)
            self._set_active_driver(driver)
            time.sleep(1.0)

            # No hider used

            for batch_idx, batch in enumerate(all_batches):
                if self._should_stop():
                    self.log("[INFO] Stop requested. Ending verification early.")
                    break
                is_warmup = batch_idx == 0
                self.log(
                    f"[INFO] Batch {batch_idx + 1}/{len(all_batches)} ({len(batch)} models)..."
                )

                try:
                    driver.get(batch[0])
                    for url in batch[1:]:
                        driver.execute_script(f"window.open('{url}', '_blank');")

                    start_batch = time.time()
                    results = {handle: None for handle in driver.window_handles}
                    time.sleep(1.0)

                    while True:
                        if self._should_stop():
                            break
                        all_done = True
                        current_handles = driver.window_handles

                        # Captcha check (Logic simplified as we are watching it)
                        try:
                            check_title = driver.title.lower()
                            if (
                                "just a moment" in check_title
                                or "verify" in check_title
                                or "cloudflare" in check_title
                            ):
                                self.log("[WARN] CAPTCHA Detected! Please solve it.")
                                # Loop until solved
                                while (
                                    "just a moment" in driver.title.lower()
                                    or "verify" in driver.title.lower()
                                ):
                                    time.sleep(0.5)
                                self.log("[INFO] Captcha solved.")
                                start_batch = time.time()
                        except Exception:
                            pass

                        timeout = (
                            30.0 if is_warmup else 15.0
                        )  # Increased slightly to ensure loading
                        if time.time() - start_batch > timeout:
                            break

                        for handle in current_handles:
                            if handle not in results:
                                continue
                            if results[handle] is not None:
                                continue

                            try:
                                driver.switch_to.window(handle)
                                current_browser_url = driver.current_url.rstrip("/")
                                page_src = driver.page_source.lower()

                                is_blocked = False

                                # Exact Step 4 Logic
                                if current_browser_url == "https://chaturbate.com":
                                    is_blocked = True
                                elif (
                                    len(
                                        driver.find_elements(
                                            By.CSS_SELECTOR,
                                            "[data-testid='denied-notice']",
                                        )
                                    )
                                    > 0
                                ):
                                    is_blocked = True
                                elif "region or gender" in page_src:
                                    is_blocked = True

                                # Also check explicit 'accessible' signals to fast-track
                                # But primary decision is blocked vs not blocked.
                                is_accessible = False
                                if not is_blocked:
                                    if (
                                        len(
                                            driver.find_elements(
                                                By.ID, "room_view_container"
                                            )
                                        )
                                        > 0
                                    ):
                                        is_accessible = True

                                if is_blocked:
                                    results[handle] = "blocked"
                                elif is_accessible:
                                    results[handle] = "accessible"

                            except Exception:
                                pass

                            if results.get(handle) is None:
                                all_done = False

                        if all_done:
                            break
                        time.sleep(0.5)

                    if self._should_stop():
                        break

                    # Process results for this batch
                    for handle in driver.window_handles:
                        try:
                            driver.switch_to.window(handle)
                            u = driver.current_url
                            status = results.get(handle, "unknown")

                            norm_u = self._normalize_url(u)
                            found_original = None
                            for orig in batch:
                                if self._normalize_url(orig) == norm_u:
                                    found_original = orig
                                    break

                            if not found_original:
                                continue

                            if status == "blocked":
                                kept_models.append(found_original)
                            else:
                                # If accessible, or unknown (timeout/didn't see block), we REMOVE.
                                # User instruction: "if i will not see the access denied message that means that its a false positive"
                                removed_models.append(found_original)
                                if status == "accessible":
                                    self.log(f"[REMOVE] {found_original} (Accessible)")
                                else:
                                    self.log(
                                        f"[REMOVE] {found_original} (Not Blocked / Timeout)"
                                    )

                        except Exception:
                            pass

                    # Close all but one
                    try:
                        handles = driver.window_handles
                        for h in handles[1:]:
                            driver.switch_to.window(h)
                            driver.close()
                        driver.switch_to.window(handles[0])
                    except Exception:
                        pass

                except Exception as exc:
                    self.log(f"[ERROR] Batch failed: {exc}")

                if self._should_stop():
                    break

            # End of all batches
            # Rewrite Master List
            self.log("[INFO] Verification complete.")
            self.log(f"[INFO] Removed {len(removed_models)} models.")
            self.log(f"[INFO] Kept {len(kept_models)} confirmed blocked models.")

            # Blacklist removed models so they don't reappear
            if removed_models:
                self.log(f"[INFO] Blacklisting {len(removed_models)} removed models...")
                try:
                    with open(self.get_global_blacklist_path(), "a", encoding="utf-8") as f:
                        for item in removed_models:
                            f.write(item + "\n")
                except Exception as e:
                    self.log(f"[ERROR] Failed to update blacklist: {e}")

            # Save reduced TXT immediately (backup)
            with open(master_path, "w", encoding="utf-8") as handle:
                for item in sorted(kept_models):
                    handle.write(item + "\n")

            # Full Recompile to sync JSON and ensure consistency
            self.compile_master_list()

        except Exception as exc:
            self.log(f"[ERROR] Verification critical failure: {exc}")
        finally:
            self._set_active_driver(None)
            safe_quit(driver)
            shutil.rmtree(profile_dir, ignore_errors=True)

    def verify_master_list(
        self,
        start_minimized=True,
        verdict_provider=None,
        run_id=None,
        max_batch=200,
    ):
        """Verify master entries with positive-proof tri-state semantics."""
        self._ensure_session()
        run_id = run_id or str(uuid.uuid4())
        repository = MasterRepository(self.base_dir)
        consistency = repository.validate_consistency(repair_txt=True)
        if not consistency.valid:
            return StepOutcome.failed(
                run_id=run_id,
                generation_id=run_id,
                platform="Chaturbate",
                step=StepName.VERIFY,
                error_code="master_metadata_invalid",
                error_message=(
                    "Master metadata is invalid; verification was blocked."
                ),
            )
        try:
            entries = repository.load(required=True)
        except (OSError, ValueError) as exc:
            return StepOutcome.failed(
                run_id=run_id,
                generation_id=run_id,
                platform="Chaturbate",
                step=StepName.VERIFY,
                error_code="master_metadata_invalid",
                error_message=str(exc)[:200],
            )
        candidates = [
            item
            for item in entries
            if not item.get("manual")
            and not item.get("location_profile_match")
        ]
        candidates.sort(
            key=lambda item: (
                0 if item.get("pending_verification") else 1,
                item.get("last_verified_at") or "",
                item["name"],
            )
        )
        candidates = candidates[: max(1, int(max_batch))]
        if not candidates:
            return StepOutcome.succeeded(
                run_id=run_id,
                generation_id=run_id,
                platform="Chaturbate",
                step=StepName.VERIFY,
            )

        driver = None
        profile_dir = None
        profile_manager = None
        hider = None
        records = []
        try:
            provider = verdict_provider
            if provider is None:
                options = uc.ChromeOptions()
                options.add_argument("--mute-audio")
                options.add_argument("--disable-extensions")
                options.add_argument("--window-size=1200,900")
                # Headless Chrome is fingerprinted by Cloudflare and gets
                # challenged constantly, so run a real window off-screen.
                if start_minimized:
                    options.add_argument("--window-position=-32000,-32000")
                profile_dir = persistent_verify_profile_dir()
                driver = _uc_chrome(
                    options=options, user_data_dir=profile_dir
                )
                self._set_active_driver(driver)
                try:
                    driver.set_page_load_timeout(30)
                    driver.set_script_timeout(15)
                except Exception:
                    pass
                if start_minimized:
                    try:
                        chromedriver_pid = driver.service.process.pid
                    except Exception:
                        chromedriver_pid = None
                    hider = _ChromeWindowHider(
                        profile_dir, parent_pid=chromedriver_pid
                    )
                    hider.start()
                    for _ in range(10):
                        if hider.hide():
                            break
                        time.sleep(0.2)
                try:
                    driver.get("https://chaturbate.com/")
                    dismiss_age_gate(driver)
                except Exception:
                    pass

                def provider(url, attempt):
                    try:
                        driver.get(url)
                        self._await_captcha_clear(driver, hider, start_minimized)
                        dismiss_age_gate(driver)
                        current_url = driver.current_url
                        title = driver.title or ""
                        source = driver.page_source or ""
                        denied = bool(
                            driver.find_elements(
                                By.CSS_SELECTOR,
                                "[data-testid='denied-notice']",
                            )
                        )
                        room = bool(
                            driver.find_elements(
                                By.CSS_SELECTOR, ROOM_CONTAINER_SELECTOR
                            )
                        )
                        excerpt = _visible_block_excerpt(driver)
                        return classify_page(
                            PageObservation(
                                original_url=url,
                                current_url=current_url,
                                title=title,
                                has_denied_notice=denied,
                                has_room_container=room,
                                has_offline_notice=page_is_offline(excerpt),
                                page_text_excerpt=excerpt,
                            ),
                            attempt=attempt,
                            run_id=run_id,
                        )
                    except Exception as exc:
                        return classify_page(
                            PageObservation(
                                original_url=url,
                                current_url="",
                                navigation_error=(
                                    "browser_closed"
                                    if "session" in str(exc).lower()
                                    else "navigation_timeout"
                                ),
                            ),
                            attempt=attempt,
                            run_id=run_id,
                        )

            for item in candidates:
                if self._should_stop():
                    break
                try:
                    record = provider(item["name"], 1)
                except Exception:
                    record = classify_page(
                        PageObservation(
                            original_url=item["name"],
                            current_url="",
                            navigation_error="selector_error",
                        ),
                        run_id=run_id,
                    )
                if not record.run_id:
                    record = VerificationRecord.from_dict(
                        {**record.to_dict(), "run_id": run_id}
                    )
                records.append(record)
            result = repository.apply_verification_records(records)
            remaining = len(candidates) - len(records)
            if self._should_stop():
                return StepOutcome.cancelled(
                    run_id=run_id,
                    generation_id=run_id,
                    platform="Chaturbate",
                    step=StepName.VERIFY,
                    input_count=len(candidates),
                    processed_count=len(records),
                    remaining_count=remaining,
                    error_code="cancelled_by_user",
                )
            self.log(
                "[INFO] Master verification applied "
                f"{result['applied']} verdicts; removed {len(result['removed'])} "
                "after two positive confirmations."
            )
            return StepOutcome.succeeded(
                run_id=run_id,
                generation_id=run_id,
                platform="Chaturbate",
                step=StepName.VERIFY,
                input_count=len(candidates),
                output_count=len(result["removed"]),
                processed_count=len(records),
                remaining_count=remaining,
            )
        finally:
            if hider is not None:
                hider.stop()
            self._set_active_driver(None)
            safe_quit(driver)
            if profile_manager:
                profile_manager.cleanup()

    def _await_captcha_clear(
        self, driver, hider=None, rehide=True, timeout=600.0
    ):
        """Block until a Cloudflare interstitial clears.

        Reveals the hidden browser window so the challenge can actually be
        solved, then hides it again. Returns True if the page is clear.
        """
        deadline = time.monotonic() + float(timeout)
        revealed = False
        while True:
            if self._should_stop():
                return False
            try:
                title = driver.title or ""
                source = driver.page_source or ""
            except Exception:
                return False
            if not page_has_captcha(title, source):
                if revealed and rehide and hider is not None:
                    hider.hide()
                    self.log("[INFO] CAPTCHA solved. Browser hidden again.")
                return True
            if not revealed:
                self.log(
                    "[WAITING] CAPTCHA detected. Solve it within 10 minutes."
                )
                if hider is not None:
                    hider.show()
                else:
                    try:
                        driver.set_window_position(0, 0)
                        driver.maximize_window()
                    except Exception:
                        pass
                revealed = True
            if time.monotonic() >= deadline:
                if revealed and rehide and hider is not None:
                    hider.hide()
                return False
            time.sleep(0.5)

    def _compare_lists_legacy(self):
        self._ensure_session()
        if not os.path.exists(self.paths.vpn_list) or not os.path.exists(
            self.paths.local_list
        ):
            self.log("[WARN] Missing files. Run steps 1 and 2 first.")
            return

        self.log("[INFO] Loading lists...")
        with open(self.paths.vpn_list, "r", encoding="utf-8") as handle:
            vpn = set(line.strip() for line in handle if line.strip())
        with open(self.paths.local_list, "r", encoding="utf-8") as handle:
            local = set(line.strip() for line in handle if line.strip())

        raw_suspects = vpn - local

        self.log("[INFO] Preliminary analysis:")
        self.log(f"[INFO] VPN list: {len(vpn)}")
        self.log(f"[INFO] Local list: {len(local)}")
        self.log(f"[INFO] Missing locally: {len(raw_suspects)}")

        metadata = {}
        if os.path.exists(self.paths.metadata):
            self.log("[INFO] Loading VPN snapshot for stability check...")
            with open(self.paths.metadata, "r", encoding="utf-8") as handle:
                metadata = json.load(handle)
        else:
            self.log("[WARN] Metadata snapshot not found. Stability filter skipped.")

        final_candidates = []
        debug_log = []

        self.log("[INFO] Filtering false positives...")
        for link in raw_suspects:
            username = link.rstrip("/").split("/")[-1]
            if username in metadata:
                uptime = metadata[username]
                if uptime < 60:
                    debug_log.append(
                        f"[SKIPPED] {username} (online {uptime}s - unstable)"
                    )
                    continue
                final_candidates.append(link)
                debug_log.append(f"[CANDIDATE] {username} (online {uptime}s)")
            else:
                final_candidates.append(link)
                debug_log.append(f"[CANDIDATE] {username} (no metadata)")

        self.log(
            f"[INFO] Refined candidates: {len(raw_suspects)} -> {len(final_candidates)}"
        )

        with open(self.paths.candidates, "w", encoding="utf-8") as handle:
            for link in sorted(final_candidates):
                handle.write(link + "\n")

        with open(self.paths.debug, "w", encoding="utf-8") as handle:
            handle.write("\n".join(debug_log))

        self.log(f"[INFO] Candidates saved: {self.paths.candidates}")
        self.log(f"[INFO] Debug log saved: {self.paths.debug}")

    def compare_lists(self):
        """Compare only validated artifacts from the active generation."""
        self._ensure_session()
        store = ChaturbateRunStore(self.session_folder)
        try:
            context = store.load_active()
            if context is None:
                raise ValueError("no active manifest run")
            manifest = store.load_manifest(context)
            for step in (
                StepName.VPN_SNAPSHOT,
                StepName.LOCAL_SNAPSHOT,
            ):
                if (
                    manifest.get("steps", {})
                    .get(step.value, {})
                    .get("status")
                    != "succeeded"
                ):
                    raise ValueError(f"unmet prerequisite: {step.value}")
            records = {
                item.get("artifact_type"): item
                for item in manifest.get("artifacts", [])
            }
            vpn_record = records.get("vpn_snapshot")
            local_record = records.get("local_snapshot")
            if not vpn_record or not local_record:
                raise ValueError("snapshot artifact record missing")
            vpn_record = ArtifactRecord.from_dict(vpn_record)
            local_record = ArtifactRecord.from_dict(local_record)
            store.validate_artifact(context, vpn_record)
            store.validate_artifact(context, local_record)
            with open(
                os.path.join(self.session_folder, vpn_record.path),
                "r",
                encoding="utf-8",
            ) as handle:
                vpn = {line.strip() for line in handle if line.strip()}
            with open(
                os.path.join(self.session_folder, local_record.path),
                "r",
                encoding="utf-8",
            ) as handle:
                local = {line.strip() for line in handle if line.strip()}
        except (OSError, ValueError) as exc:
            run_id = context.run_id if "context" in locals() and context else ""
            generation_id = (
                context.generation_id
                if "context" in locals() and context
                else ""
            )
            outcome = StepOutcome.failed(
                run_id=run_id,
                generation_id=generation_id,
                platform="Chaturbate",
                step=StepName.COMPARE,
                error_code="generation_prerequisite_mismatch",
                error_message=str(exc)[:200],
            )
            if "context" in locals() and context:
                store.record_step(context, outcome)
            return outcome

        raw_suspects = vpn - local
        metadata = {}
        if os.path.exists(self.paths.metadata):
            try:
                with open(
                    self.paths.metadata, "r", encoding="utf-8"
                ) as handle:
                    metadata = json.load(handle)
            except (OSError, ValueError):
                metadata = {}
        candidates = []
        debug = []
        for link in sorted(raw_suspects):
            username = link.rstrip("/").split("/")[-1]
            uptime = metadata.get(username)
            if uptime is not None and _safe_int(uptime, 0) < 60:
                debug.append(
                    f"[SKIPPED] {username} (online {_safe_int(uptime)}s - unstable)"
                )
            else:
                candidates.append(link)
                debug.append(f"[CANDIDATE] {username}")
        candidate_record = store.artifact_lines(
            context,
            StepName.COMPARE,
            "cb_candidates.txt",
            candidates,
            "candidates",
        )
        debug_record = store.artifact_lines(
            context,
            StepName.COMPARE,
            "debug_log.txt",
            debug,
            "compare_diagnostics",
        )
        outcome = StepOutcome.succeeded(
            run_id=context.run_id,
            generation_id=context.generation_id,
            platform=context.platform,
            step=StepName.COMPARE,
            input_count=len(vpn),
            output_count=len(candidates),
            processed_count=len(raw_suspects),
            artifacts=(candidate_record, debug_record),
        )
        store.record_step(context, outcome)
        store.publish_lines(
            context, candidate_record, "cb_candidates.txt"
        )
        store.publish_lines(context, debug_record, "debug_log.txt")
        self.log(
            f"[INFO] Refined candidates: {len(raw_suspects)} -> {len(candidates)}"
        )
        return outcome

    def _finalize_candidate_verification(self, confirmed_blocks):
        confirmed_blocks = sorted(
            {
                self._normalize_url(link)
                for link in confirmed_blocks
                if self._normalize_url(link)
            }
        )
        with open(self.paths.verified, "w", encoding="utf-8") as handle:
            for link in confirmed_blocks:
                handle.write(link + "\n")
        self.log(f"[INFO] Final list saved: {self.paths.verified}")

        # Cleanup intermediate files, keeping only the final blocked list
        intermediate_files = [
            self.paths.vpn_list,
            self.paths.local_list,
            self.paths.candidates,
            self.paths.metadata,
            self.paths.debug,
        ]
        for file_path in intermediate_files:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as exc:
                    self.log(
                        f"[WARN] Failed to delete {os.path.basename(file_path)}: {exc}"
                    )
        self.log("[INFO] Intermediate session files cleaned up.")

        # Auto-update master blocked list
        self.compile_master_list()

    def _verify_candidates_legacy(
        self, start_minimized=True, reveal_on_captcha=True
    ):
        self._ensure_session()
        if self._should_stop():
            self.log("[INFO] Stop requested before verification started.")
            return
        if not os.path.exists(self.paths.candidates):
            self.log("[WARN] No candidate file found. Run step 3 first.")
            self.compile_master_list()
            return

        with open(self.paths.candidates, "r", encoding="utf-8") as handle:
            urls = [self._normalize_url(line) for line in handle if line.strip()]

        if not urls:
            self.log("[WARN] Candidate list is empty.")
            self.compile_master_list()
            return

        master_urls = {
            self._normalize_url(item.get("name", ""))
            for item in self.get_master_list_models()
            if item.get("name")
        }
        skipped_master_urls = [url for url in urls if url in master_urls]
        skipped_master = len(skipped_master_urls)
        if skipped_master:
            urls = [url for url in urls if url not in master_urls]
            self.log(
                f"[INFO] Skipping {skipped_master} candidates already in the master list."
            )
            self.log(
                "[INFO] Skipped master-list models will still be included in the "
                "session blocked list."
            )

        if not urls:
            self.log(
                "[INFO] All Step 4 candidates are already in the CTB master list. "
                "Skipping browser verification."
            )
            self._finalize_candidate_verification(skipped_master_urls)
            return

        self.log(f"[INFO] Verification phase ({len(urls)} candidates).")
        self.log("[INFO] VPN should be OFF for verification.")
        self.log("[INFO] Browser will appear if captcha is detected.")
        if start_minimized:
            self.log(
                "[INFO] Browser will hide after captcha and continue in background."
            )
        else:
            self.log("[INFO] Browser will stay visible during verification.")

        prefs = {
            "profile.managed_default_content_settings.images": 1,
            "profile.default_content_setting_values.popups": 1,
            "profile.managed_default_content_settings.popups": 1,
            "profile.content_settings.exceptions.popups.*.setting": 1,
        }

        profile_dir = os.path.join(self.session_folder, "uc_profile")
        os.makedirs(profile_dir, exist_ok=True)

        options = uc.ChromeOptions()
        options.add_argument("--mute-audio")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--window-size=1200,900")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-extensions")
        if start_minimized:
            options.add_argument("--start-minimized")
            options.add_argument("--window-position=-32000,-32000")
            options.add_argument("--window-size=1,1")
        options.add_experimental_option("prefs", prefs)
        options.page_load_strategy = "normal"

        confirmed_blocks = []
        all_batches = []

        all_batches.append([urls[0]])
        batch_size = 5
        remaining_urls = urls[1:]
        if remaining_urls:
            for idx in range(0, len(remaining_urls), batch_size):
                all_batches.append(remaining_urls[idx : idx + batch_size])

        driver = None
        chrome_pids = set()
        chromedriver_pid = None
        chrome_pids_before = _win_get_chrome_pids() if IS_WINDOWS else set()
        warned_hide_fail = False
        hidden_logged = False
        hider = None

        def is_captcha(title, page_src):
            title_text = (title or "").lower()
            if "just a moment" in title_text:
                return True
            if "verify" in title_text or "cloudflare" in title_text:
                return True

            if page_src:
                src = page_src.lower()
                if "cf-challenge" in src:
                    return True
                if "checking your browser" in src:
                    return True
                if "just a moment" in src:
                    return True
            return False

        def wait_for_captcha_clear():
            while True:
                try:
                    title_now = driver.title
                except Exception:
                    title_now = ""
                try:
                    page_src = driver.page_source
                except Exception:
                    page_src = ""
                if not is_captcha(title_now, page_src):
                    return True
                time.sleep(0.5)

        try:
            driver = _uc_chrome(options=options, user_data_dir=profile_dir)
            self._set_active_driver(driver)
            time.sleep(1.0)

            if IS_WINDOWS:
                try:
                    chromedriver_pid = driver.service.process.pid
                except Exception:
                    chromedriver_pid = None
                chrome_pids = _win_get_chrome_pids_for_profile(profile_dir)
                if not chrome_pids and chromedriver_pid:
                    chrome_pids = _win_get_chrome_pids_for_parent(chromedriver_pid)
                if not chrome_pids:
                    chrome_pids = _win_get_chrome_pids() - chrome_pids_before
                if not chrome_pids:
                    time.sleep(1.0)
                    chrome_pids = _win_get_chrome_pids_for_profile(profile_dir)
                    if not chrome_pids and chromedriver_pid:
                        chrome_pids = _win_get_chrome_pids_for_parent(chromedriver_pid)
                    if not chrome_pids:
                        chrome_pids = _win_get_chrome_pids() - chrome_pids_before
                if start_minimized:
                    if chrome_pids:
                        self.log(
                            "[INFO] Chrome window will be hidden from the taskbar."
                        )
                    else:
                        self.log(
                            "[WARN] Chrome process not found; fallback hide may show in taskbar."
                        )
                if start_minimized:
                    hider = _ChromeWindowHider(profile_dir, parent_pid=chromedriver_pid)
                    hider.start()

            def show_window():
                if hider and hider.show():
                    try:
                        driver.set_window_rect(x=120, y=80, width=1200, height=900)
                    except Exception:
                        pass
                    return
                if IS_WINDOWS:
                    if _win_show_windows_for_profile(profile_dir) > 0:
                        return
                    if chromedriver_pid:
                        pids = _win_get_chrome_pids_for_parent(chromedriver_pid)
                        if pids:
                            _win_show_windows_for_pids(pids)
                            return
                    pids = _win_get_chrome_pids_for_profile(profile_dir) or chrome_pids
                    if pids:
                        _win_show_windows_for_pids(pids)
                        return
                try:
                    driver.maximize_window()
                except Exception:
                    pass

            def hide_window():
                nonlocal warned_hide_fail
                nonlocal hidden_logged
                if not start_minimized:
                    return False
                if hider and hider.hide():
                    if not hidden_logged:
                        self.log("[INFO] Browser hidden. Continuing in background.")
                        hidden_logged = True
                    return True
                if IS_WINDOWS:
                    count = _win_hide_windows_for_profile(profile_dir)
                    if count > 0:
                        if not hidden_logged:
                            self.log("[INFO] Browser hidden. Continuing in background.")
                            hidden_logged = True
                        return True
                    if chromedriver_pid:
                        count = _win_hide_windows_for_pids(
                            _win_get_chrome_pids_for_parent(chromedriver_pid)
                        )
                        if count > 0:
                            if not hidden_logged:
                                self.log(
                                    "[INFO] Browser hidden. Continuing in background."
                                )
                                hidden_logged = True
                            return True
                    pids = _win_get_chrome_pids_for_profile(profile_dir) or chrome_pids
                    if pids:
                        count = _win_hide_windows_for_pids(pids)
                        if count > 0:
                            if not hidden_logged:
                                self.log(
                                    "[INFO] Browser hidden. Continuing in background."
                                )
                                hidden_logged = True
                            return True
                    if not warned_hide_fail:
                        self.log(
                            "[WARN] Could not hide Chrome window; it may remain visible."
                        )
                        warned_hide_fail = True
                    return False
                try:
                    driver.set_window_rect(x=-20000, y=0, width=1200, height=900)
                except Exception:
                    pass
                try:
                    driver.minimize_window()
                except Exception:
                    pass
                if not hidden_logged:
                    self.log("[INFO] Browser hidden. Continuing in background.")
                    hidden_logged = True
                return True

            if start_minimized:
                for _ in range(10):
                    if hide_window():
                        break
                    time.sleep(0.2)

            total_batches = len(all_batches)

            for batch_idx, batch in enumerate(all_batches):
                if self._should_stop():
                    self.log("[INFO] Stop requested. Ending verification early.")
                    break
                is_warmup = batch_idx == 0
                batch_num = batch_idx + 1

                if is_warmup:
                    self.log(
                        f"[INFO] Batch {batch_num}/{total_batches} warm-up (1 page)."
                    )
                else:
                    self.log(
                        f"[INFO] Batch {batch_num}/{total_batches} checking {len(batch)} tabs."
                    )

                driver.get(batch[0])
                for url in batch[1:]:
                    driver.execute_script(f"window.open('{url}', '_blank');")

                if start_minimized:
                    for _ in range(5):
                        if hide_window():
                            break
                        time.sleep(0.2)

                start_batch = time.time()
                results = {handle: None for handle in driver.window_handles}
                time.sleep(1.0)

                while True:
                    if self._should_stop():
                        break
                    all_done = True
                    current_handles = driver.window_handles
                    captcha_handled = False

                    timeout_limit = 30.0 if is_warmup else 8.0
                    if time.time() - start_batch > timeout_limit:
                        break

                    for idx, handle in enumerate(current_handles):
                        if handle not in results or results[handle] is not None:
                            continue

                        try:
                            driver.switch_to.window(handle)
                            title_now = driver.title
                            page_src = driver.page_source
                        except Exception:
                            continue

                        if is_captcha(title_now, page_src):
                            self.log(
                                "[WARN] Captcha detected. Please solve in the browser."
                            )
                            if reveal_on_captcha:
                                show_window()
                            wait_for_captcha_clear()
                            self.log("[INFO] Captcha solved. Resuming checks.")
                            if start_minimized:
                                hide_window()
                            start_batch = time.time()
                            try:
                                results = {h: None for h in driver.window_handles}
                            except Exception:
                                results = {}
                            captcha_handled = True
                            break

                        try:
                            current_url = driver.current_url.rstrip("/")
                        except Exception:
                            current_url = ""

                        page_src_lower = (page_src or "").lower()
                        is_blocked = False
                        try:
                            if current_url == "https://chaturbate.com":
                                is_blocked = True
                            elif driver.find_elements(
                                By.CSS_SELECTOR, "[data-testid='denied-notice']"
                            ):
                                is_blocked = True
                            elif "region or gender" in page_src_lower:
                                is_blocked = True
                            elif driver.find_elements(By.ID, "room_view_container"):
                                results[handle] = "clean"
                        except Exception:
                            pass

                        if is_blocked:
                            if current_url and current_url != "https://chaturbate.com":
                                confirmed_blocks.append(current_url)
                            else:
                                if idx < len(batch):
                                    confirmed_blocks.append(batch[idx])
                            self.log("[INFO] Blocked page detected.")
                            results[handle] = "blocked"

                        if results.get(handle) is None:
                            all_done = False

                    if captcha_handled:
                        continue

                    if all_done:
                        break
                    time.sleep(0.5)

                if self._should_stop():
                    break

                for handle in driver.window_handles[1:]:
                    driver.switch_to.window(handle)
                    driver.close()

                if driver.window_handles:
                    driver.switch_to.window(driver.window_handles[0])

                if self._should_stop():
                    break

        except Exception as exc:
            message = str(exc)
            if (
                "invalid session id" in message
                or "session deleted" in message
                or "not connected to DevTools" in message
            ):
                self.log(
                    "[WARN] Browser window was closed. Verification stopped early."
                )
            else:
                self.log(f"[ERROR] Verification failed: {exc}")
        finally:
            if hider:
                hider.stop()
            self._set_active_driver(None)
            safe_quit(driver)
            shutil.rmtree(profile_dir, ignore_errors=True)

        self._finalize_candidate_verification(skipped_master_urls + confirmed_blocks)

    def verify_candidates(self, start_minimized=True, reveal_on_captcha=True):
        """Verify the active generation without publishing partial results."""
        self._ensure_session()
        store = ChaturbateRunStore(self.session_folder)
        try:
            context = store.load_active()
        except (OSError, ValueError) as exc:
            self.log(f"[ERROR] Invalid run state: {exc}")
            return StepOutcome.failed(
                run_id="",
                generation_id="",
                platform="Chaturbate",
                step=StepName.VERIFY,
                error_code="invalid_run_state",
                error_message="The saved run state is invalid.",
            )
        if context is None:
            self.log(
                "[WARN] This is a legacy session. Use explicit legacy adoption "
                "before Resume or start a new run."
            )
            return StepOutcome.failed(
                run_id="",
                generation_id="",
                platform="Chaturbate",
                step=StepName.VERIFY,
                error_code="legacy_session_requires_adoption",
                error_message=(
                    "Legacy incomplete sessions are never changed automatically."
                ),
            )
        try:
            manifest = store.load_manifest(context)
        except (OSError, ValueError) as exc:
            return StepOutcome.failed(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.VERIFY,
                error_code="invalid_manifest",
                error_message=str(exc)[:200],
            )
        compare_state = manifest.get("steps", {}).get(
            StepName.COMPARE.value, {}
        )
        if compare_state.get("status") != "succeeded":
            outcome = StepOutcome.failed(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.VERIFY,
                error_code="unmet_step3_prerequisite",
                error_message=(
                    "Step 4 requires a successful Step 3 from this generation."
                ),
            )
            store.record_step(context, outcome)
            return outcome
        candidate_records = [
            ArtifactRecord.from_dict(item)
            for item in manifest.get("artifacts", [])
            if item.get("artifact_type") == "candidates"
        ]
        try:
            if len(candidate_records) != 1:
                raise ValueError("candidate artifact record missing")
            store.validate_artifact(context, candidate_records[0])
            if (
                os.path.exists(self.paths.candidates)
                and sha256_file(self.paths.candidates)
                != candidate_records[0].sha256
            ):
                raise ValueError("candidate compatibility view mismatch")
        except (OSError, ValueError) as exc:
            outcome = StepOutcome.failed(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.VERIFY,
                error_code="generation_prerequisite_mismatch",
                error_message=str(exc)[:200],
            )
            store.record_step(context, outcome)
            return outcome
        if not os.path.exists(self.paths.candidates):
            outcome = StepOutcome.failed(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.VERIFY,
                error_code="missing_candidates",
                error_message="The current generation has no candidate artifact.",
            )
            store.record_step(context, outcome)
            return outcome
        with open(self.paths.candidates, "r", encoding="utf-8") as handle:
            urls = [
                self._normalize_url(line)
                for line in handle
                if line.strip()
            ]

        master_urls = {
            self._normalize_url(item.get("name", ""))
            for item in self.get_master_list_models()
            if item.get("name")
        }
        skipped_master_urls = [url for url in urls if url in master_urls]
        if skipped_master_urls:
            urls = [url for url in urls if url not in master_urls]
            self.log(
                f"[INFO] Skipping {len(skipped_master_urls)} candidates "
                "already confirmed in the master list."
            )
            self.log(
                "[INFO] Skipped models stay in the session blocked list; use "
                "master verification to re-confirm them."
            )
            store.prune_checkpoint(context, urls)

        class RunnerStopToken:
            def __init__(self, runner):
                self.runner = runner

            def is_cancelled(self):
                return self.runner._should_stop()

        driver = None
        profile_dir = None
        profile_manager = None
        hider = None
        try:
            provider = None
            if urls:
                options = uc.ChromeOptions()
                options.add_argument("--mute-audio")
                options.add_argument("--disable-popup-blocking")
                options.add_argument("--window-size=1200,900")
                options.add_argument("--no-first-run")
                options.add_argument("--no-default-browser-check")
                options.add_argument("--disable-extensions")
                # Never run headless here: Cloudflare fingerprints headless
                # Chrome and issues a challenge on nearly every navigation,
                # which nothing can solve because no window is visible.
                # Instead start a real window off-screen and reveal it only
                # when a challenge actually appears.
                if start_minimized:
                    options.add_argument("--window-position=-32000,-32000")
                profile_dir = persistent_verify_profile_dir()
                driver = _uc_chrome(
                    options=options, user_data_dir=profile_dir
                )
                self._set_active_driver(driver)
                try:
                    driver.set_page_load_timeout(30)
                    driver.set_script_timeout(15)
                except Exception:
                    pass
                if start_minimized:
                    try:
                        chromedriver_pid = driver.service.process.pid
                    except Exception:
                        chromedriver_pid = None
                    hider = _ChromeWindowHider(
                        profile_dir, parent_pid=chromedriver_pid
                    )
                    hider.start()
                    for _ in range(10):
                        if hider.hide():
                            break
                        time.sleep(0.2)
                    self.log(
                        "[INFO] Browser running hidden; it will appear if a "
                        "CAPTCHA needs solving."
                    )
                else:
                    self.log("[INFO] Browser stays visible during verification.")

                def reveal_browser():
                    if hider is not None and hider.show():
                        return True
                    try:
                        driver.set_window_position(0, 0)
                        driver.maximize_window()
                        return True
                    except Exception:
                        return False

                def conceal_browser():
                    if not start_minimized:
                        return False
                    if hider is not None and hider.hide():
                        return True
                    try:
                        driver.set_window_position(-32000, -32000)
                        return True
                    except Exception:
                        return False

                try:
                    driver.get("https://chaturbate.com/")
                    state = age_gate_state(driver)
                    if state == "unreadable":
                        # Waiting for a click on a blank window would stall for
                        # minutes, so say what is actually wrong instead.
                        self.log(
                            "[WARN] The page did not render. The profile at "
                            f"{profile_dir} is most likely still open in "
                            "another Chrome or a previous run. Close it and "
                            "retry."
                        )
                    elif state == "clear" or dismiss_age_gate(driver):
                        self.log("[INFO] Terms splash accepted.")
                    else:
                        dump = dump_age_gate_markup(
                            driver, self.session_folder
                        )
                        if dump:
                            self.log(
                                f"[INFO] Splash markup saved for diagnosis: "
                                f"{dump}"
                            )
                        if reveal_on_captcha:
                            self.log(
                                "[WAITING] Please click the age confirmation "
                                "in the browser window. This is a one-time "
                                "step; the profile remembers it afterwards."
                            )
                            reveal_browser()
                            accepted = wait_for_age_gate_clear(
                                driver, should_stop=self._should_stop
                            )
                            conceal_browser()
                            self.log(
                                "[INFO] Terms splash accepted."
                                if accepted
                                else "[WARN] Terms splash still blocking; "
                                "pages may not classify correctly."
                            )
                        else:
                            self.log(
                                "[WARN] Could not accept the terms splash; "
                                "pages may not classify correctly."
                            )
                except Exception:
                    pass

                def provider(url, attempt):
                    try:
                        driver.get(url)
                    except Exception as exc:
                        return classify_page(
                            PageObservation(
                                original_url=url,
                                current_url="",
                                navigation_error=(
                                    "browser_closed"
                                    if "session" in str(exc).lower()
                                    else "navigation_timeout"
                                ),
                            ),
                            attempt=attempt,
                            run_id=context.run_id,
                        )
                    deadline = time.monotonic() + 600.0
                    captcha_logged = False
                    revealed = False
                    while True:
                        if self._should_stop():
                            return classify_page(
                                PageObservation(
                                    original_url=url,
                                    current_url="",
                                    navigation_error="cancelled",
                                ),
                                attempt=attempt,
                                run_id=context.run_id,
                            )
                        try:
                            title = driver.title or ""
                            source = driver.page_source or ""
                        except Exception:
                            title, source = "", ""
                        captcha = page_has_captcha(title, source)
                        if not captcha:
                            if revealed:
                                conceal_browser()
                                self.log(
                                    "[INFO] CAPTCHA solved. Browser hidden again."
                                )
                            break
                        if not captcha_logged:
                            self.log(
                                "[WAITING] CAPTCHA detected. Solve it within 10 minutes."
                            )
                            captcha_logged = True
                        if reveal_on_captcha and not revealed:
                            revealed = reveal_browser()
                            if not revealed:
                                self.log(
                                    "[WARN] Could not bring the browser to the "
                                    "front; find the Chrome window manually."
                                )
                                revealed = True
                        if time.monotonic() >= deadline:
                            if revealed:
                                conceal_browser()
                            return classify_page(
                                PageObservation(
                                    original_url=url,
                                    current_url="",
                                    navigation_error="captcha_timeout",
                                ),
                                attempt=attempt,
                                run_id=context.run_id,
                            )
                        time.sleep(0.5)
                    try:
                        current_url = driver.current_url
                        denied = bool(
                            driver.find_elements(
                                By.CSS_SELECTOR,
                                "[data-testid='denied-notice']",
                            )
                        )
                        excerpt = _visible_block_excerpt(driver)
                        # A denied notice is authoritative even behind the
                        # splash; otherwise the room only renders once the
                        # terms are accepted.
                        if not denied and page_has_age_gate(excerpt):
                            if not dismiss_age_gate(driver):
                                return classify_page(
                                    PageObservation(
                                        original_url=url,
                                        current_url=current_url,
                                        navigation_error="age_gate_blocked",
                                    ),
                                    attempt=attempt,
                                    run_id=context.run_id,
                                )
                            denied = bool(
                                driver.find_elements(
                                    By.CSS_SELECTOR,
                                    "[data-testid='denied-notice']",
                                )
                            )
                            excerpt = _visible_block_excerpt(driver)
                        room = bool(
                            driver.find_elements(
                                By.CSS_SELECTOR, ROOM_CONTAINER_SELECTOR
                            )
                        )
                    except Exception:
                        return classify_page(
                            PageObservation(
                                original_url=url,
                                current_url="",
                                navigation_error="selector_error",
                            ),
                            attempt=attempt,
                            run_id=context.run_id,
                        )
                    return classify_page(
                        PageObservation(
                            original_url=url,
                            current_url=current_url,
                            title=title,
                            has_denied_notice=denied,
                            has_room_container=room,
                            has_offline_notice=page_is_offline(excerpt),
                            page_text_excerpt=excerpt,
                        ),
                        attempt=attempt,
                        run_id=context.run_id,
                    )

            verifier = ChaturbateVerifier(
                store,
                context,
                provider or (lambda *_: None),
                stop_token=RunnerStopToken(self),
                progress_callback=lambda event: self.log(
                    f"[PROGRESS] {event.message}"
                ),
                known_blocked=skipped_master_urls,
            )
            outcome = verifier.verify(urls, resume=True)
            if outcome.can_continue:
                self.log(
                    f"[INFO] Final list safely published ({outcome.output_count})."
                )
                self.compile_master_list()
            else:
                self.log(
                    f"[WARN] Step 4 {outcome.status.value}; "
                    f"{outcome.remaining_count} candidates remain."
                )
            return outcome
        except Exception as exc:
            # Every other exit from this method turns a failure into a
            # recorded StepOutcome. Without this the browser paths crash
            # straight past the store, so the manifest keeps no step4 entry
            # and the run log can only report a bare "workflow_step_failed".
            outcome = StepOutcome.failed(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.VERIFY,
                error_code="verify_crashed",
                error_message=f"{type(exc).__name__}: {exc}"[:200],
            )
            self.log(f"[ERROR] Step 4 crashed: {outcome.error_message}")
            store.record_step(context, outcome)
            return outcome
        finally:
            if hider is not None:
                hider.stop()
            self._set_active_driver(None)
            safe_quit(driver)
            if profile_manager:
                profile_manager.cleanup()

    def resolve_verification_discrepancy(self):
        """Compatibility alias for non-destructive consistency validation."""
        result = self.validate_master_consistency()
        return 0 if result.valid else -1

    def validate_master_consistency(self):
        result = MasterRepository(self.base_dir).validate_consistency(
            repair_txt=True
        )
        if result.valid:
            if result.repaired_txt:
                self.log(
                    "[INFO] Master TXT regenerated from canonical JSON; "
                    "the blacklist was not changed."
                )
            else:
                self.log("[INFO] Master JSON/TXT consistency verified.")
        else:
            self.log(
                f"[ERROR] Master consistency failed: {result.error_code}. "
                "Destructive verification is blocked."
            )
        return result
