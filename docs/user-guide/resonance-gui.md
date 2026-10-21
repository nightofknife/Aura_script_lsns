# Resonance GUI

The GUI lives in `packages/resonance_gui` and launches with:

```powershell
.\scripts\run_cli.ps1 gui resonance
```

The command name remains `gui resonance`, but the primary desktop workflow
executes Windows-client tasks from the `resonance_pc` plan through an isolated
subprocess runner.

The main navigation contains Workflow, Independent Features and Settings.

- Workflow: enable and reorder startup, freight, passenger, battle and close
  tasks. Freight and passenger have independent budgets and full parameter
  editors in the workflow page. Battle retains its ordered job editor.
- Independent Features: refresh player data, preview freight, recommend teams,
  refresh an equipment affix, run Deep Dive and Eternal Scuffle, or collect data. These
  entries use a flat task list and a wide detail panel.
- Settings: configure runtime and workflow preferences.

The Affix Refresh small task starts at the open equipment affix replacement
window, in a 1280x720 game client. In Independent Features, select Affix Refresh,
search the complete effect catalog, choose a numerical requirement (any, at
least, equal, or maximum), and set the film budget. The catalog is not filtered
by equipment, so choose an effect available to your current equipment. Press
Start Auto Refresh; the task checks the current candidate before spending,
handles refresh confirmations, and stops when a candidate meets the target.
Apply that candidate manually in the game. Automatic replacement is disabled,
including for previously saved settings that enabled it.

No equipment name, level, old-affix selection, or replacement-slot number is
required. The task does not read or scroll the old-affix list, so a long list,
locked slots, or its current scroll position does not affect target selection.
Target, numerical requirement, and budget preferences are saved between runs.
Fixed controls, refresh confirmation windows, and film counters use bundled
templates. OCR is limited to the new candidate, with unchanged crops cached.
Each actual OCR call preserves raw text, confidence, location, and parsing
results in the application log; use the panel's log control to inspect them.

A zero budget only checks an existing candidate and never refreshes. The last
refresh inside the budget is still evaluated before stopping. During execution,
the panel shows the candidate, film spent versus budget, remaining balance,
and current status. End states distinguish a target found for manual
replacement, budget exhausted, insufficient film, cancellation, and recognition
or operation errors. Uncertain transactions stop without repeating a potentially
spending click, and retain any unconfirmed cost in the result. Stopping leaves
the current game window and pending candidate in place.

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

Freight, passenger, battle and workflow inputs use typed Qt controls. Nested
JSON editing remains available only in the lower-level workbench where it is
useful for arbitrary task inputs.

The retained lower-level workbench groups include:

- Market data: refresh, latest snapshot and product query.
- Trade planning: next step, best cycle and simulation.
- Automatic trade: `auto_cycle_trade`.
- City operations: travel, enter shop, buy goods and sell goods.
- Battle dispatch: input preview and automatic dispatch.
