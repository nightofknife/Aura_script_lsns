# Validation and Release

## Plan Validation

```powershell
python -m packages.aura_core.cli.package_cli check plans/resonance
python -m packages.aura_core.cli.package_cli validate plans/resonance
python tools\plan_doctor.py --plan resonance
```

## Tests

```powershell
New-Item -ItemType Directory -Force -Path .pytest_tmp | Out-Null
python -m pytest tests\test_resonance_*.py --basetemp .pytest_tmp\resonance
python -m pytest tests\test_resonance_gui_*.py --basetemp .pytest_tmp\resonance_gui
```

## CLI Smoke

```powershell
.\scripts\run_cli.ps1 tasks resonance
.\scripts\run_cli.ps1 run resonance tasks:market_data.yaml:market_data_get_latest --timeout-sec 120
```

## Release Smoke

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <release>\run.ps1 games --all
powershell -NoProfile -ExecutionPolicy Bypass -File <release>\run.ps1 tasks resonance
& <release>\runtime\AuraResonanceRuntime.exe --self-check
```

When GUI packaging is enabled, the release root should contain `AuraResonanceGui.exe` and `runtime\AuraResonanceRuntime.exe`.
