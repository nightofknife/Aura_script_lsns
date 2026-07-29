from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from packages.aura_core.config.validator import validate_task_definition
from plans.resonance_pc.src.actions import reforming_center_pc_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = REPO_ROOT / "plans" / "resonance_pc" / "tasks" / "admit_prisoners_pc.yaml"


class _FakeApp:
    def __init__(self):
        self.clicks = []

    def click(self, x=None, y=None, **_kwargs):
        self.clicks.append((int(x), int(y)))


def test_extract_fraction_accepts_single_and_split_ocr_rows():
    assert actions._extract_fraction([{"text": "整顿中囚犯数量 0 / 24", "center": [100, 10]}]) == (0, 24)
    assert actions._extract_fraction(
        [
            {"text": "0", "center": [100, 10]},
            {"text": "/", "center": [120, 10]},
            {"text": "24", "center": [140, 10]},
        ]
    ) == (0, 24)


def test_extract_success_count_accepts_admission_document_text():
    assert actions._extract_success_count(
        [{"text": "成功办理24名囚犯入狱!", "center": [400, 80]}]
    ) == 24


def test_admit_all_available_prisoners_uses_only_safe_controls(monkeypatch):
    app = _FakeApp()
    marker_calls = []
    fraction_results = iter(
        [
            {"current": 0, "capacity": 24},
            {"current": 32, "capacity": 32},
            {"current": 0, "capacity": 24},
            {"current": 4, "capacity": 24},
            {"current": 24, "capacity": 24},
        ]
    )
    marker_centers = {
        "囚犯名册": [300, 40],
        "办理囚犯入住": [1160, 680],
        "办理入住": [300, 40],
        "一键全选": [950, 680],
        "确认办理": [1160, 680],
    }

    monkeypatch.setattr(
        actions,
        "_detect_overview_once",
        lambda *_args, **_kwargs: {"found": True},
    )
    monkeypatch.setattr(
        actions,
        "_wait_for_overview",
        lambda *_args, **_kwargs: {"found": True},
    )
    monkeypatch.setattr(
        actions,
        "_read_fraction",
        lambda *_args, **_kwargs: next(fraction_results),
    )
    monkeypatch.setattr(
        actions,
        "_wait_for_success_count",
        lambda *_args, **_kwargs: {
            "found": True,
            "admitted_count": 24,
            "recognized_texts": ["成功办理24名囚犯入狱!"],
        },
    )

    def wait_for_marker(*_args, markers, **_kwargs):
        marker_calls.extend(markers)
        marker = markers[0]
        return {"text": marker, "marker": marker, "center": marker_centers[marker]}

    monkeypatch.setattr(actions, "_wait_for_marker", wait_for_marker)
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)

    result = actions.resonance_pc_admit_all_available_prisoners(
        after_click_sec=0,
        app=app,
        ocr=object(),
    )

    assert result["status"] == "admitted"
    assert result["initial_count"] == 0
    assert result["admitted_count"] == 24
    assert result["final_count"] == 24
    assert result["roster_count_matches"] is False
    assert result["roster_count"] == {"current": 4, "capacity": 24}
    assert result["final_overview_count"] == {"current": 24, "capacity": 24}
    assert app.clicks == [
        (850, 40),
        (1160, 680),
        (950, 680),
        (1160, 680),
        (100, 360),
        (82, 37),
    ]
    assert "放TA一马" not in marker_calls
    assert "全部放生" not in marker_calls


def test_admit_all_available_prisoners_stops_when_center_is_full(monkeypatch):
    app = _FakeApp()
    monkeypatch.setattr(
        actions,
        "_detect_overview_once",
        lambda *_args, **_kwargs: {"found": True},
    )
    monkeypatch.setattr(
        actions,
        "_read_fraction",
        lambda *_args, **_kwargs: {"current": 24, "capacity": 24},
    )

    result = actions.resonance_pc_admit_all_available_prisoners(app=app, ocr=object())

    assert result["status"] == "already_full"
    assert result["admitted_count"] == 0
    assert app.clicks == []


def test_admit_all_available_prisoners_requires_final_overview_count(monkeypatch):
    app = _FakeApp()
    fraction_results = iter(
        [
            {"current": 0, "capacity": 24},
            {"current": 32, "capacity": 32},
            {"current": 0, "capacity": 24},
            {"current": 24, "capacity": 24},
            {"current": 23, "capacity": 24},
        ]
    )
    marker_centers = {
        "囚犯名册": [300, 40],
        "办理囚犯入住": [1160, 680],
        "办理入住": [300, 40],
        "一键全选": [950, 680],
        "确认办理": [1160, 680],
    }

    monkeypatch.setattr(actions, "_detect_overview_once", lambda *_args, **_kwargs: {"found": True})
    monkeypatch.setattr(actions, "_wait_for_overview", lambda *_args, **_kwargs: {"found": True})
    monkeypatch.setattr(actions, "_read_fraction", lambda *_args, **_kwargs: next(fraction_results))
    monkeypatch.setattr(
        actions,
        "_wait_for_success_count",
        lambda *_args, **_kwargs: {"found": True, "admitted_count": 24},
    )
    monkeypatch.setattr(
        actions,
        "_wait_for_marker",
        lambda *_args, markers, **_kwargs: {
            "text": markers[0],
            "marker": markers[0],
            "center": marker_centers[markers[0]],
        },
    )
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)

    with pytest.raises(actions.ReformingCenterNavigationError) as exc_info:
        actions.resonance_pc_admit_all_available_prisoners(
            after_click_sec=0,
            app=app,
            ocr=object(),
        )

    assert exc_info.value.code == "admission_result_count_mismatch"
    assert exc_info.value.detail["final_overview_count"] == {"current": 23, "capacity": 24}


def test_admit_prisoners_task_schema_and_wiring():
    task_data = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))
    task = task_data["admit_prisoners_pc"]

    ok, error = validate_task_definition(task_data)

    assert ok is True, error
    assert list(task["steps"]) == ["ensure_reforming_center", "admit"]
    assert task["steps"]["ensure_reforming_center"]["action"] == "resonance_pc.enter_reforming_center"
    assert task["steps"]["admit"]["action"] == "resonance_pc.admit_all_available_prisoners"
    assert task["steps"]["admit"]["depends_on"] == "ensure_reforming_center"
