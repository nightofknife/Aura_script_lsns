# Resonance Development

Develop Resonance game capabilities in `plans/resonance_pc`, the Windows-client
plan used by the GUI. Reusable MuMu/Android runtime adapters remain in
`plans/aura_base/src/platform/mumu` as framework capabilities.

Keep a change aligned across the matching plan layers:

```text
plans/resonance_pc/tasks/
plans/resonance_pc/src/actions/
plans/resonance_pc/src/services/
plans/resonance_pc/data/
packages/resonance_gui/widgets/
```

Task YAML should use canonical shared actions such as `plans/aura_base/click` and `plans/aura_base/sleep`. Cross-task execution should use `aura.run_task` with a canonical `task_ref`.

After adding or changing plan exports:

```powershell
python -m packages.aura_core.cli.package_cli sync plans/resonance_pc
python -m packages.aura_core.cli.package_cli check plans/resonance_pc
python tools\plan_doctor.py --plan resonance_pc
```

Expose user-facing PC tasks through the matching GUI feature or workflow
controls and their task references.

The repository smoke suite covers imports, discovery, runner lifecycle and GUI
construction, plus selected framework actions, PC character recognition and
task-to-framework reuse contracts. It does not replace live validation against
the current game client and its UI assets.
