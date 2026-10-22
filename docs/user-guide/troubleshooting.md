# Troubleshooting

## Plan Does Not Load

Validate the plan used by the failing workflow. The Windows-client GUI
uses `resonance_pc`.

```powershell
python -m packages.aura_core.cli.package_cli check plans/resonance_pc
python tools\plan_doctor.py --plan resonance_pc
```

If `manifest.yaml` is out of date, sync it:

```powershell
python -m packages.aura_core.cli.package_cli sync plans/resonance_pc
```

## GUI Does Not Start

Create or repair the complete development environment:

```powershell
.\scripts\setup_dev_environment.ps1 -VisionProvider cpu
```

Then use:

```powershell
.\scripts\run_cli.ps1 gui resonance_pc
```

## Windows PC Runtime Issues

The primary `resonance_pc` plan targets `雷索纳斯.exe`, captures through WGC
and sends input through SendInput. Check `plans/resonance_pc/config.yaml`, keep
the game window visible, and inspect target discovery and recent runs with:

```powershell
.\scripts\run_cli.ps1 doctor --all
.\scripts\run_cli.ps1 tasks resonance_pc
.\scripts\run_cli.ps1 runs --game resonance_pc
```
