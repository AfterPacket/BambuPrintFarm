from __future__ import annotations

import json
import ssl
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

import bambulabs_api as bl
import paho.mqtt.client as mqtt
from bambulabs_api.filament_info import AMSFilamentSettings
from bambulabs_api.states_info import GcodeState, PrintStatus

DEFAULT_CAMERA_PROTOCOL = "rtsps"
DEFAULT_CAMERA_PORT = 322
DEFAULT_CAMERA_PATH = "/streaming/live/1"
DEFAULT_CAMERA_USER = "bblp"


@dataclass
class PrinterConfig:
    printer_id: str
    name: str
    printer_ip: str
    serial: str
    access_code: str
    camera_enabled: bool
    camera_protocol: str
    camera_port: int
    camera_path: str
    camera_user: str
    camera_url: Optional[str]


@dataclass
class FarmConfig:
    poll_interval_sec: float
    dispatch_interval_sec: float
    printers: List[PrinterConfig]


def load_config(path: str = "config.json") -> FarmConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    poll_interval = float(data.get("poll_interval_sec", 2.0))
    dispatch_interval = float(data.get("dispatch_interval_sec", 3.0))
    printers: List[PrinterConfig] = []
    for item in data.get("printers", []):
        printers.append(
            PrinterConfig(
                printer_id=item["id"],
                name=item.get("name", item["id"]),
                printer_ip=item["printer_ip"],
                serial=item["serial"],
                access_code=item["access_code"],
                camera_enabled=bool(item.get("camera_enabled", True)),
                camera_protocol=item.get("camera_protocol", DEFAULT_CAMERA_PROTOCOL),
                camera_port=int(item.get("camera_port", DEFAULT_CAMERA_PORT)),
                camera_path=item.get("camera_path", DEFAULT_CAMERA_PATH),
                camera_user=item.get("camera_user", DEFAULT_CAMERA_USER),
                camera_url=item.get("camera_url") or None,
            )
        )
    return FarmConfig(
        poll_interval_sec=poll_interval,
        dispatch_interval_sec=dispatch_interval,
        printers=printers,
    )


