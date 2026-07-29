from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from packages.aura_core.config.validator import validate_task_definition
from plans.resonance_pc.src.actions import reforming_center_pc_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = (
    REPO_ROOT
    / "plans"
    / "resonance_pc"
    / "tasks"
    / "navigate_reforming_center_room_pc.yaml"
)


class _FakeApp:
    def __init__(self):
        self.clicks = []
        self.moves = []

    def get_window_size(self):
        return (1280, 720)

    def click(self, x=None, y=None, **_kwargs):
        self.clicks.append((int(x), int(y)))

    def move_to(self, x=None, y=None, duration=None, **_kwargs):
        self.moves.append((int(x), int(y), float(duration or 0)))


class _FakeController:
    def __init__(self):
        self.events = []

    def mouse_down(self, button):
        self.events.append(("down", button))

    def mouse_up(self, button):
        self.events.append(("up", button))


def _item(text, center, confidence=0.99):
    return {
        "text": text,
        "normalized": actions._normalize_text(text),
        "confidence": confidence,
        "center": list(center),
    }


def test_room_layout_contains_all_screenshot_rooms_and_aliases():
    rooms = actions._load_reforming_center_room_layout()
    aliases = actions._build_room_alias_lookup(rooms)

    assert len(rooms) == 32
    assert actions._resolve_room_key("囚犯管理中心", rooms, aliases) == "囚犯管理中心"
    assert actions._resolve_room_key("管理中心", rooms, aliases) == "囚犯管理中心"
    assert actions._resolve_room_key("缝工具原料车间", rooms, aliases) == "缝纫工具原料车间"
    assert actions._resolve_room_key("2号宿舍", rooms, aliases) == "2号整顿宿舍"


def test_plan_room_drag_keeps_both_endpoints_inside_safe_center_region():
    plan = actions._plan_room_drag(
        predicted_target_title=[1250, 700],
        desired_target_title=[520, 300],
        drag_region=[420, 170, 520, 360],
    )

    assert plan["requested_delta"] == [-730, -400]
    assert plan["applied_delta"] == [-480, -320]
    assert actions._point_in_region(plan["start"], [420, 170, 520, 360], margin=20)
    assert actions._point_in_region(plan["end"], [420, 170, 520, 360], margin=20)


def test_room_visible_at_edge_is_recentered_before_click(monkeypatch):
    app = _FakeApp()
    controller = _FakeController()
    observations = iter(
        [
            [
                _item("囚犯管理中心", [1010, 520]),
                _item("安保中心", [650, 520]),
            ],
            [_item("囚犯管理中心", [520, 300])],
        ]
    )
    monkeypatch.setattr(actions, "_detect_overview_once", lambda *_args, **_kwargs: {"found": True})
    monkeypatch.setattr(actions, "_capture_text_items", lambda *_args, **_kwargs: next(observations))
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)

    result = actions.resonance_pc_navigate_reforming_center_room(
        room_name="囚犯管理中心",
        app=app,
        ocr=object(),
        controller=controller,
    )

    assert result["status"] == "room_clicked"
    assert result["drag_count"] == 1
    assert app.clicks == [(640, 355)]
    assert controller.events == [("down", "left"), ("up", "left")]
    assert len(app.moves) == 2
    for x, y, _duration in app.moves:
        assert actions._point_in_region([x, y], [420, 170, 520, 360], margin=20)


def test_room_already_inside_click_region_is_clicked_without_drag(monkeypatch):
    app = _FakeApp()
    controller = _FakeController()
    monkeypatch.setattr(actions, "_detect_overview_once", lambda *_args, **_kwargs: {"found": True})
    monkeypatch.setattr(
        actions,
        "_capture_text_items",
        lambda *_args, **_kwargs: [_item("1号生产车间 LV1", [520, 300])],
    )
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)

    result = actions.resonance_pc_navigate_reforming_center_room(
        room_name="1号生产车间",
        app=app,
        ocr=object(),
        controller=controller,
    )

    assert result["drag_count"] == 0
    assert app.clicks == [(640, 355)]
    assert app.moves == []
    assert controller.events == []


def test_unknown_room_is_rejected_before_any_pointer_input(monkeypatch):
    app = _FakeApp()
    controller = _FakeController()
    monkeypatch.setattr(actions, "_detect_overview_once", lambda *_args, **_kwargs: {"found": True})

    with pytest.raises(actions.ReformingCenterNavigationError) as exc_info:
        actions.resonance_pc_navigate_reforming_center_room(
            room_name="不存在的房间",
            app=app,
            ocr=object(),
            controller=controller,
        )

    assert exc_info.value.code == "unknown_reforming_center_room"
    assert app.clicks == []
    assert app.moves == []
    assert controller.events == []


def test_no_known_anchor_refuses_blind_drag(monkeypatch):
    app = _FakeApp()
    controller = _FakeController()
    monkeypatch.setattr(actions, "_detect_overview_once", lambda *_args, **_kwargs: {"found": True})
    monkeypatch.setattr(
        actions,
        "_capture_text_items",
        lambda *_args, **_kwargs: [_item("事项一览", [1160, 120])],
    )
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)

    with pytest.raises(actions.ReformingCenterNavigationError) as exc_info:
        actions.resonance_pc_navigate_reforming_center_room(
            room_name="原料仓库",
            max_no_anchor_polls=2,
            app=app,
            ocr=object(),
            controller=controller,
        )

    assert exc_info.value.code == "room_anchor_not_found"
    assert app.clicks == []
    assert app.moves == []
    assert controller.events == []


def test_navigate_reforming_center_room_task_schema_and_wiring():
    task_data = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))
    task = task_data["navigate_reforming_center_room_pc"]

    ok, error = validate_task_definition(task_data)

    assert ok is True, error
    assert list(task["steps"]) == ["ensure_reforming_center", "navigate"]
    assert task["steps"]["ensure_reforming_center"]["action"] == "resonance_pc.enter_reforming_center"
    assert task["steps"]["navigate"]["action"] == "resonance_pc.navigate_reforming_center_room"
    assert task["steps"]["navigate"]["depends_on"] == "ensure_reforming_center"
    assert task["steps"]["navigate"]["params"]["room_name"] == "{{ inputs.room_name }}"
