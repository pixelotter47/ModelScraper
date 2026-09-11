import asyncio
import os
import socket
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from app_settings import AppSettings, install_shared_settings
from country_choices import COUNTRY_CHOICES
from modelscraper_service import ModelScraperService
from mullvad_vpn import MULLVAD_LOCATIONS


class PlatformRequest(BaseModel):
    platform: str


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targetCountries: StrictStr | None = None
    targetLocationTerms: StrictStr | None = None
    vpnRelayLocation: StrictStr | None = None
    vpnProvider: StrictStr | None = None


PUBLIC_SETTING_KEYS = ("targetCountries", "targetLocationTerms", "vpnRelayLocation", "vpnProvider")


class HeadlessRequest(BaseModel):
    headless: bool | None = None
    manualNetworkConfirmed: StrictBool = False


class VerifyRequest(BaseModel):
    startMinimized: bool = True
    manualNetworkConfirmed: StrictBool = False


class FullAutoRequest(BaseModel):
    # None means "use the platform-safe default"; Stripchat resolves it to
    # a visible window and rejects an explicit True.
    headless: bool | None = None
    startMinimized: bool = True
    runSelection: str = "auto"


class TextUploadRequest(BaseModel):
    text: str


class ManualModelRequest(BaseModel):
    username: str


class DeleteModelRequest(BaseModel):
    model: str
    block: bool = False


class CountryRequest(BaseModel):
    model: str
    country: str


class SortRequest(BaseModel):
    mode: str


class CountryFilterRequest(BaseModel):
    country: str = "All"


def _state_response(service):
    return service.get_state()


def _error(status_code, code, message, **details):
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details},
    )


def _task_response(service, started):
    if not started:
        state = service.get_state()
        _error(
            409,
            "workflow_already_running",
            "Another ModelScraper task owns this workspace.",
            activeTask=state.get("currentTask"),
            platform=state.get("platform"),
        )
    return _state_response(service)


def _resolve_headless(service, requested):
    from modelscraper_service import UnsupportedBrowserMode

    state = service.get_state()
    try:
        return service.resolve_headless(state.get("platform"), requested)
    except UnsupportedBrowserMode:
        _error(
            422,
            "unsupported_browser_mode",
            (
                f"{state.get('platform')} verification requires a visible "
                "browser window."
            ),
        )


