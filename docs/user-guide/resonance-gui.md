# Resonance GUI

The GUI lives in `packages/resonance_gui` and launches with:

```powershell
.\scripts\run_cli.ps1 gui resonance
```

The command name remains `gui resonance`, but the primary desktop workflow
executes Windows-client tasks from the `resonance_pc` plan through an isolated
subprocess runner.

Click the **?** button in the GUI's top header (or press **F1**) to open the
built-in Chinese tutorial and feature Wiki. It is available offline, with a
categorized article tree, full-text search and highlighting, a per-article
outline, internal links and browsing history. Detailed feature articles explain
parameters, resource scopes, dependencies, ignored options and worked examples.
Navigation buttons open the relevant editor without starting a task or changing
its parameters. Help opens only on request.

The main navigation contains Workflow, Independent Features, Run History and Settings.

- Workflow: enable and reorder startup, freight, battle and close tasks. Freight
  has a full parameter editor and can carry optional passengers along its free
  route without changing freight planning or its fatigue budget. Battle provides
  separate weekday plans.
- Independent Features: refresh player data, preview freight, recommend teams,
  refresh an equipment affix, run Deep Dive and Eternal Scuffle, or collect data. These
  entries use a flat task list and a wide detail panel.
- Settings: configure runtime and workflow preferences.
- Run History: double-click a record to inspect its read-only result.

The Affix Refresh small task starts at the open equipment affix replacement
window, in a 1280x720 game client. In Independent Features, select Affix Refresh,
search the complete effect catalog, choose a numerical requirement (any, at
least, equal, or maximum), and set the maximum refresh rounds. The catalog is not filtered
by equipment, so choose an effect available to your current equipment. Press
Start Auto Refresh; the task checks the current candidate before spending,
handles refresh confirmations, and stops when a candidate meets the target.
Apply that candidate manually in the game. Automatic replacement is disabled,
including for previously saved settings that enabled it.

No equipment name, level, old-affix selection, or replacement-slot number is
required. The task does not read or scroll the old-affix list, so a long list,
locked slots, or its current scroll position does not affect target selection.
Target, numerical requirement, and round-limit preferences are saved between runs.
Fixed controls and refresh confirmation windows use bundled
templates. OCR is limited to the new candidate, with unchanged crops cached.
Each actual OCR call preserves raw text, confidence, location, and parsing
results in the application log; use the panel's log control to inspect them.

A zero round limit only checks an existing candidate and never refreshes. The last
permitted refresh is still evaluated before stopping. Each submitted refresh
counts once; this is not a film-cost budget, and film balance is not read.
The panel shows the candidate, refresh count versus limit and current status.
End states distinguish a target found for manual replacement, round limit reached,
cancellation, and recognition or operation errors. Uncertain transactions stop
without repeating a potentially spending click. Stopping leaves
the current game window and pending candidate in place.

Freight supports free routing and fixed routes. Free routing can target a net
profit; book policy chooses incremental-profit thresholds or filling cargo
subject to an overfill limit. Fixed routes retain city order and repeated visits;
optional repositioning moves to the route start before trading. An unreachable
profit target returns no executable route.

The purchase-book budget is shared across the entire freight route: zero
disables books, an integer limits their total use, and unlimited mode has no
book count cap. The per-book profit threshold applies to the profit-first policy;
the full-load policy uses the waste limit instead. Existing saved
Auto Book settings are migrated when the GUI reads them.

Opted-in water or bento recovery triggers an internal player-data preparation
step before freight execution. The workflow reports completion after final
sales and any enabled bento cleanup have finished. Expected route values and
confirmed execution resources are displayed separately.

Freight, attached-passenger, battle and workflow inputs use typed Qt controls. Nested
JSON editing remains available only in the lower-level workbench where it is
useful for arbitrary task inputs.

The retained lower-level workbench groups include:

- Market data: refresh, latest snapshot and product query.
- Trade planning: next step, best cycle and simulation.
- Automatic trade: `auto_cycle_trade`.
- City operations: travel, enter shop, buy goods and sell goods.
- Battle dispatch: input preview and automatic dispatch.
