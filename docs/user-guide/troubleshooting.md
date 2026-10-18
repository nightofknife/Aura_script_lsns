# Troubleshooting

## Plan Does Not Load

Validate `resonance_pc`, the Windows-client plan used by Resonance workflows.

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
.\scripts\run_cli.ps1 gui resonance
```

## Framework Simulator Runtime

Reusable MuMu/Android capture and input adapters remain under
`plans/aura_base/src/platform/mumu`, with runtime configuration in
`plans/aura_base/src/platform/runtime_config.py`. Resonance game workflows
use the Windows-client configuration described below.

## Windows PC Runtime Issues

The primary `resonance_pc` plan targets `雷索纳斯.exe`, captures through WGC
and sends input through SendInput. Check `plans/resonance_pc/config.yaml`, keep
the game window visible, and inspect target discovery and recent runs with:

```powershell
.\scripts\run_cli.ps1 doctor --all
.\scripts\run_cli.ps1 tasks resonance_pc
.\scripts\run_cli.ps1 runs --game resonance_pc
```
