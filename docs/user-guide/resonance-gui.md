# Resonance GUI

The GUI lives in `packages/resonance_gui` and launches with:

```powershell
.\scripts\run_cli.ps1 gui resonance
```

The command name remains `gui resonance`, but the primary desktop workflow
executes Windows-client tasks from the `resonance_pc` plan through an isolated
subprocess runner.

The current GUI contains these main surfaces:

- Workflow: compose startup, player-data refresh, freight/passenger work and
  battle execution in one guided sequence.
- Commerce: freight planning and execution, passenger routes, and combined
  commerce flows.
- Battle: build, validate and run an ordered battle job list.
- Workbench: run lower-level legacy `resonance` tasks directly.
- History: inspect persisted runs and their details.
- Settings: configure workflow and runtime preferences.

Freight, passenger, battle and workflow inputs use typed Qt controls. Nested
JSON editing remains available only in the lower-level workbench where it is
useful for arbitrary task inputs.

The retained workbench groups include:

- Market data: refresh, latest snapshot and product query.
- Trade planning: next step, best cycle and simulation.
- Automatic trade: `auto_cycle_trade`.
- City operations: travel, enter shop, buy goods and sell goods.
- Battle dispatch: input preview and automatic dispatch.
