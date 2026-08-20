from __future__ import annotations

import copy
import json
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
ALL_STAGES = [
    "location",
    "profile",
    "currencies",
    "clarity",
    "fatigue",
    "inventory",
    "persist",
]
NOW = "2026-08-20T02:30:00+00:00"
INVENTORY_PAYLOAD = {
    "items": [
        {
            "item_id": "cactus_energy_lollipop",
            "name": "仙人掌能量棒棒糖",
            "count": 3,
            "expiry": {"kind": "days_remaining", "value": 5, "raw": "5天"},
        }
    ],
    "pages_scanned": 2,
    "complete": True,
    "stop_reason": "all_supported_items_found",
}


class _FakeApp:
    def __init__(self):
        self.clicks: list[tuple[int, int]] = []

    def click(self, x=None, y=None, **_kwargs):
        self.clicks.append((int(x), int(y)))


def _install_successful_flow(monkeypatch):
    app = _FakeApp()
    trace: list[str] = []
    marker_labels: list[str] = []

    def fake_wait(_app, _ocr, **kwargs):
        marker_labels.append(kwargs["label"])
        return [{"text": next(iter(kwargs["markers"]))}]

    def fake_capture(_app, _ocr, region=None, **_kwargs):
        assert region == pc_actions._MAIN_CITY_REGION
        trace.append("location")
        return [{"text": "修格里城"}]

    def fake_profile(_app, _ocr):
        trace.append("profile")
        return {
            "profile": {"uid": "8820206170", "nickname": "面包猫南北", "level": 71},
            "cargo": {"current": 20, "max": 650},
        }

    def fake_currencies(_app, _ocr):
        trace.append("currencies")
        return {"iron_coins": 12, "birch_stone": 615}

    def fake_int(_app, _ocr, region):
        assert region == pc_actions._CURRENCY_FIELD_REGIONS["iron_coins"]
        trace.append("currency_popup")
        return 9132364

    def fake_ratio(_app, _ocr, region):
        if region == pc_actions._CLARITY_RATIO_REGION:
            trace.append("clarity_ratio")
            return {"current": 292, "max": 292}
        assert region == pc_actions._FATIGUE_RATIO_REGION
        trace.append("fatigue_ratio")
        return {"current": 0, "max": 824}

    def fake_enter_warehouse(_app, _ocr, **_kwargs):
        marker_labels.append("warehouse item page")
        _app.click(x=pc_actions._CLICK_INVENTORY[0], y=pc_actions._CLICK_INVENTORY[1])

    def fake_inventory(_app, _ocr, **_kwargs):
        trace.append("inventory")
        return copy.deepcopy(INVENTORY_PAYLOAD)

    monkeypatch.setattr(pc_actions, "_wait_for_any_marker", fake_wait)
    monkeypatch.setattr(pc_actions, "_capture_ocr_items", fake_capture)
    monkeypatch.setattr(pc_actions, "_read_profile_stage", fake_profile)
    monkeypatch.setattr(pc_actions, "_read_currencies_stage", fake_currencies)
    monkeypatch.setattr(pc_actions, "_read_int_region", fake_int)
    monkeypatch.setattr(pc_actions, "_read_ratio_region", fake_ratio)
    monkeypatch.setattr(pc_actions, "_enter_warehouse_page", fake_enter_warehouse)
    monkeypatch.setattr(pc_actions, "_scan_inventory_stage", fake_inventory)
    monkeypatch.setattr(pc_actions, "_utc_now_iso", lambda: NOW)
    monkeypatch.setattr(pc_actions.time, "sleep", lambda _seconds: None)
    return app, trace, marker_labels