def create_app(service=None, base_dir=None):
    resolved_base_dir = Path(base_dir or Path(__file__).resolve().parent)
    app = FastAPI(title="ModelScraper Web", version="1.0.0")
    service_settings = getattr(service, "settings", None)
    settings = service_settings if isinstance(service_settings, AppSettings) else AppSettings(str(resolved_base_dir))
    install_shared_settings(settings)
    if service is None:
        # Install the same persisted settings the GUI uses, so both
        # surfaces resolve the Mullvad path, relay, and defaults
        # identically instead of the Web silently using bare defaults.
        service = ModelScraperService(str(resolved_base_dir), settings=settings)
        default_platform = settings.get("defaultPlatform")
        if default_platform:
            service.set_platform(default_platform)
    app.state.service = service
    app.state.settings = settings
    service.settings = settings

    static_dir = resolved_base_dir / "web_static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    ui_dir = resolved_base_dir / "ui"
    if ui_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(ui_dir)), name="ui")

    @app.get("/")
    def index():
        index_path = static_dir / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return HTMLResponse("<!doctype html><title>ModelScraper Web</title>")

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/state")
    def state():
        return _state_response(app.state.service)

    def current_preferences():
        try:
            return app.state.service.get_settings()
        except ValueError:
            _error(422, "invalid_settings", "Saved preferences are invalid. Correct the settings file before starting a task.")
        except OSError:
            _error(500, "settings_read_failed", "Saved preferences could not be read.")

    def public_settings():
        values = current_preferences()
        return {
            "settings": {key: values.get(key) for key in PUBLIC_SETTING_KEYS},
            "countryChoices": COUNTRY_CHOICES,
            "vpnLocations": [{"code": code, "name": name} for code, name in MULLVAD_LOCATIONS.items()],
        }

    @app.get("/api/settings")
    def get_settings():
        return public_settings()

    @app.put("/api/settings")
    def update_settings(request: SettingsRequest):
        updates = request.model_dump(exclude_unset=True)
        if not updates or any(value is None for value in updates.values()):
            _error(422, "invalid_settings", "Provide a country, location term, VPN provider, or relay setting as text.")
        try:
            if not app.state.service.update_settings(updates):
                _error(422, "invalid_settings", "Preferences could not be saved.")
        except RuntimeError:
            _error(409, "workflow_already_running", "Wait for the current task to finish before changing preferences.")
        except ValueError as error:
            _error(422, "invalid_settings", str(error))
        except OSError:
            _error(500, "settings_save_failed", "Preferences could not be saved to disk.")
        return public_settings()

    @app.get("/api/master-list")
    def master_list(
        offset: int = 0,
        limit: int = 200,
        query: str = "",
        sort: str | None = None,
        country: str | None = None,
    ):
        return app.state.service.query_master_list(
            offset=offset,
            limit=limit,
            query=query,
            sort=sort,
            country=country,
        )

    @app.post("/api/platform")
    def set_platform(request: PlatformRequest):
        if not app.state.service.set_platform(request.platform):
            raise HTTPException(status_code=422, detail="Invalid platform.")
        return _state_response(app.state.service)

    @app.post("/api/session/create")
    def create_session():
        app.state.service.create_session()
        return _state_response(app.state.service)

    @app.post("/api/session/latest")
    def use_latest_session():
        app.state.service.use_latest_session()
        return _state_response(app.state.service)

    @app.post("/api/session/open-folder")
    def open_session_folder():
        if not app.state.service.open_session_folder():
            raise HTTPException(status_code=422, detail="No session folder is available.")
        return _state_response(app.state.service)

    @app.post("/api/tasks/full-auto")
    def start_full_auto(request: FullAutoRequest):
        if request.runSelection not in ("auto", "resume", "new"):
            _error(
                422,
                "invalid_run_selection",
                "runSelection must be auto, resume, or new.",
            )
        service = app.state.service
        require_automatic_vpn()
        headless = _resolve_headless(service, request.headless)
        return _task_response(
            service,
            service.start_full_auto_flow(
                headless=headless,
                start_minimized=request.startMinimized,
                run_selection=request.runSelection,
            ),
        )

    @app.post("/api/tasks/resume")
    def resume_full_auto(request: FullAutoRequest):
        service = app.state.service
        require_automatic_vpn()
        if not service.get_state()["resume"]["available"]:
            _error(
                409,
                "resume_unavailable",
                "There is no resumable run for this platform and session.",
            )
        headless = _resolve_headless(service, request.headless)
        return _task_response(
            service,
            service.start_full_auto_flow(
                headless=headless,
                start_minimized=request.startMinimized,
                run_selection="resume",
            ),
        )

    @app.post("/api/machine-policy/restore")
    def restore_machine_policy():
        result = app.state.service.restore_machine_policy()
        if not result.get("ok") and result.get("error_code"):
            _error(
                409,
                result["error_code"],
                "Machine policy could not be restored.",
            )
        return {"result": result, "state": _state_response(app.state.service)}

    @app.post("/api/machine-policy/reconcile")
    def reconcile_machine_policy():
        result = app.state.service.reconcile_machine_policy_record()
        if not result.get("ok") and result.get("error_code"):
            _error(
                409,
                result["error_code"],
                "The restoration record could not be reconciled.",
            )
        return {"result": result, "state": _state_response(app.state.service)}

    def require_automatic_vpn():
        if current_preferences().get("vpnProvider") != "mullvad":
            _error(422, "automatic_vpn_required", "Full Auto requires Mullvad (automatic). Use individual steps with Manual / any VPN.")

    def require_manual_network_confirmation(confirmed):
        if current_preferences().get("vpnProvider") == "manual" and not confirmed:
            _error(422, "manual_network_confirmation_required", "Prepare the VPN or local network for this step, then confirm it with manualNetworkConfirmed: true.")

    @app.post("/api/tasks/step1")
    def start_step1(request: HeadlessRequest):
        require_manual_network_confirmation(request.manualNetworkConfirmed)
        headless = _resolve_headless(app.state.service, request.headless)
        return _task_response(
            app.state.service,
            app.state.service.start_vpn_list(headless=headless, manual_network_confirmed=request.manualNetworkConfirmed),
        )

    @app.post("/api/tasks/step2")
    def start_step2(request: HeadlessRequest):
        require_manual_network_confirmation(request.manualNetworkConfirmed)
        headless = _resolve_headless(app.state.service, request.headless)
        return _task_response(
            app.state.service,
            app.state.service.start_local_list(headless=headless, manual_network_confirmed=request.manualNetworkConfirmed),
        )

    @app.post("/api/tasks/step3")
    def start_step3():
        return _task_response(app.state.service, app.state.service.start_compare())

    @app.post("/api/tasks/step4")
    def start_step4(request: VerifyRequest):
        require_manual_network_confirmation(request.manualNetworkConfirmed)
        return _task_response(
            app.state.service,
            app.state.service.start_verify(start_minimized=request.startMinimized, manual_network_confirmed=request.manualNetworkConfirmed),
        )

    @app.post("/api/tasks/stop")
    def stop_current_task():
        app.state.service.stop_current_task()
        return _state_response(app.state.service)

    @app.post("/api/master-list/compile")
    def compile_master_list():
        return _task_response(app.state.service, app.state.service.compile_master_list())

    @app.post("/api/master-list/verify")
    def verify_master_list(request: VerifyRequest):
        require_manual_network_confirmation(request.manualNetworkConfirmed)
        return _task_response(
            app.state.service,
            app.state.service.verify_master_list(
                start_minimized=request.startMinimized,
                manual_network_confirmed=request.manualNetworkConfirmed,
            ),
        )

    @app.post("/api/master-list/manual")
    def add_manual_to_master(request: ManualModelRequest):
        if not app.state.service.add_manual_to_master(request.username):
            raise HTTPException(status_code=422, detail="Could not add model.")
        return _state_response(app.state.service)

    @app.delete("/api/master-list")
    def delete_from_master_list(request: DeleteModelRequest):
        if not app.state.service.delete_from_master_list(
            request.model,
            block=request.block,
        ):
            raise HTTPException(status_code=422, detail="Could not delete model.")
        return _state_response(app.state.service)

    @app.patch("/api/master-list/country")
    def update_master_model_country(request: CountryRequest):
        if not app.state.service.update_master_model_country(
            request.model,
            request.country,
        ):
            raise HTTPException(status_code=422, detail="Could not update country.")
        return _state_response(app.state.service)

    @app.post("/api/master-list/sort")
    def sort_master_list(request: SortRequest):
        if not app.state.service.sort_master_list(request.mode):
            raise HTTPException(status_code=422, detail="Invalid sort mode.")
        return _state_response(app.state.service)

    @app.post("/api/master-list/filter-country")
    def filter_master_list_country(request: CountryFilterRequest):
        app.state.service.set_master_list_country_filter(request.country)
        return _state_response(app.state.service)

    @app.post("/api/blocked-list/load-text")
    def load_blocked_list_text(request: TextUploadRequest):
        app.state.service.load_blocked_list_text(request.text)
        return _state_response(app.state.service)

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket):
        await websocket.accept()
        subscriber = app.state.service.subscribe()
        try:
            while True:
                event = await asyncio.to_thread(subscriber.get)
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            app.state.service.unsubscribe(subscriber)

    return app


def _is_port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def resolve_port():
    configured = os.environ.get("MODEL_SCRAPER_WEB_PORT")
    if configured:
        return int(configured)
    for port in range(8788, 8800):
        if _is_port_available(port):
            return port
    return 8788


def main():
    port = resolve_port()
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
