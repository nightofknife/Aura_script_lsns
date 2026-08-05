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

日常验证应使用 `.venv\Scripts\python.exe`，不要从历史 `.venv-*` 或
`.runtime-*` 目录中挑选解释器。

## CLI Smoke

```powershell
.\scripts\run_cli.ps1 tasks resonance
.\scripts\run_cli.ps1 run resonance tasks:market_data.yaml:market_data_get_latest --timeout-sec 120
```

## Release Smoke

本地打包统一从高层入口开始，它会按 profile 创建并复用隔离的发布环境：

```powershell
.\scripts\package_release.ps1 -Profile cpu
.\scripts\package_release.ps1 -Profile gpu
```

只有调试 PyInstaller 或 CI 流程时才直接调用 `build_release.ps1`。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <release>\run.ps1 games --all
powershell -NoProfile -ExecutionPolicy Bypass -File <release>\run.ps1 tasks resonance
& <release>\runtime\AuraResonanceRuntime.exe --self-check
```

When GUI packaging is enabled, the release root should contain `AuraResonanceGui.exe` and `runtime\AuraResonanceRuntime.exe`.