def test_enter_warehouse_page_continuously_locates_clicks_and_strictly_verifies(monkeypatch):
    app = _FakeApp()
    page_calls = 0

    def fake_capture(_app, _ocr, region=None, **_kwargs):
        nonlocal page_calls
        if region == pc_actions._INVENTORY_PAGE_REGION:
            page_calls += 1
            if page_calls == 1:
                return [{"text": "道具"}]
            return [{"text": "道具 材料 装备"}]
        assert region == pc_actions._WAREHOUSE_ENTRY_REGION
        return [{"text": "仓库", "center": [165, 660]}]

    ticks = iter(index * 0.1 for index in range(100))
    monkeypatch.setattr(pc_actions, "_capture_ocr_items", fake_capture)
    monkeypatch.setattr(pc_actions.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(pc_actions.time, "sleep", lambda _seconds: None)

    pc_actions._enter_warehouse_page(app, object())

    assert pc_actions._WAREHOUSE_ENTRY_TIMEOUT_SEC == 3.0
    assert app.clicks == [(165, 615)]
    assert page_calls == 2


def test_enter_warehouse_page_times_out_without_strict_markers(monkeypatch):
    app = _FakeApp()

    def fake_capture(_app, _ocr, region=None, **_kwargs):
        if region == pc_actions._INVENTORY_PAGE_REGION:
            return [{"text": "道具"}]
        return [{"text": "仓库", "center": [165, 660]}]

    ticks = iter(index * 0.25 for index in range(100))
    monkeypatch.setattr(pc_actions, "_capture_ocr_items", fake_capture)
    monkeypatch.setattr(pc_actions.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(pc_actions.time, "sleep", lambda _seconds: None)

    with pytest.raises(StopTaskException, match="within 3.0s"):
        pc_actions._enter_warehouse_page(app, object())

    assert 1 <= len(app.clicks) <= 6


def _redirect_cache(monkeypatch, cache_file: Path) -> None:
    monkeypatch.setattr(pc_actions, "_PLAYER_LATEST_FILE", cache_file)


def test_pc_player_data_coordinates_match_mumu_except_expanded_numeric_regions():
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
        "_CURRENCY_FIELD_REGIONS",
        "_CLARITY_RATIO_REGION",
    )

    for name in names:
        assert getattr(pc_actions, name) == getattr(mumu_actions, name), name

    for field, region in mumu_actions._PROFILE_FIELD_REGIONS.items():
        if field != "birch_stone":
            assert pc_actions._PROFILE_FIELD_REGIONS[field] == region

    def contains(outer, inner):
        outer_x, outer_y, outer_w, outer_h = outer
        inner_x, inner_y, inner_w, inner_h = inner
        return (
            outer_x <= inner_x
            and outer_y <= inner_y
            and outer_x + outer_w >= inner_x + inner_w
            and outer_y + outer_h >= inner_y + inner_h
        )

    pc_birch = pc_actions._PROFILE_FIELD_REGIONS["birch_stone"]
    mumu_birch = mumu_actions._PROFILE_FIELD_REGIONS["birch_stone"]
    assert pc_birch == (420, 193, 105, 50)
    assert contains(pc_birch, mumu_birch)

    assert pc_actions._FATIGUE_RATIO_REGION == (85, 580, 160, 75)
    assert contains(pc_actions._FATIGUE_RATIO_REGION, mumu_actions._FATIGUE_RATIO_REGION)


@pytest.mark.parametrize(
    ("stage", "expected_clicks", "expected_trace", "expected_keys"),
    [
        ("location", [], ["location"], {"location", "metadata"}),
        (
            "profile",
            [pc_actions._CLICK_PROFILE, pc_actions._CLICK_PROFILE_CLOSE],
            ["profile"],
            {"profile", "status", "metadata"},
        ),
        (
            "currencies",
            [
                pc_actions._CLICK_PROFILE,
                pc_actions._CLICK_CURRENCY_EYE,
                pc_actions._CLICK_CONFIRM,
                pc_actions._CLICK_PROFILE_CLOSE,
            ],
            ["currencies", "currency_popup"],
            {"currencies", "metadata"},
        ),
        (
            "clarity",
            [
                pc_actions._CLICK_PROFILE,
                pc_actions._CLICK_CLARITY,
                pc_actions._CLICK_BACK,
                pc_actions._CLICK_PROFILE_CLOSE,
            ],
            ["clarity_ratio"],
            {"status", "metadata"},
        ),
        (
            "fatigue",
            [
                pc_actions._CLICK_PROFILE,
                pc_actions._CLICK_FATIGUE,
                pc_actions._CLICK_BACK,
                pc_actions._CLICK_PROFILE_CLOSE,
            ],
            ["fatigue_ratio"],
            {"status", "metadata"},
        ),
        (
            "inventory",
            [
                pc_actions._CLICK_PROFILE,
                pc_actions._CLICK_INVENTORY,
                pc_actions._CLICK_BACK,
                pc_actions._CLICK_PROFILE_CLOSE,
            ],
            ["inventory"],
            {"inventory", "metadata"},
        ),
    ],
)
def test_each_data_stage_uses_only_its_own_ui_and_ocr_path(
    monkeypatch,
    stage,
    expected_clicks,
    expected_trace,
    expected_keys,
):
    app, trace, marker_labels = _install_successful_flow(monkeypatch)

    result = pc_actions.resonance_pc_player_data_refresh(
        stages=[stage],
        app=app,
        ocr=object(),
    )

    assert app.clicks == expected_clicks
    assert trace == expected_trace
    assert set(result) == expected_keys
    assert marker_labels[0] == "main page before player data refresh"
    assert result["metadata"] == {
        "refreshed_at": NOW,
        "source": "ocr",
        "executed_stages": [stage],
        "skipped_stages": [item for item in ALL_STAGES if item != stage],
        "persisted": False,
        "section_updated_at": {stage: NOW},
    }
    if stage == "profile":
        assert result["status"] == {"cargo": {"current": 20, "max": 650}}
    elif stage in {"clarity", "fatigue"}:
        assert set(result["status"]) == {stage}
    elif stage == "inventory":
        assert result["inventory"] == INVENTORY_PAYLOAD


def test_default_stages_run_full_flow_and_persist(monkeypatch):
    app, trace, marker_labels = _install_successful_flow(monkeypatch)
    persisted: list[tuple[dict, dict]] = []
    monkeypatch.setattr(
        pc_actions,
        "_persist_latest",
        lambda fresh, *, section_updated_at: persisted.append(
            (copy.deepcopy(fresh), copy.deepcopy(section_updated_at))
        ),
    )

    result = pc_actions.resonance_pc_player_data_refresh(app=app, ocr=object())

    assert app.clicks == [
        pc_actions._CLICK_PROFILE,
        pc_actions._CLICK_CURRENCY_EYE,
        pc_actions._CLICK_CONFIRM,
        pc_actions._CLICK_CLARITY,
        pc_actions._CLICK_BACK,
        pc_actions._CLICK_FATIGUE,
        pc_actions._CLICK_BACK,
        pc_actions._CLICK_INVENTORY,
        pc_actions._CLICK_BACK,
        pc_actions._CLICK_PROFILE_CLOSE,
    ]
    assert trace == [
        "location",
        "profile",
        "currencies",
        "currency_popup",
        "clarity_ratio",
        "fatigue_ratio",
        "inventory",
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
        "warehouse item page",
        "profile panel after warehouse item page",
        "main page after player data refresh",
    ]
    assert result["location"] == {"current_city": "修格里城"}
    assert result["profile"]["uid"] == "8820206170"
    assert result["currencies"] == {"iron_coins": 9132364, "birch_stone": 615}
    assert result["status"]["cargo"] == {"current": 20, "max": 650}
    assert result["status"]["clarity"]["current"] == 292
    assert result["status"]["fatigue"]["max"] == 824
    assert result["inventory"] == INVENTORY_PAYLOAD
    assert result["metadata"]["executed_stages"] == ALL_STAGES
    assert result["metadata"]["skipped_stages"] == []
    assert result["metadata"]["persisted"] is True
    assert persisted == [
        (
            {
                key: copy.deepcopy(result[key])
                for key in ("location", "profile", "currencies", "status", "inventory")
            },
            {stage: NOW for stage in ALL_STAGES[:-1]},
        )
    ]


def test_stage_order_is_canonical_and_duplicates_are_removed(monkeypatch):
    app, trace, _marker_labels = _install_successful_flow(monkeypatch)
    persisted: list[dict] = []
    monkeypatch.setattr(
        pc_actions,
        "_persist_latest",
        lambda _fresh, *, section_updated_at: persisted.append(copy.deepcopy(section_updated_at)),
    )

    result = pc_actions.resonance_pc_player_data_refresh(
        stages=["fatigue", "persist", "location", "fatigue"],
        app=app,
        ocr=object(),
    )

    assert trace == ["location", "fatigue_ratio"]
    assert result["metadata"]["executed_stages"] == ["location", "fatigue", "persist"]
    assert result["metadata"]["skipped_stages"] == [
        "profile",
        "currencies",
        "clarity",
        "inventory",
    ]
    assert persisted == [{"location": NOW, "fatigue": NOW}]


@pytest.mark.parametrize(
    "stages",
    [
        [],
        ["persist"],
        ["unknown"],
        ["location", "unknown"],
        "location",
        [None],
    ],
)
def test_invalid_stage_selection_is_rejected_before_game_access(stages):
    app = _FakeApp()

    with pytest.raises(ValueError):
        pc_actions.resonance_pc_player_data_refresh(stages=stages, app=app, ocr=object())

    assert app.clicks == []


def test_pc_player_data_refresh_stops_before_clicking_when_not_on_main(monkeypatch):
    app = _FakeApp()

    def fail_main_check(*_args, **_kwargs):
        raise StopTaskException("main page missing", success=False)

    monkeypatch.setattr(pc_actions, "_wait_for_any_marker", fail_main_check)

    with pytest.raises(StopTaskException, match="main page missing"):
        pc_actions.resonance_pc_player_data_refresh(
            stages=["location", "persist"],
            app=app,
            ocr=object(),
        )

    assert app.clicks == []


def test_stage_failure_preserves_original_error_and_does_not_persist(monkeypatch):
    app, _trace, _marker_labels = _install_successful_flow(monkeypatch)
    persist_calls: list[dict] = []

    def fail_clarity(*_args, **_kwargs):
        raise StopTaskException("clarity OCR failed", success=False)

    monkeypatch.setattr(pc_actions, "_read_ratio_region", fail_clarity)
    monkeypatch.setattr(
        pc_actions,
        "_persist_latest",
        lambda *_args, **_kwargs: persist_calls.append({"called": True}),
    )

    with pytest.raises(StopTaskException, match="clarity OCR failed"):
        pc_actions.resonance_pc_player_data_refresh(
            stages=["clarity", "persist"],
            app=app,
            ocr=object(),
        )

    assert persist_calls == []
    assert app.clicks == [
        pc_actions._CLICK_PROFILE,
        pc_actions._CLICK_CLARITY,
        pc_actions._CLICK_BACK,
        pc_actions._CLICK_PROFILE_CLOSE,
    ]


def test_inventory_failure_returns_to_main_and_does_not_persist(monkeypatch):
    app, _trace, _marker_labels = _install_successful_flow(monkeypatch)
    cleanup_pages: list[str] = []
    persist_calls: list[dict] = []

    def fail_inventory(*_args, **_kwargs):
        raise StopTaskException("inventory scan failed", success=False)

    monkeypatch.setattr(pc_actions, "_scan_inventory_stage", fail_inventory)
    monkeypatch.setattr(
        pc_actions,
        "_best_effort_return_to_main",
        lambda _app, _ocr, page: cleanup_pages.append(page),
    )
    monkeypatch.setattr(
        pc_actions,
        "_persist_latest",
        lambda *_args, **_kwargs: persist_calls.append({"called": True}),
    )

    with pytest.raises(StopTaskException, match="inventory scan failed"):
        pc_actions.resonance_pc_player_data_refresh(
            stages=["inventory", "persist"],
            app=app,
            ocr=object(),
        )

    assert cleanup_pages == ["inventory"]
    assert persist_calls == []
    assert app.clicks == [pc_actions._CLICK_PROFILE, pc_actions._CLICK_INVENTORY]


def test_inventory_partial_merge_replaces_snapshot_as_a_whole():
    old_inventory = {
        "items": [
            {
                "item_id": "cactus_energy_lollipop",
                "name": "仙人掌能量棒棒糖",
                "count": 99,
                "expiry": {"kind": "days_remaining", "value": 1, "raw": "1天"},
            },
            {
                "item_id": "stale_item",
                "name": "已消耗道具",
                "count": 7,
            },
        ],
        "pages_scanned": 8,
        "complete": False,
        "stop_reason": "max_pages_reached",
    }
    existing = {
        "profile": {"uid": "keep-me"},
        "inventory": old_inventory,
        "metadata": {
            "section_updated_at": {
                "profile": "2026-08-19T00:00:00+00:00",
                "inventory": "2026-08-19T00:00:00+00:00",
            }
        },
    }
    fresh = {"inventory": copy.deepcopy(INVENTORY_PAYLOAD)}

    merged = pc_actions._merge_latest(
        existing,
        fresh,
        section_updated_at={"inventory": NOW},
        updated_at=NOW,
    )

    assert merged["inventory"] == INVENTORY_PAYLOAD
    assert merged["inventory"] is not fresh["inventory"]
    assert merged["profile"] == {"uid": "keep-me"}
    assert merged["metadata"]["section_updated_at"] == {
        "profile": "2026-08-19T00:00:00+00:00",
        "inventory": NOW,
    }


def test_partial_refresh_merges_cache_and_preserves_other_section_times(monkeypatch, tmp_path):
    cache_file = tmp_path / "player" / "latest.json"
    old_cache = {
        "profile": {"uid": "old", "nickname": "旧昵称", "level": 70},
        "location": {"current_city": "曼德矿场"},
        "currencies": {"iron_coins": 1, "birch_stone": 2},
        "status": {
            "cargo": {"current": 3, "max": 600},
            "clarity": {"current": 100, "max": 292},
            "fatigue": {"current": 400, "max": 824},
        },
        "metadata": {
            "source": "ocr",
            "updated_at": "2026-08-19T00:00:00+00:00",
            "section_updated_at": {
                "profile": "2026-08-19T00:00:00+00:00",
                "location": "2026-08-19T00:00:00+00:00",
                "currencies": "2026-08-19T00:00:00+00:00",
                "clarity": "2026-08-19T00:00:00+00:00",
                "fatigue": "2026-08-19T00:00:00+00:00",
            },
        },
    }
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(json.dumps(old_cache, ensure_ascii=False), encoding="utf-8")
    _redirect_cache(monkeypatch, cache_file)
    app, _trace, _marker_labels = _install_successful_flow(monkeypatch)

    result = pc_actions.resonance_pc_player_data_refresh(
        stages=["location", "persist"],
        app=app,
        ocr=object(),
    )
    cached = pc_actions.resonance_pc_player_data_get_latest()

    assert set(result) == {"location", "metadata"}
    assert result["metadata"]["section_updated_at"] == {"location": NOW}
    assert cached["location"] == {"current_city": "修格里城"}
    for key in ("profile", "currencies", "status"):
        assert cached[key] == old_cache[key]
    assert cached["metadata"]["updated_at"] == NOW
    assert cached["metadata"]["section_updated_at"]["location"] == NOW
    for stage in ("profile", "currencies", "clarity", "fatigue"):
        assert (
            cached["metadata"]["section_updated_at"][stage]
            == old_cache["metadata"]["section_updated_at"][stage]
        )
    assert not cache_file.with_suffix(".json.tmp").exists()


def test_first_partial_persist_creates_only_refreshed_section(monkeypatch, tmp_path):
    cache_file = tmp_path / "player" / "latest.json"
    _redirect_cache(monkeypatch, cache_file)
    app, _trace, _marker_labels = _install_successful_flow(monkeypatch)

    pc_actions.resonance_pc_player_data_refresh(
        stages=["profile", "persist"],
        app=app,
        ocr=object(),
    )
    cached = json.loads(cache_file.read_text(encoding="utf-8"))

    assert set(cached) == {"profile", "status", "metadata"}
    assert cached["status"] == {"cargo": {"current": 20, "max": 650}}
    assert cached["metadata"] == {
        "source": "ocr",
        "updated_at": NOW,
        "section_updated_at": {"profile": NOW},
    }


def test_refresh_without_persist_does_not_touch_existing_cache(monkeypatch, tmp_path):
    cache_file = tmp_path / "player" / "latest.json"
    cache_file.parent.mkdir(parents=True)
    original = '{"sentinel": "保持原样"}\n'
    cache_file.write_text(original, encoding="utf-8")
    _redirect_cache(monkeypatch, cache_file)
    app, _trace, _marker_labels = _install_successful_flow(monkeypatch)

    result = pc_actions.resonance_pc_player_data_refresh(
        stages=["location"],
        app=app,
        ocr=object(),
    )

    assert result["metadata"]["persisted"] is False
    assert cache_file.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("contents", ["[]", "not-json"])
def test_invalid_cache_is_rejected_and_not_overwritten(monkeypatch, tmp_path, contents):
    cache_file = tmp_path / "player" / "latest.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(contents, encoding="utf-8")
    _redirect_cache(monkeypatch, cache_file)

    with pytest.raises(RuntimeError):
        pc_actions.resonance_pc_player_data_get_latest()

    app, _trace, _marker_labels = _install_successful_flow(monkeypatch)
    with pytest.raises(RuntimeError):
        pc_actions.resonance_pc_player_data_refresh(
            stages=["location", "persist"],
            app=app,
            ocr=object(),
        )
    assert cache_file.read_text(encoding="utf-8") == contents


def test_get_latest_reports_missing_cache_and_returns_a_copy(monkeypatch, tmp_path):
    cache_file = tmp_path / "player" / "latest.json"
    _redirect_cache(monkeypatch, cache_file)

    with pytest.raises(RuntimeError, match="No cached Resonance PC player data"):
        pc_actions.resonance_pc_player_data_get_latest()

    payload = {"location": {"current_city": "修格里城"}}
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loaded = pc_actions.resonance_pc_player_data_get_latest()
    loaded["location"]["current_city"] = "changed"
    assert pc_actions.resonance_pc_player_data_get_latest() == payload


def test_merge_accepts_legacy_cache_without_section_timestamps():
    merged = pc_actions._merge_latest(
        {"profile": {"uid": "legacy"}, "metadata": {"refreshed_at": "old"}},
        {"location": {"current_city": "修格里城"}},
        section_updated_at={"location": NOW},
        updated_at=NOW,
    )

    assert merged["profile"] == {"uid": "legacy"}
    assert merged["metadata"] == {
        "refreshed_at": "old",
        "source": "ocr",
        "updated_at": NOW,
        "section_updated_at": {"location": NOW},
    }


def test_pc_player_data_task_schema_and_manifest_exports():
    task_data = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))
    refresh = task_data["player_data_refresh"]
    latest = task_data["player_data_get_latest"]

    ok, error = validate_task_definition(task_data)

    assert ok is True, error
    assert refresh["meta"]["entry_point"] is True
    assert refresh["meta"]["concurrency"] == "exclusive"
    assert refresh["meta"]["inputs"][0]["name"] == "stages"
    assert refresh["meta"]["inputs"][0]["type"] == "list"
    assert refresh["meta"]["inputs"][0]["default"] == ALL_STAGES
    assert refresh["steps"]["refresh"]["action"] == "resonance_pc.player_data_refresh"
    assert refresh["steps"]["refresh"]["params"]["stages"] == "{{ inputs.stages }}"
    assert refresh["returns"]["player_data"] == "{{ nodes.refresh.output }}"
    assert latest["meta"]["inputs"] == []
    assert latest["steps"]["get_latest"] == {
        "action": "resonance_pc.player_data_get_latest",
        "params": {},
    }
    assert latest["returns"]["player_data"] == "{{ nodes.get_latest.output }}"

    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    actions = {item["name"]: item for item in manifest["exports"]["actions"]}
    task_ids = {item["id"] for item in manifest["exports"]["tasks"]}
    assert actions["resonance_pc.player_data_refresh"]["parameters"][0]["name"] == "stages"
    assert actions["resonance_pc.player_data_refresh"]["parameters"][0]["default"] is None
    assert actions["resonance_pc.player_data_get_latest"]["read_only"] is True
    assert actions["resonance_pc.player_data_get_latest"]["parameters"] == []
    assert "player_data_pc/player_data_refresh" in task_ids
    assert "player_data_pc/player_data_get_latest" in task_ids
