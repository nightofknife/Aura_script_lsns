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
.\scripts\run_cli.ps1 tasks resonance
.\scripts\run_cli.ps1 run resonance tasks:market_data.yaml:market_data_get_latest --timeout-sec 120
.\scripts\run_cli.ps1 gui resonance
```

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

Previous game-specific business assets are intentionally not part of this repository.
