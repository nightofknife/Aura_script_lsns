from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
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
    "inventory",
]
NOW = "2026-08-20T02:30:00+00:00"
INVENTORY_PAYLOAD = {
    "items": [
        {
            "item_id": "cactus_energy_lollipop",
            "name": "仙人掌能量棒棒糖",
            "count": 3,
            "expiry": {"kind": "days_remaining", "value": 5, "raw": "5天"},
        },
        {
            "item_id": "iron_alliance_coin",
            "name": "铁盟币",
            "count": 9132364,
        },
        {
            "item_id": "birch_crystal",
            "name": "桦石",
            "count": 615,
        },
    ],
    "pages_scanned": 2,
    "complete": True,
    "stop_reason": "all_supported_items_found",
}
MATERIAL_PAYLOAD = {
    "category": "materials",
    "materials": [
        {"material_id": "sample_material", "name": "测试材料", "count": 12}
    ],
    "matched_stack_count": 1,
    "pages_scanned": 1,
    "scan_complete": True,
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
            "clarity": {"current": 292, "max": 292},
            "fatigue": {"current": 0, "max": 824},
        }

    def fake_enter_warehouse(_app, _ocr, **_kwargs):
        marker_labels.append("warehouse item page")
        _app.click(x=pc_actions._CLICK_INVENTORY[0], y=pc_actions._CLICK_INVENTORY[1])

    def fake_inventory(_app, _ocr, _vision, **kwargs):
        trace.append("inventory")
        if kwargs.get("category") == "materials":
            return copy.deepcopy(MATERIAL_PAYLOAD)
        return copy.deepcopy(INVENTORY_PAYLOAD)

    def fake_select_inventory_category(_app, category, **_kwargs):
        point = pc_actions._CLICK_INVENTORY_CATEGORY[category]
        _app.click(x=point[0], y=point[1])

    monkeypatch.setattr(pc_actions, "_wait_for_any_marker", fake_wait)
    monkeypatch.setattr(pc_actions, "_capture_ocr_items", fake_capture)
    monkeypatch.setattr(pc_actions, "_read_profile_stage", fake_profile)
    monkeypatch.setattr(pc_actions, "_enter_warehouse_page", fake_enter_warehouse)
    monkeypatch.setattr(
        pc_actions,
        "_select_inventory_category",
        fake_select_inventory_category,
    )
    monkeypatch.setattr(pc_actions, "_scan_inventory_stage", fake_inventory)
    monkeypatch.setattr(pc_actions, "_utc_now_iso", lambda: NOW)
    monkeypatch.setattr(pc_actions.time, "sleep", lambda _seconds: None)
    return app, trace, marker_labels


