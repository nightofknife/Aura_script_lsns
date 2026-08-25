# Repository Architecture

Aura_script_lsns keeps reusable framework code and the Resonance business package in one source tree.

```text
packages/aura_core      runtime, scheduler, manifests, observability
packages/aura_game      embedded/subprocess runner facade
packages/resonance_gui  Qt desktop GUI for Resonance tasks
plans/aura_base         shared runtime actions and platform adapters
plans/resonance         MuMu/Android Resonance actions, services, tasks and data
plans/resonance_pc      Windows-client Resonance actions, services, tasks and data
plans/aura_benchmark    scheduler and DAG benchmark tasks
tests/smoke/            startup smoke tests plus selected framework/PC contract tests
```

Plan manifests are generated from package source. Do not hand-maintain generated
exports when adding actions, services or tasks; sync the plan that changed:

```powershell
python -m packages.aura_core.cli.package_cli sync plans/resonance
python -m packages.aura_core.cli.package_cli sync plans/resonance_pc
```

The GUI intentionally remains game-specific. Its primary guided surfaces use
`resonance_pc`; the lower-level workbench still exposes selected `resonance`
tasks. Shared runner, queue, history and settings pieces may later be extracted
into a framework GUI package.
