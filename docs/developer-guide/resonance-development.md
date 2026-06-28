# Resonance Development

When adding a Resonance automation capability, keep the change aligned across these layers:

```text
plans/resonance/tasks/
plans/resonance/src/actions/
plans/resonance/src/services/
plans/resonance/data/
packages/resonance_gui/task_specs.py
tests/test_resonance_*.py
```

Task YAML should use canonical shared actions such as `plans/aura_base/click` and `plans/aura_base/sleep`. Cross-task execution should use `aura.run_task` with a canonical `task_ref`.

After adding or changing plan exports:

```powershell
python -m packages.aura_core.cli.package_cli sync plans/resonance
python -m packages.aura_core.cli.package_cli check plans/resonance
python tools\plan_doctor.py --plan resonance
```

The GUI workbench task list is explicit. Add a new `TaskSpec` only for tasks that should be user-facing in the desktop workflow.
