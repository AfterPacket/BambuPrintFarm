# Bambu Print Farm Tools

This folder is optional.

If you want Bambu Print Farm to auto-slice `.stl` / `.3mf` uploads, you must provide a slicer CLI executable. The app can auto-detect slicers placed here:

1. `tools/orca-slicer.exe` (Windows) or `tools/orca-slicer` (Linux)
2. `tools/bambu-studio.exe` (Windows) or `tools/bambu-studio` (Linux)

On Linux, the app can also detect common system installs and Flatpak exports (for example:
`/var/lib/flatpak/exports/bin/com.orcaslicer.OrcaSlicer`).

Raspberry Pi / ARM64 note:
- Auto-slicing requires a slicer binary that can run on your architecture (`aarch64` for most Raspberry Pi 4/5 64-bit).
- If you do not have an ARM64-compatible slicer CLI available, you must pre-slice on another machine and upload
  `.gcode` / `.gcode.3mf` instead.

Then enable slicing in `config.json`:

```json
{
  "slicer": {
    "enabled": true
  }
}
```

Note: this repo does not ship slicer binaries.
