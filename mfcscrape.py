import datetime
import json
import os
import random
import re
import shutil
import subprocess
import time
import uuid
from urllib.parse import unquote

import undetected_chromedriver as uc
import requests
from selenium.webdriver.common.by import By

from mfc_verifier import PLATFORM as MFC_PLATFORM, MFCVerificationResult
from windows_subprocess import hidden_subprocess_kwargs
from workflow_types import StepName, StepOutcome

try:
    import websocket
except Exception:
    websocket = None


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


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


MFC_HOME_URL = "https://www.myfreecams.com/"
MFC_SERVERCONFIG_URL = "https://www.myfreecams.com/_js/serverconfig.js"
MFC_PHP_BULK_URL = "https://www.myfreecams.com/php/FcwExtResp.php"


class MFCPaths:
    def __init__(self, session_folder):
        self.session_folder = session_folder
        self.vpn_list = os.path.join(session_folder, "mfc_vpn_list.txt")
        self.local_list = os.path.join(session_folder, "mfc_local_list.txt")
        self.blocked = os.path.join(session_folder, "BLOCKED_MODELS.txt")
        self.verified_live = os.path.join(session_folder, "mfc_verified_live.txt")
        # Work salvaged from a run that never finished. Kept apart from
        # verified_live so an interrupted run can never be mistaken for a
        # complete one.
        self.partial_verified = os.path.join(
            session_folder, "mfc_verified_live.partial.txt"
        )


