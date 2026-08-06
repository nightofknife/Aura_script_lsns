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
scripts/package_release.ps1    Canonical local and CI release entrypoint
```

## Quick Commands

```powershell
.\scripts\setup_dev_environment.ps1 -VisionProvider cuda
.\scripts\run_cli.ps1 tasks resonance
.\scripts\run_cli.ps1 run resonance tasks:market_data.yaml:market_data_get_latest --timeout-sec 120
.\scripts\run_cli.ps1 gui resonance
```

The project uses one canonical `.venv` for development. Release builds use
separate `.venv-release-cpu`, `.venv-release-gpu`, and
`.venv-release-overlay` environments so mutually exclusive runtime packages
cannot contaminate each other.

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
.\scripts\package_release.ps1 -Profile cpu -ReleaseLabel local
.\scripts\package_release.ps1 -Profile all -ReleaseLabel local
```

`cpu`, `gpu`, `overlay`, and `all` use the same locked build path locally and in
GitHub Actions. Generated artifacts are written under
`.runtime\releases\<label>`. The low-level builder is an internal implementation
detail and does not install dependencies, validate artifacts, or create ZIPs.

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

Plans remain editable source files inside each full release. They are versioned
and released together with Core; standalone Plan replacement archives are not
published.
