from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import inventory_pc_actions as inventory_actions
from plans.resonance_pc.src.actions.inventory_pc_actions import aggregate_inventory_observations


REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_ROOT = REPO_ROOT / "plans" / "resonance_pc"
CATALOG_PATH = PLAN_ROOT / "data" / "meta" / "inventory_items.json"


CATALOG = {
    "default_stack_policy": "merge",
    "items": [
        {"item_id": "ordinary", "name": "普通道具"},
        {
            "item_id": "cactus_energy_lollipop",
            "name": "仙人掌能量棒棒糖",
            "stack_policy": "split_by_expiry",
        },
        {
            "item_id": "stamina_lollipop",
            "name": "提神棒棒糖",
            "stack_policy": "split_by_expiry",
        },
    ],
}


def test_ordinary_item_stacks_merge_by_item_id():
    result = aggregate_inventory_observations(
        [
            {"item_id": "ordinary", "count": 2},
            {"item_id": "ordinary", "count": 5},
        ],
        CATALOG,
    )

    assert result == [{"item_id": "ordinary", "name": "普通道具", "count": 7}]


def test_expiring_item_splits_by_expiry_and_merges_equal_expiry():
    result = aggregate_inventory_observations(
        [
            {
                "item_id": "cactus_energy_lollipop",
                "count": 3,
                "expiry": {"kind": "days_remaining", "value": 5, "raw": "5天"},
            },
            {
                "item_id": "cactus_energy_lollipop",
                "count": 8,
                "expiry": {"kind": "days_remaining", "value": 4, "raw": "4天"},
            },
            {
                "item_id": "cactus_energy_lollipop",
                "count": 2,
                "expiry": {"kind": "days_remaining", "value": 5, "raw": "5天"},
            },
        ],
        CATALOG,
    )

    assert result == [
        {
            "item_id": "cactus_energy_lollipop",
            "name": "仙人掌能量棒棒糖",
            "count": 5,
            "expiry": {"kind": "days_remaining", "value": 5, "raw": "5天"},
        },
        {
            "item_id": "cactus_energy_lollipop",
            "name": "仙人掌能量棒棒糖",
            "count": 8,
            "expiry": {"kind": "days_remaining", "value": 4, "raw": "4天"},
        },
    ]


def test_expiring_item_requires_readable_expiry():
    with pytest.raises(ValueError, match="expiry is required"):
        aggregate_inventory_observations(
            [{"item_id": "cactus_energy_lollipop", "count": 3}],
            CATALOG,
        )


def test_second_expiring_item_also_splits_by_expiry():
    result = aggregate_inventory_observations(
        [
            {
                "item_id": "stamina_lollipop",
                "count": 8,
                "expiry": {"kind": "days_remaining", "value": 4, "raw": "4天"},
            },
            {
                "item_id": "stamina_lollipop",
                "count": 2,
                "expiry": {"kind": "days_remaining", "value": 1, "raw": "1天"},
            },
        ],
        CATALOG,
    )

    assert result == [
        {
            "item_id": "stamina_lollipop",
            "name": "提神棒棒糖",
            "count": 8,
            "expiry": {"kind": "days_remaining", "value": 4, "raw": "4天"},
        },
        {
            "item_id": "stamina_lollipop",
            "name": "提神棒棒糖",
            "count": 2,
            "expiry": {"kind": "days_remaining", "value": 1, "raw": "1天"},
        },
    ]


def test_unknown_item_is_rejected():
    with pytest.raises(ValueError, match="unknown item_id"):
        aggregate_inventory_observations(
            [{"item_id": "missing", "count": 1}],
            CATALOG,
        )


def test_default_catalog_loads_real_100_by_70_templates():
    catalog = inventory_actions.load_inventory_catalog()

    assert catalog["schema_version"] == 1
    assert catalog["layout"]["template_size"] == (100, 70)
    assert {item["item_id"] for item in catalog["items"]} == {
        "cactus_energy_lollipop",
        "stamina_lollipop",
    }

    for item in catalog["items"]:
        template_path = PLAN_ROOT / item["template"]
        image = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        assert image is not None, template_path
        assert image.shape[:2] == (70, 100), template_path


