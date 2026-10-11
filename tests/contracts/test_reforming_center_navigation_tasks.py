"""Open-room task contract; ordinary overview is a caller-owned precondition."""

import ast
from pathlib import Path

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

from packages.aura_core.config.validator import (
    get_task_schema_validator,
    validate_task_definition,
)
from packages.aura_core.scheduler.validation import InputValidator


ROOT = Path(__file__).resolve().parents[2]
TASK_NAME = "open_reforming_center_room_pc"
RETURN_FIELDS = {
    "success", "status", "room_name", "entry_type", "room_entered", "reason", "data",
}


def _task():
    path = ROOT / "plans/resonance_pc/tasks" / f"{TASK_NAME}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(document) == {TASK_NAME}
    return document[TASK_NAME]


def test_public_task_definition_validates():
    assert get_task_schema_validator() is not None
    valid, reason = validate_task_definition({TASK_NAME: _task()})
    assert valid, reason


def test_open_is_resolution_checked_without_state_detection_entry_or_return():
    spec = _task()
    assert spec["meta"]["entry_point"] is True
    assert spec["meta"]["concurrency"] == "exclusive"
    assert list(spec["steps"]) == ["require_resolution", "open_room"]
    assert spec["steps"]["require_resolution"] == {
        "action": "resonance_pc.require_client_resolution",
    }
    assert spec["steps"]["open_room"]["action"] == "resonance_pc.open_reforming_center_room"
    assert spec["steps"]["open_room"]["depends_on"] == "require_resolution"
    assert all(node["action"] != "aura.run_task" for node in spec["steps"].values())


def test_defaults_and_parameters_forward_without_hidden_inputs():
    spec = _task()
    expected = {"room_name": "财务中心", "max_search_steps": 12, "settle_sec": 0.8}
    ok, values = InputValidator(None).validate_inputs_against_meta(
        spec["meta"]["inputs"], {"room_name": "财务中心"},
    )
    assert ok, values
    assert values == expected
    rendered = {
        key: NativeEnvironment().from_string(value).render(inputs=values)
        for key, value in spec["steps"]["open_room"]["params"].items()
    }
    assert rendered == expected
    assert {row["name"] for row in spec["meta"]["inputs"]} == set(expected)


@pytest.mark.parametrize("provided", [
    {},
    {"room_name": "财务中心", "max_search_steps": 0},
    {"room_name": "财务中心", "max_search_steps": 25},
    {"room_name": "财务中心", "settle_sec": 0.049},
    {"room_name": "财务中心", "settle_sec": 10.001},
])
def test_missing_room_and_out_of_bounds_inputs_are_rejected(provided):
    ok, _ = InputValidator(None).validate_inputs_against_meta(
        _task()["meta"]["inputs"], provided,
    )
    assert ok is False


@pytest.mark.parametrize("provided", [
    {"room_name": "财务中心", "max_search_steps": 1, "settle_sec": 0.05},
    {"room_name": "财务中心", "max_search_steps": 24, "settle_sec": 10},
])
def test_navigation_boundaries_are_inclusive(provided):
    ok, values = InputValidator(None).validate_inputs_against_meta(
        _task()["meta"]["inputs"], provided,
    )
    assert ok, values
    assert all(values[key] == value for key, value in provided.items())


@pytest.mark.parametrize(("success", "status", "entry_type", "room_entered"), [
    (True, "completed", "direct", True),
    (True, "completed", "production", True),
    (True, "completed", "detail", True),
    (True, "selected_only", "selection_only", False),
    (False, "failed", "production", False),
    (False, "failed", "direct", False),
])
def test_result_contract_preserves_business_outcome_and_entry_distinction(
    success, status, entry_type, room_entered,
):
    output = {
        "success": success,
        "status": status,
        "room_name": "植物纤维原料车间",
        "entry_type": entry_type,
        "room_entered": room_entered,
        "reason": "" if success else "Destination could not be confirmed.",
        "data": {"target": {"name": "植物纤维原料车间"}, "attempts_used": 1},
    }
    expressions = _task()["returns"]
    assert set(expressions) == RETURN_FIELDS
    rendered = {
        key: NativeEnvironment().from_string(expression).render(
            nodes={"open_room": {"output": output}},
        )
        for key, expression in expressions.items()
    }
    assert rendered == output


def _source_tree():
    source = ROOT / "plans/resonance_pc/src/actions/reforming_center_navigation_pc_actions.py"
    return ast.parse(source.read_text(encoding="utf-8"))


def _decorated_actions():
    actions = []
    for node in _source_tree().body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name):
                if decorator.func.id == "action_info":
                    metadata = {
                        row.arg: ast.literal_eval(row.value)
                        for row in decorator.keywords
                    }
                    actions.append((node, metadata))
    return actions


def test_navigation_module_exports_only_public_open_room_action():
    actions = _decorated_actions()
    assert len(actions) == 1
    node, metadata = actions[0]
    assert node.name == "resonance_pc_open_reforming_center_room"
    assert metadata["name"] == "resonance_pc.open_reforming_center_room"
    assert metadata["public"] is True
    assert metadata["read_only"] is False


def test_open_room_injects_only_app_and_has_no_ocr_parameter():
    node, _ = _decorated_actions()[0]
    service_calls = [
        decorator for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "requires_services"
    ]
    assert len(service_calls) == 1
    assert not service_calls[0].args
    services = {
        row.arg: ast.literal_eval(row.value)
        for row in service_calls[0].keywords
    }
    assert services == {"app": "plans/aura_base/app"}
    assert [row.arg for row in node.args.args] == [
        "room_name", "max_search_steps", "settle_sec", "app",
    ]


def test_template_only_navigation_has_no_ocr_symbol_or_recognition_call():
    identifiers = []
    for node in ast.walk(_source_tree()):
        if isinstance(node, ast.Name):
            identifiers.append(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            identifiers.append(node.name)
        elif isinstance(node, ast.arg):
            identifiers.append(node.arg)
    assert not any("ocr" in name.lower() for name in identifiers)
    assert "recognize_all" not in identifiers


def test_generated_manifest_has_only_open_export_for_navigation_module():
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text(encoding="utf-8"))
    actions = manifest["exports"]["actions"]
    module_actions = [
        row for row in actions
        if row["module"] == "plans.resonance_pc.src.actions.reforming_center_navigation_pc_actions"
    ]
    assert len(module_actions) == 1
    exported = module_actions[0]
    assert exported["name"] == "resonance_pc.open_reforming_center_room"
    assert exported["function"] == "resonance_pc_open_reforming_center_room"
    assert exported["public"] is True
    assert exported["read_only"] is False
    parameter_names = [row["name"] for row in exported["parameters"]]
    assert "ocr" not in parameter_names
    assert {"room_name", "max_search_steps", "settle_sec"} <= set(parameter_names)
    assert not any(row["name"] in {
        "resonance_pc.detect_reforming_center_page",
        "resonance_pc.return_reforming_center_overview",
    } for row in actions)
