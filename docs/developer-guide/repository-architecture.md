# Repository Architecture

Aura_script_lsns keeps the reusable framework and the Resonance Windows client plan in one source tree.

```text
packages/aura_core      runtime, scheduler, manifests, observability
packages/aura_game      embedded/subprocess runner facade
packages/resonance_gui  Qt desktop GUI for Resonance PC workflows
plans/aura_base         shared runtime actions and Windows platform adapters
plans/resonance_pc      current Windows-client actions, services, tasks and data
plans/aura_benchmark    local scheduler benchmarks, excluded from releases
tools/                 diagnostic and asset-production implementations
research/              current four-view research and offline readers
scripts/release/       release assembly, metadata and validation
```

Plan manifests are generated from package source. Sync the plan after changing its exports:

```powershell
python -m packages.aura_core.cli.package_cli sync plans/resonance_pc
```

Task metadata separates lifecycle, orchestration, user tasks, user tools, internal steps and developer wrappers. The GUI exposes the PC workflow catalog. The release assembler requires both `aura_base` and `resonance_pc`, filters developer task definitions and their manifest exports, and excludes benchmark plans, research tools and retired emulator code. Scan and planning implementations used by the formal runtime remain in the PC plan.

Run development tooling from the repository root through `python -m tools diagnostics`, `python -m tools assets` or `python -m tools research`; each group lists its commands with `--help`. The implementations retain their specific arguments and algorithms. Tests, temporary outputs and generated evidence stay under repository-owned paths.

Existing retired-plan cache data remains on disk without a discoverable plan manifest. It is not included in releases.