class PrinterService:
    def __init__(self, config: PrinterConfig, poll_interval_sec: float) -> None:
        self._config = config
        self._poll_interval_sec = poll_interval_sec
        self._printer: Optional[bl.Printer] = None
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._selected_ams: Optional[dict[str, int]] = None
        self._status: Dict[str, Any] = {
            "id": config.printer_id,
            "name": config.name,
            "connected": False,
            "last_update": None,
            "last_error": None,
            "printer_state": None,
            "print_status": None,
            "filament_runout": None,
            "sequence_id": None,
            "print_error_code": None,
            "print_error_raw": None,
            "fail_reason": None,
            "mc_print_error_code": None,
            "hms": None,
            "gcode_file": None,
            "subtask_name": None,
            "selected_ams": None,
            "last_start_ams_mapping": None,
            "part_fan_percent": None,
            "aux_fan_percent": None,
            "chamber_fan_percent": None,
            "percentage": None,
            "bed_temp": None,
            "nozzle_temp": None,
            "remaining_time": None,
            "camera_enabled": config.camera_enabled,
            "light_state": None,
        }
        self._next_connect_time = 0.0
        self._backoff_sec = 2.0

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            if self._printer:
                try:
                    self._printer.disconnect()
                except Exception:
                    pass
            self._status["connected"] = False

    def get_camera_url(self) -> Optional[str]:
        if not self._config.camera_enabled:
            return None
        if self._config.camera_url:
            return self._config.camera_url
        protocol = self._config.camera_protocol
        user = self._config.camera_user
        path = self._config.camera_path
        if not path.startswith("/"):
            path = "/" + path
        return (
            f"{protocol}://{user}:{self._config.access_code}@"
            f"{self._config.printer_ip}:{self._config.camera_port}{path}"
        )

    def test_connection(self, force: bool = True) -> Dict[str, Any]:
        with self._lock:
            ok = self._ensure_connected(force=force)
            if ok and self._printer:
                try:
                    self._status["printer_state"] = self._printer.get_state()
                    status = self._printer.get_current_state()
                    self._status["print_status"] = str(status)
                    self._status["filament_runout"] = (
                        status == PrintStatus.PAUSED_FILAMENT_RUNOUT
                    )
                    self._status["sequence_id"] = self._get_sequence_id()
                    try:
                        self._status["print_error_code"] = int(
                            self._printer.print_error_code()
                        )
                    except Exception:
                        self._status["print_error_code"] = None

                    # Capture fault detail directly from the MQTT dump (local state),
                    # since some fields are not always parseable as ints.
                    try:
                        dump = self._printer.mqtt_dump()
                        print_section = dump.get("print", {}) if isinstance(dump, dict) else {}
                        if isinstance(print_section, dict):
                            self._status["print_error_raw"] = print_section.get("print_error")
                            self._status["fail_reason"] = print_section.get("fail_reason")
                            self._status["mc_print_error_code"] = print_section.get(
                                "mc_print_error_code"
                            )
                            hms = print_section.get("hms")
                            if isinstance(hms, list):
                                self._status["hms"] = hms[:10]
                            else:
                                self._status["hms"] = hms
                    except Exception:
                        self._status["print_error_raw"] = None
                        self._status["fail_reason"] = None
                        self._status["mc_print_error_code"] = None
                        self._status["hms"] = None

                    if self._status.get("print_error_code") is None:
                        raw = self._status.get("print_error_raw")
                        try:
                            if raw is not None:
                                self._status["print_error_code"] = int(raw)
                        except Exception:
                            pass
                    try:
                        self._status["gcode_file"] = self._printer.gcode_file()
                    except Exception:
                        self._status["gcode_file"] = None
                    try:
                        self._status["subtask_name"] = self._printer.subtask_name()
                    except Exception:
                        self._status["subtask_name"] = None
                    self._status["percentage"] = self._printer.get_percentage()
                    self._status["bed_temp"] = self._printer.get_bed_temperature()
                    self._status["nozzle_temp"] = self._printer.get_nozzle_temperature()
                    self._status["remaining_time"] = self._printer.get_time()
                    self._status["light_state"] = self._printer.get_light_state()
                    self._status["last_update"] = time.time()
                except Exception:
                    # Leave existing status fields as-is; the connection itself is
                    # still considered ok.
                    pass
            return {
                "ok": ok,
                "last_error": self._status.get("last_error"),
                "connected": self._status.get("connected"),
            }

    def _ensure_connected(self, force: bool = False) -> bool:
        now = time.time()
        if not force and now < self._next_connect_time:
            return False
        if self._printer is None:
            self._printer = bl.Printer(
                self._config.printer_ip,
                self._config.access_code,
                self._config.serial,
            )
        if not self._status["connected"]:
            try:
                # We only need MQTT for status + control; the dashboard handles
                # camera streaming separately via ffmpeg.
                self._printer.mqtt_start()
                # mqtt_start() uses connect_async(), so wait briefly for the
                # broker session to be usable before we declare "connected".
                ready = False
                for _ in range(15):  # ~3s
                    try:
                        self._printer.get_state()
                        ready = True
                        break
                    except Exception:
                        time.sleep(0.2)
                if not ready:
                    raise RuntimeError("MQTT connection not ready")
                try:
                    self._printer.mqtt_client.pushall()
                except Exception:
                    pass
                self._status["connected"] = True
                self._status["last_error"] = None
                self._backoff_sec = 2.0
                self._next_connect_time = 0.0
            except Exception as exc:  # noqa: BLE001
                self._status["last_error"] = str(exc)
                self._status["connected"] = False
                self._next_connect_time = now + self._backoff_sec
                self._backoff_sec = min(self._backoff_sec * 2, 60.0)
                return False
        return True

    def _poll_loop(self) -> None:
        while self._running:
            with self._lock:
                if not self._ensure_connected():
                    time.sleep(self._poll_interval_sec)
                    continue
                try:
                    self._status["printer_state"] = self._printer.get_state()
                    status = self._printer.get_current_state()
                    self._status["print_status"] = str(status)
                    self._status["filament_runout"] = (
                        status == PrintStatus.PAUSED_FILAMENT_RUNOUT
                    )
                    self._status["sequence_id"] = self._get_sequence_id()
                    try:
                        self._status["print_error_code"] = int(
                            self._printer.print_error_code()
                        )
                    except Exception:
                        self._status["print_error_code"] = None

                    try:
                        dump = self._printer.mqtt_dump()
                        print_section = dump.get("print", {}) if isinstance(dump, dict) else {}
                        if isinstance(print_section, dict):
                            self._status["print_error_raw"] = print_section.get("print_error")
                            self._status["fail_reason"] = print_section.get("fail_reason")
                            self._status["mc_print_error_code"] = print_section.get(
                                "mc_print_error_code"
                            )
                            hms = print_section.get("hms")
                            if isinstance(hms, list):
                                self._status["hms"] = hms[:10]
                            else:
                                self._status["hms"] = hms
                    except Exception:
                        self._status["print_error_raw"] = None
                        self._status["fail_reason"] = None
                        self._status["mc_print_error_code"] = None
                        self._status["hms"] = None

                    if self._status.get("print_error_code") is None:
                        raw = self._status.get("print_error_raw")
                        try:
                            if raw is not None:
                                self._status["print_error_code"] = int(raw)
                        except Exception:
                            pass
                    self._status["percentage"] = self._printer.get_percentage()
                    self._status["bed_temp"] = self._printer.get_bed_temperature()
                    self._status["nozzle_temp"] = self._printer.get_nozzle_temperature()
                    self._status["remaining_time"] = self._printer.get_time()
                    self._status["light_state"] = self._printer.get_light_state()
                    self._status["last_error"] = None
                    self._status["last_update"] = time.time()
                except Exception as exc:  # noqa: BLE001
                    self._status["last_error"] = str(exc)
                    self._status["connected"] = False
            time.sleep(self._poll_interval_sec)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def get_mqtt_dump(self) -> Dict[str, Any]:
        with self._lock:
            if not self._ensure_connected(force=True):
                raise RuntimeError("Printer not connected")
            return self._printer.mqtt_dump()

    def get_ams(self) -> Dict[str, Any]:
        with self._lock:
            if not self._ensure_connected(force=True):
                raise RuntimeError("Printer not connected")
            # Ask for a fresh state to populate AMS details
            try:
                self._printer.mqtt_client.pushall()
            except Exception:
                pass
            time.sleep(0.2)
            hub = self._printer.ams_hub()
            ams_list: list[dict[str, Any]] = []
            for ams_id, ams in hub.ams_hub.items():
                trays = []
                for tray_id, tray in ams.filament_trays.items():
                    trays.append(
                        {
                            "tray_id": tray_id,
                            "tray_id_name": tray.tray_id_name,
                            "tray_type": tray.tray_type,
                            "tray_color": tray.tray_color,
                            "tray_info_idx": tray.tray_info_idx,
                            "nozzle_temp_min": tray.nozzle_temp_min,
                            "nozzle_temp_max": tray.nozzle_temp_max,
                            "tray_temp": tray.tray_temp,
                            "tray_weight": tray.tray_weight,
                            "tray_uuid": tray.tray_uuid,
                        }
                    )
                ams_list.append(
                    {
                        "ams_id": ams_id,
                        "humidity": ams.humidity,
                        "temperature": ams.temperature,
                        "trays": trays,
                    }
                )
            if not ams_list or all(not unit.get("trays") for unit in ams_list):
                raw = self._get_ams_raw()
                if raw:
                    ams_list = raw
            vt_tray = None
            try:
                ext = self._printer.vt_tray()
                vt_tray = {
                    "tray_id_name": ext.tray_id_name,
                    "tray_type": ext.tray_type,
                    "tray_color": ext.tray_color,
                    "tray_info_idx": ext.tray_info_idx,
                    "nozzle_temp_min": ext.nozzle_temp_min,
                    "nozzle_temp_max": ext.nozzle_temp_max,
                    "tray_temp": ext.tray_temp,
                    "tray_weight": ext.tray_weight,
                    "tray_uuid": ext.tray_uuid,
                }
            except Exception:
                vt_tray = None
            return {
                "ams": ams_list,
                "external_tray": vt_tray,
                "selected": self._status.get("selected_ams"),
            }

    def select_ams_tray(self, ams_id: int, tray_id: int) -> Dict[str, Any]:
        with self._lock:
            if not self._ensure_connected(force=True):
                raise RuntimeError("Printer not connected")

            tool_id = int(ams_id) * 4 + int(tray_id)
            self._selected_ams = {"ams_id": int(ams_id), "tray_id": int(tray_id), "tool_id": tool_id}
            self._status["selected_ams"] = dict(self._selected_ams)

            hub = self._printer.ams_hub()
            tray_info = None
            ams = hub.ams_hub.get(ams_id)
            if ams:
                tray = ams.filament_trays.get(tray_id)
                if tray:
                    tray_info = {
                        "tray_info_idx": tray.tray_info_idx,
                        "nozzle_temp_min": tray.nozzle_temp_min,
                        "nozzle_temp_max": tray.nozzle_temp_max,
                        "tray_type": tray.tray_type,
                        "tray_color": tray.tray_color,
                    }
            if tray_info is None:
                raw = self._get_ams_raw()
                for unit in raw or []:
                    if unit.get("ams_id") == ams_id:
                        for t in unit.get("trays", []):
                            if t.get("tray_id") == tray_id:
                                tray_info = t
                                break
            if tray_info is None:
                raise RuntimeError(f"Tray {tray_id} not found on AMS {ams_id}")

            # Always return the selection so the UI can reflect it even if the
            # printer isn't currently in a filament action.
            color = (tray_info.get("tray_color") or "FFFFFF").lstrip("#")
            if len(color) >= 6:
                color = color[:6]
            else:
                color = color.ljust(6, "F")
            settings = AMSFilamentSettings(
                str(tray_info.get("tray_info_idx") or ""),
                int(tray_info.get("nozzle_temp_min") or 0),
                int(tray_info.get("nozzle_temp_max") or 0),
                str(tray_info.get("tray_type") or ""),
            )
            # set_filament_printer() is best-effort: some firmwares/models reject
            # it or require additional state. Selection should still be saved
            # locally and used for the next start_print() mapping.
            try:
                set_ok = bool(
                    self._printer.set_filament_printer(
                        color, settings, ams_id=ams_id, tray_id=tray_id
                    )
                )
            except Exception:
                set_ok = False
            resume_ok = False
            toolchange_ok = False
            try:
                state = self._printer.get_current_state()
            except Exception:
                state = None

            # Only attempt to resume the printer's filament workflow when the printer
            # is paused for a filament action (e.g., runout). Otherwise this is a
            # confusing no-op.
            if state == PrintStatus.PAUSED_FILAMENT_RUNOUT:
                # In runout state, a tool selection is required to choose the
                # replacement spool.
                try:
                    toolchange_ok = bool(self._printer.gcode(f"T{tool_id}", gcode_check=False))
                except Exception:
                    toolchange_ok = False
                resume_ok = bool(self._printer.retry_filament_action())

            return {
                "selected": dict(self._selected_ams),
                "set": set_ok,
                "toolchange": toolchange_ok,
                "resume": resume_ok,
            }

    def _get_ams_raw(self) -> list[dict[str, Any]]:
        try:
            dump = self._printer.mqtt_dump()
        except Exception:
            return []
        ams_info = None
        if isinstance(dump, dict):
            ams_info = dump.get("print", {}).get("ams") or dump.get("ams")
        if not isinstance(ams_info, dict):
            return []
        units = ams_info.get("ams", []) or []
        ams_list: list[dict[str, Any]] = []
        for unit in units:
            try:
                ams_id = int(unit.get("id", 0))
            except Exception:
                ams_id = 0
            trays_raw = unit.get("tray", []) or []
            trays = []
            for tray in trays_raw:
                tray_id = tray.get("id")
                if tray_id is None:
                    continue
                tray_state = tray.get("state")
                tray_info_idx = tray.get("tray_info_idx")
                if tray_info_idx in (None, "", "0") and tray_state in (0, "0", None):
                    # Empty slot with no info
                    continue
                trays.append(
                    {
                        "tray_id": int(tray_id),
                        "tray_id_name": tray.get("tray_id_name"),
                        "tray_type": tray.get("tray_type"),
                        "tray_color": tray.get("tray_color"),
                        "tray_info_idx": tray.get("tray_info_idx"),
                        "nozzle_temp_min": tray.get("nozzle_temp_min"),
                        "nozzle_temp_max": tray.get("nozzle_temp_max"),
                        "tray_temp": tray.get("tray_temp"),
                        "tray_weight": tray.get("tray_weight"),
                        "tray_uuid": tray.get("tray_uuid"),
                    }
                )
            ams_list.append(
                {
                    "ams_id": ams_id,
                    "humidity": unit.get("humidity"),
                    "temperature": unit.get("temp") or unit.get("temperature"),
                    "trays": trays,
                }
            )
        return ams_list

    def _get_sequence_id(self) -> Optional[int]:
        if not self._printer:
            return None
        try:
            return int(self._printer.mqtt_client.get_sequence_id())
        except Exception:
            return None

    def _wait_for_gcode_state(
        self,
        targets: set[GcodeState],
        timeout_sec: float = 8.0,
        poll_interval_sec: float = 0.25,
        pushall_interval_sec: float = 1.0,
    ) -> bool:
        if not self._printer:
            return False
        deadline = time.time() + timeout_sec
        last_pushall = 0.0
        while time.time() < deadline:
            try:
                state = self._printer.get_state()
            except Exception:
                state = None
            if state in targets:
                return True
            # Nudge the printer to emit a fresh state snapshot (but don't spam it).
            now = time.time()
            if now - last_pushall >= pushall_interval_sec:
                last_pushall = now
                try:
                    self._printer.mqtt_client.pushall()
                except Exception:
                    pass
            time.sleep(poll_interval_sec)
        return False

    def pause(self) -> bool:
        with self._lock:
            if not self._ensure_connected(force=True):
                return False
            state = self._printer.get_state()
            if state == GcodeState.PAUSE:
                return True
            if state in (GcodeState.IDLE, GcodeState.FINISH, GcodeState.FAILED):
                # Not currently pausable.
                return False

            seq = self._get_sequence_id()
            # Fire a few variants quickly, then wait once for the state transition.
            self._mqtt_print_command("pause")
            if seq is not None:
                self._mqtt_print_command("pause", sequence_id=str(seq))
            self._mqtt_print_command("pause", sequence_id="0")
            self._mqtt_print_command("pause", sequence_id="0", param="")
            if self._wait_for_gcode_state({GcodeState.PAUSE}):
                return True

            # Fallback: try standard G-code pause (often ignored on Bambu firmware).
            try:
                self._printer.gcode("M25", gcode_check=False)
            except Exception:
                return False
            return self._wait_for_gcode_state({GcodeState.PAUSE})

    def resume(self) -> bool:
        with self._lock:
            if not self._ensure_connected(force=True):
                return False
            state = self._printer.get_state()
            if state == GcodeState.RUNNING:
                return True
            if state != GcodeState.PAUSE:
                # Only resumable if we're actually paused.
                return False

            seq = self._get_sequence_id()
            self._mqtt_print_command("resume")
            if seq is not None:
                self._mqtt_print_command("resume", sequence_id=str(seq))
            self._mqtt_print_command("resume", sequence_id="0")
            self._mqtt_print_command("resume", sequence_id="0", param="")
            if self._wait_for_gcode_state({GcodeState.RUNNING}):
                return True

            # Filament-runout pause can require a different resume action.
            try:
                self._printer.retry_filament_action()
            except Exception:
                return False
            if self._wait_for_gcode_state({GcodeState.RUNNING}):
                return True

            # Fallback: try standard G-code resume.
            try:
                self._printer.gcode("M24", gcode_check=False)
            except Exception:
                return False
            return self._wait_for_gcode_state({GcodeState.RUNNING})

    def stop_print(self) -> bool:
        with self._lock:
            if not self._ensure_connected(force=True):
                return False
            state = self._printer.get_state()
            if state in (GcodeState.IDLE, GcodeState.FINISH, GcodeState.FAILED):
                return True

            expected = {GcodeState.IDLE, GcodeState.FINISH, GcodeState.FAILED}
            seq = self._get_sequence_id()
            self._mqtt_print_command("stop")
            if seq is not None:
                self._mqtt_print_command("stop", sequence_id=str(seq))
            self._mqtt_print_command("stop", sequence_id="0")
            self._mqtt_print_command("stop", sequence_id="0", param="")
            if self._wait_for_gcode_state(expected):
                return True

            # Last resort: attempt to halt via common G-code.
            try:
                self._printer.gcode("M0", gcode_check=False)
            except Exception:
                pass
            if self._wait_for_gcode_state(expected):
                return True
            try:
                self._printer.gcode("M25", gcode_check=False)
            except Exception:
                return False
            return self._wait_for_gcode_state(expected)

    def clear_failed_state(self) -> Dict[str, Any]:
        """
        Best-effort attempt to clear a latched FAILED gcode_state.

        This is intentionally conservative:
        - It only sends a stop command (no movement, no print start).
        - It does not mark the printer as "available"; availability is based on
          the printer's reported gcode_state.
        """
        with self._lock:
            if not self._ensure_connected(force=True):
                return {"ok": False, "error": "Printer not connected"}

            before = self._printer.get_state()
            if before != GcodeState.FAILED:
                return {
                    "ok": True,
                    "skipped": "not_failed",
                    "before": str(before),
                    "after": str(before),
                }

            # Send stop variants (same approach as stop_print) but wait for a
            # transition out of FAILED.
            seq = self._get_sequence_id()
            self._mqtt_print_command("stop")
            if seq is not None:
                self._mqtt_print_command("stop", sequence_id=str(seq))
            self._mqtt_print_command("stop", sequence_id="0")
            self._mqtt_print_command("stop", sequence_id="0", param="")

            cleared = self._wait_for_gcode_state({GcodeState.IDLE, GcodeState.FINISH})
            try:
                after = self._printer.get_state()
            except Exception:
                after = before
            return {"ok": bool(cleared), "before": str(before), "after": str(after)}

    def light_on(self) -> bool:
        with self._lock:
            if not self._ensure_connected(force=True):
                return False
            ok = bool(self._printer.turn_light_on())
            # Also try ledctrl for logo nodes to cover firmware differences
            for node in ("logo_light", "logo", "logo_led", "led_logo"):
                ok = self._mqtt_ledctrl("on", node=node) or ok
            return ok

    def light_off(self) -> bool:
        with self._lock:
            if not self._ensure_connected(force=True):
                return False
            ok = bool(self._printer.turn_light_off())
            # Also try ledctrl for logo nodes to cover firmware differences
            for node in ("logo_light", "logo", "logo_led", "led_logo"):
                ok = self._mqtt_ledctrl("off", node=node) or ok
            return ok

    def _mqtt_publish(self, payload: dict) -> bool:
        client = mqtt.Client()
        client.username_pw_set("bblp", self._config.access_code)
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
        client.connect(self._config.printer_ip, 8883, 60)
        client.loop_start()
        result = client.publish(
            f"device/{self._config.serial}/request",
            json.dumps(payload),
            qos=0,
            retain=False,
        )
        result.wait_for_publish()
        client.loop_stop()
        client.disconnect()
        return result.is_published()

    def _publish_command(self, payload: dict[str, Any]) -> bool:
        """
        Publish a command to the printer.

        Prefer the existing bambulabs_api MQTT session (when available) so commands
        go out over the same connection we're already using for status updates.
        Fall back to a one-shot paho connection if needed.
        """
        if self._printer:
            try:
                publish = getattr(
                    self._printer.mqtt_client,
                    "_PrinterMQTTClient__publish_command",
                    None,
                )
                if callable(publish) and bool(publish(payload)):
                    return True
            except Exception:
                pass
        try:
            return bool(self._mqtt_publish(payload))
        except Exception:
            return False

    def _mqtt_ledctrl(self, mode: str, node: str = "chamber_light") -> bool:
        payload = {
            "system": {
                "sequence_id": "0",
                "command": "ledctrl",
                "led_node": node,
                "led_mode": mode,
                "led_on_time": 500,
                "led_off_time": 500,
                "loop_times": 0,
                "interval_time": 0,
            }
        }
        return self._publish_command(payload)

    def _mqtt_print_command(
        self,
        command: str,
        *,
        sequence_id: Optional[str] = None,
        param: Any = None,
    ) -> bool:
        body: dict[str, Any] = {"command": command}
        if sequence_id is not None:
            body["sequence_id"] = str(sequence_id)
        if param is not None:
            body["param"] = param
        payload = {"print": body}
        return self._publish_command(payload)

    def chamber_light_on(self) -> bool:
        with self._lock:
            return self._mqtt_ledctrl("on", node="chamber_light")

    def chamber_light_off(self) -> bool:
        with self._lock:
            return self._mqtt_ledctrl("off", node="chamber_light")

    def jog(self, dx: float, dy: float, dz: float, feed: int) -> bool:
        with self._lock:
            if not self._ensure_connected(force=True):
                return False
            gcode = ["G91", f"G0 X{dx:.3f} Y{dy:.3f} Z{dz:.3f} F{int(feed)}", "G90"]
            return bool(self._printer.gcode(gcode, gcode_check=False))

    def home(self) -> bool:
        with self._lock:
            if not self._ensure_connected(force=True):
                return False
            return bool(self._printer.gcode(["G28"], gcode_check=False))

    def set_temps(self, bed: Optional[int], nozzle: Optional[int]) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        with self._lock:
            if not self._ensure_connected(force=True):
                return {"connected": False}
            if bed is not None:
                results["bed"] = bool(self._printer.set_bed_temperature(bed))
            if nozzle is not None:
                results["nozzle"] = bool(self._printer.set_nozzle_temperature(nozzle))
        return results

    def set_fans(
        self,
        part_percent: Optional[int] = None,
        aux_percent: Optional[int] = None,
        chamber_percent: Optional[int] = None,
    ) -> Dict[str, bool]:
        """
        Set fan speeds as percentages (0-100).

        Uses the bambulabs_api helpers, which send M106 commands over MQTT:
        - part fan: M106 P1 Sxxx
        - aux fan:  M106 P2 Sxxx
        - chamber:  M106 P3 Sxxx
        """

        def clamp_percent(value: int) -> int:
            return max(0, min(100, int(value)))

        def percent_to_pwm(percent: int) -> int:
            return int(round(255 * (percent / 100.0)))

        results: Dict[str, bool] = {}
        with self._lock:
            if not self._ensure_connected(force=True):
                return {"connected": False}

            if part_percent is not None:
                p = clamp_percent(part_percent)
                results["part"] = bool(self._printer.set_part_fan_speed(percent_to_pwm(p)))
                if results["part"]:
                    self._status["part_fan_percent"] = p

            if aux_percent is not None:
                p = clamp_percent(aux_percent)
                results["aux"] = bool(self._printer.set_aux_fan_speed(percent_to_pwm(p)))
                if results["aux"]:
                    self._status["aux_fan_percent"] = p

            if chamber_percent is not None:
                p = clamp_percent(chamber_percent)
                results["chamber"] = bool(self._printer.set_chamber_fan_speed(percent_to_pwm(p)))
                if results["chamber"]:
                    self._status["chamber_fan_percent"] = p

        return results

    def upload_file(self, filename: str, data: bytes) -> str:
        with self._lock:
            if not self._ensure_connected(force=True):
                raise RuntimeError("Printer not connected")
            bio = BytesIO(data)
            bio.seek(0)
            return self._printer.upload_file(bio, filename)

    def upload_file_obj(self, fileobj, filename: str) -> str:
        with self._lock:
            if not self._ensure_connected(force=True):
                raise RuntimeError("Printer not connected")
            if hasattr(fileobj, "seek"):
                try:
                    fileobj.seek(0)
                except OSError:
                    pass
            return self._printer.upload_file(fileobj, filename)

    def upload_file_path(self, filepath: str, filename: str) -> str:
        with self._lock:
            if not self._ensure_connected(force=True):
                raise RuntimeError("Printer not connected")
            with open(filepath, "rb") as handle:
                return self._printer.upload_file(handle, filename)

    def start_print(self, filename: str, plate: int = 1) -> bool:
        with self._lock:
            if not self._ensure_connected(force=True):
                return False
            ams_mapping = [0]
            if self._selected_ams and "tool_id" in self._selected_ams:
                # Single-color override: map extruder 0 to the selected AMS slot.
                ams_mapping = [int(self._selected_ams["tool_id"])]
            self._status["last_start_ams_mapping"] = list(ams_mapping)
            return bool(self._printer.start_print(filename, plate, ams_mapping=ams_mapping))


class PrinterManager:
    def __init__(self, config: FarmConfig) -> None:
        self._services: Dict[str, PrinterService] = {}
        for printer in config.printers:
            self._services[printer.printer_id] = PrinterService(
                printer, config.poll_interval_sec
            )

    def start_all(self) -> None:
        for service in self._services.values():
            service.start()

    def stop_all(self) -> None:
        for service in self._services.values():
            service.stop()

    def list_printers(self) -> List[Dict[str, Any]]:
        return [service.get_status() for service in self._services.values()]

    def get_service(self, printer_id: str) -> PrinterService:
        if printer_id not in self._services:
            raise KeyError(f"Unknown printer_id: {printer_id}")
        return self._services[printer_id]

    def get_status(self, printer_id: Optional[str] = None) -> Dict[str, Any]:
        if printer_id:
            return self.get_service(printer_id).get_status()
        return {pid: svc.get_status() for pid, svc in self._services.items()}
