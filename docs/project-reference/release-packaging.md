# Release Packaging

`scripts/package_release.ps1` is the supported local entrypoint. It creates a
profile-specific Python 3.12 environment, installs pinned release dependencies,
checks MuMu and OCR assets, then delegates to the low-level
`scripts/build_release.ps1` builder.

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

Example local builds:

```powershell
.\scripts\package_release.ps1 -Profile cpu
.\scripts\package_release.ps1 -Profile gpu
```

The environments are `.venv-release-cpu` and `.venv-release-gpu`; outputs are
under `.runtime\packages\<profile>`. Reuse is intentional. Add
`-RefreshDependencies` after changing package indexes, or
`-RecreateEnvironment` when repairing an environment. The builder never reuses
the development `.venv`.

If the OCR bundle is absent, download the project model release before building:

```powershell
.\scripts\release\download_ocr_models.ps1 -Repository nightofknife/Aura_script_lsns
```

`scripts/build_release.ps1` remains available for CI and low-level diagnostics,
but callers must then supply the matching clean release environment themselves.

The active PyInstaller definition is
`packaging\pyinstaller\aura.spec`. The former standalone GUI spec was removed to
avoid producing a second, behaviorally different package.

## Workspace cleanup

Generated runtime trees and test artifacts can be inspected with:

```powershell
.\scripts\clean_workspace.ps1
```

The command is preview-only by default. Add `-Apply` to remove the listed
generated directories. Release environments and canonical `.runtime` output are
preserved unless their explicit include switches are supplied.

To remove only Python bytecode caches without touching historical build output:

```powershell
.\scripts\clean_workspace.ps1 -PythonCachesOnly -Apply
```

Example release smoke:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <release>\run.ps1 tasks resonance
& <release>\runtime\AuraResonanceRuntime.exe --self-check
```
