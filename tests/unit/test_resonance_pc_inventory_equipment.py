from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import yaml

from packages.aura_core.utils.exceptions import StopTaskException
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import inventory_pc_actions as inventory
from plans.resonance_pc.src.actions import player_data_pc_actions as player_data


REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_ROOT = REPO_ROOT / "plans" / "resonance_pc"


class ResolvingVision:
    load_image_file = staticmethod(VisionService.load_image_file)

    def resolve_template(self, _plan_key: str, template_ref: str, plan_root: Path) -> str:
        return str(Path(plan_root) / template_ref)


def _write_equipment_catalog(
    root: Path,
    *,
    aggregation: str = "count_cards_by_equipment_id",
    template_size: tuple[int, int] = (100, 60),
    entries: list[dict[str, str]] | None = None,
) -> Path:
    entries = entries or [
        {
            "equipment_id": "test_equipment",
            "name": "测试装备",
            "template": "templates/inventory/equipment/test_equipment.png",
        }
    ]
    for entry in entries:
        template_path = (root / entry["template"]).resolve()
        if inventory._path_is_within(template_path, root.resolve()):
            template_path.parent.mkdir(parents=True, exist_ok=True)
            width, height = template_size
            assert cv2.imwrite(
                str(template_path),
                np.full((height, width, 3), 127, dtype=np.uint8),
            )
    catalog_path = root / "data" / "meta" / "inventory_equipment.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "category": "equipment",
                "aggregation": aggregation,
                "layout": {
                    "template_size": list(template_size),
                    "template_offset_from_card": [10, 35],
                    "card_size": [122, 121],
                    "grid_region": [397, 94, 680, 626],
                    "match_threshold": 0.82,
                    "scroll_start": [1000, 620],
                    "scroll_end": [1000, 310],
                },
                "equipment": entries,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return catalog_path


def test_actual_equipment_catalog_is_prepared_without_digit_readers() -> None:
    catalog = inventory.prepare_inventory_catalog("equipment", ResolvingVision())

    assert catalog["category"] == "equipment"
    assert catalog["_count_mode"] == inventory._COUNT_MODE_CARD_INSTANCES
    assert catalog["_digit_reader"] is None
    assert catalog["_expiry_digit_reader"] is None
    assert len(catalog["items"]) == 175
    assert len(catalog["_template_paths"]) == 175
    assert len({item["item_id"] for item in catalog["items"]}) == 175
    for template_path in catalog["_template_paths"]:
        image = cv2.imread(str(template_path), cv2.IMREAD_UNCHANGED)
        assert image is not None
        assert image.shape[:2] == (60, 100)


@pytest.mark.parametrize(
    ("case", "match"),
    (
        ("aggregation", "aggregation"),
        ("size", "100x60"),
        ("duplicate", "duplicate inventory item_id"),
        ("escape", "escapes plan root"),
    ),
)
def test_equipment_catalog_rejects_invalid_inputs(
    tmp_path: Path,
    case: str,
    match: str,
) -> None:
    plan_root = tmp_path / "plan"
    kwargs: dict[str, object] = {}
    if case == "aggregation":
        kwargs["aggregation"] = "merge"
    elif case == "size":
        kwargs["template_size"] = (99, 60)
    elif case == "duplicate":
        kwargs["entries"] = [
            {
                "equipment_id": "duplicate",
                "name": "甲",
                "template": "templates/inventory/equipment/a.png",
            },
            {
                "equipment_id": "duplicate",
                "name": "乙",
                "template": "templates/inventory/equipment/b.png",
            },
        ]
    elif case == "escape":
        outside = plan_root.parent / "outside.png"
        assert cv2.imwrite(str(outside), np.zeros((60, 100, 3), dtype=np.uint8))
        kwargs["entries"] = [
            {
                "equipment_id": "escape",
                "name": "越界",
                "template": "../outside.png",
            }
        ]
    catalog_path = _write_equipment_catalog(plan_root, **kwargs)

    with pytest.raises(ValueError, match=match):
        inventory.prepare_inventory_catalog(
            "equipment",
            ResolvingVision(),
            catalog_path=catalog_path,
            plan_root=plan_root,
        )