def test_enter_warehouse_page_continuously_template_matches_clicks_and_strictly_verifies(monkeypatch):
    app = _FakeApp()
    page_calls = 0

    def fake_capture(_app, _ocr, region=None, **_kwargs):
        nonlocal page_calls
        if region == pc_actions._INVENTORY_PAGE_REGION:
            page_calls += 1
            if page_calls == 1:
                return [{"text": "道具"}]
            return [{"text": "道具 材料 装备"}]
        raise AssertionError(f"unexpected OCR region: {region}")

    ticks = iter(index * 0.1 for index in range(100))
    monkeypatch.setattr(pc_actions, "_capture_ocr_items", fake_capture)
    monkeypatch.setattr(
        pc_actions,
        "_match_warehouse_entry",
        lambda _app: {"found": True, "confidence": 0.99, "center": [157, 619]},
    )
    monkeypatch.setattr(pc_actions.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(pc_actions.time, "sleep", lambda _seconds: None)

    pc_actions._enter_warehouse_page(app, object())

    assert pc_actions._WAREHOUSE_ENTRY_TIMEOUT_SEC == 3.0
    assert app.clicks == [(157, 619)]
    assert page_calls == 2


def test_enter_warehouse_page_times_out_without_strict_markers(monkeypatch):
    app = _FakeApp()

    def fake_capture(_app, _ocr, region=None, **_kwargs):
        if region == pc_actions._INVENTORY_PAGE_REGION:
            return [{"text": "道具"}]
        raise AssertionError(f"unexpected OCR region: {region}")

    ticks = iter(index * 0.25 for index in range(100))
    monkeypatch.setattr(pc_actions, "_capture_ocr_items", fake_capture)
    monkeypatch.setattr(
        pc_actions,
        "_match_warehouse_entry",
        lambda _app: {"found": True, "confidence": 0.98, "center": [157, 619]},
    )
    monkeypatch.setattr(pc_actions.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(pc_actions.time, "sleep", lambda _seconds: None)

    with pytest.raises(StopTaskException, match="within 3.0s"):
        pc_actions._enter_warehouse_page(app, object())

    assert 1 <= len(app.clicks) <= 6


def test_select_inventory_category_clicks_and_verifies_without_ocr(monkeypatch):
    class _CategoryApp(_FakeApp):
        selected = "items"

        def click(self, x=None, y=None, **kwargs):
            super().click(x=x, y=y, **kwargs)
            if (int(x), int(y)) == pc_actions._CLICK_INVENTORY_CATEGORY["materials"]:
                self.selected = "materials"

        def capture(self, rect=None):
            category = next(
                key for key, value in pc_actions._INVENTORY_CATEGORY_REGIONS.items()
                if value == rect
            )
            value = 220 if category == self.selected else 40
            return SimpleNamespace(
                success=True,
                image=np.full((rect[3], rect[2], 3), value, dtype=np.uint8),
            )

    app = _CategoryApp()
    monkeypatch.setattr(pc_actions.time, "sleep", lambda _seconds: None)

    pc_actions._select_inventory_category(app, "materials")

    assert app.clicks == [pc_actions._CLICK_INVENTORY_CATEGORY["materials"]]


def test_warehouse_entry_template_contains_only_square_icon_region():
    image = cv2.imread(str(pc_actions._WAREHOUSE_ENTRY_TEMPLATE), cv2.IMREAD_COLOR)

    assert image is not None
    assert image.shape[:2] == (60, 60)
    assert int(image[-5:].max()) < 200
    assert pc_actions._WAREHOUSE_ENTRY_TEMPLATE_THRESHOLD == 0.82


def test_warehouse_entry_template_match_returns_absolute_center():
    template = cv2.imread(str(pc_actions._WAREHOUSE_ENTRY_TEMPLATE), cv2.IMREAD_COLOR)
    source = np.zeros((150, 140, 3), dtype=np.uint8)
    source[9:69, 12:72] = template

    class _CaptureApp:
        def capture(self, rect=None):
            assert rect == pc_actions._WAREHOUSE_ENTRY_REGION
            return SimpleNamespace(success=True, image=source)

    match = pc_actions._match_warehouse_entry(_CaptureApp())

    assert match["found"] is True
    assert match["confidence"] > 0.999
    assert match["center"] == [152, 599]


def _redirect_cache(monkeypatch, cache_file: Path) -> None:
    monkeypatch.setattr(pc_actions, "_PLAYER_LATEST_FILE", cache_file)


def test_profile_stage_reads_account_and_all_status_from_profile_panel(monkeypatch):
    values = {
        (95, 8, 160, 35): "UID: 8820206170",
        pc_actions._PROFILE_FIELD_REGIONS["nickname"]: "面包猫南北",
        pc_actions._PROFILE_FIELD_REGIONS["level"]: "LV 74",
        pc_actions._PROFILE_FIELD_REGIONS["cargo"]: "3/748",
        pc_actions._PROFILE_FIELD_REGIONS["clarity"]: "96/282",
        pc_actions._PROFILE_FIELD_REGIONS["fatigue"]: "399/848",
    }
    monkeypatch.setattr(
        pc_actions,
        "_read_region_text",
        lambda _app, _ocr, region, **_kwargs: values[region],
    )

    result = pc_actions._read_profile_stage(_FakeApp(), object())

    assert result == {
        "profile": {"uid": "8820206170", "nickname": "面包猫南北", "level": 74},
        "cargo": {"current": 3, "max": 748},
        "clarity": {"current": 96, "max": 282},
        "fatigue": {"current": 399, "max": 848},
    }


def test_inventory_currency_values_are_derived_by_item_id_and_merge_partially():
    inventory = {
        "schema_version": 2,
        "categories": {
            "items": {
                "items": [
                    {"item_id": "iron_alliance_coin", "name": "铁盟币", "count": 99},
                    {"item_id": "birch_crystal", "name": "桦石", "count": "bad"},
                    {"item_id": "other", "name": "其他", "count": 1000},
                ]
            }
        },
    }

    assert pc_actions._currencies_from_inventory(inventory) == {"iron_coins": 99}

    merged = pc_actions._merge_latest(
        {
            "currencies": {"iron_coins": 1, "birch_stone": 2},
            "metadata": {
                "section_updated_at": {
                    "currencies": "legacy-currency-time",
                    "inventory": "old-inventory-time",
                }
            },
        },
        {
            "inventory": inventory,
            "currencies": {"iron_coins": 99},
        },
        section_updated_at={"inventory": NOW},
        updated_at=NOW,
    )

    assert merged["currencies"] == {"iron_coins": 99, "birch_stone": 2}
    assert merged["metadata"]["section_updated_at"] == {"inventory": NOW}


def test_pc_player_data_coordinates_keep_only_shared_profile_and_navigation_regions():
    names = (
        "_CLICK_PROFILE",
        "_CLICK_BACK",
        "_MAIN_CITY_REGION",
        "_PROFILE_REGION",
        "_MAIN_PAGE_REGION",
        "_MAIN_PAGE_MARKERS",
    )

    for name in names:
        assert getattr(pc_actions, name) == getattr(mumu_actions, name), name

    for field in ("uid", "level", "nickname", "clarity", "fatigue", "cargo"):
        assert pc_actions._PROFILE_FIELD_REGIONS[field] == mumu_actions._PROFILE_FIELD_REGIONS[field]
    assert set(pc_actions._PROFILE_FIELD_REGIONS) == {
        "uid", "level", "nickname", "clarity", "fatigue", "cargo"
    }


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
            "inventory",
            [
                pc_actions._CLICK_PROFILE,
                pc_actions._CLICK_INVENTORY,
                pc_actions._CLICK_INVENTORY_CATEGORY["items"],
                pc_actions._CLICK_BACK,
                pc_actions._CLICK_PROFILE_CLOSE,
            ],
            ["inventory"],
            {"currencies", "inventory", "metadata"},
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
    monkeypatch.setattr(pc_actions, "_persist_latest", lambda *_args, **_kwargs: None)

    result = pc_actions.resonance_pc_player_data_refresh(
        stages=[stage],
        app=app,
        ocr=object(),
    )

    assert app.clicks == expected_clicks
    assert trace == expected_trace
    assert set(result) == expected_keys
    assert marker_labels[0] == "main page before player data refresh"
    expected_metadata = {
        "refreshed_at": NOW,
        "source": "ocr",
        "executed_stages": [stage],
        "skipped_stages": [item for item in ALL_STAGES if item != stage],
        "persisted": True,
        "section_updated_at": {stage: NOW},
    }
    if stage == "inventory":
        expected_metadata["inventory_category_updated_at"] = {"items": NOW}
    assert result["metadata"] == expected_metadata
    if stage == "profile":
        assert result["status"] == {
            "cargo": {"current": 20, "max": 650},
            "clarity": {"current": 292, "max": 292},
            "fatigue": {"current": 0, "max": 824},
        }
    elif stage == "inventory":
        assert result["inventory"] == {
            "schema_version": 2,
            "categories": {"items": INVENTORY_PAYLOAD},
        }
        assert result["currencies"] == {"iron_coins": 9132364, "birch_stone": 615}
        assert result["metadata"]["inventory_category_updated_at"] == {"items": NOW}


def test_default_stages_run_full_flow_and_persist(monkeypatch):
    app, trace, marker_labels = _install_successful_flow(monkeypatch)
    persisted: list[tuple[dict, dict, dict]] = []
    monkeypatch.setattr(
        pc_actions,
        "_persist_latest",
        lambda fresh, *, section_updated_at, inventory_category_updated_at=None: persisted.append(
            (
                copy.deepcopy(fresh),
                copy.deepcopy(section_updated_at),
                copy.deepcopy(inventory_category_updated_at or {}),
            )
        ),
    )

    result = pc_actions.resonance_pc_player_data_refresh(app=app, ocr=object())

    assert app.clicks == [
        pc_actions._CLICK_PROFILE,
        pc_actions._CLICK_INVENTORY,
        pc_actions._CLICK_INVENTORY_CATEGORY["items"],
        pc_actions._CLICK_BACK,
        pc_actions._CLICK_PROFILE_CLOSE,
    ]
    assert trace == [
        "location",
        "profile",
        "inventory",
    ]
    assert marker_labels == [
        "main page before player data refresh",
        "profile panel",
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
    assert result["inventory"] == {
        "schema_version": 2,
        "categories": {"items": INVENTORY_PAYLOAD},
    }
    assert result["metadata"]["executed_stages"] == ALL_STAGES
    assert result["metadata"]["skipped_stages"] == []
    assert result["metadata"]["persisted"] is True
    assert persisted == [
        (
            {
                key: copy.deepcopy(result[key])
                for key in ("location", "profile", "currencies", "status", "inventory")
            },
            {stage: NOW for stage in ALL_STAGES},
            {"items": NOW},
        )
    ]


def test_stage_order_is_canonical_and_duplicates_are_removed(monkeypatch):
    app, trace, _marker_labels = _install_successful_flow(monkeypatch)
    persisted: list[dict] = []
    monkeypatch.setattr(
        pc_actions,
        "_persist_latest",
        lambda _fresh, *, section_updated_at, **_kwargs: persisted.append(
            copy.deepcopy(section_updated_at)
        ),
    )

    result = pc_actions.resonance_pc_player_data_refresh(
        stages=["inventory", "location", "inventory"],
        app=app,
        ocr=object(),
    )

    assert trace == ["location", "inventory"]
    assert result["metadata"]["executed_stages"] == ["location", "inventory"]
    assert result["metadata"]["skipped_stages"] == ["profile"]
    assert persisted == [{"location": NOW, "inventory": NOW}]


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


@pytest.mark.parametrize(
    "categories",
    [[], ["unknown"], "items", [None]],
)
def test_invalid_inventory_category_selection_is_rejected_before_game_access(categories):
    app = _FakeApp()

    with pytest.raises(ValueError):
        pc_actions.resonance_pc_player_data_refresh(
            stages=["inventory"],
            inventory_categories=categories,
            app=app,
            ocr=object(),
        )

    assert app.clicks == []


def test_inventory_categories_execute_in_canonical_order_and_dedupe(monkeypatch):
    app, trace, _marker_labels = _install_successful_flow(monkeypatch)
    monkeypatch.setattr(pc_actions, "_persist_latest", lambda *_args, **_kwargs: None)

    result = pc_actions.resonance_pc_player_data_refresh(
        stages=["inventory"],
        inventory_categories=["materials", "items", "materials"],
        app=app,
        ocr=object(),
    )

    assert trace == ["inventory", "inventory"]
    assert app.clicks == [
        pc_actions._CLICK_PROFILE,
        pc_actions._CLICK_INVENTORY,
        pc_actions._CLICK_INVENTORY_CATEGORY["items"],
        pc_actions._CLICK_INVENTORY_CATEGORY["materials"],
        pc_actions._CLICK_BACK,
        pc_actions._CLICK_PROFILE_CLOSE,
    ]
    assert list(result["inventory"]["categories"]) == ["items", "materials"]
    assert result["metadata"]["inventory_category_updated_at"] == {
        "items": NOW,
        "materials": NOW,
    }


def test_pc_player_data_refresh_stops_before_clicking_when_not_on_main(monkeypatch):
    app = _FakeApp()

    def fail_main_check(*_args, **_kwargs):
        raise StopTaskException("main page missing", success=False)

    monkeypatch.setattr(pc_actions, "_wait_for_any_marker", fail_main_check)

    with pytest.raises(StopTaskException, match="main page missing"):
        pc_actions.resonance_pc_player_data_refresh(
            stages=["location"],
            app=app,
            ocr=object(),
        )

    assert app.clicks == []


def test_stage_failure_preserves_original_error_and_does_not_persist(monkeypatch):
    app, _trace, _marker_labels = _install_successful_flow(monkeypatch)
    persist_calls: list[dict] = []

    def fail_profile(*_args, **_kwargs):
        raise StopTaskException("profile status OCR failed", success=False)

    monkeypatch.setattr(pc_actions, "_read_profile_stage", fail_profile)
    monkeypatch.setattr(
        pc_actions,
        "_persist_latest",
        lambda *_args, **_kwargs: persist_calls.append({"called": True}),
    )

    with pytest.raises(StopTaskException, match="profile status OCR failed"):
        pc_actions.resonance_pc_player_data_refresh(
            stages=["profile"],
            app=app,
            ocr=object(),
        )

    assert persist_calls == []
    assert app.clicks == [
        pc_actions._CLICK_PROFILE,
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
            stages=["inventory"],
            app=app,
            ocr=object(),
        )

    assert cleanup_pages == ["inventory"]
    assert persist_calls == []
    assert app.clicks == [
        pc_actions._CLICK_PROFILE,
        pc_actions._CLICK_INVENTORY,
        pc_actions._CLICK_INVENTORY_CATEGORY["items"],
    ]


def test_inventory_partial_category_merge_migrates_legacy_items_and_preserves_them():
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
    fresh = {
        "inventory": {
            "schema_version": 2,
            "categories": {"materials": copy.deepcopy(MATERIAL_PAYLOAD)},
        }
    }

    merged = pc_actions._merge_latest(
        existing,
        fresh,
        section_updated_at={"inventory": NOW},
        updated_at=NOW,
        inventory_category_updated_at={"materials": NOW},
    )

    assert merged["inventory"]["schema_version"] == 2
    assert merged["inventory"]["categories"] == {
        "items": old_inventory,
        "materials": MATERIAL_PAYLOAD,
    }
    assert merged["profile"] == {"uid": "keep-me"}
    assert merged["metadata"]["section_updated_at"] == {
        "profile": "2026-08-19T00:00:00+00:00",
        "inventory": NOW,
    }
    assert merged["metadata"]["inventory_category_updated_at"] == {
        "materials": NOW
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
        stages=["location"],
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
    assert cached["metadata"]["section_updated_at"] == {
        "location": NOW,
        "profile": "2026-08-19T00:00:00+00:00",
    }
    assert not cache_file.with_suffix(".json.tmp").exists()


def test_first_partial_persist_creates_only_refreshed_section(monkeypatch, tmp_path):
    cache_file = tmp_path / "player" / "latest.json"
    _redirect_cache(monkeypatch, cache_file)
    app, _trace, _marker_labels = _install_successful_flow(monkeypatch)

    pc_actions.resonance_pc_player_data_refresh(
        stages=["profile"],
        app=app,
        ocr=object(),
    )
    cached = json.loads(cache_file.read_text(encoding="utf-8"))

    assert set(cached) == {"profile", "status", "metadata"}
    assert cached["status"] == {
        "cargo": {"current": 20, "max": 650},
        "clarity": {"current": 292, "max": 292},
        "fatigue": {"current": 0, "max": 824},
    }
    assert cached["metadata"] == {
        "source": "ocr",
        "updated_at": NOW,
        "section_updated_at": {"profile": NOW},
    }


def test_successful_refresh_always_persists_and_preserves_unrelated_cache(monkeypatch, tmp_path):
    cache_file = tmp_path / "player" / "latest.json"
    cache_file.parent.mkdir(parents=True)
    original = {"sentinel": "保持原样"}
    cache_file.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    _redirect_cache(monkeypatch, cache_file)
    app, _trace, _marker_labels = _install_successful_flow(monkeypatch)

    result = pc_actions.resonance_pc_player_data_refresh(
        stages=["location"],
        app=app,
        ocr=object(),
    )

    assert result["metadata"]["persisted"] is True
    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cached["sentinel"] == "保持原样"
    assert cached["location"] == {"current_city": "修格里城"}


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
            stages=["location"],
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
    assert refresh["meta"]["inputs"][1]["name"] == "inventory_categories"
    assert refresh["meta"]["inputs"][1]["default"] == ["items"]
    assert refresh["steps"]["refresh"]["action"] == "resonance_pc.player_data_refresh"
    assert refresh["steps"]["refresh"]["params"]["stages"] == "{{ inputs.stages }}"
    assert refresh["steps"]["refresh"]["params"]["inventory_categories"] == (
        "{{ inputs.inventory_categories }}"
    )
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
    assert actions["resonance_pc.player_data_refresh"]["parameters"][1]["name"] == (
        "inventory_categories"
    )
    assert actions["resonance_pc.player_data_get_latest"]["read_only"] is True
    assert actions["resonance_pc.player_data_get_latest"]["parameters"] == []
    assert "player_data_pc/player_data_refresh" in task_ids
    assert "player_data_pc/player_data_get_latest" in task_ids
