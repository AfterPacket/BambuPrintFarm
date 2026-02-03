# Bambu Print Farm

LAN dashboard and control surface for 3D printers. This project currently targets Bambu Lab LAN Mode via `bambulabs_api` (MQTT + FTP) and is designed for multi-printer operation.

## 1. Project Status & Updates

Development status: **EXPERIMENTAL**.

Screenshots (see `docs/screenshots/`):
- `docs/screenshots/control.png`
- `docs/screenshots/control-ams.png`
- `docs/screenshots/jobs.png`

![Control](docs/screenshots/control.png)
![Control (AMS)](docs/screenshots/control-ams.png)
![Jobs](docs/screenshots/jobs.png)

Recent updates (current behavior):
- Added job queue dispatcher with periodic dispatch and manual `Dispatch Now` trigger.
- Added dispatcher diagnostics endpoints (`/api/dispatch/status`, `/api/dispatch/once`).
- Made `config.json`, `jobs/`, and `static/` paths deterministic (anchored to repo directory) to prevent CWD-related failures.
- Added safety gating: jog/home is blocked while the printer is busy to prevent crashes during prints.
- Improved pause/resume/stop reliability by publishing through the active MQTT session and validating state transitions.
- Added fan controls (part / aux / chamber) with per-printer and fleet actions.
- Added temperature controls (bed/nozzle targets) and explicit cooldown.
- Added AMS tray visibility and manual tray selection, including "saved for next start" behavior.
- Camera updates: added snapshot mode (`/api/camera/snapshot`) and a camera diagnostics endpoint; MJPEG streaming remains available.
- Added fault handling for job dispatch: many firmwares report `FAILED` after stop/cancel; the dispatcher can treat `FAILED` as available when error indicators are zero/empty. Added a best-effort `Clear Fault` action (`POST /api/fault/clear`).
- Added `fail_reason` / `mc_print_error_code` surfacing in dispatch diagnostics and status display to make printer availability decisions visible.
- Repository hygiene: `config.example.json`, `.gitignore` (ignores `config.json` and `jobs/`), GPL-3.0 license, and UI theme updates.

Breaking changes / behavior differences:
- `config.json` is now loaded from the repo directory (not the current working directory). Running the server from another folder will no longer "accidentally" pick up a different config.
- If `config.json` is missing, the server falls back to `config.example.json` (development-friendly, repo-safe default).
- The job queue stores absolute file paths and the queue directory is anchored to `jobs/` in the repo directory.
- Jog/home actions are now blocked while the printer is busy (HTTP 409). This is intentional and non-negotiable for safety.
- Camera now supports snapshot mode. Streaming behavior may differ depending on ffmpeg and network conditions.
- Job dispatch behavior differs from earlier versions when the printer reports `FAILED`: a "soft fail" (`print_error_code=0`, `mc_print_error_code=0`, `hms=[]`) is treated as available, but a real fault remains blocked.

## 2. System Requirements

Supported operating systems:
- Windows 10/11
- Linux (Debian/Ubuntu/Raspberry Pi OS)

Supported printer models and firmware:
- Bambu Lab printers that support **LAN Mode** and local control via MQTT + FTP.
- Tested: X1 Carbon (X1C) in LAN Mode.
- Expected (not guaranteed): P1P, P1S, X1E, A1, A1 mini when LAN Mode is available.
- Firmware is not pinned. LAN APIs can change without notice. Compatibility must be re-validated after every firmware update.

Python/runtime:
- Python 3.11+

External tools:
- `ffmpeg` is required for camera features (`/api/camera`, `/api/camera/snapshot`).
- Optional: a slicer CLI is required for auto-slicing (`.stl` / project `.3mf` uploads).

Required slicers (only if auto-slicing is enabled):
- OrcaSlicer CLI **2.3.1+** (must support `--slice` and `--export-3mf`; `--allow-newer-file` is used by default).
- Bambu Studio CLI (must support project slicing to a Bambu-style `.gcode.3mf` container; version not pinned).

Hardware/network prerequisites:
- Printer must be reachable on the same LAN (stable Wi-Fi/Ethernet required).
- LAN Mode must be enabled on the printer and an access code must be configured.
- Camera access requires network reachability to the printer's RTSP(S) endpoint.

## 3. Pre-Requisites (Mandatory)

All items in this section are mandatory requirements.

LAN mode / local control:
- The printer must be configured for LAN use (LAN Mode / LAN Only Mode / Bambu Connect LAN) so it is reachable on the local network.
- You must have the correct connection details for each printer (IP address, serial number, and LAN access code / access code).
- If you do not have LAN mode enabled and correct connection details, this project cannot connect and cannot control the printer.

Printer profile:
- The correct printer profile must be selected for the **exact** printer model being used.
- Build volume, bed type, and start/end gcode must match the target printer.

Slicing/export:
- Models must be sliced and exported before use.
- Do not upload unsliced models to the printer unless auto-slicing is explicitly enabled and validated.

