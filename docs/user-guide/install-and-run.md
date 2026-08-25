# Install and Run

## From Source

```powershell
.\scripts\setup_dev_environment.ps1 -VisionProvider cpu
.\scripts\run_cli.ps1 tasks resonance
.\scripts\run_cli.ps1 tasks resonance_pc
.\scripts\run_cli.ps1 gui resonance
```

`setup_dev_environment.ps1` creates the repository-local `.venv`, installs the
runtime, GUI and test dependencies, and runs the development preflight. Use
`-VisionProvider cuda` on a CUDA 13-capable development machine, or
`-VisionProvider none` when no OCR/YOLO runtime is needed.

## CLI Examples

```powershell
.\scripts\run_cli.ps1 tasks resonance
.\scripts\run_cli.ps1 run resonance tasks:market_data.yaml:market_data_get_latest --timeout-sec 120
.\scripts\run_cli.ps1 run resonance tasks:auto_battle_input_preview.yaml:auto_battle_input_preview --inputs '{"jobs":[{"route_id":"gp.action_summary.global_supply.savior","difficulty":1}],"stop_on_failure":true}'
```

## Release Layout

```text
AuraResonanceGui.exe
runtime/
  aura.exe
  AuraResonanceRuntime.exe
plans/
  aura_base/
  aura_benchmark/
  resonance/
  resonance_pc/
models/
  ocr/
  yolo/
run.ps1
config.yaml
更新.exe
BUILD-INFO.txt
BUILD-INFO.json
```

Use the root `AuraResonanceGui.exe` for the desktop workflow. Use `run.ps1` for CLI commands inside a release bundle.
