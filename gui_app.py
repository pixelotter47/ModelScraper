"""PySide6 bridge for the shared ModelScraper service backend."""

from __future__ import annotations

import os
import queue
import sys

from PySide6 import QtCore, QtGui, QtQml, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle

from app_settings import (
    DEFAULTS as SETTING_DEFAULTS,
    AppSettings,
    install_shared_settings,
)
from external_search import (
    get_external_search_menu_providers,
    launch_all_external_searches,
    launch_external_search,
    launch_urls_in_chrome,
)
from modelscraper_service import ModelScraperService
from mullvad_vpn import MULLVAD_LOCATIONS
from country_choices import COUNTRY_CHOICES


class AppController(QtCore.QObject):
    logMessage = QtCore.Signal(str)
    statusChanged = QtCore.Signal(str)
    busyChanged = QtCore.Signal(bool)
    sessionChanged = QtCore.Signal(str)
    blockedModelsChanged = QtCore.Signal()
    platformChanged = QtCore.Signal(str)
    masterListChanged = QtCore.Signal()
    masterListCompiled = QtCore.Signal(int, int)
    progressChanged = QtCore.Signal()
    outcomeChanged = QtCore.Signal()
    resumableChanged = QtCore.Signal(bool)
    settingsChanged = QtCore.Signal()
    searchProvidersChanged = QtCore.Signal()

    def __init__(self, base_dir, service=None, settings=None):
        super().__init__()
        self.base_dir = os.path.abspath(base_dir)
        service_settings = getattr(service, "settings", None)
        self._settings = (
            service_settings if isinstance(service_settings, AppSettings) else settings
        ) or AppSettings(self.base_dir)
        install_shared_settings(self._settings)
        self._service = service or ModelScraperService(self.base_dir, settings=self._settings)
        self._service.settings = self._settings
        self._state = self._service.get_state()
        self._subscriber = self._service.subscribe()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(75)
        self._timer.timeout.connect(self._drain_service_events)
        self._timer.start()
        self._apply_startup_settings()

    def _apply_startup_settings(self):
        """Push saved preferences into the service before the UI binds."""
        platform = self._settings.get("defaultPlatform")
        if platform and platform != self._state.get("platform"):
            self._service.set_platform(platform)
        if self._settings.get("rememberMasterListView"):
            saved_sort = self._settings.get("masterListSortMode")
            if saved_sort != self._state.get(
                "masterListSortMode", SETTING_DEFAULTS["masterListSortMode"]
            ):
                self._service.sort_master_list(saved_sort)
            saved_country = self._settings.get("masterListCountryFilter")
            if saved_country != self._state.get(
                "masterListCountryFilter",
                SETTING_DEFAULTS["masterListCountryFilter"],
            ):
                self._service.set_master_list_country_filter(saved_country)
        if self._settings.get("autoLoadLatestSession"):
            self._service.use_latest_session()
        self._state = self._service.get_state()

    def _drain_service_events(self):
        while True:
            try:
                event = self._subscriber.get_nowait()
            except queue.Empty:
                return
            event_type = event.get("type")
            if event_type == "log":
                self.logMessage.emit(str(event.get("message") or ""))
            elif event_type == "state":
                self._apply_state(event.get("state") or {})
            elif event_type == "progress":
                self._state["progress"] = event.get("progress")
                self.progressChanged.emit()
            elif event_type == "masterListChanged":
                self.masterListChanged.emit()
            elif event_type == "masterListCompiled":
                self.masterListCompiled.emit(
                    int(event.get("addedCount", 0)),
                    int(event.get("totalCount", 0)),
                )
                self.masterListChanged.emit()
            elif event_type == "taskFinished":
                self.outcomeChanged.emit()

    def _apply_state(self, state):
        previous = self._state
        self._state = dict(state)
        if previous.get("status") != state.get("status"):
            self.statusChanged.emit(self.status)
        if previous.get("busy") != state.get("busy"):
            self.busyChanged.emit(self.busy)
        if previous.get("sessionPath") != state.get("sessionPath"):
            self.sessionChanged.emit(self.sessionPath)
        if previous.get("platform") != state.get("platform"):
            self.platformChanged.emit(self.platform)
        if previous.get("blockedModels") != state.get("blockedModels"):
            self.blockedModelsChanged.emit()
        if previous.get("masterListModels") != state.get("masterListModels"):
            self.masterListChanged.emit()
        if previous.get("progress") != state.get("progress"):
            self.progressChanged.emit()
        if previous.get("lastOutcome") != state.get("lastOutcome"):
            self.outcomeChanged.emit()
        if previous.get("resumable") != state.get("resumable"):
            self.resumableChanged.emit(bool(state.get("resumable")))

    def _log(self, message):
        self._service._log(message)

    @QtCore.Property(str, notify=sessionChanged)
    def sessionPath(self):
        return str(self._state.get("sessionPath") or "")

    def _setting(self, key, default=None):
        settings = getattr(self, "_settings", None)
        if settings is None:
            return SETTING_DEFAULTS.get(key, default)
        return settings.get(key, default)

    def _provider_registry(self):
        """Return the user's registry, or None to fall back to the built-ins."""
        settings = getattr(self, "_settings", None)
        return None if settings is None else settings.provider_registry()

    @QtCore.Property("QVariantList", notify=searchProvidersChanged)
    def externalSearchProviders(self):
        return get_external_search_menu_providers(self._provider_registry())

    @QtCore.Property("QVariantList", notify=searchProvidersChanged)
    def searchProviderRows(self):
        settings = getattr(self, "_settings", None)
        return [] if settings is None else settings.providers()

    @QtCore.Property("QVariantMap", notify=settingsChanged)
    def settings(self):
        settings = getattr(self, "_settings", None)
        return dict(SETTING_DEFAULTS) if settings is None else settings.as_dict()

    @QtCore.Property("QVariantList", constant=True)
    def vpnLocations(self):
        return [
            {"code": code, "name": name}
            for code, name in MULLVAD_LOCATIONS.items()
        ]

    @QtCore.Property("QVariantList", constant=True)
    def countryChoices(self):
        return [{"code": "", "name": "Unknown", "flag": ""}] + [
            {
                "code": item["code"].lower(), "name": item["name"], "flag": item["code"],
                "flagImage": "assets/flags/" + item["code"].lower() + ".png"
                if os.path.isfile(os.path.join(self.base_dir, "ui", "assets", "flags", item["code"].lower() + ".png")) else "",
            }
            for item in COUNTRY_CHOICES
        ]

    @QtCore.Property(str, constant=True)
    def settingsPath(self):
        settings = getattr(self, "_settings", None)
        return "" if settings is None else settings.path

    @QtCore.Slot(str, "QVariant", result=bool)
    def setSetting(self, key, value):
        return self._update_preferences({key: value})

    def _update_preferences(self, updates):
        try:
            if not self._service.update_settings(updates):
                return False
        except (ValueError, RuntimeError, OSError) as error:
            self._log(f"[WARN] Preferences were not saved: {error}")
            return False
        self.settingsChanged.emit()
        return True

    @QtCore.Slot(str, str, result=bool)
    def setLocationPreferences(self, countries, location_terms):
        return self._update_preferences({
            "targetCountries": countries,
            "targetLocationTerms": location_terms,
        })

    @QtCore.Slot()
    def resetPreferences(self):
        if self._update_preferences(dict(SETTING_DEFAULTS)):
            self._log("[INFO] Settings restored to defaults.")

    @QtCore.Slot(str, result=bool)
    def isSearchProviderEnabled(self, provider_id):
        return self._settings.is_provider_enabled(provider_id)

    def _edit_search_providers(self, operation, *args):
        try:
            result = self._service.edit_search_providers(operation, *args)
        except (RuntimeError, ValueError, OSError) as error:
            self._log(f"[WARN] Search sources were not saved: {error}")
            return False, None
        self.settingsChanged.emit()
        return True, result

    @QtCore.Slot(str, bool, result=bool)
    def setSearchProviderEnabled(self, provider_id, enabled):
        ok, result = self._edit_search_providers("set_provider_enabled", provider_id, enabled)
        if not ok or not result:
            return False
        self.searchProvidersChanged.emit()
        return True

    @QtCore.Slot(str, str, result=str)
    def addSearchProvider(self, label, url_template):
        """Add a custom source. Returns an error message, empty when added."""
        ok, result = self._edit_search_providers("add_provider", label, url_template)
        if not ok:
            return "Search source could not be saved. Wait for the current task to finish and check the log."
        provider_id, error = result
        if error:
            return error
        self.searchProvidersChanged.emit()
        self._log(f"[INFO] Added search source: {label} ({provider_id})")
        return ""

    @QtCore.Slot(str, result=bool)
    def removeSearchProvider(self, provider_id):
        ok, result = self._edit_search_providers("remove_provider", provider_id)
        if not ok or not result:
            return False
        self.searchProvidersChanged.emit()
        self._log(f"[INFO] Removed search source: {provider_id}")
        return True

    @QtCore.Slot(str, int, result=bool)
    def moveSearchProvider(self, provider_id, delta):
        ok, result = self._edit_search_providers("move_provider", provider_id, delta)
        if not ok or not result:
            return False
        self.searchProvidersChanged.emit()
        return True

    @QtCore.Slot()
    def resetSearchProviders(self):
        ok, _ = self._edit_search_providers("reset_providers")
        if not ok:
            return
        self.searchProvidersChanged.emit()
        self._log("[INFO] Search network restored to defaults.")

    @QtCore.Slot(str, result=str)
    def pickExecutablePath(self, key):
        """Browse for chromePath / mullvadPath and store the choice."""
        titles = {
            "chromePath": "Select chrome.exe",
            "mullvadPath": "Select mullvad.exe",
        }
        if key not in titles:
            return ""
        current = str(self._setting(key, "") or "")
        start_dir = os.path.dirname(current) if current else ""
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            None,
            titles[key],
            start_dir,
            "Executables (*.exe);;All Files (*)",
        )
        if not file_path:
            return ""
        self.setSetting(key, file_path)
        return file_path

    @QtCore.Property("QStringList", notify=blockedModelsChanged)
    def blockedModels(self):
        return list(self._state.get("blockedModels") or [])

    @QtCore.Property("QVariantList", notify=masterListChanged)
    def masterListModels(self):
        return list(self._state.get("masterListModels") or [])

    @QtCore.Property(str, notify=platformChanged)
    def platform(self):
        return str(self._state.get("platform") or "Chaturbate")

    @QtCore.Property(str, notify=statusChanged)
    def status(self):
        return str(self._state.get("status") or "Idle")

    @QtCore.Property(bool, notify=busyChanged)
    def busy(self):
        return bool(self._state.get("busy"))

    @QtCore.Property("QVariantMap", notify=progressChanged)
    def progress(self):
        return dict(self._state.get("progress") or {})

    @QtCore.Property("QVariantMap", notify=outcomeChanged)
    def lastOutcome(self):
        return dict(self._state.get("lastOutcome") or {})

    @QtCore.Property(bool, notify=resumableChanged)
    def resumable(self):
        return bool(self._state.get("resumable"))

    @QtCore.Slot()
    def stopCurrentTask(self):
        self._service.stop_current_task()

    @QtCore.Slot()
    def createSession(self):
        self._service.create_session()

    @QtCore.Slot()
    def useLatestSession(self):
        self._service.use_latest_session()

    @QtCore.Slot()
    def openSessionFolder(self):
        self._service.open_session_folder()

    @QtCore.Slot(bool)
    def startVpnList(self, headless):
        if self.platform == "Stripchat":
            headless = False
        self._service.start_vpn_list(
            headless=headless,
            manual_network_confirmed=self._setting("vpnProvider") == "manual",
        )

    @QtCore.Slot(bool)
    def startLocalList(self, headless):
        if self.platform == "Stripchat":
            headless = False
        self._service.start_local_list(
            headless=headless,
            manual_network_confirmed=self._setting("vpnProvider") == "manual",
        )

    @QtCore.Slot()
    def startCompare(self):
        self._service.start_compare()

    @QtCore.Slot(bool)
    def startVerify(self, start_minimized):
        self._service.start_verify(
            start_minimized=start_minimized,
            manual_network_confirmed=self._setting("vpnProvider") == "manual",
        )

    @QtCore.Slot(bool, bool)
    def startFullAutoFlow(self, headless, start_minimized):
        if self.platform == "Stripchat":
            headless = False
        self._service.start_full_auto_flow(
            headless=headless, start_minimized=start_minimized
        )

    @QtCore.Slot()
    def resumeFullAutoFlow(self):
        self._service.start_full_auto_flow(
            headless=False if self.platform == "Stripchat" else True,
            start_minimized=True,
            run_selection="resume",
        )

    @QtCore.Slot(str)
    def setPlatform(self, name):
        self._service.set_platform(name)

    @QtCore.Slot(result=str)
    def loadBlockedList(self):
        roots = {
            "Chaturbate": "cb sessions",
            "MyFreeCams": "mfc sessions",
            "Stripchat": "sc sessions",
            "XHamsterLive": "xhl sessions",
        }
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            None,
            f"Load {self.platform} Blocked List",
            os.path.join(self.base_dir, roots[self.platform]),
            "Text Files (*.txt);;All Files (*)",
        )
        if not file_path:
            return ""
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                self._service.load_blocked_list_text(handle.read())
            return file_path
        except OSError as exc:
            self._log(f"[ERROR] Failed to load blocked list: {exc}")
            return ""

    @QtCore.Slot(result=str)
    def loadLatestBlockedList(self):
        path = self._service.use_latest_session()
        return path or ""

    @QtCore.Slot(str, str, result=bool)
    def copyModelReference(self, model_name, model_url):
        """Copy a row's handle or URL, honouring the auto-copy preference."""
        if not self._setting("copyOnOpen", True):
            return False
        source = (
            model_url
            if self._setting("copyMode", "username") == "url"
            else model_name
        )
        value = (source or "").strip()
        if not value:
            return False
        QtGui.QGuiApplication.clipboard().setText(value)
        return True

    @QtCore.Slot(str, result=bool)
    def openModelUrl(self, url):
        return self.openModelUrls([url])

    @QtCore.Slot("QStringList", result=bool)
    def openModelUrls(self, urls):
        """Open model pages in the configured browser, one Chrome tab batch."""
        targets = [
            str(url).strip() for url in urls or [] if str(url or "").strip()
        ]
        if not targets:
            return False
        if self._setting("openModelIn", "default") == "chrome":
            try:
                launch_urls_in_chrome(targets)
                return True
            except (OSError, ValueError) as exc:
                self._log(
                    f"[WARN] Chrome could not be launched ({exc}). "
                    "Falling back to the default browser."
                )
        opened = True
        for target in targets:
            if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(target)):
                opened = False
                self._log(f"[ERROR] Could not open URL: {target}")
        return opened

    @QtCore.Slot(str, str, result=bool)
    def openExternalSearch(self, provider_id, model_name):
        try:
            url = launch_external_search(
                provider_id,
                model_name,
                providers=self._provider_registry(),
            )
        except (OSError, ValueError) as exc:
            self._log(f"[ERROR] Could not open external search: {exc}")
            return False
        self._log(f"[INFO] Opened external search in Chrome: {url}")
        return True

    @QtCore.Slot(str, result=bool)
    def openAllExternalSearches(self, model_name):
        try:
            urls = launch_all_external_searches(
                model_name,
                providers=self._provider_registry(),
            )
        except (OSError, ValueError) as exc:
            self._log(f"[ERROR] Could not open external searches: {exc}")
            return False
        self._log(
            f"[INFO] Opened {len(urls)} external searches in Chrome "
            f"for {(model_name or '').strip()}."
        )
        return True

    @QtCore.Slot()
    def compileMasterList(self):
        self._service.compile_master_list()

    @QtCore.Slot(bool)
    def verifyMasterList(self, start_minimized):
        self._service.verify_master_list(
            start_minimized=start_minimized,
            manual_network_confirmed=self._setting("vpnProvider") == "manual",
        )

    @QtCore.Slot(str)
    def addManualToMaster(self, username):
        self._service.add_manual_to_master(username)

    @QtCore.Slot(str, bool)
    def deleteFromMasterList(self, model_input, block):
        self._service.delete_from_master_list(model_input, block=block)

    @QtCore.Slot(str)
    def sortMasterList(self, mode):
        self._service.sort_master_list(mode)
        if self._setting("rememberMasterListView", True):
            self.setSetting("masterListSortMode", mode)

    @QtCore.Slot(str)
    def setMasterListCountryFilter(self, country):
        self._service.set_master_list_country_filter(country)
        if self._setting("rememberMasterListView", True):
            self.setSetting("masterListCountryFilter", country)

    @QtCore.Slot(str, str)
    def updateMasterModelCountry(self, model_url, country):
        self._service.update_master_model_country(model_url, country)


def main():
    try:
        QQuickStyle.setStyle("Basic")
    except Exception:
        pass
    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("ModelScraper")
    app.setOrganizationDomain("modelscraper.local")
    app.setApplicationName("ModelScraper")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(
        base_dir, "ui", "assets", "generated", "app-icon.png"
    )
    if os.path.exists(icon_path):
        app.setWindowIcon(QtGui.QIcon(icon_path))
    controller = AppController(base_dir)
    engine = QtQml.QQmlApplicationEngine()
    engine.rootContext().setContextProperty("appController", controller)
    engine.load(
        QtCore.QUrl.fromLocalFile(os.path.join(base_dir, "ui", "Main.qml"))
    )
    if not engine.rootObjects():
        sys.exit(1)
    engine.rootObjects()[0].show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
