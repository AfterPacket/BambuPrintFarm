import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
import re
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Tuple

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from job_queue import JobQueue
from mqtt_client import PrinterManager, load_config
from slicer import auto_slice, load_slicer_config


# Auth is optional (enabled only when DASH_USER and/or DASH_PASS is set).
# Use auto_error=False so unauthenticated requests can be allowed when auth is not configured.
security = HTTPBasic(auto_error=False)
DASH_USER = os.getenv("DASH_USER")
DASH_PASS = os.getenv("DASH_PASS")
CONNECT_ON_START = os.getenv("CONNECT_ON_START", "1") != "0"
BASE_DIR = Path(__file__).resolve().parent

READY_EXTS = (".gcode", ".gcode.3mf")
# Files that *may* be slicable if an external slicer CLI is configured.
# Keep this list conservative; "auto-slice" is best-effort and depends on the slicer.
SLICABLE_EXTS = (".stl", ".obj", ".3mf")
PLATE_GCODE_RE = re.compile(r"^Metadata[\\/]plate_\d+\.gcode$", re.IGNORECASE)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
    if not DASH_USER and not DASH_PASS:
        return
    if not credentials:
        raise HTTPException(
            status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Basic"}
        )
    user_ok = secrets.compare_digest(credentials.username, DASH_USER or "")
    pass_ok = secrets.compare_digest(credentials.password, DASH_PASS or "")
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"}
        )


app = FastAPI()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

CONFIG_PATH = BASE_DIR / "config.json"
if not CONFIG_PATH.exists():
    CONFIG_PATH = BASE_DIR / "config.example.json"

config = load_config(str(CONFIG_PATH))
manager = PrinterManager(config)
queue = JobQueue(str(BASE_DIR / "jobs"))
slicer_config = load_slicer_config(str(CONFIG_PATH))

IDLE_STATE_TOKENS = ("idle", "ready", "finish", "completed", "standby")
BUSY_STATE_TOKENS = ("print", "running", "busy", "pause", "prepar", "calib", "heating", "homing")


def resolve_printer_id(printer_id: Optional[str]) -> str:
    if not printer_id:
        raise HTTPException(status_code=400, detail="printer_id is required")
    return printer_id