def test_catalog_can_be_loaded_from_an_explicit_path():
    explicit = inventory_actions.load_inventory_catalog(CATALOG_PATH)
    default = inventory_actions.load_inventory_catalog()

    assert explicit["schema_version"] == default["schema_version"]
    assert explicit["layout"] == default["layout"]
    assert [item["item_id"] for item in explicit["items"]] == [
        item["item_id"] for item in default["items"]
    ]
    assert [item["_template_path"] for item in explicit["items"]] == [
        item["_template_path"] for item in default["items"]
    ]


def test_relative_roi_uses_template_top_left_and_clips_to_frame():
    frame_shape = (720, 1280, 3)

    assert inventory_actions.relative_roi(
        (100, 200), [48, -23, 52, 23], frame_shape
    ) == (148, 177, 52, 23)
    assert inventory_actions.relative_roi(
        (100, 200), [82, 68, 30, 29], frame_shape
    ) == (182, 268, 30, 29)
    assert (
        inventory_actions.relative_roi(
            (5, 10), [-20, -20, 30, 30], frame_shape
        )
        is None
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [("12", 12), ("x 8", 8), ("×003", 3), ("数量：27", 27)],
)
def test_parse_count_text(text, expected):
    assert inventory_actions.parse_count_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("剩余5天", {"kind": "days_remaining", "value": 5, "raw": "剩余5天"}),
        ("4天", {"kind": "days_remaining", "value": 4, "raw": "4天"}),
    ],
)
def test_parse_expiry_text(text, expected):
    assert inventory_actions.parse_expiry_text(text) == expected


class _TextResult:
    def __init__(self, text: str):
        self.text = text


class _ShapeAwareOcr:
    """Return expiry/count text by ROI aspect ratio, preserving match order."""

    def __init__(self):
        self.expiry_values = iter(("剩余5天", "4天"))
        self.count_values = iter(("3", "8"))
        self.roi_shapes: list[tuple[int, int]] = []

    def recognize_all(self, source_image):
        height, width = source_image.shape[:2]
        self.roi_shapes.append((height, width))
        text = next(self.expiry_values) if width / height > 1.5 else next(self.count_values)
        return SimpleNamespace(results=[_TextResult(text)])


def test_scan_inventory_page_finds_multiple_instances_and_reads_relative_rois():
    catalog = inventory_actions.load_inventory_catalog()
    cactus = next(
        item for item in catalog["items"] if item["item_id"] == "cactus_energy_lollipop"
    )
    template = cv2.imread(str(PLAN_ROOT / cactus["template"]), cv2.IMREAD_COLOR)
    assert template is not None

    page = np.zeros((720, 1280, 3), dtype=np.uint8)
    for x, y in ((160, 150), (620, 360)):
        page[y : y + 70, x : x + 100] = template

    ocr = _ShapeAwareOcr()
    observations = inventory_actions.scan_inventory_page(page, catalog, ocr)
    cactus_observations = [
        item for item in observations if item["item_id"] == "cactus_energy_lollipop"
    ]

    public_observations = [
        {
            key: item[key]
            for key in ("item_id", "name", "count", "expiry")
        }
        for item in cactus_observations
    ]
    assert public_observations == [
        {
            "item_id": "cactus_energy_lollipop",
            "name": "仙人掌能量棒棒糖",
            "count": 3,
            "expiry": {"kind": "days_remaining", "value": 5, "raw": "剩余5天"},
        },
        {
            "item_id": "cactus_energy_lollipop",
            "name": "仙人掌能量棒棒糖",
            "count": 8,
            "expiry": {"kind": "days_remaining", "value": 4, "raw": "4天"},
        },
    ]


