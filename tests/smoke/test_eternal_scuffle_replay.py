"""Execute the real Scuffle task graph through local and IPC runners, with no OS input."""
from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path

import pytest
import yaml

from packages.aura_game import EmbeddedGameRunner, SubprocessGameRunner

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "plans/resonance_pc/tasks/eternal_scuffle_pc.yaml"
ACTIONS = ROOT / "plans/resonance_pc/src/actions/eternal_scuffle_pc_actions.py"
FIXTURE = ROOT / "tests/fixtures/eternal_scuffle_replay/scripted_surface.py"


def build_replay_project(tmp_path, *, missing_finish=False, failed_action=False, cancellation=False):
    base = tmp_path / "project"
    plan = base / "plans/scuffle_replay"
    (plan / "tasks").mkdir(parents=True)
    tasks = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    if missing_finish:
        # Valid YAML child that performs its actions but omits the contractual completion node.
        child = tasks["scuffle_draft_pair_pc"]
        child["steps"].pop("finish")
        child.pop("returns")
    (plan / "tasks/eternal_scuffle_pc.yaml").write_text(yaml.safe_dump(tasks, allow_unicode=True), encoding="utf-8")
    module = "scuffle_replay_fixture_" + re.sub(r"\W", "_", tmp_path.name)
    wrappers = ACTIONS.read_text(encoding="utf-8")
    wrappers = wrappers[wrappers.index("@action_info"):]
    wrappers = wrappers.replace('app="plans/aura_base/app", vision="plans/aura_base/vision", ', "")
    wrappers = wrappers.replace("    return await runtime.initialize", "    app, vision = GAME, None\n    return await runtime.initialize")
    wrappers = wrappers.replace("    return await runtime.invoke", "    app, vision = GAME, None\n    return await replay_invoke")
    wrappers = wrappers.replace("return await runtime.finish", "return await replay_finish")
    source = FIXTURE.read_text(encoding="utf-8") + "\nfrom typing import Any\nfrom packages.aura_core.api import action_info, requires_services\n" + wrappers
    if failed_action:
        source += "\nGAME.fail_second_pair = True\n"
    if cancellation:
        source += "\nGAME.pause_second_pair = True\n"
    (base / f"{module}.py").write_text(source, encoding="utf-8")
    actions = []
    for node in ast.parse(wrappers).body:
        if isinstance(node, ast.AsyncFunctionDef):
            decorator = node.decorator_list[0]
            name = next(kw.value.value for kw in decorator.keywords if kw.arg == "name")
            actions.append({"name": name, "module": module, "function": node.name, "public": True})
    manifest = {"package": {"name": "@plans/scuffle_replay", "version": "0.1.0", "description": "Offline replay", "license": "MIT"},
                "requires": {"aura": ">=2.0.0"}, "dependencies": {},
                "exports": {"services": [], "actions": actions, "tasks": []}}
    (plan / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return base


def assert_existing_logger_only(base, *, expected_kind):
    """No Scuffle journal/diagnostic artifacts; details reach real framework log."""
    assert not (base / "logs/eternal_scuffle").exists()
    prohibited = {"events.jsonl", "summary.json", "failure.json", "last_frame.png", "last_target.png"}
    assert not [path for path in base.rglob("*") if path.is_file() and path.name in prohibited]
    logs = list((base / "logs").glob("aura_session_*.log*"))
    assert logs, "The real framework file logger did not create its normal log"
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in logs)
    rows = [json.loads(line.split("[EternalScuffle] ", 1)[1])
            for line in text.splitlines() if "[EternalScuffle] " in line]
    assert rows, "Scuffle details did not reach the existing logger"
    assert any(row.get("type") == expected_kind for row in rows), rows[-10:]
    return rows


