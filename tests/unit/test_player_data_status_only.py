from __future__ import annotations

import copy
import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLabel

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.widgets.player_data_panel import PlayerDataPanel
from plans.resonance_pc.src.actions import player_data_pc_actions as player_data


STATUS = {
    "cargo": {"current": 126, "max": 748},
    "clarity": {"current": 282, "max": 282},
    "fatigue": {"current": 597, "max": 848},
}
OLD_IDENTITY = {"uid": "8820206170", "nickname": "旧昵称", "level": 74}


@pytest.mark.parametrize("fail_capture", [False, True])
def test_profile_refresh_only_reads_status_and_commits_after_success(tmp_path, monkeypatch, fail_capture):
    service = PersistentDataService(tmp_path / "install")
    old = {
        "profile": OLD_IDENTITY,
        "status": {"cargo": {"current": 1, "max": 2}, "future_status": 7},
        "location": {"current_city": "修格里城"},
        "inventory": {"categories": {}},
        "metadata": {"section_updated_at": {"location": "old-location", "profile": "old-profile"}},
    }
    service.set(player_data.USER_INFO_FILE, [], old)
    captured = []

    class App:
        def click(self, **kwargs):
            pass

        def capture(self, *, rect):
            captured.append(rect)
            # Any identity ROI would fail here, including the old enlarged UID crop.
            field = next(key for key, region in player_data._PROFILE_FIELD_REGIONS.items() if region == rect)
            assert field in STATUS
            if fail_capture and field == "fatigue":
                return SimpleNamespace(success=False)
            return SimpleNamespace(success=True, image=np.full((45, 125, 3), list(STATUS).index(field), dtype=np.uint8))

    class Ocr:
        def recognize_all(self, *, source_image):
            field = list(STATUS)[int(source_image[0, 0, 0])]
            value = STATUS[field]
            return SimpleNamespace(results=[SimpleNamespace(text=f"{value['current']}/{value['max']}", confidence=1.0)])

    monkeypatch.setattr(player_data, "_wait_for_any_marker", lambda *args, **kwargs: [])
    monkeypatch.setattr(player_data, "_close_profile_panel_to_main", lambda *args: None)
    monkeypatch.setattr(player_data, "_best_effort_return_to_main", lambda *args: None)
    kwargs = dict(stages=["profile"], app=App(), ocr=Ocr(), vision=object(), persistent_data=service)
    if fail_capture:
        with pytest.raises(player_data.StopTaskException, match="capture failed"):
            player_data.resonance_pc_player_data_refresh(**kwargs)
        assert service.read(player_data.USER_INFO_FILE) == old
    else:
        result = player_data.resonance_pc_player_data_refresh(**kwargs)
        assert result["status"] == STATUS
        assert "profile" not in result
        assert result["metadata"]["executed_stages"] == ["profile"]
        assert result["metadata"]["executed_profile_sections"] == ["cargo", "clarity", "fatigue"]
        assert result["metadata"]["skipped_profile_sections"] == ["sparkling_water", "work_meals", "love_bentos"]
        assert "recovery" not in result
        saved = service.read(player_data.USER_INFO_FILE)
        assert "profile" not in saved
        assert saved["status"] == {**STATUS, "future_status": 7}
        assert saved["location"] == old["location"]
        assert saved["inventory"] == old["inventory"]
        assert saved["metadata"]["section_updated_at"]["location"] == "old-location"
        assert saved["metadata"]["section_updated_at"]["profile"] == result["metadata"]["section_updated_at"]["profile"]
    assert captured == [player_data._PROFILE_FIELD_REGIONS[key] for key in STATUS]


def test_profile_retirement_preserves_unrelated_keys_and_unselected_data():
    old = {"profile": {**OLD_IDENTITY, "custom": "keep"}}
    original = copy.deepcopy(old)
    merged = player_data._merge_latest(old, {"status": STATUS}, section_updated_at={"profile": "new"}, updated_at="new")
    assert merged["profile"] == {"custom": "keep"}
    assert old == original
    location_only = player_data._merge_latest(old, {"location": {"current_city": "海角城"}}, section_updated_at={"location": "new"}, updated_at="new")
    assert location_only["profile"] == original["profile"]


def test_panel_displays_status_and_location_without_legacy_identity(tmp_path):
    app = QApplication.instance() or QApplication([])
    repository = ResonanceConfigRepository(settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    panel = PlayerDataPanel(repository)
    try:
        panel.set_snapshot({"profile": OLD_IDENTITY, "location": {"current_city": "修格里城"}, "status": STATUS})
        assert panel.location_label.text() == "当前位置：修格里城"
        assert panel.profile_value_labels["cargo"].text() == "126 / 748"
        texts = " ".join(label.text() for label in panel.findChildren(QLabel))
        for removed in ("UID", "昵称", "等级", "8820206170", "Lv.", "账号："):
            assert removed not in texts
        assert panel._stage_checks["profile"].toolTip() == "独立选择货舱、澄明度、疲劳、气泡水次数、工作餐和爱心便当"
        panel.apply_refresh_result({"status": {"fatigue": {"current": 12, "max": 848}}, "metadata": {"persisted": True, "section_updated_at": {"profile": "2026-09-05T00:00:00+00:00"}}})
        assert panel.profile_value_labels["fatigue"].text() == "12 / 848"
        assert panel.profile_value_labels["cargo"].text() == "126 / 748"
        assert panel.location_label.text() == "当前位置：修格里城"
        app.processEvents()
    finally:
        panel.close()
