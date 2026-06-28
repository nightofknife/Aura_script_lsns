# Repository Architecture

Aura_script_lsns keeps reusable framework code and the Resonance business package in one source tree.

```text
packages/aura_core      runtime, scheduler, manifests, observability
packages/aura_game      embedded/subprocess runner facade
packages/resonance_gui  Qt desktop GUI for Resonance tasks
plans/aura_base         shared runtime actions and platform adapters
plans/resonance         Resonance actions, services, tasks and data
tests/                  framework and Resonance tests
```

`plans/resonance/manifest.yaml` is generated from the package source. Do not hand-maintain generated exports when adding actions, services or tasks; run:

```powershell
python -m packages.aura_core.cli.package_cli sync plans/resonance
```

The GUI intentionally remains game-specific in this first migration pass. Obvious shared parts such as runner bridge, queue, history and settings can later be extracted into a framework GUI package.
