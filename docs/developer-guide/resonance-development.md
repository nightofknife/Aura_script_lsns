# Resonance Development

`plans/resonance_pc` is the supported Windows-client plan. Keep changes aligned across task YAML, actions, services, runtime data and the GUI workflow catalog. Reusable platform actions belong to `plans/aura_base`.

Task YAML uses canonical shared actions such as `plans/aura_base/click` and `plans/aura_base/sleep`. Cross-task execution uses `aura.run_task` with a canonical `task_ref`. Give each task an explicit `meta.role` and `meta.domain`; developer wrappers remain in the source checkout and are removed from release task YAML and manifest exports. Formal runtime scan and planning algorithms remain available to current workflows.

Child calls return the child's declared `returns` as business data. A missing, null, or empty business return fails the call; a task with only side effects can explicitly declare `returns: true`. Callers can supply `params.result_assertions`, for example `{'result.success': true, 'result.status': completed}`. Each dotted field must exist and match its expected literal value and type before the next loop iteration starts. Private child node IDs remain available in framework diagnostics and are not a business interface.

After changing exports, use:

```powershell
python -m packages.aura_core.cli.package_cli sync plans/resonance_pc
python -m packages.aura_core.cli.package_cli check plans/resonance_pc
python -m tools diagnostics plan --plan resonance_pc
```

Development entrypoints:

```powershell
python -m tools diagnostics --help
python -m tools assets --help
python -m tools research --help
python -m tools diagnostics runtime --help
python -m tools assets build-city-identity-templates --help
python -m tools research saved-view --help
```

Diagnostics groups runtime, capture, input, GPU, window, logs and structure inspection. Assets groups the existing build, generate, calibrate and review algorithms, alongside model, frame, OCR and catalog tools. Research exposes the current four-view reader, offline scoring and owned live servo/broker protocol. See `research/README.md` for required inputs and the boundary between offline research and live acceptance.

`plans/aura_benchmark` is a development-only scheduler fixture. Build preflight and the GUI use the current runtime rather than requiring benchmark content in player releases. Release maintenance stays under `scripts/package_release.ps1`, `scripts/build_release.ps1` and `scripts/release/`.

Run authorized tests from the repository root with temporary directories under `.pytest_tmp`. Mocked or offline checks do not replace live validation against the current game client. Live control tools require authorization to operate the game.
