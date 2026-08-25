"""Smoke checks for the Consciousness Deep Dive entry task and GUI wiring."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets import SmallTasksPage
from plans.resonance_pc.src.actions import consciousness_deep_dive_pc_actions as deep_dive


def _application() -> QApplication:
    return QApplication.instance() or QApplication(["aura-deep-dive-smoke"])


def _match(*, found: bool, confidence: float = 0.95) -> SimpleNamespace:
    return SimpleNamespace(
        found=found,
        confidence=confidence,
        center_point=(650, 550) if found else None,
    )


def test_click_rechecks_original_after_each_300ms_delay(monkeypatch) -> None:
    waits: list[float] = []
    clicks: list[tuple[int, int]] = []
    rechecks = iter((_match(found=True), _match(found=False, confidence=0.2)))

    async def fake_wait_for_image(**_kwargs):
        return _match(found=True)

    async def fake_sleep(seconds: float):
        waits.append(float(seconds))

    monkeypatch.setattr(deep_dive, "aura_wait_for_image", fake_wait_for_image)
    monkeypatch.setattr(deep_dive, "aura_find_image", lambda **_kwargs: next(rechecks))
    monkeypatch.setattr(
        deep_dive,
        "aura_click",
        lambda *, app, x, y: clicks.append((int(x), int(y))),
    )
    monkeypatch.setattr(deep_dive, "aura_sleep", fake_sleep)

    result = asyncio.run(
        deep_dive._click_until_original_absent(
            deep_dive._START_DIVE,
            app=object(),
            vision=object(),
            engine=object(),
        )
    )

    assert clicks == [(650, 550), (650, 550)]
    assert waits == [0.3, 0.3]
    assert result["click_attempts"] == 2
    assert result["last_recheck"]["found"] is False


def test_difficulty_one_skips_drag_when_already_selected(monkeypatch) -> None:
    drags: list[dict] = []

    def fake_find_target(target, **_kwargs):
        return _match(found=target is deep_dive._DIFFICULTY_1_SELECTED)

    async def fake_wait_for_target(target, **_kwargs):
        assert target is deep_dive._DIFFICULTY_1_SELECTED
        return _match(found=True)

    monkeypatch.setattr(deep_dive, "_find_target", fake_find_target)
    monkeypatch.setattr(deep_dive, "_wait_for_target", fake_wait_for_target)
    monkeypatch.setattr(deep_dive, "aura_drag", lambda **kwargs: drags.append(kwargs))

    result = asyncio.run(
        deep_dive._ensure_difficulty_one(
            app=object(),
            vision=object(),
            engine=object(),
        )
    )

    assert result["status"] == "already_selected"
    assert result["drag_attempts"] == 0
    assert result["click_attempts"] == 0
    assert result["selection_click"] is None
    assert drags == []


def test_difficulty_one_drag_holds_endpoint_before_release(monkeypatch) -> None:
    drags: list[dict] = []
    waits: list[float] = []
    app = object()

    def fake_find_target(target, **_kwargs):
        if target is deep_dive._DIFFICULTY_1_SELECTED:
            return _match(found=False)
        return _match(found=bool(drags))

    async def fake_click_until_absent(target, **_kwargs):
        assert target is deep_dive._DIFFICULTY_1_UNSELECTED
        return {"click_attempts": 1, "clicks": []}

    async def fake_wait_for_target(target, **_kwargs):
        assert target is deep_dive._DIFFICULTY_1_SELECTED
        return _match(found=True)

    async def fake_sleep(seconds: float):
        waits.append(float(seconds))

    monkeypatch.setattr(deep_dive, "_find_target", fake_find_target)
    monkeypatch.setattr(
        deep_dive,
        "_click_until_original_absent",
        fake_click_until_absent,
    )
    monkeypatch.setattr(deep_dive, "_wait_for_target", fake_wait_for_target)
    monkeypatch.setattr(deep_dive, "aura_drag", lambda **kwargs: drags.append(kwargs))
    monkeypatch.setattr(deep_dive, "aura_sleep", fake_sleep)

    result = asyncio.run(
        deep_dive._ensure_difficulty_one(
            app=app,
            vision=object(),
            engine=object(),
        )
    )

    assert len(drags) == 1
    assert drags[0]["app"] is app
    assert {key: value for key, value in drags[0].items() if key != "app"} == {
        "start_x": 900,
        "start_y": 360,
        "end_x": 500,
        "end_y": 360,
        "duration": 0.5,
        "hold_before_release_sec": 0.4,
    }
    assert waits == [0.3]
    assert result["status"] == "selected"
    assert result["drag_attempts"] == 1
    assert result["click_attempts"] == 1
    assert result["selection_click"]["click_attempts"] == 1


def test_entry_action_runs_difficulty_check_before_start_game(monkeypatch) -> None:
    calls: list[tuple[str, str, str, tuple[int, int] | None, str | None]] = []
    timeline: list[str] = []

    async def fake_transition(step, control, next_state, **kwargs):
        timeline.append(str(step))
        calls.append(
            (
                str(step),
                str(control.key),
                str(next_state.key),
                kwargs.get("fixed_click_point"),
                kwargs.get("next_error_code"),
            )
        )
        return {"step": step, "click_attempts": 1}

    async def fake_ensure_difficulty(**_kwargs):
        timeline.append("ensure_difficulty_1")
        return {
            "step": "ensure_difficulty_1",
            "status": "already_selected",
            "drag_attempts": 0,
        }

    monkeypatch.setattr(deep_dive, "aura_get_window_size", lambda **_kwargs: (1280, 720))
    monkeypatch.setattr(deep_dive, "_run_transition", fake_transition)
    monkeypatch.setattr(deep_dive, "_ensure_difficulty_one", fake_ensure_difficulty)

    result = asyncio.run(
        deep_dive.resonance_pc_consciousness_deep_dive_enter_stage(
            app=object(),
            vision=object(),
            engine=object(),
        )
    )

    assert [row[0] for row in calls] == [
        "start_dive",
        "start_game",
        "select_strategy",
        "confirm_strategy",
        "confirm_formation",
        "select_middle_boon",
        "confirm_boon",
        "dismiss_reward",
    ]
    assert timeline[:3] == ["start_dive", "ensure_difficulty_1", "start_game"]
    assert calls[5][4] == "deep_dive_boon_selection_not_confirmed"
    assert calls[-1][3] == (640, 680)
    assert result["status"] == "completed"
    assert result["page_state"] == "deep_dive_board"
    assert len(result["transitions"]) == 9


def test_difficulty_failure_blocks_start_game(monkeypatch) -> None:
    timeline: list[str] = []

    async def fake_transition(step, _control, _next_state, **_kwargs):
        timeline.append(str(step))
        return {"step": step}

    async def fail_difficulty(**_kwargs):
        timeline.append("ensure_difficulty_1")
        raise deep_dive.ConsciousnessDeepDiveError(
            "deep_dive_difficulty_1_not_found",
            "difficulty not found",
        )

    monkeypatch.setattr(deep_dive, "aura_get_window_size", lambda **_kwargs: (1280, 720))
    monkeypatch.setattr(deep_dive, "_run_transition", fake_transition)
    monkeypatch.setattr(deep_dive, "_ensure_difficulty_one", fail_difficulty)

    with pytest.raises(
        deep_dive.ConsciousnessDeepDiveError,
        match="deep_dive_difficulty_1_not_found",
    ):
        asyncio.run(
            deep_dive.resonance_pc_consciousness_deep_dive_enter_stage(
                app=object(),
                vision=object(),
                engine=object(),
            )
        )

    assert timeline == ["start_dive", "ensure_difficulty_1"]


def test_small_tasks_page_runs_and_renders_deep_dive(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    page = SmallTasksPage(ResonanceConfigRepository(settings))
    activity_category = page.category_list.findItems(
        "活动玩法", Qt.MatchFlag.MatchExactly
    )[0]
    page.category_list.setCurrentItem(activity_category)

    requests: list[bool] = []
    page.runConsciousnessDeepDiveRequested.connect(lambda: requests.append(True))
    page.consciousness_deep_dive_panel.run_button.click()
    assert requests == [True]

    page.begin_consciousness_deep_dive_run()
    page.set_runner_busy(True)
    assert page.consciousness_deep_dive_panel.cancel_button.isEnabled()
    page.set_runner_busy(False)
    page.apply_consciousness_deep_dive_result(
        {
            "status": "completed",
            "page_state": "deep_dive_board",
            "transitions": [{"click_attempts": 2}, {"click_attempts": 1}],
            "elapsed_ms": 3200,
        }
    )

    panel = page.consciousness_deep_dive_panel
    assert panel.status_label.text() == "已进入识海深潜棋盘"
    assert "完成 2 个页面转换" in panel.summary_label.text()
    assert "点击 3 次" in panel.summary_label.text()


def test_main_window_dispatches_and_routes_deep_dive_result(tmp_path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = ResonanceMainWindow(
        settings=ResonanceConfigRepository(settings),
        initialize_on_startup=False,
        update_checker=lambda: "",
    )
    try:
        window.requestRunPcTask.disconnect()
        dispatches: list[tuple[str, dict, str, float]] = []
        window.requestRunPcTask.connect(
            lambda task_ref, inputs, label, timeout: dispatches.append(
                (str(task_ref), dict(inputs), str(label), float(timeout))
            )
        )
        activity_category = window.small_tasks_page.category_list.findItems(
            "活动玩法", Qt.MatchFlag.MatchExactly
        )[0]
        window.small_tasks_page.category_list.setCurrentItem(activity_category)
        window.small_tasks_page.consciousness_deep_dive_panel.run_button.click()

        assert dispatches == [(PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF, {}, "识海深潜", 0.0)]
        window._active_game_name = "resonance_pc"
        window._on_task_finished(
            {
                "status": "success",
                "gui_item": {
                    "kind": "workflow_task",
                    "task_ref": PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF,
                },
                "final_result": {
                    "deep_dive": {
                        "status": "completed",
                        "page_state": "deep_dive_board",
                        "transitions": [],
                        "elapsed_ms": 1000,
                    }
                },
            }
        )

        assert (
            window.small_tasks_page.consciousness_deep_dive_panel.status_label.text()
            == "已进入识海深潜棋盘"
        )
        assert window._small_task_active_ref == ""
    finally:
        window.close()
        _application().processEvents()