class _CaptureApp:
    def __init__(self, pages):
        self.pages = [np.asarray(page) for page in pages]
        self.index = 0
        self.scroll_calls = 0
        self.drag_calls = 0

    def capture(self, rect=None):
        return SimpleNamespace(success=True, image=self.pages[self.index])

    def scroll(self, *_args, **_kwargs):
        self.scroll_calls += 1
        self.index = min(self.index + 1, len(self.pages) - 1)

    def drag(self, *_args, **_kwargs):
        self.drag_calls += 1
        self.index = min(self.index + 1, len(self.pages) - 1)


def _marker_page(marker: int):
    page = np.zeros((720, 1280, 3), dtype=np.uint8)
    page[0, 0, 0] = marker
    return page


def test_read_inventory_items_stops_before_scroll_when_all_supported_ids_found(monkeypatch):
    pages = [_marker_page(1)]
    app = _CaptureApp(pages)
    catalog = inventory_actions.load_inventory_catalog()
    monkeypatch.setattr(inventory_actions, "load_inventory_catalog", lambda *_args, **_kwargs: catalog)
    monkeypatch.setattr(inventory_actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        inventory_actions,
        "scan_inventory_page",
        lambda page, _catalog, _ocr: [
            {
                "item_id": "cactus_energy_lollipop",
                "name": "仙人掌能量棒棒糖",
                "count": 3,
                "expiry": {"kind": "days_remaining", "value": 5, "raw": "5天"},
                "card_top_left": [10, 10],
            },
            {
                "item_id": "stamina_lollipop",
                "name": "提神棒棒糖",
                "count": 8,
                "expiry": {"kind": "days_remaining", "value": 4, "raw": "4天"},
                "card_top_left": [150, 10],
            },
        ],
    )

    result = inventory_actions.read_inventory_items(
        app,
        object(),
        max_scrolls=5,
    )

    assert result["pages_scanned"] == 1
    assert result["completion_reason"] == "all_supported_items_found"
    assert result["scan_complete"] is True
    assert {item["item_id"] for item in result["items"]} == {
        "cactus_energy_lollipop",
        "stamina_lollipop",
    }
    assert app.scroll_calls + app.drag_calls == 0


def test_read_inventory_items_scans_to_bottom_when_supported_id_is_missing(monkeypatch):
    pages = [_marker_page(1), _marker_page(2), _marker_page(3), _marker_page(4)]
    app = _CaptureApp(pages)
    catalog = inventory_actions.load_inventory_catalog()
    scanned_markers: list[int] = []
    alignments = iter(((100, 1.0), (0, 1.0), (0, 1.0)))
    monkeypatch.setattr(inventory_actions, "load_inventory_catalog", lambda *_args, **_kwargs: catalog)
    monkeypatch.setattr(inventory_actions, "_estimate_scroll_delta", lambda *_args: next(alignments))
    monkeypatch.setattr(inventory_actions.time, "sleep", lambda _seconds: None)

    def fake_scan(page, _catalog, _ocr):
        marker = int(page[0, 0, 0])
        scanned_markers.append(marker)
        if marker == 1:
            return [
                {
                    "item_id": "cactus_energy_lollipop",
                    "name": "仙人掌能量棒棒糖",
                    "count": 3,
                    "expiry": {"kind": "days_remaining", "value": 5, "raw": "5天"},
                    "card_top_left": [10, 10],
                }
            ]
        return []

    monkeypatch.setattr(inventory_actions, "scan_inventory_page", fake_scan)

    result = inventory_actions.read_inventory_items(
        app,
        object(),
        max_scrolls=5,
    )

    assert scanned_markers == [1, 2, 3, 4]
    assert result["pages_scanned"] == 4
    assert result["completion_reason"] == "warehouse_bottom_reached"
    assert result["scan_complete"] is True
    assert [item["item_id"] for item in result["items"]] == [
        "cactus_energy_lollipop"
    ]
    assert app.scroll_calls + app.drag_calls == 3