@pytest.mark.parametrize("subprocess", [False, True], ids=["embedded-orchestrator", "subprocess-ipc"])
def test_real_scuffle_graph_two_round_replay(tmp_path, monkeypatch, subprocess):
    base = build_replay_project(tmp_path)
    from plans.resonance_pc.src.actions import _eternal_scuffle_runtime as runtime
    # Fixture modules patch observer construction only; restore those seams after embedded execution.
    for name in ("catalog_for", "make_observer", "poll_until", "aura_sleep"):
        monkeypatch.setattr(runtime, name, getattr(runtime, name))
    monkeypatch.setenv("AURA_BASE_PATH", str(base))
    runner = SubprocessGameRunner(env_overrides={"AURA_BASE_PATH": str(base)}) if subprocess else EmbeddedGameRunner()
    try:
        result = runner.run_task(game_name="scuffle_replay", task_ref="tasks:eternal_scuffle_pc.yaml:eternal_scuffle_pc",
                                 inputs={"coins_per_run": 1, "run_count": 2}, wait=True, timeout_sec=90)
        (base / "runner_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        assert result["run"]["detail"]["status"] == "success", result
        audit = json.loads((base / "replay_audit.json").read_text(encoding="utf-8"))
        summary = audit["summary"]
        assert summary["completed_runs"] == 2
        assert summary["cleared_runs"] == summary["abandoned_runs"] == 1
        assert [row["outcome"] for row in summary["rounds"]] == ["cleared", "abandoned"]
        assert audit["session_deleted"] is True
        assert len([c for c in audit["clicks"] if c["control"] == "play"]) == 2
        assert len([c for c in audit["clicks"] if c["control"] == "min"]) == 2
        assert not [c for c in audit["clicks"] if c["control"] in {"plus", "max"}]
        assert audit["assignments"] == [{"round": i, "id": 1, "screen_index": 4} for i in (1, 2)]
        boxes = [c for c in audit["clicks"] if c["scene"] == "settlement" and c["control"] is None]
        assert [len([c for c in boxes if c["round"] == i]) for i in (1, 2)] == [7, 4]
        assert audit["observations"] > len(audit["clicks"]) * 2
        events = runner.poll_events(limit=10000, timeout_sec=0.1)
        (base / "bridge_events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        progress = [event["payload"] for event in events
                    if event.get("name") == runtime.PROGRESS_EVENT or event.get("event") == runtime.PROGRESS_EVENT]
        assert progress, {"event_keys": [list(event) for event in events[:3]]}
        assert all(event["cid"] == result["dispatch"]["cid"] for event in progress)
        sequences = [event["sequence"] for event in progress]
        assert sequences == sorted(set(sequences))
        assert progress[-1]["stage"] == progress[-1]["status"] == "completed"
        rows = assert_existing_logger_only(base, expected_kind="round_completed")
        assert any(row.get("type") == "equipment_assigned" for row in rows)
        completed = [row for row in rows if row.get("type") == "run_completed"]
        assert len(completed) == 1
        assert completed[0]["result"]["rounds"] == summary["rounds"]
    finally:
        runner.close()


@pytest.mark.parametrize("failure", ["missing_finish", "failed_action"])
def test_real_scuffle_graph_failure_stops_before_next_coin(tmp_path, monkeypatch, failure):
    base = build_replay_project(tmp_path, **{failure: True})
    from plans.resonance_pc.src.actions import _eternal_scuffle_runtime as runtime
    for name in ("catalog_for", "make_observer", "poll_until", "aura_sleep"):
        monkeypatch.setattr(runtime, name, getattr(runtime, name))
    monkeypatch.setenv("AURA_BASE_PATH", str(base))
    runner = EmbeddedGameRunner()
    try:
        result = runner.run_task(game_name="scuffle_replay", task_ref="tasks:eternal_scuffle_pc.yaml:eternal_scuffle_pc",
                                 inputs={"coins_per_run": 1, "run_count": 2}, wait=True, timeout_sec=90)
        assert result["run"]["detail"]["status"] in {"error", "failed"}, result
        audit = json.loads((base / "replay_audit.json").read_text(encoding="utf-8"))
        assert len([c for c in audit["clicks"] if c["control"] == "play"]) == 1
        assert "summary" not in audit
        assert len([c for c in audit["clicks"] if c["scene"] == "role_select"]) == 1
        assert len([c for c in audit["clicks"] if c["scene"] == "initial_equipment"]) == 1
        assert_existing_logger_only(base, expected_kind="failure")
    finally:
        runner.close()


def test_real_scuffle_subprocess_cancellation_stops_next_choice_and_coin(tmp_path):
    base = build_replay_project(tmp_path, cancellation=True)
    runner = SubprocessGameRunner(env_overrides={"AURA_BASE_PATH": str(base)})
    try:
        dispatch = runner.run_task(game_name="scuffle_replay", task_ref="tasks:eternal_scuffle_pc.yaml:eternal_scuffle_pc",
                                   inputs={"coins_per_run": 1, "run_count": 2}, wait=False)
        deadline = time.monotonic() + 15
        while not (base / "cancellation_ready.json").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (base / "cancellation_ready.json").exists(), dispatch
        cancelled = runner.cancel_task(dispatch["cid"])
        deadline = time.monotonic() + 15
        result = runner.get_run(dispatch["cid"])
        while result.get("status") not in {"cancelled", "success", "failed", "error"} and time.monotonic() < deadline:
            time.sleep(0.02)
            result = runner.get_run(dispatch["cid"])
        (base / "cancel_result.json").write_text(json.dumps({"cancel": cancelled, "run": result}, default=str, indent=2), encoding="utf-8")
        assert result["status"] == "cancelled", result
        audit = json.loads((base / "replay_audit.json").read_text(encoding="utf-8"))
        assert len([c for c in audit["clicks"] if c["control"] == "play"]) == 1
        assert len([c for c in audit["clicks"] if c["scene"] == "role_select"]) == 1
        assert len([c for c in audit["clicks"] if c["scene"] == "initial_equipment"]) == 1
        assert "summary" not in audit
        time.sleep(0.1)
        after = json.loads((base / "replay_audit.json").read_text(encoding="utf-8"))
        assert after["clicks"] == audit["clicks"]
        rows = assert_existing_logger_only(base, expected_kind="cancelled")
        assert any("state" in row and "observation" in row and "last_target" in row for row in rows)
    finally:
        runner.close()