def test_equipment_page_counts_cards_without_reading_digits(monkeypatch) -> None:
    catalog = inventory.prepare_inventory_catalog("equipment", ResolvingVision())
    result_by_id = {
        item["item_id"]: SimpleNamespace(matches=[])
        for item in catalog["items"]
    }
    result_by_id["thunder_god"] = SimpleNamespace(
        matches=[
            SimpleNamespace(top_left=(10, 35), confidence=0.97, rect=None),
            SimpleNamespace(top_left=(142, 35), confidence=0.95, rect=None),
            SimpleNamespace(top_left=(10, 25), confidence=0.99, rect=None),
        ]
    )
    result_by_id["resonance_fiber"] = SimpleNamespace(
        matches=[SimpleNamespace(top_left=(10, 35), confidence=0.90, rect=None)]
    )

    class BatchVision(ResolvingVision):
        def find_all_templates_batch(self, **_kwargs):
            return [result_by_id[item["item_id"]] for item in catalog["items"]]

    def fail_digit_read(*_args, **_kwargs):
        raise AssertionError("equipment must not read count digits")

    monkeypatch.setattr(inventory, "read_inventory_count", fail_digit_read)
    observations = inventory.scan_inventory_page(
        np.zeros((626, 680, 3), dtype=np.uint8),
        catalog,
        object(),
        BatchVision(),
    )
    aggregated = inventory.aggregate_inventory_observations(observations, catalog)

    assert [item["item_id"] for item in observations] == [
        "thunder_god",
        "thunder_god",
    ]
    assert all(item["count"] == 1 for item in observations)
    assert aggregated == [{"item_id": "thunder_god", "name": "雷神", "count": 2}]


def _prepared_test_catalog() -> dict:
    equipment = [
        {"equipment_id": "a", "name": "甲", "template": "a.png"},
        {"equipment_id": "b", "name": "乙", "template": "b.png"},
    ]
    return {
        "schema_version": 1,
        "category": "equipment",
        "aggregation": "count_cards_by_equipment_id",
        "layout": {
            "template_size": (100, 60),
            "template_offset_from_card": (10, 35),
            "card_size": (122, 121),
            "grid_region": (397, 94, 680, 626),
            "match_threshold": 0.82,
            "scroll_start": (1000, 620),
            "scroll_end": (1000, 310),
        },
        "equipment": equipment,
        "items": [
            {
                "item_id": entry["equipment_id"],
                "equipment_id": entry["equipment_id"],
                "name": entry["name"],
                "template": entry["template"],
                "stack_policy": inventory.STACK_POLICY_MERGE,
            }
            for entry in equipment
        ],
        "_template_paths": ["a.png", "b.png"],
        "_count_mode": inventory._COUNT_MODE_CARD_INSTANCES,
        "_supports_expiry": False,
        "_digit_reader": None,
        "_expiry_digit_reader": None,
    }


def test_equipment_scroll_dedupes_overlap_and_stops_after_three_empty_scans(
    monkeypatch,
) -> None:
    images = [
        np.full((626, 680, 3), index, dtype=np.uint8)
        for index in range(1, 6)
    ]
    image_iter = iter(images)
    pages = {
        1: [{"item_id": "a", "name": "甲", "count": 1, "card_top_left": [0, 100]}],
        2: [
            {"item_id": "a", "name": "甲", "count": 1, "card_top_left": [0, 0]},
            {"item_id": "b", "name": "乙", "count": 1, "card_top_left": [132, 100]},
        ],
        3: [{"item_id": "b", "name": "乙", "count": 1, "card_top_left": [132, 100]}],
        4: [{"item_id": "b", "name": "乙", "count": 1, "card_top_left": [132, 100]}],
        5: [{"item_id": "b", "name": "乙", "count": 1, "card_top_left": [132, 100]}],
    }

    class App:
        def __init__(self) -> None:
            self.drags: list[tuple[tuple, dict]] = []

        def drag(self, *args, **kwargs) -> None:
            self.drags.append((args, kwargs))

    app = App()
    monkeypatch.setattr(
        inventory,
        "_capture_stable_grid",
        lambda *_args, **_kwargs: next(image_iter),
    )
    monkeypatch.setattr(
        inventory,
        "scan_inventory_page",
        lambda image, *_args, **_kwargs: pages[int(image[0, 0, 0])],
    )

    result = inventory.read_inventory_category(
        app,
        object(),
        object(),
        category="equipment",
        catalog=_prepared_test_catalog(),
    )

    assert result["equipment"] == [
        {"name": "甲", "count": 1, "equipment_id": "a"},
        {"name": "乙", "count": 1, "equipment_id": "b"},
    ]
    assert result["matched_card_count"] == 2
    assert result["matched_equipment_count"] == 2
    assert result["pages_scanned"] == 5
    assert result["completion_reason"] == "three_consecutive_scans_without_new_items"
    assert len(app.drags) == 4
    assert all(args == (1000, 620, 1000, 310) for args, _kwargs in app.drags)
    assert all(kwargs["duration"] == 0.5 for _args, kwargs in app.drags)
    assert all(kwargs["hold_before_release_sec"] == 0.5 for _args, kwargs in app.drags)


