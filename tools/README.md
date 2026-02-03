# Bambu Print Farm Tools

This folder is optional.

If you want Bambu Print Farm to auto-slice `.stl` / `.3mf` uploads, you must provide a slicer CLI executable. The app can auto-detect slicers placed here:

1. `tools/orca-slicer.exe` (Windows) or `tools/orca-slicer` (Linux)
2. `tools/bambu-studio.exe` (Windows) or `tools/bambu-studio` (Linux)

Then enable slicing in `config.json`:

```json
{
  "slicer": {
    "enabled": true
  }
}
```

Note: this repo does not ship slicer binaries.