def get_service_or_404(printer_id: Optional[str]):
    pid = resolve_printer_id(printer_id)
    try:
        return manager.get_service(pid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def normalize_state(value: Optional[str]) -> str:
    if not value:
        return ""
    return str(value).lower()


def is_printer_available(status: dict) -> bool:
    if not status.get("connected"):
        return False
    state = normalize_state(status.get("printer_state"))
    if any(token in state for token in BUSY_STATE_TOKENS):
        return False
    if any(token in state for token in IDLE_STATE_TOKENS):
        return True
    # Many Bambu firmwares report gcode_state=FAILED after a user stop/cancel,
    # while still being able to start the next job. Treat this as available
    # only when there are no non-zero error indicators.
    if "failed" in state:
        try:
            code = status.get("print_error_code")
            code_ok = int(code) == 0
        except Exception:
            code_ok = False
        try:
            mc = status.get("mc_print_error_code")
            mc_ok = (mc is None) or int(mc) == 0
        except Exception:
            mc_ok = False
        hms = status.get("hms")
        hms_ok = (hms is None) or (isinstance(hms, list) and len(hms) == 0)
        if code_ok and mc_ok and hms_ok:
            return True
    return False


def ensure_not_printing(printer_id: str) -> None:
    status = manager.get_status(printer_id)
    if not status.get("connected"):
        raise HTTPException(status_code=409, detail="Printer not connected")
    state = normalize_state(status.get("printer_state"))
    if any(token in state for token in BUSY_STATE_TOKENS):
        raise HTTPException(status_code=409, detail="Action blocked while printer is busy")


def is_ready_file(filename: str) -> bool:
    lower = filename.lower()
    return lower.endswith(READY_EXTS)


def is_slicable_file(filename: str) -> bool:
    lower = filename.lower()
    if lower.endswith(".gcode.3mf"):
        return False
    return lower.endswith(SLICABLE_EXTS)

def is_presliced_3mf_upload(file: UploadFile) -> bool:
    """
    Detect "sliced 3MF" containers (same content as .gcode.3mf) by looking
    for Metadata/plate_N.gcode inside the archive.
    """
    filename = (file.filename or "").lower()
    if not filename.endswith(".3mf") or filename.endswith(".gcode.3mf"):
        return False
    fileobj = file.file
    if not hasattr(fileobj, "seek"):
        return False
    try:
        fileobj.seek(0)
        with zipfile.ZipFile(fileobj) as zf:
            for name in zf.namelist():
                if PLATE_GCODE_RE.match(name):
                    return True
    except zipfile.BadZipFile:
        return False
    finally:
        try:
            fileobj.seek(0)
        except OSError:
            pass
    return False


def validate_upload_file(file: UploadFile) -> dict:
    filename = file.filename or ""
    if is_ready_file(filename):
        return {"presliced_3mf": False}
    presliced_3mf = is_presliced_3mf_upload(file)
    if presliced_3mf:
        return {"presliced_3mf": True}
    if slicer_config.enabled and is_slicable_file(filename):
        return {"presliced_3mf": False}
    slicable = ", ".join(SLICABLE_EXTS)
    raise HTTPException(
        status_code=400,
        detail=(
            "Unsupported file type. Upload .gcode, .gcode.3mf, or a sliced .3mf. "
            f"If auto-slice is enabled, supported model uploads are: {slicable}."
        ),
    )


def save_upload_to_path(file: UploadFile, path: str) -> None:
    file.file.seek(0)
    with open(path, "wb") as handle:
        shutil.copyfileobj(file.file, handle)


def slice_upload(file: UploadFile) -> Tuple[str, str, str]:
    tmpdir = tempfile.mkdtemp(prefix="bambu_slice_")
    input_path = str(Path(tmpdir) / Path(file.filename).name)
    save_upload_to_path(file, input_path)
    output_path = auto_slice(slicer_config, input_path, tmpdir)
    output_name = Path(output_path).name
    return output_path, output_name, tmpdir


class Dispatcher:
    def __init__(self, interval_sec: float) -> None:
        self._interval_sec = interval_sec
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_error: Optional[str] = None
        self._last_dispatch_at: Optional[float] = None
        self._last_result: Optional[dict] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            self._last_dispatch_at = time.time()
            try:
                self._last_result = self.dispatch_once()
                self._last_error = None
            except Exception as exc:  # noqa: BLE001
                # Never let the dispatcher thread die silently.
                self._last_error = str(exc)
            time.sleep(self._interval_sec)

    def dispatch_once(self) -> dict:
        summary = {
            "queued": 0,
            "available_printers": [],
            "dispatched": [],
            "failed": [],
            "skipped": [],
        }
        statuses = {item["id"]: item for item in manager.list_printers()}

        # If polling isn't running (or a printer dropped), attempt to connect
        # on-demand so jobs can still dispatch.
        for pid, st in list(statuses.items()):
            needs_refresh = (not st.get("connected")) or (st.get("printer_state") is None)
            if not needs_refresh:
                continue
            try:
                manager.get_service(pid).test_connection(force=False)
            except Exception:
                pass
        statuses = {item["id"]: item for item in manager.list_printers()}

        # Opportunistically update running jobs based on printer state.
        for job in queue.list_jobs(status="running"):
            pid = job.get("assigned_printer_id")
            if not pid or pid not in statuses:
                continue
            st = statuses[pid]
            state = normalize_state(st.get("printer_state"))
            if any(token in state for token in IDLE_STATE_TOKENS):
                queue.mark_completed(job["id"])
                continue
            if "failed" in state:
                err = st.get("print_error_code")
                fail_reason = st.get("fail_reason")
                mc = st.get("mc_print_error_code")
                details = []
                if err not in (None, 0, "0"):
                    details.append(f"print_error_code={err}")
                if mc not in (None, 0, "0"):
                    details.append(f"mc_print_error_code={mc}")
                if fail_reason not in (None, "", "0", 0):
                    details.append(f"fail_reason={fail_reason}")
                msg = "printer entered FAILED"
                if details:
                    msg += " (" + ", ".join(details) + ")"
                queue.mark_failed(job["id"], msg)

        jobs = queue.list_jobs(status="queued")
        if not jobs:
            return summary
        summary["queued"] = len(jobs)
        available = [pid for pid, st in statuses.items() if is_printer_available(st)]
        summary["available_printers"] = list(available)
        if not available:
            summary["skipped"].append(
                {
                    "reason": "no_available_printers",
                    "printers": [
                        {
                            "id": st.get("id"),
                            "connected": st.get("connected"),
                            "printer_state": st.get("printer_state"),
                            "print_status": st.get("print_status"),
                            "print_error_code": st.get("print_error_code"),
                            "fail_reason": st.get("fail_reason"),
                            "mc_print_error_code": st.get("mc_print_error_code"),
                        }
                        for st in statuses.values()
                    ],
                }
            )
            return summary

        for job in jobs:
            target = job.get("printer_id")
            if target:
                candidate = target if target in available else None
            else:
                candidate = available[0] if available else None
            if not candidate:
                summary["skipped"].append(
                    {"job_id": job.get("id"), "reason": "no_available_printer"}
                )
                continue
            if not queue.mark_dispatching(job["id"], candidate):
                summary["skipped"].append(
                    {"job_id": job.get("id"), "reason": "not_queued"}
                )
                continue
            service = manager.get_service(candidate)
            try:
                if not os.path.exists(job["filepath"]):
                    raise FileNotFoundError(job["filepath"])
                service.upload_file_path(job["filepath"], job["filename"])
                ok = service.start_print(job["filename"], plate=job["plate"])
                if ok:
                    queue.mark_running(job["id"])
                    summary["dispatched"].append(
                        {"job_id": job.get("id"), "printer_id": candidate}
                    )
                else:
                    queue.mark_failed(job["id"], "start_print returned false")
                    summary["failed"].append(
                        {
                            "job_id": job.get("id"),
                            "printer_id": candidate,
                            "error": "start_print returned false",
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                queue.mark_failed(job["id"], str(exc))
                summary["failed"].append(
                    {"job_id": job.get("id"), "printer_id": candidate, "error": str(exc)}
                )
            if candidate in available and not target:
                available.remove(candidate)
            if not available:
                break
        return summary


dispatcher = Dispatcher(config.dispatch_interval_sec)


@app.on_event("startup")
def on_startup() -> None:
    if CONNECT_ON_START:
        manager.start_all()
    dispatcher.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    dispatcher.stop()
    manager.stop_all()


@app.get("/", dependencies=[Depends(require_auth)])
def index() -> FileResponse:
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.get("/api/printers", dependencies=[Depends(require_auth)])
def printers() -> list:
    return manager.list_printers()


@app.get("/api/status", dependencies=[Depends(require_auth)])
def status(printer_id: Optional[str] = Query(default=None)) -> dict:
    return manager.get_status(printer_id)


@app.get("/api/ams", dependencies=[Depends(require_auth)])
def ams_status(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return service.get_ams()


@app.get("/api/diag", dependencies=[Depends(require_auth)])
def diag(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return service.test_connection()


@app.post("/api/diag/commands", dependencies=[Depends(require_auth)])
def diag_commands(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    status = manager.get_status(printer_id)
    busy = bool(status) and not is_printer_available(status)

    results = {
        "connected": bool(status.get("connected")),
        "busy": busy,
        "printer_state": status.get("printer_state"),
    }

    # Safe light diagnostics
    results["logo_light_on"] = service.light_on()
    results["logo_light_off"] = service.light_off()
    results["chamber_light_on"] = service.chamber_light_on()
    results["chamber_light_off"] = service.chamber_light_off()

    # Pause/stop are safe to test while running; resume only if paused.
    results["pause"] = service.pause()
    results["stop"] = service.stop_print()
    status_after = manager.get_status(printer_id)
    state_after = normalize_state(status_after.get("printer_state"))
    if "pause" in state_after:
        results["resume"] = service.resume()
    else:
        results["resume"] = "skipped_not_paused"

    return results


@app.get("/api/diag/ams_raw", dependencies=[Depends(require_auth)])
def diag_ams_raw(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    dump = service.get_mqtt_dump()
    print_section = dump.get("print", {}) if isinstance(dump, dict) else {}
    return {
        "print_keys": list(print_section.keys()) if isinstance(print_section, dict) else [],
        "ams": print_section.get("ams") if isinstance(print_section, dict) else None,
        "ams_root": dump.get("ams") if isinstance(dump, dict) else None,
        "ams_raw_print": print_section.get("ams") if isinstance(print_section, dict) else None,
    }


@app.get("/api/diag/state", dependencies=[Depends(require_auth)])
def diag_state(printer_id: Optional[str] = Query(default=None)) -> dict:
    """
    Minimal, safe printer fault/state dump for troubleshooting.
    """
    service = get_service_or_404(printer_id)
    dump = service.get_mqtt_dump()
    print_section = dump.get("print", {}) if isinstance(dump, dict) else {}
    info_section = dump.get("info", {}) if isinstance(dump, dict) else {}

    def pick(section: dict, keys: tuple[str, ...]) -> dict:
        out = {}
        for key in keys:
            if key in section:
                out[key] = section.get(key)
        return out

    return {
        "ok": True,
        "print": pick(
            print_section if isinstance(print_section, dict) else {},
            (
                "gcode_state",
                "state",
                "stg",
                "stg_cur",
                "mc_print_stage",
                "mc_print_sub_stage",
                "mc_stage",
                "mc_err",
                "mc_print_error_code",
                "print_error",
                "fail_reason",
                "err",
                "hms",
                "gcode_file",
                "subtask_name",
                "percent",
                "remain_time",
                "nozzle_diameter",
                "nozzle_type",
            ),
        ),
        "info": pick(
            info_section if isinstance(info_section, dict) else {},
            (
                "model_id",
                "ver",
                "ip",
                "sn",
            ),
        ),
    }

@app.post("/api/pause", dependencies=[Depends(require_auth)])
def pause(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return {"ok": service.pause()}


@app.post("/api/resume", dependencies=[Depends(require_auth)])
def resume(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return {"ok": service.resume()}


@app.post("/api/stop", dependencies=[Depends(require_auth)])
def stop_print(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return {"ok": service.stop_print()}

@app.post("/api/fault/clear", dependencies=[Depends(require_auth)])
def clear_fault(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return service.clear_failed_state()


@app.post("/api/light/on", dependencies=[Depends(require_auth)])
def light_on(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return {"ok": service.light_on()}


@app.post("/api/light/off", dependencies=[Depends(require_auth)])
def light_off(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return {"ok": service.light_off()}


@app.post("/api/light/chamber/on", dependencies=[Depends(require_auth)])
def chamber_light_on(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return {"ok": service.chamber_light_on()}


@app.post("/api/light/chamber/off", dependencies=[Depends(require_auth)])
def chamber_light_off(printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return {"ok": service.chamber_light_off()}


class JogRequest(BaseModel):
    dx: float = 0
    dy: float = 0
    dz: float = 0
    feed: int = 3000


@app.post("/api/jog", dependencies=[Depends(require_auth)])
def jog(body: JogRequest, printer_id: Optional[str] = Query(default=None)) -> dict:
    pid = resolve_printer_id(printer_id)
    ensure_not_printing(pid)
    service = get_service_or_404(pid)
    return {"ok": service.jog(body.dx, body.dy, body.dz, body.feed)}


@app.post("/api/jog/home", dependencies=[Depends(require_auth)])
def jog_home(printer_id: Optional[str] = Query(default=None)) -> dict:
    pid = resolve_printer_id(printer_id)
    ensure_not_printing(pid)
    service = get_service_or_404(pid)
    return {"ok": service.home()}


def broadcast(action):
    results = {}
    for item in manager.list_printers():
        pid = item["id"]
        try:
            ok = bool(action(manager.get_service(pid)))
            results[pid] = {"ok": ok}
        except Exception as exc:  # noqa: BLE001
            results[pid] = {"ok": False, "error": str(exc)}
    return results


@app.post("/api/broadcast/pause", dependencies=[Depends(require_auth)])
def broadcast_pause() -> dict:
    return {"results": broadcast(lambda svc: svc.pause())}


@app.post("/api/broadcast/resume", dependencies=[Depends(require_auth)])
def broadcast_resume() -> dict:
    return {"results": broadcast(lambda svc: svc.resume())}


@app.post("/api/broadcast/stop", dependencies=[Depends(require_auth)])
def broadcast_stop() -> dict:
    return {"results": broadcast(lambda svc: svc.stop_print())}


@app.post("/api/broadcast/light/on", dependencies=[Depends(require_auth)])
def broadcast_light_on() -> dict:
    return {"results": broadcast(lambda svc: svc.light_on())}


@app.post("/api/broadcast/light/off", dependencies=[Depends(require_auth)])
def broadcast_light_off() -> dict:
    return {"results": broadcast(lambda svc: svc.light_off())}


@app.post("/api/broadcast/light/chamber/on", dependencies=[Depends(require_auth)])
def broadcast_chamber_light_on() -> dict:
    return {"results": broadcast(lambda svc: svc.chamber_light_on())}


@app.post("/api/broadcast/light/chamber/off", dependencies=[Depends(require_auth)])
def broadcast_chamber_light_off() -> dict:
    return {"results": broadcast(lambda svc: svc.chamber_light_off())}


class TempRequest(BaseModel):
    bed: Optional[int] = None
    nozzle: Optional[int] = None


class FanRequest(BaseModel):
    part: Optional[int] = Field(default=None, ge=0, le=100)
    aux: Optional[int] = Field(default=None, ge=0, le=100)
    chamber: Optional[int] = Field(default=None, ge=0, le=100)


class AmsSelectRequest(BaseModel):
    ams_id: int
    tray_id: int


@app.post("/api/temps", dependencies=[Depends(require_auth)])
def set_temps(body: TempRequest, printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return {"ok": True, "results": service.set_temps(body.bed, body.nozzle)}


@app.post("/api/fans", dependencies=[Depends(require_auth)])
def set_fans(body: FanRequest, printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    return {
        "ok": True,
        "results": service.set_fans(body.part, body.aux, body.chamber),
    }


@app.post("/api/broadcast/fans", dependencies=[Depends(require_auth)])
def broadcast_fans(body: FanRequest) -> dict:
    return {"results": broadcast(lambda svc: svc.set_fans(body.part, body.aux, body.chamber))}


@app.post("/api/ams/select", dependencies=[Depends(require_auth)])
def ams_select(body: AmsSelectRequest, printer_id: Optional[str] = Query(default=None)) -> dict:
    service = get_service_or_404(printer_id)
    try:
        result = service.select_ams_tray(body.ams_id, body.tray_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "result": result}


@app.post("/api/upload", dependencies=[Depends(require_auth)])
async def upload_file(
    file: UploadFile = File(...),
    start: bool = False,
    plate: int = 1,
    printer_id: Optional[str] = Query(default=None),
) -> dict:
    upload_meta = validate_upload_file(file)
    presliced_3mf = bool(upload_meta.get("presliced_3mf"))
    service = get_service_or_404(printer_id)
    started = False
    sliced = False
    filename = file.filename
    tmpdir: Optional[str] = None
    try:
        if is_slicable_file(filename) and not presliced_3mf:
            output_path, output_name, tmpdir = slice_upload(file)
            upload_result = service.upload_file_path(output_path, output_name)
            filename = output_name
            sliced = True
        else:
            upload_result = service.upload_file_obj(file.file, filename)
        if start:
            started = service.start_print(filename, plate=plate)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return {"upload_result": upload_result, "started": started, "sliced": sliced, "filename": filename}


@app.post("/api/start", dependencies=[Depends(require_auth)])
def start_print(filename: str, plate: int = 1, printer_id: Optional[str] = Query(default=None)) -> dict:
    lower = filename.lower()
    if not (is_ready_file(filename) or lower.endswith(".3mf")):
        raise HTTPException(status_code=400, detail="start requires .gcode, .gcode.3mf, or .3mf filename")
    service = get_service_or_404(printer_id)
    return {"ok": service.start_print(filename, plate=plate)}


@app.get("/api/jobs", dependencies=[Depends(require_auth)])
def list_jobs() -> list:
    return queue.list_jobs()


@app.post("/api/jobs", dependencies=[Depends(require_auth)])
async def enqueue_job(
    file: UploadFile = File(...),
    plate: int = 1,
    printer_id: Optional[str] = Query(default=None),
    auto_assign: bool = True,
) -> dict:
    upload_meta = validate_upload_file(file)
    presliced_3mf = bool(upload_meta.get("presliced_3mf"))
    if not auto_assign and not printer_id:
        raise HTTPException(status_code=400, detail="printer_id required when auto_assign is false")
    target = None if auto_assign else printer_id
    filename = file.filename
    fileobj = file.file
    tmpdir: Optional[str] = None
    try:
        if is_slicable_file(filename) and not presliced_3mf:
            output_path, output_name, tmpdir = slice_upload(file)
            filename = output_name
            fileobj = open(output_path, "rb")
        job = queue.enqueue(
            filename=filename,
            fileobj=fileobj,
            plate=plate,
            printer_id=target,
            auto_assign=auto_assign,
        )
        # Kick the dispatcher immediately so the queue feels responsive even
        # if the periodic loop is delayed.
        threading.Thread(target=dispatcher.dispatch_once, daemon=True).start()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if hasattr(fileobj, "close") and fileobj is not file.file:
            fileobj.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return asdict(job)


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_job(job_id: str) -> dict:
    return {"ok": queue.mark_canceled(job_id)}


@app.post("/api/jobs/{job_id}/complete", dependencies=[Depends(require_auth)])
def complete_job(job_id: str) -> dict:
    return {"ok": queue.mark_completed(job_id)}


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
def remove_job(job_id: str) -> dict:
    return {"ok": queue.remove_job(job_id)}


@app.get("/api/dispatch/status", dependencies=[Depends(require_auth)])
def dispatch_status() -> dict:
    thread_alive = bool(dispatcher._thread and dispatcher._thread.is_alive())
    printers = []
    for st in manager.list_printers():
        try:
            available = is_printer_available(st)
        except Exception:
            available = False
        printers.append(
            {
                "id": st.get("id"),
                "name": st.get("name"),
                "connected": st.get("connected"),
                "printer_state": st.get("printer_state"),
                "print_status": st.get("print_status"),
                "print_error_code": st.get("print_error_code"),
                "fail_reason": st.get("fail_reason"),
                "mc_print_error_code": st.get("mc_print_error_code"),
                "gcode_file": st.get("gcode_file"),
                "subtask_name": st.get("subtask_name"),
                "available": available,
            }
        )
    return {
        "running": bool(dispatcher._running),
        "thread_alive": thread_alive,
        "interval_sec": config.dispatch_interval_sec,
        "last_dispatch_at": dispatcher._last_dispatch_at,
        "last_error": dispatcher._last_error,
        "last_result": dispatcher._last_result,
        "printers": printers,
        "jobs": {
            "queued": len(queue.list_jobs(status="queued")),
            "dispatching": len(queue.list_jobs(status="dispatching")),
            "running": len(queue.list_jobs(status="running")),
            "failed": len(queue.list_jobs(status="failed")),
        },
    }


@app.post("/api/dispatch/once", dependencies=[Depends(require_auth)])
def dispatch_once() -> dict:
    try:
        result = dispatcher.dispatch_once()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "result": result}


def mjpeg_stream(url: str):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=501, detail="ffmpeg not installed")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-timeout",
        "5000000",
        "-analyzeduration",
        "0",
        "-probesize",
        "32",
        "-fflags",
        "nobuffer",
        "-i",
        url,
        "-an",
        "-vf",
        "scale=960:-1",
        "-r",
        "5",
        "-f",
        "mjpeg",
        "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    if proc.stdout is None:
        raise HTTPException(status_code=500, detail="ffmpeg failed to start")
    # If ffmpeg exits immediately (bad flags, auth errors, etc.), surface the
    # error instead of returning a silent black/empty feed.
    time.sleep(0.15)
    if proc.poll() is not None:
        try:
            stderr = (proc.stderr.read() if proc.stderr else b"")  # type: ignore[union-attr]
        except OSError:
            stderr = b""
        msg = stderr.decode("utf-8", errors="ignore").strip()
        if not msg:
            msg = f"ffmpeg exited with code {proc.returncode}"
        raise HTTPException(status_code=500, detail=msg[:800])

    boundary = b"--frame\r\n"
    buffer = b""

    def generate():
        nonlocal buffer
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    start = buffer.find(b"\xff\xd8")
                    if start == -1:
                        if len(buffer) > 2_000_000:
                            buffer = buffer[-2_000_000:]
                        break
                    end = buffer.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        if start > 0:
                            buffer = buffer[start:]
                        break
                    frame = buffer[start : end + 2]
                    buffer = buffer[end + 2 :]
                    headers = (
                        boundary
                        + b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(frame)}\r\n\r\n".encode("utf-8")
                    )
                    yield headers + frame + b"\r\n"
        finally:
            try:
                proc.kill()
            except OSError:
                pass

    return generate()


def mjpeg_snapshot(url: str, timeout_sec: float = 8.0) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=501, detail="ffmpeg not installed")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-timeout",
        "5000000",
        "-analyzeduration",
        "0",
        "-probesize",
        "32",
        "-i",
        url,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Camera snapshot timed out") from exc
    if result.returncode != 0 or not result.stdout:
        msg = (result.stderr or b"").decode("utf-8", errors="ignore").strip()
        if not msg:
            msg = f"ffmpeg exited with code {result.returncode}"
        raise HTTPException(status_code=500, detail=msg[:800])
    return result.stdout


@app.get("/api/camera", dependencies=[Depends(require_auth)])
def camera(printer_id: Optional[str] = Query(default=None)) -> StreamingResponse:
    service = get_service_or_404(printer_id)
    url = service.get_camera_url()
    if not url:
        raise HTTPException(status_code=404, detail="Camera disabled or not configured")
    return StreamingResponse(
        mjpeg_stream(url),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/camera/snapshot", dependencies=[Depends(require_auth)])
def camera_snapshot(printer_id: Optional[str] = Query(default=None)) -> Response:
    service = get_service_or_404(printer_id)
    url = service.get_camera_url()
    if not url:
        raise HTTPException(status_code=404, detail="Camera disabled or not configured")
    frame = mjpeg_snapshot(url)
    return Response(content=frame, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.get("/api/camera/diag", dependencies=[Depends(require_auth)])
def camera_diag(printer_id: Optional[str] = Query(default=None)) -> JSONResponse:
    service = get_service_or_404(printer_id)
    url = service.get_camera_url()
    if not url:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Camera disabled or not configured"})
    try:
        frame = mjpeg_snapshot(url, timeout_sec=6.0)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.detail})
    return JSONResponse(content={"ok": True, "bytes": len(frame)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api_server:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "0") == "1",
    )
