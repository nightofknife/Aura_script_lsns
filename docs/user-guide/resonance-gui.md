# Resonance GUI

The GUI lives in `packages/resonance_gui` and launches with:

```powershell
.\scripts\run_cli.ps1 gui resonance
```

The `gui resonance` command executes Windows-client tasks from the
`resonance_pc` plan through an isolated subprocess runner.

The main navigation contains Workflow, Independent Features and Settings.

- Workflow: enable and reorder startup, freight, passenger, battle and close
  tasks. Freight and passenger have independent budgets and full parameter
  editors in the workflow page. Battle retains its ordered job editor.
- Independent Features: refresh player data, preview freight, recommend teams,
  run the existing Deep Dive and Eternal Scuffle tasks, or collect data. These
  entries use a flat task list and a wide detail panel.
- Settings: configure runtime and workflow preferences.

Freight supports four modes: maximum profit, quick full-load trading, fixed
route and target profit. Fixed routes retain city order and repeated visits;
optional repositioning moves to the route start before trading. An unreachable
profit target returns no executable route.

The purchase-book budget is shared across the entire freight route: zero
disables books, an integer limits their total use, and unlimited mode has no
book count cap. The per-book profit threshold still applies. Existing saved
Auto Book settings are migrated when the GUI reads them.

Opted-in water or bento recovery triggers an internal player-data preparation
step before freight execution. The workflow reports completion after final
sales and any enabled bento cleanup have finished. Expected route values and
confirmed execution resources are displayed separately.

Freight, passenger, battle and workflow inputs use typed Qt controls.
