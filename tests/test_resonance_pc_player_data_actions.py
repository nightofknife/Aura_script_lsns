from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from packages.aura_core.config.validator import validate_task_definition
from packages.aura_core.utils.exceptions import StopTaskException
from plans.resonance.src.actions import player_data_actions as mumu_actions
from plans.resonance_pc.src.actions import player_data_pc_actions as pc_actions


REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = REPO_ROOT / "plans" / "resonance_pc" / "tasks" / "player_data_pc.yaml"
MANIFEST_PATH = REPO_ROOT / "plans" / "resonance_pc" / "manifest.yaml"


class _FakeApp:
    def __init__(self):
        self.clicks = []

    def click(self, x=None, y=None, **_kwargs):
        self.clicks.append((int(x), int(y)))


def test_pc_player_data_coordinates_and_material_regions_match_mumu():
    names = (
        "_CLICK_PROFILE",
        "_CLICK_CURRENCY_EYE",
        "_CLICK_CONFIRM",
        "_CLICK_BACK",
        "_CLICK_CLARITY",
        "_CLICK_FATIGUE",
        "_MAIN_CITY_REGION",
        "_PROFILE_REGION",
        "_CURRENCY_POPUP_REGION",
        "_CLARITY_PAGE_REGION",
        "_FATIGUE_PAGE_REGION",
        "_MAIN_PAGE_REGION",
        "_MAIN_PAGE_MARKERS",
        "_PROFILE_FIELD_REGIONS",
        "_CURRENCY_FIELD_REGIONS",
        "_CLARITY_RATIO_REGION",
        "_FATIGUE_RATIO_REGION",
        "_CLARITY_OPTIONS",
        "_FATIGUE_OPTIONS",
    )

    for name in names:
        assert getattr(pc_actions, name) == getattr(mumu_actions, name), name


def test_pc_player_data_refresh_uses_pc_app_flow(monkeypatch):
    app = _FakeApp()
    marker_labels = []
    ratio_values = iter(
        [
            {"current": 292, "max": 292},
            {"current": 0, "max": 824},
        ]
    )

    def fake_wait(_app, _ocr, **kwargs):
        marker_labels.append(kwargs["label"])
        return [{"text": next(iter(kwargs["markers"]))}]

    monkeypatch.setattr(pc_actions, "_wait_for_any_marker", fake_wait)
    monkeypatch.setattr(
        pc_actions,
        "_capture_ocr_items",
        lambda _app, _ocr, region=None, **_kwargs: [{"text": "修格里城"}],
    )
    monkeypatch.setattr(
        pc_actions,
        "_parse_profile_panel",
        lambda _app, _ocr: {
            "profile": {"uid": "8820206170", "nickname": "面包猫南北", "level": 71},
            "currencies": {"iron_coins": 12, "birch_stone": 615},
            "status": {
                "clarity": {"current": 1, "max": 1},
                "fatigue": {"current": 1, "max": 1},
                "cargo": {"current": 20, "max": 650},
            },
        },
    )
    monkeypatch.setattr(pc_actions, "_read_int_region", lambda *_args, **_kwargs: 9132364)
    monkeypatch.setattr(pc_actions, "_read_ratio_region", lambda *_args, **_kwargs: next(ratio_values))
    monkeypatch.setattr(
        pc_actions,
        "_read_recovery_options",
        lambda _app, _ocr, specs: [{"name": specs[0]["name"], "delta": specs[0]["delta"]}],
    )
    monkeypatch.setattr(pc_actions.time, "sleep", lambda _seconds: None)

    result = pc_actions.resonance_pc_player_data_refresh(app=app, ocr=object())

    assert app.clicks == [
        pc_actions._CLICK_PROFILE,
        pc_actions._CLICK_CURRENCY_EYE,
        pc_actions._CLICK_CONFIRM,
        pc_actions._CLICK_CLARITY,
        pc_actions._CLICK_BACK,
        pc_actions._CLICK_FATIGUE,
        pc_actions._CLICK_BACK,
        pc_actions._CLICK_BACK,
    ]
    assert marker_labels == [
        "main page before player data refresh",
        "profile panel",
        "currency popup",
        "profile panel after currency popup",
        "clarity page",
        "profile panel after clarity page",
        "fatigue page",
        "profile panel after fatigue page",
        "main page after player data refresh",
    ]
    assert result["profile"] == {
        "uid": "8820206170",
        "nickname": "面包猫南北",
        "level": 71,
    }
    assert result["location"] == {"current_city": "修格里城"}
    assert result["currencies"] == {"iron_coins": 9132364, "birch_stone": 615}
    assert result["status"]["clarity"]["current"] == 292
    assert result["status"]["fatigue"]["max"] == 824
    assert result["status"]["cargo"] == {"current": 20, "max": 650}
    assert result["metadata"]["source"] == "ocr"


def test_pc_player_data_refresh_stops_before_clicking_when_not_on_main(monkeypatch):
    app = _FakeApp()

    def fail_main_check(*_args, **_kwargs):
        raise StopTaskException("main page missing", success=False)

    monkeypatch.setattr(pc_actions, "_wait_for_any_marker", fail_main_check)

    with pytest.raises(StopTaskException):
        pc_actions.resonance_pc_player_data_refresh(app=app, ocr=object())

    assert app.clicks == []


def test_pc_player_data_task_schema_and_manifest_export():
    task_data = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))
    task = task_data["player_data_refresh"]

    ok, error = validate_task_definition(task_data)

    assert ok is True, error
    assert task["meta"]["entry_point"] is True
    assert task["meta"]["concurrency"] == "exclusive"
    assert task["meta"]["inputs"] == []
    assert task["steps"]["refresh"]["action"] == "resonance_pc.player_data_refresh"
    assert task["returns"]["player_data"] == "{{ nodes.refresh.output }}"

    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    action_names = {item["name"] for item in manifest["exports"]["actions"]}
    task_ids = {item["id"] for item in manifest["exports"]["tasks"]}
    assert "resonance_pc.player_data_refresh" in action_names
    assert "player_data_pc/player_data_refresh" in task_ids
