from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from packages.aura_core.config.validator import validate_task_definition
from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult
from plans.resonance_pc.src.actions import reforming_center_pc_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = REPO_ROOT / "plans" / "resonance_pc" / "tasks" / "enter_reforming_center_pc.yaml"


class _FakeApp:
    def __init__(self):
        self.capture_rects = []
        self.clicks = []

    def capture(self, rect=None):
        self.capture_rects.append(rect)
        return SimpleNamespace(
            success=True,
            image=np.zeros((int(rect[3]), int(rect[2]), 3), dtype=np.uint8),
        )

    def click(self, x=None, y=None, **_kwargs):
        self.clicks.append((int(x), int(y)))


class _FakeOcr:
    def __init__(self, batches):
        self.batches = list(batches)

    def recognize_all(self, source_image):
        texts = self.batches.pop(0) if self.batches else []
        return MultiOcrResult(
            count=len(texts),
            results=[
                OcrResult(
                    found=True,
                    text=text,
                    center_point=(50 + index * 10, 40 + index * 10),
                    confidence=0.99 - index * 0.01,
                )
                for index, text in enumerate(texts)
            ],
        )


def test_enter_reforming_center_from_main(monkeypatch):
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)
    app = _FakeApp()
    ocr = _FakeOcr(
        [
            [],
            ["访问城市"],
            ["整顿中心"],
            ["加载中"],
            ["事项一览", "稳定度"],
        ]
    )

    result = actions.resonance_pc_enter_reforming_center(
        main_timeout_sec=0,
        menu_timeout_sec=0,
        entry_timeout_sec=1,
        interval_sec=0.05,
        after_profile_click_sec=0,
        app=app,
        ocr=ocr,
    )

    assert result["success"] is True
    assert result["status"] == "entered"
    assert result["page_state"] == "reforming_center_overview"
    assert result["overview"]["primary"]["marker"] == "事项一览"
    assert result["overview"]["secondary"]["marker"] == "稳定度"
    assert app.clicks == [(155, 660), (550, 580)]


def test_enter_reforming_center_is_idempotent_when_already_inside():
    app = _FakeApp()
    ocr = _FakeOcr([["事项一览", "安全度"]])

    result = actions.resonance_pc_enter_reforming_center(app=app, ocr=ocr)

    assert result["success"] is True
    assert result["status"] == "already_there"
    assert result["page_state"] == "reforming_center_overview"
    assert app.clicks == []


def test_enter_reforming_center_requires_menu_target(monkeypatch):
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)
    app = _FakeApp()
    ocr = _FakeOcr([[], ["访问地区"], ["任务", "活动", "仓库"]])

    with pytest.raises(actions.ReformingCenterNavigationError) as exc_info:
        actions.resonance_pc_enter_reforming_center(
            main_timeout_sec=0,
            menu_timeout_sec=0,
            interval_sec=0.05,
            after_profile_click_sec=0,
            app=app,
            ocr=ocr,
        )

    assert exc_info.value.code == "profile_menu_target_not_found"
    assert app.clicks == [(155, 660)]


def test_enter_reforming_center_task_schema_and_wiring():
    task_data = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))
    task = task_data["enter_reforming_center_pc"]

    ok, error = validate_task_definition(task_data)

    assert ok is True, error
    assert set(task_data) == {"enter_reforming_center_pc"}
    assert task["meta"]["entry_point"] is True
    assert list(task["steps"]) == ["enter"]
    assert task["steps"]["enter"]["action"] == "resonance_pc.enter_reforming_center"
    assert task["returns"]["page_state"] == "{{ nodes.enter.output.page_state }}"
