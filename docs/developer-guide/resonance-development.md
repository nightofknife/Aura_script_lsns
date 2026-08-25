# Resonance Development

Choose the target runtime before adding a capability:

- `plans/resonance_pc` is the primary Windows-client plan used by guided GUI
  workflows.
- `plans/resonance` is the retained MuMu/Android plan and legacy workbench
  catalog.

Keep a change aligned across the matching plan layers:

```text
plans/<resonance-or-resonance_pc>/tasks/
plans/<resonance-or-resonance_pc>/src/actions/
plans/<resonance-or-resonance_pc>/src/services/
plans/<resonance-or-resonance_pc>/data/
packages/resonance_gui/task_specs.py
packages/resonance_gui/widgets/
```

Task YAML should use canonical shared actions such as `plans/aura_base/click` and `plans/aura_base/sleep`. Cross-task execution should use `aura.run_task` with a canonical `task_ref`.

After adding or changing plan exports:

```powershell
python -m packages.aura_core.cli.package_cli sync plans/resonance
python -m packages.aura_core.cli.package_cli check plans/resonance
python tools\plan_doctor.py --plan resonance

python -m packages.aura_core.cli.package_cli sync plans/resonance_pc
python -m packages.aura_core.cli.package_cli check plans/resonance_pc
python tools\plan_doctor.py --plan resonance_pc
```

The GUI workbench task list is explicit. Add a new `TaskSpec` only for tasks that should be user-facing in the desktop workflow.

The repository smoke suite covers imports, discovery, runner lifecycle and GUI
construction, plus selected framework actions, PC character recognition and
task-to-framework reuse contracts. It does not replace live validation against
the current game client and its UI assets.