Nozzle/filament/temperature validation:
- Verify nozzle size matches the slicer profile and the physical nozzle installed.
- Verify filament type is correct for the profile and within safe temperature limits.
- Verify bed temperature, nozzle temperature, and cooling settings are appropriate for the material and environment.

Enclosure/thermal constraints:
- If an enclosure is used, verify that chamber temperature, chamber fan behavior, and ventilation are appropriate for the material.

Firmware safety features:
- Confirm printer safety features are enabled and functioning (thermal protection, jam detection, filament runout handling, emergency stop/stop button, etc.).

Failure to meet these prerequisites may result in print failure, equipment damage, or safety hazards.

## 4. Files & Preparation

No files are print-ready by default. You are responsible for preparing validated, printer-specific exports.

Source models:
- `.stl` (mesh)
- project `.3mf` (unsliced project container)

Files that require slicing:
- `.stl`
- project `.3mf` (unless it is already a sliced 3MF containing `Metadata/plate_N.gcode`)

Print-ready exports (safe to upload):
- `.gcode.3mf` (recommended for Bambu LAN printing; contains `Metadata/plate_N.gcode`)
- `.gcode` (support depends on firmware and file format expectations; prefer `.gcode.3mf`)

Editable files (repo):
- `config.example.json` (template; safe to commit)
- `config.json` (local-only; contains credentials; must not be committed)
- `api_server.py`, `mqtt_client.py`, `job_queue.py`, `slicer.py`
- `static/index.html`, `static/app.js`, `static/styles.css`

Runtime/state files (not for git):
- `jobs/` (queue metadata and queued job files)
- `__pycache__/`

Optional tool drop-in:
- `tools/` (optional slicer executables; see `tools/README.md`)

## 5. How to Run / Use

### Setup

1. Install Python 3.11+.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Create local configuration:
- Copy `config.example.json` to `config.json`.
- Fill in printer IP, serial, and LAN access code for each printer.

The server loads `config.json` if present and falls back to `config.example.json` if `config.json` is missing.

### Collect Printer Details (IP / Serial / Access Code)

You must enter exact values into `config.json`.

Required fields:
- `printer_ip`: printer IP address on your LAN
- `serial`: printer serial number (full value; do not use shortened/truncated values)
- `access_code`: LAN access code (sometimes labeled Access Code)

X1 Series (X1C / X1E):
1. On the printer screen, open Settings (honeycomb/gear icon).
2. Open the Network tab/page.
3. Record the printer IP address and Access Code.
4. Record the printer Serial Number from the device "About/Device/Printer" page (menu names vary by firmware).

P1 Series (P1P / P1S):
1. On the printer screen: Settings -> WLAN.
2. Record IP address and Access Code.
3. Record Serial Number from Settings -> Device -> Printer (menu names vary by firmware).

A1 / A1 mini:
1. On the printer screen: Settings.
2. Find "LAN Only Mode" (often on a later settings page).
3. Enable LAN Only Mode if required, then record IP address and Access Code.
4. Record Serial Number from the Device page.

Notes:
- UI labels can change with firmware. If a path above does not match your screen, locate the equivalent Network / WLAN / LAN Only Mode / Device / About pages on the printer.
- If you use DHCP, the IP address can change. Use a DHCP reservation on your router for stability.

### Enable LAN Mode / LAN Only Mode / Developer Mode

This project uses Bambu LAN local control. There are multiple LAN-related modes/paths depending on printer model and firmware:

- LAN Mode / LAN Only Mode:
  - Enables local printing/control on the LAN using an access code.
  - This is the minimum requirement for this project.

- Developer Mode (optional, higher risk):
  - Some firmware releases include a "Developer Mode" option that can leave MQTT, live stream, and FTP open on the local network.
  - Enabling developer mode shifts security responsibility to the operator and is not recommended on untrusted or shared networks.

If you do not know which mode you are using:
- Confirm you can view an Access Code on the printer.
- Confirm the printer is reachable on your LAN (ping by IP, and the dashboard can read `/api/status`).

4. Optional: enable HTTP Basic authentication (recommended on any non-local network):
- Set `DASH_USER` and `DASH_PASS` environment variables before starting the server.

PowerShell client note:
- If HTTP Basic authentication is enabled, PowerShell may not send Basic auth automatically. Use an explicit `Authorization` header.
- HTTP Basic over plain HTTP is unencrypted. Do not use it on untrusted networks.

```powershell
$cred = Get-Credential
$pair = "$($cred.UserName):$($cred.GetNetworkCredential().Password)"
$token = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
$headers = @{ Authorization = "Basic $token" }

Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/status?printer_id=PRINTER_ID" -Headers $headers
```

### Docker Compose Note (Auth Prompt)

If you run via `docker-compose.yml` and you see a browser authentication prompt, it is because `DASH_USER`/`DASH_PASS` are set for HTTP Basic auth.

If you do not want authentication:
- Remove or comment out the `environment:` section in `docker-compose.yml`.

If you do want authentication:
- Set `DASH_USER` and `DASH_PASS` to non-default values.

