# Validation and Release

## Plan Validation

```powershell
python -m packages.aura_core.cli.package_cli check plans/resonance
python -m packages.aura_core.cli.package_cli validate plans/resonance
python tools\plan_doctor.py --plan resonance
```

## Tests

所有测试、验证、冒烟检查和打包检查都必须从仓库根目录运行，测试产生的临时文件、
缓存、截图、日志和其他产物也必须保存在当前仓库内。禁止把系统临时目录、桌面、
其他 checkout 或外部项目目录作为测试工作目录。

`pytest.ini` 已将 pytest 的默认临时目录固定为 `.pytest_tmp`。需要隔离不同
测试范围时，可以显式使用 `.pytest_tmp/<scope>`，但不得把 `--basetemp` 指向仓库外：

```powershell
New-Item -ItemType Directory -Force -Path .pytest_tmp | Out-Null
python -m pytest tests\test_resonance_*.py --basetemp .pytest_tmp\resonance
python -m pytest tests\test_resonance_gui_*.py --basetemp .pytest_tmp\resonance_gui
```

非 pytest 工具如果会调用操作系统临时目录，运行前必须把临时环境变量指向仓库内的
专用目录，例如：

```powershell
$testTemp = Join-Path $PWD ".pytest_tmp\manual"
New-Item -ItemType Directory -Force -Path $testTemp | Out-Null
$env:TEMP = $testTemp
$env:TMP = $testTemp
$env:TMPDIR = $testTemp
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
.\scripts\package_release.ps1 -Profile cpu -ReleaseLabel local
.\scripts\package_release.ps1 -Profile gpu -ReleaseLabel local
.\scripts\package_release.ps1 -Profile all -ReleaseLabel local
```

`build_release.ps1` 是内部目录构建器，不应作为本地或 CI 的发行入口。

```powershell
pwsh -NoProfile -File <release>\run.ps1 games --all
pwsh -NoProfile -File <release>\run.ps1 tasks resonance
& <release>\runtime\AuraResonanceRuntime.exe --self-check
```

When GUI packaging is enabled, the release root should contain `AuraResonanceGui.exe` and `runtime\AuraResonanceRuntime.exe`.
