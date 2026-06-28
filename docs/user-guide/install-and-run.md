# Install and Run

## From Source

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements/gui.txt
.\scripts\run_cli.ps1 tasks resonance
.\scripts\run_cli.ps1 gui resonance
```

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
models/
  ocr/
  yolo/
run.ps1
```

Use the root `AuraResonanceGui.exe` for the desktop workflow. Use `run.ps1` for CLI commands inside a release bundle.
