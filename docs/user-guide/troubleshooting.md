# Troubleshooting

## Plan Does Not Load

Run:

```powershell
python -m packages.aura_core.cli.package_cli check plans/resonance
python tools\plan_doctor.py --plan resonance
```

If `manifest.yaml` is out of date, sync it:

```powershell
python -m packages.aura_core.cli.package_cli sync plans/resonance
```

## GUI Does Not Start

Install GUI dependencies:

```powershell
python -m pip install -r requirements/gui.txt
```

Then use:

```powershell
.\scripts\run_cli.ps1 gui resonance
```

## MuMu Runtime Issues

The Resonance plan uses `runtime.provider: mumu` with `scrcpy_stream` capture and `android_touch` input. Check `plans/resonance/config.yaml` first, then run a safe CLI command such as:

```powershell
.\scripts\run_cli.ps1 tasks resonance
```

For runtime errors, inspect the most recent run detail:

```powershell
.\scripts\run_cli.ps1 runs --game resonance
```
