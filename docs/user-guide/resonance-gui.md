# Resonance GUI

The GUI lives in `packages/resonance_gui` and launches with:

```powershell
.\scripts\run_cli.ps1 gui resonance
```

The first Resonance GUI pass exposes these workbench groups:

- Market data: refresh, latest snapshot and product query.
- Trade planning: next step, best cycle and simulation.
- Automatic trade: `auto_cycle_trade`.
- City operations: travel, enter shop, buy goods and sell goods.
- Battle dispatch: input preview and automatic dispatch.

Task inputs are edited as JSON because several Resonance tasks use nested lists and dictionaries. The workbench keeps the runner bridge, queue, history and settings surfaces from the previous GUI shape while removing old game-specific pages.