class MFCRunner:
    def __init__(self, base_dir, logger=None):
        self.base_dir = base_dir
        self.logger = logger
        self.session_folder = ""
        self.paths = None
        self._stop_requested = False
        self._active_driver = None
        # The two snapshots step 4 judges against: who is on air (VPN side)
        # and who this connection may watch (local side).
        self._live_reference = None
        self._local_reference = None

    def set_logger(self, logger):
        self.logger = logger

    def log(self, message):
        if self.logger:
            self.logger(message)

    def clear_stop(self):
        self._stop_requested = False

    def _should_stop(self):
        return bool(self._stop_requested)

    def is_cancelled(self):
        """Stop-token interface, so the runner can be passed to a verifier."""
        return self._should_stop()

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
        session_num = self._next_session_number()
        folder_name = f"session {session_num} {date_str}"
        session_path = os.path.join(self.base_dir, folder_name)
        os.makedirs(session_path, exist_ok=False)
        self.set_session(session_path)
        self.log(f"[INFO] New session created: {session_path}")
        return session_path

    def use_latest_session(self):
        session_dirs = self._list_session_dirs()
        if not session_dirs:
            self.log("[WARN] No session folders found. Creating a new session.")
            return self.create_session()

        session_dirs.sort(key=lambda p: os.path.getmtime(p))
        latest = session_dirs[-1]
        self.set_session(latest)
        self.log(f"[INFO] Using latest session: {latest}")
        return latest

    def set_session(self, session_path):
        self.session_folder = session_path
        self.paths = MFCPaths(session_path)

    def _ensure_session(self):
        if not self.session_folder:
            self.create_session()

    def set_live_reference(self, models):
        """Record who is on air, as seen from the VPN side."""
        self._live_reference = set(models) if models else None

    def set_local_reference(self, models):
        """Record who this connection is actually allowed to watch."""
        self._local_reference = set(models) if models else None

    def capture_live_reference(self):
        """Take the on-air snapshot. Only call this with the VPN connected.

        With the VPN down this would read the local list, which is exactly the
        list blocked models are missing from, and every candidate would look
        as though it had gone off air.
        """
        self._ensure_session()
        models = self.fetch_models_via_api("On-air snapshot")
        self.set_live_reference(models)
        self._log_snapshot("On-air", models)
        return len(models)

    def capture_local_reference(self):
        """Take the viewable snapshot. Only call this with the VPN down."""
        self._ensure_session()
        models = self.fetch_models_via_api("Viewable snapshot")
        self.set_local_reference(models)
        self._log_snapshot("Viewable", models)
        return len(models)

    def _log_snapshot(self, label, models):
        if models:
            self.log(f"[INFO] {label} snapshot: {len(models)} models.")
        else:
            self.log(
                f"[WARN] {label} snapshot is empty; step 4 cannot verify "
                "anything against it."
            )

    def _write_list(self, path, items):
        with open(path, "w") as handle:
            for item in sorted(items):
                handle.write(item + "\n")

    def _remove_file(self, path):
        if not os.path.exists(path):
            return
        try:
            os.remove(path)
        except Exception as exc:
            self.log(f"[WARN] Failed to delete {os.path.basename(path)}: {exc}")

    def _finalize_verification(self, result):
        """Publish a verification run only once it has actually finished.

        A run that was stopped, or that never got an answer for some
        candidates, leaves the session exactly as it found it: the previous
        verified list stays intact, the step 1/2 snapshots stay on disk so the
        step can be retried, and the master list is not recompiled. Whatever
        was confirmed is still written down, but under a name that cannot be
        mistaken for a finished result.
        """
        outcome = result.outcome
        confirmed = sorted(result.confirmed)
        if not result.publishable:
            self._write_list(self.paths.partial_verified, confirmed)
            self.log(
                f"[WARN] Verification did not complete ({outcome.status.value}). "
                f"{len(confirmed)} confirmed so far were saved to "
                f"{os.path.basename(self.paths.partial_verified)}."
            )
            self.log(
                "[INFO] Session files were kept so step 4 can be run again. "
                "The master list was not updated."
            )
            return outcome

        self._write_list(self.paths.verified_live, confirmed)
        self.log(
            f"[INFO] Verified live list saved: {self.paths.verified_live} "
            f"({len(confirmed)} models)."
        )
        self._remove_file(self.paths.partial_verified)
        for path in (self.paths.vpn_list, self.paths.local_list):
            self._remove_file(path)
        self.log("[INFO] Intermediate session files cleaned up.")
        self.compile_master_list()
        return outcome

    def get_master_list_path(self):
        """Return the path to the master blocked list file."""
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
        try:
            with open(path, "r") as handle:
                return set(line.strip() for line in handle if line.strip())
        except Exception:
            return set()

    def compile_master_list(self):
        """
        Build the master list from what step 4 actually proved.

        Where a session has a verified list, only the models it confirmed are
        carried over: a candidate step 4 checked and rejected was a false
        positive and does not belong here. Where step 4 never ran, the raw
        candidates are kept but flagged with pending_verification, because
        never having asked is not the same as having been told no.

        Dates come from the folder names (e.g. 'session 1 04.01.2026'). The
        result is written to MASTER_BLOCKED.txt (names) and
        MASTER_BLOCKED_DATA.json (names plus metadata).
        """
        self.log("[INFO] Compiling master blocked list...")

        # Load manual additions
        manual_models = set()
        manual_path = self.get_manual_list_path()
        if os.path.exists(manual_path):
            try:
                with open(manual_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            manual_models.add(line)
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
                            existing_info[name] = item
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
                raw_date = parts[-1]
                try:
                    dt = datetime.datetime.strptime(raw_date, "%d.%m.%Y")
                    date_str = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass

            verified_path = os.path.join(session_dir, "mfc_verified_live.txt")
            blocked_path = os.path.join(session_dir, "BLOCKED_MODELS.txt")
            if os.path.exists(verified_path):
                # Step 4 ran here, so its answer is the whole answer.
                source_path, pending = verified_path, False
            elif os.path.exists(blocked_path):
                # Step 4 never ran, so these were never judged either way.
                source_path, pending = blocked_path, True
            else:
                continue

            try:
                file_dt = datetime.datetime.fromtimestamp(
                    os.path.getmtime(source_path)
                )
                timestamp_date = (
                    date_str if date_str != "Unknown" else file_dt.strftime("%Y-%m-%d")
                )
                source_timestamp = f"{timestamp_date}T{file_dt.strftime('%H:%M:%S')}"
                with open(source_path, "r", encoding="utf-8") as handle:
                    lines = [line.strip() for line in handle if line.strip()]
            except Exception as exc:
                self.log(f"[WARN] Failed to read {source_path}: {exc}")
                continue

            for line in lines:
                if line in blacklist:
                    continue
                curr = model_info.get(line)
                if curr is None:
                    model_info[line] = {
                        "date": date_str,
                        "session": session_num,
                        "timestamp": source_timestamp,
                        "pending": pending,
                    }
                    continue

                # Being confirmed in any session outweighs a session that
                # never got round to checking.
                curr["pending"] = curr["pending"] and pending

                # Keep the earliest discovery
                is_earlier = False
                if date_str != "Unknown":
                    if curr["date"] == "Unknown":
                        is_earlier = True
                    elif date_str < curr["date"]:
                        is_earlier = True
                    elif (
                        date_str == curr["date"] and session_num < curr["session"]
                    ):
                        is_earlier = True
                if is_earlier:
                    curr["date"] = date_str
                    curr["session"] = session_num
                    curr["timestamp"] = source_timestamp

        # Preserve an exact insertion timestamp when one already exists. New
        # entries use this compilation time; legacy date-only entries retain
        # the time of the BLOCKED_MODELS file from their original session.
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
                # Manual entries are deliberately excluded from verification,
                # so flagging them as awaiting it would be misleading.
                model_info[m]["pending"] = False

        # Save Text File
        master_path = self.get_master_list_path()
        sorted_models = sorted(model_info.keys())
        try:
            with open(master_path, "w") as handle:
                for item in sorted_models:
                    handle.write(item + "\n")
            self.log(
                f"[INFO] Master list TXT saved: {master_path} ({len(sorted_models)} models)"
            )
        except Exception as exc:
            self.log(f"[ERROR] Failed to save master TXT: {exc}")

        # Save JSON Data
        master_json = self.get_master_json_path()
        json_data = []
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

            # Carried over from a session where step 4 never ran.
            if info.get("pending"):
                entry["pending_verification"] = True

            # Preserve country if it exists
            if m in existing_info and "country" in existing_info[m]:
                entry["country"] = existing_info[m]["country"]

            json_data.append(entry)

        # A recompile legitimately shrinks the list when verification rejects
        # earlier candidates, so keep the previous file recoverable.
        if os.path.exists(master_json):
            try:
                shutil.copyfile(master_json, master_json + ".bak")
            except Exception as exc:
                self.log(f"[WARN] Failed to back up the master list: {exc}")
            # The rolling .bak only survives until the next compile, which is
            # no use when the list just lost entries. Keep a dated copy of the
            # larger list so a shrink is always recoverable.
            if len(json_data) < len(existing_info):
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                try:
                    shutil.copyfile(master_json, f"{master_json}.{stamp}.bak")
                    self.log(
                        f"[INFO] Master list shrank from {len(existing_info)} to "
                        f"{len(json_data)}; kept a dated backup."
                    )
                except Exception as exc:
                    self.log(f"[WARN] Failed to keep a dated backup: {exc}")

        try:
            with open(master_json, "w", encoding="utf-8") as handle:
                json.dump(json_data, handle, indent=2)
            pending_count = sum(
                1 for entry in json_data if entry.get("pending_verification")
            )
            self.log(f"[INFO] Master list JSON saved: {master_json}")
            if pending_count:
                self.log(
                    f"[INFO] {pending_count} entries are still awaiting "
                    "verification (step 4 never ran on their session)."
                )
        except Exception as exc:
            self.log(f"[ERROR] Failed to save master JSON: {exc}")

        return len(sorted_models)

    def get_master_list_models(self):
        """
        Return list of models in the master blocked list.
        Returns list of dicts: [{'name': 'url', 'date': 'YYYY-MM-DD'}, ...]
        """
        json_path = self.get_master_json_path()
        if os.path.exists(json_path):
            try:
                with open(json_path, "r") as handle:
                    data = json.load(handle)
                    if isinstance(data, list):
                        return data
            except Exception:
                pass

        master_path = self.get_master_list_path()
        if not os.path.exists(master_path):
            return []
        try:
            with open(master_path, "r") as handle:
                lines = [line.strip() for line in handle if line.strip()]
                return [
                    {"name": x, "date": "Unknown", "session": 0} for x in sorted(lines)
                ]
        except Exception:
            return []

    def remove_from_master_list(self, model_url):
        master_path = self.get_master_list_path()
        if not os.path.exists(master_path):
            return

        # Legacy TXT removal
        try:
            with open(master_path, "r") as f:
                lines = [line.strip() for line in f if line.strip()]

            if model_url not in lines:
                return

            lines.remove(model_url)
            self.log(f"[INFO] Removing {model_url} from Master List.")

            with open(master_path, "w") as f:
                for line in sorted(lines):
                    f.write(line + "\n")
        except Exception as exc:
            self.log(f"[ERROR] Failed to remove model from master list: {exc}")

        # Manual list removal
        manual_path = self.get_manual_list_path()
        if os.path.exists(manual_path):
            try:
                with open(manual_path, "r") as f:
                    lines = [line.strip() for line in f if line.strip()]
                if model_url in lines:
                    lines.remove(model_url)
                    with open(manual_path, "w") as f:
                        for line in sorted(lines):
                            f.write(line + "\n")
            except Exception:
                pass

        # JSON removal
        master_json = self.get_master_json_path()
        if os.path.exists(master_json):
            try:
                with open(master_json, "r") as f:
                    data = json.load(f)

                new_data = [d for d in data if d.get("name") != model_url]
                if len(new_data) < len(data):
                    with open(master_json, "w") as f:
                        json.dump(new_data, f, indent=2)
            except Exception:
                pass

    def block_model(self, model_url):
        """
        Add the given model URL to GLOBAL_BLACKLIST.txt so it doesn't appear in future compilations.
        """
        blacklist_path = self.get_global_blacklist_path()
        try:
            current_blacklist = self.get_blacklisted_models()
            if model_url in current_blacklist:
                return

            self.log(f"[INFO] Blocking {model_url} globally.")
            with open(blacklist_path, "a") as f:
                f.write(model_url + "\n")

            # Also ensure it's removed from Master List if present
            self.remove_from_master_list(model_url)

        except Exception as exc:
            self.log(f"[ERROR] Failed to block model: {exc}")

    def add_manual_to_master(self, username):
        """Construct MFC URL and add to manual list."""
        if not username:
            return
        username = username.strip().replace(" ", "")
        url = f"https://www.myfreecams.com/#{username}"
        manual_path = self.get_manual_list_path()

        try:
            existing = set()
            if os.path.exists(manual_path):
                with open(manual_path, "r") as f:
                    existing = set(line.strip() for line in f if line.strip())

            if url not in existing:
                with open(manual_path, "a") as f:
                    f.write(url + "\n")
                self.log(f"[INFO] Manually added {url} to MFC manual list.")

            self.compile_master_list()
        except Exception as exc:
            self.log(f"[ERROR] Failed to add manual model: {exc}")

    def update_master_model_country(self, model_url, country):
        """Update the country for a given model in the master list JSON."""
        json_path = self.get_master_json_path()
        if not os.path.exists(json_path):
            return False

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            updated = False
            for item in data:
                if item.get("name") == model_url:
                    item["country"] = country
                    updated = True
                    break

            if updated:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                return True
        except Exception as exc:
            self.log(f"[ERROR] Failed to update country for {model_url}: {exc}")

        return False

    def _normalize_mfc_username(self, username):
        if username is None:
            return ""
        value = str(username).strip()
        if not value:
            return ""
        if value.startswith("#"):
            value = value[1:].strip()
        if "/" in value:
            value = value.split("/", 1)[0].strip()
        if not value or " " in value:
            return ""
        return value

    def _mfc_model_url(self, username):
        return f"https://www.myfreecams.com/#{username}"

    def _decode_payload_json(self, payload):
        if payload is None:
            return None
        decoded = unquote(str(payload).strip())
        if not decoded or decoded[0] not in ("{", "["):
            return None
        try:
            return json.loads(decoded)
        except Exception:
            return None

    def _split_fc_frames(self, raw_message):
        message = raw_message
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        if not isinstance(message, str):
            return []

        text = message.strip("\r\n\0")
        if not text:
            return []

        frames = []
        cursor = 0
        size = len(text)

        while cursor + 7 <= size and text[cursor : cursor + 6].isdigit():
            msg_len = _safe_int(text[cursor : cursor + 6], -1)
            if msg_len <= 0:
                break
            end = cursor + 6 + msg_len
            if end > size:
                break
            frames.append(text[cursor:end])
            cursor = end
            while cursor < size and text[cursor] in ("\r", "\n", "\0"):
                cursor += 1

        if frames:
            return frames

        return [text]

    def _parse_fc_message(self, raw_message):
        message = raw_message
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        if not isinstance(message, str):
            return None
        parts = message.split(" ", 5)
        if len(parts) < 5:
            return None
        header = parts[0]
        if len(header) < 7 or not header[:6].isdigit():
            return None
        fctype_text = header[6:]
        if not fctype_text.isdigit():
            return None
        return {
            "length": _safe_int(header[:6], 0),
            "fctype": _safe_int(fctype_text, -1),
            "from": _safe_int(parts[1], 0),
            "to": _safe_int(parts[2], 0),
            "msg_type": _safe_int(parts[3], 0),
            "arg": _safe_int(parts[4], 0),
            "payload": parts[5].strip() if len(parts) > 5 else "",
        }

    def _fetch_chat_servers(self):
        response = requests.get(MFC_SERVERCONFIG_URL, timeout=25)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            raw = response.text.strip().rstrip(";")
            data = json.loads(raw)

        servers = data.get("chat_servers") if isinstance(data, dict) else []
        normalized = []
        seen = set()
        for server in servers or []:
            name = str(server).strip()
            if not name or name in seen:
                continue
            normalized.append(name)
            seen.add(name)
        return normalized

    def _build_server_attempts(self, servers, max_attempts=12):
        if not servers:
            return []

        primary = [s for s in servers if s.startswith(("wchat", "xchat"))]
        secondary = [s for s in servers if s not in primary]
        random.shuffle(primary)
        random.shuffle(secondary)
        combined = primary + secondary
        if max_attempts and len(combined) > max_attempts:
            return combined[:max_attempts]
        return combined

    def _open_guest_socket(self, server_name, timeout=15):
        host = server_name if "." in server_name else f"{server_name}.myfreecams.com"
        ws_url = f"wss://{host}/fcsl"
        ws = websocket.create_connection(ws_url, timeout=timeout)
        ws.send("hello fcserver\n\0")
        ws.send(f"1 0 0 20071025 0 {uuid.uuid4().hex}@guest:guest\n")
        return ws

    def _collect_ws_bootstrap(self, ws, read_window=12, idle_grace=1.5):
        deadline = time.time() + read_window
        last_message_at = time.time()
        stream_state = {}
        php_candidates = []

        while time.time() < deadline:
            if self._should_stop():
                break
            timeout_left = max(0.2, min(1.0, deadline - time.time()))
            try:
                ws.settimeout(timeout_left)
            except Exception:
                pass

            try:
                raw = ws.recv()
            except Exception as exc:
                if exc.__class__.__name__ in ("WebSocketTimeoutException", "TimeoutError"):
                    if php_candidates and (time.time() - last_message_at) >= idle_grace:
                        break
                    continue
                break

            last_message_at = time.time()
            for frame in self._split_fc_frames(raw):
                parsed = self._parse_fc_message(frame)
                if not parsed:
                    continue

                payload = parsed.get("payload")
                if not payload:
                    continue

                fctype = parsed.get("fctype")
                if fctype == 81:
                    data = self._decode_payload_json(payload)
                    if not isinstance(data, dict):
                        continue
                    respkey = data.get("respkey")
                    serv = data.get("serv")
                    msg_info = data.get("msg") if isinstance(data.get("msg"), dict) else {}
                    if respkey and serv is not None:
                        candidate = {
                            "respkey": respkey,
                            "serv": serv,
                            "type": _safe_int(data.get("type"), 14),
                            "opts": _safe_int(data.get("opts"), 256),
                            "msglen": _safe_int(data.get("msglen"), 0),
                            "arg2": _safe_int(msg_info.get("arg2"), 0),
                        }
                        php_candidates.append(candidate)
                        if candidate["arg2"] == 21:
                            return candidate, stream_state
                    continue

                if fctype not in (10, 20):
                    continue

                data = self._decode_payload_json(payload)
                if not isinstance(data, dict):
                    continue
                username = self._normalize_mfc_username(data.get("nm"))
                if not username:
                    continue
                stream_state[username] = _safe_int(data.get("vs"), 127)

        best_params = None
        if php_candidates:
            preferred = [item for item in php_candidates if item.get("arg2") == 21]
            target_pool = preferred if preferred else php_candidates
            best_params = max(target_pool, key=lambda item: item.get("msglen", 0))
        return best_params, stream_state

    def _parse_bulk_php_models(self, payload):
        if not isinstance(payload, dict):
            return set()
        rdata = payload.get("rdata")
        if not isinstance(rdata, list) or not rdata:
            return set()

        header_index = -1
        header_row = None
        for idx, row in enumerate(rdata):
            if isinstance(row, list) and "nm" in row and "vs" in row:
                header_row = row
                header_index = idx
                break
        if header_row is None:
            return set()

        idx_nm = header_row.index("nm")
        idx_vs = header_row.index("vs")
        models = set()

        for row in rdata[header_index + 1 :]:
            if not isinstance(row, list):
                continue
            if idx_nm >= len(row) or idx_vs >= len(row):
                continue
            username = self._normalize_mfc_username(row[idx_nm])
            if not username:
                continue
            if _safe_int(row[idx_vs], 127) == 0:
                models.add(self._mfc_model_url(username))
        return models

    def _fetch_models_from_php_snapshot(self, php_params):
        params = {
            "respkey": php_params.get("respkey"),
            "type": _safe_int(php_params.get("type"), 14),
            "opts": _safe_int(php_params.get("opts"), 256),
            "serv": php_params.get("serv"),
        }
        response = requests.get(MFC_PHP_BULK_URL, params=params, timeout=45)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            payload = json.loads(response.text)
        return self._parse_bulk_php_models(payload)

    def fetch_models_via_api(self, mode_name, headless=True):
        """Fetch all online MFC models via WebSocket/API (no Chrome scraping)."""
        _ = headless  # Kept for interface parity with other runners.

        self._ensure_session()
        if self._should_stop():
            self.log("[INFO] Stop requested before API fetch started.")
            return set()
        self.log(f"[INFO] Contacting MFC API for {mode_name} list...")

        if websocket is None:
            self.log(
                "[ERROR] websocket-client is not installed. Run: pip install websocket-client"
            )
            return set()

        try:
            chat_servers = self._fetch_chat_servers()
        except Exception as exc:
            self.log(f"[ERROR] Failed to load MFC server config: {exc}")
            return set()

        if not chat_servers:
            self.log("[ERROR] MFC server config returned no chat servers.")
            return set()

        attempts = self._build_server_attempts(chat_servers, max_attempts=12)
        self.log(
            f"[INFO] Loaded {len(chat_servers)} chat servers. Trying up to {len(attempts)}."
        )

        last_error = ""
        for idx, server in enumerate(attempts, start=1):
            if self._should_stop():
                self.log("[INFO] Stop requested. Ending API fetch.")
                break
            ws = None
            self.log(f"[INFO] API attempt {idx}/{len(attempts)} via {server}...")
            try:
                ws = self._open_guest_socket(server, timeout=15)
                php_params, stream_state = self._collect_ws_bootstrap(ws, read_window=12)

                if self._should_stop():
                    self.log("[INFO] Stop requested. Ending API fetch.")
                    break

                if php_params:
                    models = self._fetch_models_from_php_snapshot(php_params)
                    if models:
                        self.log(
                            f"[INFO] {mode_name} fetch complete. Unique models: {len(models)}"
                        )
                        return models
                    self.log("[WARN] PHP snapshot returned no free-chat models.")

                stream_models = {
                    self._mfc_model_url(username)
                    for username, vs in stream_state.items()
                    if vs == 0
                }
                if stream_models:
                    self.log(
                        "[WARN] PHP snapshot unavailable; using WebSocket stream snapshot."
                    )
                    self.log(
                        f"[INFO] {mode_name} fetch complete. Unique models: {len(stream_models)}"
                    )
                    return stream_models

                self.log("[WARN] API attempt returned no model data.")
            except Exception as exc:
                last_error = str(exc)
                self.log(f"[WARN] API attempt failed on {server}: {exc}")
            finally:
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass

        if last_error:
            self.log(f"[ERROR] MFC API fetch failed. Last error: {last_error}")
        else:
            self.log("[ERROR] MFC API fetch failed. No model data returned.")
        return set()

    def get_models_aggressive(self, wait_ready=None):
        self.log("[INFO] Launching MFC aggressive scraper.")
        options = uc.ChromeOptions()
        options.add_argument("--no-first-run")
        options.add_argument("--mute-audio")
        driver = _uc_chrome(options=options)

        scraped_models = set()

        try:
            driver.get("https://www.myfreecams.com/")
            self.log("[ACTION] Set your filters (must match both runs).")
            self.log("[ACTION] Wait for models to appear, then continue.")
            if wait_ready:
                wait_ready()
            else:
                input("Press Enter to start scraping...")

            self.log("[INFO] Phase 1: Aggressive scrolling.")
            last_height = driver.execute_script("return document.body.scrollHeight")
            no_change_count = 0

            for _ in range(40):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)
                driver.execute_script("window.scrollBy(0, -300);")
                time.sleep(1)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1)

                new_height = driver.execute_script("return document.body.scrollHeight")
                if new_height == last_height:
                    no_change_count += 1
                    if no_change_count >= 3:
                        break
                else:
                    no_change_count = 0
                last_height = new_height

            self.log("[INFO] Phase 2: Deep extraction.")
            cards = driver.find_elements(By.CSS_SELECTOR, ".model_online")
            self.log(f"[INFO] Analyzing {len(cards)} card containers.")

            for card in cards:
                username = ""

                if not username:
                    try:
                        el = card.find_element(By.CSS_SELECTOR, ".model_title a")
                        username = el.get_attribute("textContent").strip()
                    except Exception:
                        pass

                if not username:
                    username = card.get_attribute("data-username")

                if not username:
                    try:
                        img = card.find_element(By.TAG_NAME, "img")
                        alt = img.get_attribute("alt")
                        if alt and len(alt) < 30:
                            username = alt.split("'")[0].strip()
                    except Exception:
                        pass

                if not username:
                    try:
                        links = card.find_elements(By.TAG_NAME, "a")
                        for link in links:
                            href = link.get_attribute("href")
                            if href and "myfreecams.com/" in href and "#" not in href:
                                parts = href.split("myfreecams.com/")
                                if len(parts) > 1:
                                    username = parts[1].split("?")[0].strip()
                                    break
                    except Exception:
                        pass

                if username:
                    username = username.strip()
                    if len(username) > 1 and " " not in username:
                        full_link = f"https://www.myfreecams.com/#{username}"
                        scraped_models.add(full_link)

            self.log(f"[INFO] Extracted {len(scraped_models)} unique models.")
            return scraped_models

        except Exception as exc:
            self.log(f"[ERROR] Scrape failed: {exc}")
            return set()
        finally:
            safe_quit(driver)

    def step_vpn_list(self, wait_ready=None, headless=True):
        _ = wait_ready
        self._ensure_session()
        self.log("[INFO] Step 1: VPN list (VPN ON).")
        models = self.fetch_models_via_api("VPN", headless=headless)
        if models:
            self._write_list(self.paths.vpn_list, models)
            self.log(f"[INFO] VPN list saved: {self.paths.vpn_list}")
        else:
            self.log("[WARN] VPN list empty. API fetch failed or returned no models.")
            self.log(
                "[INFO] Manual fallback available: run get_models_aggressive() if needed."
            )

    def step_local_list(self, wait_ready=None, headless=True):
        _ = wait_ready
        self._ensure_session()
        self.log("[INFO] Step 2: Local list (VPN OFF).")
        models = self.fetch_models_via_api("Local", headless=headless)
        if models:
            self._write_list(self.paths.local_list, models)
            self.log(f"[INFO] Local list saved: {self.paths.local_list}")
        else:
            self.log("[WARN] Local list empty. API fetch failed or returned no models.")
            self.log(
                "[INFO] Manual fallback available: run get_models_aggressive() if needed."
            )

    def compare_lists(self):
        self._ensure_session()
        if not os.path.exists(self.paths.vpn_list) or not os.path.exists(
            self.paths.local_list
        ):
            self.log("[WARN] Missing files. Run steps 1 and 2 first.")
            return

        self.log("[INFO] Loading lists...")
        with open(self.paths.vpn_list, "r") as handle:
            vpn_set = set(line.strip() for line in handle if line.strip())
        with open(self.paths.local_list, "r") as handle:
            local_set = set(line.strip() for line in handle if line.strip())

        blocked_models = vpn_set - local_set
        self.log(f"[INFO] VPN list: {len(vpn_set)}")
        self.log(f"[INFO] Local list: {len(local_set)}")
        self.log(f"[INFO] Blocked models: {len(blocked_models)}")

        if blocked_models:
            self._write_list(self.paths.blocked, blocked_models)
            self.log(f"[INFO] Blocked list saved: {self.paths.blocked}")
        else:
            self.log("[INFO] No differences found.")

    def _verify_step_outcome(self, factory, run_id, generation_id, **values):
        return factory(
            run_id=run_id,
            generation_id=generation_id,
            platform=MFC_PLATFORM,
            step=StepName.VERIFY,
            **values,
        )

    def verify_live_models(self, start_minimized=False, timeout=12):
        """Confirm a candidate against two snapshots taken moments apart.

        The browser used to answer this by opening each room, but a room that
        will not play looks the same whether the model is blocked, off air, or
        merely slow, so its verdicts could not be trusted. The API can be asked
        directly instead: a candidate still on air from the VPN's vantage point
        and still missing from this connection's own listing is hidden from us,
        which is the whole question. It also takes seconds rather than a
        quarter of an hour.
        """
        _ = (start_minimized, timeout)  # No browser is involved any more.
        self._ensure_session()
        # A snapshot describes one moment. Taking it now means a later run can
        # never quietly reuse an answer from an hour ago.
        live_reference = self._live_reference
        local_reference = self._local_reference
        self._live_reference = None
        self._local_reference = None
        run_id = uuid.uuid4().hex
        generation_id = uuid.uuid4().hex

        def outcome(factory, **values):
            return self._verify_step_outcome(factory, run_id, generation_id, **values)

        if self._should_stop():
            self.log("[INFO] Stop requested before verification started.")
            return outcome(StepOutcome.cancelled, error_code="cancelled_by_user")
        if not os.path.exists(self.paths.blocked):
            self.log("[WARN] No blocked list found. Run step 3 first.")
            return outcome(
                StepOutcome.failed,
                error_code="missing_candidates",
                error_message="No candidate list found. Run step 3 first.",
            )

        # Load JSON to check for manual flag
        manual_urls = set()
        master_json = self.get_master_json_path()
        if os.path.exists(master_json):
            try:
                with open(master_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        if item.get("manual"):
                            manual_urls.add(item.get("name"))
            except Exception:
                pass

        with open(self.paths.blocked, "r", encoding="utf-8") as handle:
            raw_urls = [line.strip() for line in handle if line.strip()]

        urls = [u for u in raw_urls if u not in manual_urls]
        skipped_manual_count = len(raw_urls) - len(urls)

        if skipped_manual_count > 0:
            self.log(
                f"[INFO] Skipping {skipped_manual_count} manually added models from verification."
            )

        if not urls:
            self.log("[WARN] Blocked list is empty.")
            return outcome(
                StepOutcome.failed,
                error_code="empty_candidates",
                error_message="The candidate list is empty.",
            )

        if live_reference is None or local_reference is None:
            self.log(
                "[ERROR] Step 4 needs both snapshots and did not get them. "
                "Nothing was published and the candidates were kept."
            )
            return outcome(
                StepOutcome.failed,
                input_count=len(urls),
                error_code="missing_snapshots",
                error_message=(
                    "Step 4 needs an on-air snapshot taken with the VPN up and "
                    "a viewable snapshot taken with it down."
                ),
            )

        self.log(f"[INFO] Step 4: Verifying {len(urls)} candidates against snapshots.")
        confirmed = []
        breakdown = {}
        for url in urls:
            if url not in live_reference:
                # She left between step 1 and now, so her absence from the
                # local list never needed a block to explain it.
                reason = "went_off_air"
            elif url in local_reference:
                # Visible from here after all: step 3 caught a gap in the
                # listing rather than a block.
                reason = "viewable_here"
            else:
                # On air, and still not offered to this connection.
                reason = "hidden_while_on_air"
                confirmed.append(url)
            breakdown[reason] = breakdown.get(reason, 0) + 1

        summary = ", ".join(
            f"{reason}: {count}" for reason, count in sorted(breakdown.items())
        )
        self.log(f"[INFO] Step 4 verdicts: {summary}")

        result = MFCVerificationResult(
            outcome=outcome(
                StepOutcome.succeeded,
                input_count=len(urls),
                output_count=len(confirmed),
                processed_count=len(urls),
            ),
            confirmed=tuple(sorted(confirmed)),
            unresolved={},
            breakdown=breakdown,
        )
        return self._finalize_verification(result)


def main_menu(runner):
    while True:
        print("\n" + "=" * 40)
        print("   MFC ULTIMATE GEOBLOCK HUNTER   ")
        print("=" * 40)
        print(f"Current Session: {runner.session_folder}")
        print("1. Fetch API list with VPN ON  (Save to vpn_list)")
        print("2. Fetch API list with VPN OFF (Save to local_list)")
        print("3. COMPARE LISTS       (Generate Results)")
        print("4. VERIFY LIVE         (Filter to playing video)")
        print("5. Exit")

        choice = input("\nSelect Option (1-5): ").strip()

        if choice == "1":
            print("\n>> Ensure VPN is CONNECTED now.")
            runner.step_vpn_list()
        elif choice == "2":
            print("\n>> Ensure VPN is DISCONNECTED now.")
            runner.step_local_list()
        elif choice == "3":
            runner.compare_lists()
            input("\nPress Enter to return to menu...")
        elif choice == "4":
            runner.verify_live_models()
            input("\nPress Enter to return to menu...")
        elif choice == "5":
            break


if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cli_runner = MFCRunner(base_dir, logger=print)
    cli_runner.create_session()
    main_menu(cli_runner)