def test_equipment_scroll_limit_is_a_failure(monkeypatch) -> None:
    image = np.zeros((626, 680, 3), dtype=np.uint8)
    monkeypatch.setattr(inventory, "_capture_stable_grid", lambda *_args, **_kwargs: image)
    monkeypatch.setattr(
        inventory,
        "scan_inventory_page",
        lambda *_args, **_kwargs: [
            {"item_id": "a", "name": "甲", "count": 1, "card_top_left": [0, 0]}
        ],
    )

    with pytest.raises(StopTaskException, match="maximum scroll count"):
        inventory.read_inventory_category(
            SimpleNamespace(drag=lambda *_args, **_kwargs: None),
            object(),
            object(),
            category="equipment",
            catalog=_prepared_test_catalog(),
            max_scrolls=0,
        )


def test_equipment_categories_normalize_and_merge_without_touching_currencies() -> None:
    assert player_data._normalize_inventory_categories(
        ["equipment", "items", "equipment"]
    ) == ("items", "equipment")
    with pytest.raises(ValueError, match="unsupported value"):
        player_data._normalize_inventory_categories(["unknown"])
    with pytest.raises(ValueError, match="at least one category"):
        player_data._normalize_inventory_categories([])

    existing = {
        "inventory": {
            "schema_version": 2,
            "categories": {
                "items": {"category": "items", "items": [{"item_id": "coin"}]},
                "materials": {"category": "materials", "materials": []},
            },
        },
        "currencies": {"iron_coins": 123, "birch_stone": 456},
        "metadata": {
            "inventory_category_updated_at": {
                "items": "old-items",
                "materials": "old-materials",
            }
        },
    }
    fresh = {
        "inventory": {
            "schema_version": 2,
            "categories": {
                "equipment": {
                    "category": "equipment",
                    "equipment": [{"equipment_id": "a", "name": "甲", "count": 2}],
                }
            },
        }
    }
    merged = player_data._merge_latest(
        existing,
        fresh,
        section_updated_at={"inventory": "new-section"},
        inventory_category_updated_at={"equipment": "new-equipment"},
        updated_at="new-cache",
    )

    assert set(merged["inventory"]["categories"]) == {"items", "materials", "equipment"}
    assert merged["currencies"] == existing["currencies"]
    assert merged["metadata"]["inventory_category_updated_at"] == {
        "items": "old-items",
        "materials": "old-materials",
        "equipment": "new-equipment",
    }