### Docker Compose Example (With Password)

If you want an authentication prompt, start from `docker-compose.example.yml` (it includes the `environment:` block with correct indentation).

### Start the Server

```powershell
python api_server.py
```

The server listens on `http://127.0.0.1:8000` by default unless `PORT` is set.

### Camera

- MJPEG stream endpoint: `/api/camera?printer_id=...`
- Snapshot endpoint (single JPEG): `/api/camera/snapshot?printer_id=...`
- Diagnostics: `/api/camera/diag?printer_id=...`

If streaming stalls, use snapshot mode. Camera reliability depends on `ffmpeg`, network stability, and printer camera firmware.

### Slicer Configuration (Auto-Slice)

Auto-slicing is optional and disabled by default.

1. Provide a slicer executable:
- Put OrcaSlicer/Bambu Studio CLI in one of the supported locations (see `tools/README.md`), or
- Configure explicit paths in `config.json` under `slicer.orca_paths` / `slicer.bambu_paths`.

2. Enable slicing in `config.json`:

```json
{
  \"slicer\": {
    \"enabled\": true,
    \"preferred\": \"auto\"
  }
}
```

3. Validate slicer output before printing:
- Confirm the output is a `.gcode.3mf` containing `Metadata/plate_1.gcode` (and the intended plate number).
- Confirm nozzle size, filament, temperatures, and bed type match the target printer.

### Upload & Start (Direct Print)

1. Select the correct active printer in the UI.
2. Upload a print-ready file (`.gcode.3mf` recommended).
3. Select the correct plate number.
4. Use `Upload & Start`.

Validation checks before starting:
- Correct printer selected.
- Printer state is idle/ready/finish (not running, paused, heating, calibrating, homing).
- Correct plate number.
- Correct AMS/filament selection (if used).
- Camera feed visible (optional but recommended).

Stop conditions (mandatory):
- Any abnormal motion, grinding, repeated impacts, or unexpected axis movement.
- Filament jam, extrusion failure, smoke/odor, or thermal runaway symptoms.
- Any behavior that does not match expected start gcode or printer preparation steps.

When a stop condition occurs:
- Stop the print using the printer's physical controls first.
- Then use the dashboard stop button if required.

### Job Queue

The job queue stores files locally in `jobs/files/` and metadata in `jobs/queue.json`.

Behavior:
- `Auto assign` enabled: the dispatcher selects the first available printer.
- `Auto assign` disabled: the job targets the currently selected printer.
- Dispatch occurs periodically and can be triggered manually.
- A job will not dispatch unless at least one printer is available.
- `FAILED` handling: Many Bambu firmwares report `gcode_state=FAILED` after a user stop/cancel. The dispatcher treats `FAILED` as available only when error indicators are zero/empty (`print_error_code=0`, `mc_print_error_code=0`, `hms=[]`). If the printer is `FAILED` with non-zero error indicators, jobs will remain queued until the fault is cleared on the device.

Diagnostics:
- `GET /api/dispatch/status`
- `POST /api/dispatch/once`

## 6. Safety & Non-Bypass Policy

Do not bypass, disable, or modify any built-in printer safety mechanisms.

This policy is mandatory.

Safety systems are required for:
- Operator protection
- Fire prevention
- Mechanical longevity

Any attempt to defeat safety features is unsupported and unsafe.

The dashboard enforces additional safety restrictions:
- Jog/home movement is blocked while the printer is busy to prevent toolhead crashes.

## 7. Known Issues & Lessons Learned

Known failure modes:
- Auto-slicing is not a slicer replacement. It depends on an external slicer CLI and its file compatibility.
- OrcaSlicer CLI may reject newer 3MF project versions. If slicing fails, upgrade the slicer and re-validate outputs.
- Camera streaming can stall depending on network and `ffmpeg`. Use snapshot mode when needed.
- Job queue dispatch depends on printer availability detection and reliable LAN connectivity.

Lessons learned (do not repeat):
- Do not jog axes while printing. This can cause collisions, skipped steps, and extruder/toolhead damage.
- Do not print files sliced for a different printer model/bed size. This can cause boundary violations and mechanical impacts.
- Do not ignore abnormal motion. Stop immediately and investigate before continuing.
- An on-print jog caused an extruder/toolhead jam. Do not use jog controls while the printer is printing.
- A pre-sliced file for an A1 was started on an X1C and did not respect boundaries. Do not start jobs sliced for a different model.
- If the printer reports `gcode_state: FAILED`, job dispatch depends on error indicators. Soft-fail (`print_error_code=0`, `mc_print_error_code=0`, `hms=[]`) can still dispatch; real faults must be cleared on the printer. The best-effort `Clear Fault` action does not bypass safety and may not clear hardware faults.

## 8. Responsibility & Disclaimer

Operation is the responsibility of the user.

Improper configuration voids support.

This project assumes a competent operator following documented procedures. It does not replace manufacturer safety guidance, printer supervision, or standard fire safety practices.
