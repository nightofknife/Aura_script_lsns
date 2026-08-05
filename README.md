# Aura_script_lsns

Aura_script_lsns is a local Windows automation project for ResoNance/《雷索纳斯》. It keeps the Aura framework layers in this repository and ships a Resonance plan package plus a small desktop GUI for running common trade, market, city and battle-dispatch tasks.

## Repository Shape

```text
cli.py                         CLI entrypoint
packages/aura_core             Scheduler, task runtime, manifest tooling
packages/aura_game             Local runner facade used by CLI and GUI
packages/resonance_gui         Resonance desktop GUI
plans/aura_base                Shared runtime actions and services
plans/aura_benchmark           Lightweight framework smoke plan
plans/resonance                Resonance automation plan
scripts/build_release.ps1      Windows release builder
```

## Quick Commands

```powershell
.\scripts\setup_dev_environment.ps1 -VisionProvider cuda
.\scripts\run_cli.ps1 tasks resonance
.\scripts\run_cli.ps1 run resonance tasks:market_data.yaml:market_data_get_latest --timeout-sec 120
.\scripts\run_cli.ps1 gui resonance
```

The project uses one canonical `.venv` for development. Release builds use
separate `.venv-release-cpu` and `.venv-release-gpu` environments so their
mutually exclusive ONNX Runtime packages cannot contaminate each other.

## Validation

```powershell
python -m packages.aura_core.cli.package_cli check plans/resonance
python -m packages.aura_core.cli.package_cli validate plans/resonance
python tools\plan_doctor.py --plan resonance
python -m pytest tests\test_resonance_*.py --basetemp .pytest_tmp\resonance
```

## Release Names

The Resonance release uses:

- `AuraResonanceGui.exe`
- `runtime\AuraResonanceRuntime.exe`
- external editable plan packages under `plans\`

Build a local CPU release with the high-level entrypoint:

```powershell
.\scripts\package_release.ps1 -Profile cpu
```

Use `-Profile gpu` for the GPU archive. Generated build trees are kept under
`.runtime\packages\`; `scripts\build_release.ps1` remains the low-level builder
used by CI and advanced packaging work.

Preview reclaimable legacy build directories without deleting anything:

```powershell
.\scripts\clean_workspace.ps1
```

Previous game-specific business assets are intentionally not part of this repository.

## Using a Release

Choose the CPU archive for a universal Windows build, or the GPU archive for a
machine that may use NVIDIA CUDA acceleration. Extract the archive completely,
then double-click `AuraResonanceGui.exe`; Python does not need to be installed.
The GPU build falls back to CPU when CUDA is unavailable. To enable a bundled
CUDA 13 runtime, extract the matching `nvidia-cu13-overlay.zip` over the GPU
release directory and allow it to merge the `runtime` directory.

Plans remain editable source files. To apply a Plan replacement archive, close
Aura Resonance, back up the current `plans\` directory if desired, replace that
directory with the complete `plans\` directory from the archive, and start the
GUI again. Plan archives intentionally contain no installer, compatibility
metadata, cache, logs, credentials, or bytecode.
