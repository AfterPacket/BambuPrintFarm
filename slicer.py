from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import zipfile
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

PLATE_GCODE_RE = re.compile(r"^Metadata[\\/]plate_\\d+\\.gcode$", re.IGNORECASE)


@dataclass
class SlicerConfig:
    enabled: bool
    preferred: str
    command_args: List[str]
    orca_paths: List[str]
    bambu_paths: List[str]
    max_wait_sec: int


def load_slicer_config(path: str = "config.json") -> SlicerConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    slicer = data.get("slicer", {}) or {}
    enabled = bool(slicer.get("enabled", False))
    preferred = str(slicer.get("preferred", "auto")).lower()
    command_args = slicer.get("command_args") or [
        "{exe}",
        "--allow-newer-file",
        "1",
        "--no-check",
        "--slice",
        "0",
        "--export-3mf",
        "{output}",
        "{input}",
    ]
    orca_paths = slicer.get("orca_paths") or []
    bambu_paths = slicer.get("bambu_paths") or []
    max_wait_sec = int(slicer.get("max_wait_sec", 600))
    return SlicerConfig(
        enabled=enabled,
        preferred=preferred,
        command_args=command_args,
        orca_paths=orca_paths,
        bambu_paths=bambu_paths,
        max_wait_sec=max_wait_sec,
    )


def _default_paths() -> Tuple[List[str], List[str]]:
    system = platform.system().lower()
    here = Path(__file__).resolve().parent
    orca = []
    bambu = []
    if system == "windows":
        orca += [
            str(here / "tools" / "orca-slicer.exe"),
            str(here / "tools" / "OrcaSlicer" / "orca-slicer.exe"),
            r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
            r"C:\Program Files\OrcaSlicer\OrcaSlicer.exe",
        ]
        bambu += [
            str(here / "tools" / "bambu-studio.exe"),
            str(here / "tools" / "BambuStudio" / "bambu-studio.exe"),
            r"C:\Program Files\Bambu Studio\bambu-studio.exe",
            r"C:\Program Files\Bambu Studio\BambuStudio.exe",
        ]
    else:
        # Linux common paths
        orca += [
            str(here / "tools" / "orca-slicer"),
            str(here / "tools" / "OrcaSlicer.AppImage"),
            "/usr/bin/orca-slicer",
            "/usr/local/bin/orca-slicer",
            os.path.expanduser("~/.local/bin/OrcaSlicer"),
            os.path.expanduser("~/OrcaSlicer.AppImage"),
        ]
        bambu += [
            "/usr/bin/bambu-studio",
            "/usr/local/bin/bambu-studio",
            os.path.expanduser("~/.local/bin/BambuStudio"),
            "/var/lib/flatpak/exports/bin/com.bambulab.BambuStudio",
            os.path.expanduser("~/.local/share/flatpak/exports/bin/com.bambulab.BambuStudio"),
        ]
    return orca, bambu


def _first_existing(paths: List[str]) -> Optional[str]:
    for p in paths:
        if not p:
            continue
        if os.path.exists(p):
            return p
    return None


def resolve_slicer_exe(config: SlicerConfig) -> Optional[str]:
    orca_default, bambu_default = _default_paths()
    orca_paths = config.orca_paths + orca_default
    bambu_paths = config.bambu_paths + bambu_default

    if config.preferred == "orca":
        return _first_existing(orca_paths)
    if config.preferred == "bambu":
        return _first_existing(bambu_paths)

    # auto
    return _first_existing(orca_paths) or _first_existing(bambu_paths)


def build_command(config: SlicerConfig, exe: str, input_path: str, output_path: str) -> List[str]:
    args = []
    for item in config.command_args:
        args.append(
            item.format(
                exe=exe,
                input=input_path,
                output=output_path,
                outdir=str(Path(output_path).parent),
                base=Path(output_path).stem,
            )
        )
    return args


def auto_slice(config: SlicerConfig, input_path: str, output_dir: str) -> str:
    if not config.enabled:
        raise RuntimeError("Auto-slice disabled. Enable slicer in config.json.")
    exe = resolve_slicer_exe(config)
    if not exe:
        raise RuntimeError("No slicer executable found. Set slicer.orca_paths or slicer.bambu_paths.")

    os.makedirs(output_dir, exist_ok=True)
    base = Path(input_path).stem
    output_path = str(Path(output_dir) / f"{base}.gcode.3mf")
    cmd = build_command(config, exe, input_path, output_path)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.max_wait_sec,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Slicer not found: {exe}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Slicer timed out") from exc

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").splitlines()[-12:]
        raise RuntimeError("Slicer failed: " + "\n".join(tail))

    if not os.path.exists(output_path):
        # Some slicers write to input dir; try to detect.
        candidates = list(Path(output_dir).glob("*.gcode.3mf"))
        if candidates:
            return str(candidates[0])
        raise RuntimeError("Slicer reported success but output file not found.")

    # Sanity-check: for Bambu-style 3MF output, we must have plate_N.gcode inside.
    lower = output_path.lower()
    if lower.endswith(".3mf"):
        try:
            with zipfile.ZipFile(output_path) as zf:
                if not any(PLATE_GCODE_RE.match(name) for name in zf.namelist()):
                    raise RuntimeError(
                        "Slicer produced a .3mf without Metadata/plate_N.gcode. "
                        "Make sure the slicer actually sliced (not just exported a project)."
                    )
        except zipfile.BadZipFile:
            # Not a zip; treat as a normal file.
            pass

    return output_path