def test_inventory_failure_does_not_persist_partial_category_results(monkeypatch) -> None:
    class App:
        def click(self, **_kwargs) -> None:
            return None

    persisted: list[dict] = []
    monkeypatch.setattr(player_data, "_wait_for_any_marker", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(player_data, "_enter_warehouse_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(player_data, "_select_inventory_category", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(player_data, "_best_effort_return_to_main", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(player_data.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        player_data,
        "prepare_inventory_catalog",
        lambda category, _vision: {"category": category},
    )

    def scan(*_args, category: str, **_kwargs):
        if category == "equipment":
            raise RuntimeError("equipment failed")
        return {"category": category, "items": []}

    monkeypatch.setattr(player_data, "_scan_inventory_stage", scan)
    monkeypatch.setattr(
        player_data,
        "_persist_latest",
        lambda *args, **kwargs: persisted.append({"args": args, "kwargs": kwargs}),
    )

    with pytest.raises(RuntimeError, match="equipment failed"):
        player_data.resonance_pc_player_data_refresh(
            stages=["inventory"],
            inventory_categories=["items", "equipment"],
            app=App(),
            ocr=object(),
            vision=object(),
        )
    assert persisted == []


def test_inventory_three_category_orchestration_is_ordered_and_persisted(monkeypatch) -> None:
    class App:
        def click(self, **_kwargs) -> None:
            return None

    prepared: list[str] = []
    selected: list[str] = []
    scanned: list[str] = []
    persisted: list[dict] = []
    monkeypatch.setattr(player_data, "_wait_for_any_marker", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(player_data, "_enter_warehouse_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(player_data, "_close_profile_panel_to_main", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(player_data.time, "sleep", lambda *_args, **_kwargs: None)

    def prepare(category: str, _vision):
        prepared.append(category)
        return {"category": category}

    def select(_app, category: str) -> None:
        selected.append(category)

    def scan(*_args, category: str, **_kwargs):
        scanned.append(category)
        if category == "items":
            return {
                "category": "items",
                "items": [
                    {"item_id": "iron_alliance_coin", "name": "铁盟币", "count": 5}
                ],
            }
        if category == "materials":
            return {"category": "materials", "materials": []}
        return {
            "category": "equipment",
            "equipment": [{"equipment_id": "a", "name": "甲", "count": 2}],
        }

    monkeypatch.setattr(player_data, "prepare_inventory_catalog", prepare)
    monkeypatch.setattr(player_data, "_select_inventory_category", select)
    monkeypatch.setattr(player_data, "_scan_inventory_stage", scan)
    monkeypatch.setattr(
        player_data,
        "_persist_latest",
        lambda fresh, **kwargs: persisted.append(
            {"fresh": fresh, "kwargs": kwargs}
        ),
    )

    result = player_data.resonance_pc_player_data_refresh(
        stages=["inventory"],
        inventory_categories=["equipment", "materials", "items", "equipment"],
        app=App(),
        ocr=object(),
        vision=object(),
    )

    assert prepared == ["items", "materials", "equipment"]
    assert selected == prepared
    assert scanned == prepared
    assert list(result["inventory"]["categories"]) == prepared
    assert result["currencies"] == {"iron_coins": 5}
    assert set(result["metadata"]["inventory_category_updated_at"]) == set(prepared)
    assert result["metadata"]["persisted"] is True
    assert len(persisted) == 1
    assert list(persisted[0]["fresh"]["inventory"]["categories"]) == prepared


def test_equipment_category_selection_compares_both_other_buttons(monkeypatch) -> None:
    calls = {category: 0 for category in player_data._INVENTORY_CATEGORY_ORDER}
    clicks: list[dict] = []

    def brightness(_app, category: str) -> float:
        calls[category] += 1
        if calls[category] == 1:
            return {"items": 160.0, "materials": 100.0, "equipment": 180.0}[category]
        return {"items": 120.0, "materials": 110.0, "equipment": 180.0}[category]

    app = SimpleNamespace(click=lambda **kwargs: clicks.append(kwargs))
    monkeypatch.setattr(player_data, "_inventory_category_brightness", brightness)
    monkeypatch.setattr(player_data.time, "sleep", lambda *_args, **_kwargs: None)

    player_data._select_inventory_category(app, "equipment", timeout_sec=1.0)

    assert calls == {"items": 2, "materials": 2, "equipment": 2}
    assert clicks[0] == {"x": 1205, "y": 203}
    assert player_data._INVENTORY_CATEGORY_REGIONS["equipment"] == (1110, 174, 170, 58)


def test_player_data_task_exports_equipment_category() -> None:
    task = yaml.safe_load(
        (PLAN_ROOT / "tasks" / "player_data_pc.yaml").read_text(encoding="utf-8")
    )
    category_input = next(
        item
        for item in task["player_data_refresh"]["meta"]["inputs"]
        if item["name"] == "inventory_categories"
    )
    assert category_input["default"] == ["items"]
    assert category_input["item"]["enum"] == ["items", "materials", "equipment"]
