# Release Packaging

`scripts/build_release.ps1` builds a Windows release with external editable plans.

Important output names:

- `AuraResonanceGui.exe`
- `runtime\AuraResonanceRuntime.exe`
- `runtime\aura.exe`

The release root copies all manifest-backed plan packages from `plans/`, including `aura_base`, `aura_benchmark`, `resonance`, and `resonance_pc`. Plan caches, state, logs, screenshots, and credentials are excluded.

## Payload policy

The frozen runtime keeps both PC and emulator capabilities. WGC, DXGI, MuMu scrcpy/PyAV, ONNX Runtime, OCR models, Qt Widgets, and external editable plans are release requirements.

The build deliberately excludes or removes files that are not used at runtime:

- raw Python source duplicated by PyInstaller's PYZ archive
- dependency test suites and ONNX Runtime conversion/quantization tools
- Qt QML, Quick, PDF, virtual keyboard, and unloaded translation catalogs
- Pillow AVIF and OpenCV video FFmpeg codecs
- build self-check logs and run history
- NVIDIA overlay headers, import libraries, symbols, and other development files

`scripts/release/prune_release_payload.py --check-only` is run by the Windows release workflow before artifacts are uploaded. The frozen GUI subprocess self-check and CPU OCR doctor run after runtime pruning, so a removed runtime dependency fails the release build.

Example build:

```powershell
.\scripts\build_release.ps1 -IncludeGui -CreateZip -ReleaseName aura-resonance-release
```

Example release smoke:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <release>\run.ps1 tasks resonance
& <release>\runtime\AuraResonanceRuntime.exe --self-check
```
