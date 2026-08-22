from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from packages.aura_core.utils.exceptions import StopTaskException
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import inventory_pc_actions as inventory_actions
from plans.resonance_pc.src.actions.inventory_pc_actions import aggregate_inventory_observations


REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_ROOT = REPO_ROOT / "plans" / "resonance_pc"
CATALOG_PATH = PLAN_ROOT / "data" / "meta" / "inventory_items.json"
MATERIAL_CATALOG_PATH = PLAN_ROOT / "data" / "meta" / "inventory_materials.json"
DIGIT_CATALOG_PATH = PLAN_ROOT / "data" / "meta" / "inventory_digits.json"
EXPIRY_DIGIT_CATALOG_PATH = PLAN_ROOT / "data" / "meta" / "inventory_expiry_digits.json"
EXPIRY_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "resonance_pc" / "inventory_expiry"


class _FrameworkVisionHarness:
    def __init__(self):
        self.service = VisionService()
        self.batch_calls = []

    def resolve_template(self, plan_key, ref, plan_path):
        return self.service.resolve_template(plan_key, ref, plan_path)

    def find_all_templates_batch(self, **kwargs):
        self.batch_calls.append(kwargs)
        return asyncio.run(self.service.find_all_templates_batch_async(**kwargs))


class _PathOnlyVision:
    def resolve_template(self, _plan_key, ref, plan_path):
        return Path(plan_path) / ref


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


def test_temporary_expiry_disable_merges_limited_item_stacks_without_expiry():
    assert inventory_actions._EXPIRY_RECOGNITION_ENABLED is False

    result = aggregate_inventory_observations(
        [
            {"item_id": "cactus_energy_lollipop", "count": 3},
            {"item_id": "cactus_energy_lollipop", "count": 8},
        ],
        CATALOG,
        expiry_recognition_enabled=False,
    )

    assert result == [
        {
            "item_id": "cactus_energy_lollipop",
            "name": "仙人掌能量棒棒糖",
            "count": 11,
        }
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
    assert catalog["layout"]["grid_region"] == (397, 94, 680, 626)
    assert catalog["layout"]["scroll_start"] == [1000, 620]
    assert catalog["layout"]["scroll_end"] == [1000, 310]
    assert (
        catalog["layout"]["scroll_start"][1]
        - catalog["layout"]["scroll_end"][1]
    ) == 310
    assert inventory_actions._DEFAULT_MAX_SCROLLS == 30
    assert {item["item_id"] for item in catalog["items"]} == {
        "additional_capital_injection_application",
        "advertising_ticket",
        "anita_standard_equipment_crate",
        "arrest_warrant",
        "axolotl_alliance_badge",
        "birch_crystal",
        "black_moon_standard_equipment_crate",
        "blank_videotape",
        "brawl_arcade_coin",
        "cactus_energy_lollipop",
        "cactus_jump_roll",
        "cactus_stamina_popping_candy",
        "cardamom_ginger_lily",
        "cheesecake",
        "decoy_balloon",
        "decoy_explosive_balloon",
        "decorated_fondant_cake",
        "desperate_badge",
        "duty_badge",
        "imperial_standard_equipment_crate",
        "iron_alliance_coin",
        "iron_alliance_standard_equipment_crate",
        "jiaozi",
        "magpie_pastry",
        "material_ticket",
        "order_request",
        "purchase_order_book",
        "quadratic_one_variable",
        "renegotiation_request",
        "resume_intelligence",
        "self_exploration_film_roll",
        "self_observation_film_roll",
        "seal_ticket",
        "silver_branch_mint",
        "stamina_chewing_gum",
        "stamina_lollipop",
        "survey_beacon",
        "task_commission_letter",
        "white_day_chocolate_falcon_2026",
        "white_day_chocolate_iliad_2026",
        "white_day_chocolate_simon_2026",
    }


def test_material_catalog_loads_captured_templates_as_distinct_entries():
    catalog = inventory_actions.load_inventory_catalog(MATERIAL_CATALOG_PATH)
    vision = VisionService()

    assert catalog["category"] == "materials"
    assert catalog["layout"]["match_threshold"] == 0.94
    assert len(catalog["items"]) == 110
    assert {item["item_id"] for item in catalog["items"]} >= {
        "origin_string",
        "combat_memory_8_titanium",
        "combat_memory_1_titanium",
        "nebula_matter_8_titanium",
        "nebula_matter_1_titanium",
        "countermeasure_system_carrier",
        "thunder_greatsword",
        "studious_lamp_wick",
        "twilight_hard_spike",
        "superconducting_coil",
        "deep_sleep_wood",
        "nether_thunder_fragment",
        "ancestor_repentance",
        "swamp_mire_turbid_fluid",
        "pleiades_pipeline",
        "sea_dragon_tail_fin",
        "lost_wood_reef",
        "weapon_pipeline",
        "birch_stinging_cell_sac",
        "fluffy_union_poster_bear",
        "birch_primordial_substance",
        "fluffy_union_poster_elephant",
        "standard_armament_modification_voucher",
        "pleiades_part",
        "deep_sleep_fibrous_root",
        "birch_gland",
        "surging_electric_tendril",
        "brain_fog_spore",
        "birch_flagellum",
        "classic_pet_food",
    }
    for item in catalog["items"]:
        template_path = vision.resolve_template(
            "resonance_pc",
            item["template"],
            PLAN_ROOT,
        )
        image = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        assert image is not None, template_path
        assert image.shape[:2] == (70, 100), template_path


def test_default_digit_catalog_loads_all_normalized_templates():
    reader = inventory_actions.load_inventory_digit_catalog()

    assert reader["schema_version"] == 1
    assert reader["count_band_from_card"] == (0, 80, 122, 41)
    assert reader["normalized_size"] == (18, 22)
    assert reader["min_digit_score"] == 0.75
    assert reader["min_digit_margin"] == 0.02
    assert {digit: len(samples) for digit, samples in reader["_templates"].items()} == {
        "0": 3,
        "1": 10,
        "2": 3,
        "3": 7,
        "4": 5,
        "5": 6,
        "6": 6,
        "7": 3,
        "8": 2,
        "9": 3,
    }
    assert all(
        sample.shape == (22, 18)
        for samples in reader["_templates"].values()
        for sample in samples
    )


def test_expiry_digit_catalog_loads_only_currently_available_templates():
    reader = inventory_actions.load_inventory_expiry_digit_catalog()

    assert reader["schema_version"] == 1
    assert reader["available_digits"] == ["3", "4", "6", "7"]
    assert reader["digit_x_range"] == (25, 41)
    assert reader["normalized_size"] == (14, 18)
    assert reader["similarity_mode"] == "gaussian_cosine"
    assert reader["gaussian_sigma"] == 0.9
    assert reader["max_digits"] == 2
    assert {digit: len(samples) for digit, samples in reader["_templates"].items()} == {
        "3": 1,
        "4": 1,
        "6": 1,
        "7": 1,
    }
    assert all(
        sample.shape == (18, 14)
        for samples in reader["_templates"].values()
        for sample in samples
    )


def test_catalog_can_be_loaded_from_an_explicit_path():
    explicit = inventory_actions.load_inventory_catalog(CATALOG_PATH)
    default = inventory_actions.load_inventory_catalog()

    assert explicit["schema_version"] == default["schema_version"]
    assert explicit["layout"] == default["layout"]
    assert [item["item_id"] for item in explicit["items"]] == [
        item["item_id"] for item in default["items"]
    ]
    assert [item["template"] for item in explicit["items"]] == [
        item["template"] for item in default["items"]
    ]
    assert explicit["_digit_reader"]["_template_paths"] == default["_digit_reader"][
        "_template_paths"
    ]
    assert explicit["_expiry_digit_reader"]["_template_paths"] == default[
        "_expiry_digit_reader"
    ]["_template_paths"]


def test_relative_roi_uses_template_top_left_and_clips_to_frame():
    frame_shape = (720, 1280, 3)

    assert inventory_actions.relative_roi(
        (100, 200), [48, -23, 52, 23], frame_shape
    ) == (148, 177, 52, 23)
    assert inventory_actions.relative_roi(
        (0, 0), [0, 80, 122, 41], (121, 122, 3)
    ) == (0, 80, 122, 41)
    assert (
        inventory_actions.relative_roi(
            (5, 10), [-20, -20, 30, 30], frame_shape
        )
        is None
    )


def _synthetic_count_card(text: str, reader):
    card = np.zeros((121, 122, 3), dtype=np.uint8)
    band_x, band_y, band_width, _band_height = reader["count_band_from_card"]
    right = band_x + band_width - 7
    baseline = band_y + 32
    for digit in reversed(text):
        sample = reader["_templates"][digit][0]
        ys, xs = np.where(sample > 0)
        glyph = sample[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        target_height = 13
        target_width = max(3, int(round(glyph.shape[1] * target_height / glyph.shape[0])))
        glyph = cv2.resize(
            glyph,
            (target_width, target_height),
            interpolation=cv2.INTER_NEAREST,
        )
        left = right - target_width
        top = baseline - target_height
        card[top:baseline, left:right] = np.repeat(glyph[:, :, None], 3, axis=2)
        right = left - 3
    return card


@pytest.mark.parametrize("text", ["1", "6", "9", "42396236"])
def test_digit_template_reader_segments_and_joins_counts(text):
    reader = inventory_actions.load_inventory_digit_catalog()
    card = _synthetic_count_card(text, reader)

    assert inventory_actions.read_inventory_count(
        card,
        reader,
        item_id="synthetic",
    ) == int(text)


def test_digit_template_reader_uses_stricter_white_fallback_on_light_card():
    reader = inventory_actions.load_inventory_digit_catalog()
    source = _synthetic_count_card("266", reader)
    foreground = np.any(source > 0, axis=2).astype(np.uint8) * 255
    outline = cv2.dilate(foreground, np.ones((3, 3), dtype=np.uint8))
    card = np.full_like(source, 180)
    card[outline > 0] = 25
    card[foreground > 0] = 255

    assert reader["white_min_candidates"] == [165, 180, 195]
    assert inventory_actions.read_inventory_count(
        card,
        reader,
        item_id="light_card",
    ) == 266


def test_digit_template_reader_rejects_missing_digit_run():
    reader = inventory_actions.load_inventory_digit_catalog()

    with pytest.raises(StopTaskException, match="no_right_aligned_digit_run"):
        inventory_actions.read_inventory_count(
            np.zeros((121, 122, 3), dtype=np.uint8),
            reader,
            item_id="missing",
        )


def test_digit_template_reader_rejects_ambiguous_digit(monkeypatch):
    reader = inventory_actions.load_inventory_digit_catalog()
    card = _synthetic_count_card("1", reader)
    monkeypatch.setattr(
        inventory_actions,
        "match_quantity_digit",
        lambda *_args, **_kwargs: {
            "digit": "1",
            "score": 0.9,
            "margin": 0.01,
            "second_digit": "7",
            "second_score": 0.89,
        },
    )

    with pytest.raises(StopTaskException, match="unable to match count digit"):
        inventory_actions.read_inventory_count(
            card,
            reader,
            item_id="ambiguous",
        )


def _synthetic_expiry_roi(text: str, reader):
    def fixture_for(digit: str):
        path = sorted(EXPIRY_FIXTURE_ROOT.glob(f"expiry_{digit}_*.png"))[0]
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        assert image is not None, path
        return image

    if len(text) == 1:
        return fixture_for(text)

    roi = fixture_for(text[-1]).copy()
    min_x, max_right = reader["digit_x_range"]
    roi[:, min_x:max_right] = 0
    right = 40
    baseline = 18
    for digit in reversed(text):
        components = inventory_actions.segment_expiry_digits(fixture_for(digit), reader)
        assert len(components) == 1
        glyph = components[0]["glyph"]
        height, width = glyph.shape
        left = right - width
        top = baseline - height
        roi[top:baseline, left:right] = np.repeat(glyph[:, :, None], 3, axis=2)
        right = left - 1
    return roi


@pytest.mark.parametrize("text", ["3", "4", "6", "7", "34", "63", "67"])
def test_expiry_digit_reader_segments_and_joins_up_to_two_digits(text):
    reader = inventory_actions.load_inventory_expiry_digit_catalog()

    assert inventory_actions.read_inventory_expiry(
        _synthetic_expiry_roi(text, reader),
        reader,
        item_id="synthetic",
    ) == {
        "kind": "days_remaining",
        "value": int(text),
        "raw": f"digit_template:{text}",
    }


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("expiry_3_02.png", 3),
        ("expiry_3_03.png", 3),
        ("expiry_3_stamina_20260822.png", 3),
        ("expiry_4_02.png", 4),
        ("expiry_4_20260822.png", 4),
        ("expiry_6_01.png", 6),
        ("expiry_6_20260822.png", 6),
        ("expiry_7_20260822.png", 7),
    ],
)
def test_one_expiry_template_per_digit_handles_old_and_current_raster_variants(
    fixture_name,
    expected,
):
    reader = inventory_actions.load_inventory_expiry_digit_catalog()
    image = cv2.imread(str(EXPIRY_FIXTURE_ROOT / fixture_name), cv2.IMREAD_COLOR)
    assert image is not None

    result = inventory_actions.read_inventory_expiry(
        image,
        reader,
        item_id="raster_variant",
    )

    assert result == {
        "kind": "days_remaining",
        "value": expected,
        "raw": f"digit_template:{expected}",
    }


def test_expiry_gaussian_cosine_keeps_current_variants_above_threshold():
    reader = inventory_actions.load_inventory_expiry_digit_catalog()
    expected_minimums = {
        "expiry_3_stamina_20260822.png": 0.96,
        "expiry_4_20260822.png": 0.93,
        "expiry_6_20260822.png": 0.94,
        "expiry_7_20260822.png": 0.99,
    }

    for fixture_name, minimum in expected_minimums.items():
        image = cv2.imread(str(EXPIRY_FIXTURE_ROOT / fixture_name), cv2.IMREAD_COLOR)
        components = inventory_actions.segment_expiry_digits(image, reader)
        assert len(components) == 1
        match = inventory_actions.match_expiry_digit(components[0]["glyph"], reader)
        assert match["score"] >= minimum
        assert match["margin"] >= reader["min_digit_margin"]


def test_expiry_digit_reader_rejects_an_unavailable_digit(monkeypatch):
    reader = inventory_actions.load_inventory_expiry_digit_catalog()
    monkeypatch.setattr(
        inventory_actions,
        "match_expiry_digit",
        lambda *_args, **_kwargs: {
            "digit": "6",
            "score": 0.82,
            "margin": 0.01,
            "second_digit": "3",
            "second_score": 0.81,
        },
    )

    with pytest.raises(StopTaskException, match="unable to match expiry digit"):
        inventory_actions.read_inventory_expiry(
            _synthetic_expiry_roi("6", reader),
            reader,
            item_id="unsupported",
        )


class _ForbiddenOcr:
    def recognize_all(self, *_args, **_kwargs):
        raise AssertionError("inventory expiry recognition must not call OCR")


def test_scan_inventory_page_uses_digit_templates_for_count_and_expiry(monkeypatch):
    catalog = inventory_actions.load_inventory_catalog()
    vision = _FrameworkVisionHarness()
    inventory_actions._resolve_inventory_template_paths(catalog, vision)
    cactus = next(
        item for item in catalog["items"] if item["item_id"] == "cactus_energy_lollipop"
    )
    template_bgr = cv2.imread(str(PLAN_ROOT / cactus["template"]), cv2.IMREAD_COLOR)
    assert template_bgr is not None
    template = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2RGB)

    page = np.zeros((720, 1280, 3), dtype=np.uint8)
    expiry_reader = catalog["_expiry_digit_reader"]
    for (x, y), expiry_text in zip(((160, 150), (620, 360)), ("6", "4"), strict=True):
        page[y : y + 70, x : x + 100] = template
        expiry_x = x + catalog["layout"]["expiry_roi_from_template"][0]
        expiry_y = y + catalog["layout"]["expiry_roi_from_template"][1]
        page[expiry_y : expiry_y + 23, expiry_x : expiry_x + 52] = _synthetic_expiry_roi(
            expiry_text,
            expiry_reader,
        )

    counts = iter((3, 8))
    count_card_shapes = []

    def fake_read_count(card_image, _digit_reader, *, item_id):
        assert item_id == "cactus_energy_lollipop"
        count_card_shapes.append(card_image.shape[:2])
        return next(counts)

    monkeypatch.setattr(inventory_actions, "read_inventory_count", fake_read_count)
    observations = inventory_actions.scan_inventory_page(
        page,
        catalog,
        _ForbiddenOcr(),
        vision,
        expiry_recognition_enabled=True,
    )
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
    assert {
        (item["count"], item["expiry"]["value"], item["expiry"]["raw"])
        for item in public_observations
    } == {
        (3, 6, "digit_template:6"),
        (8, 4, "digit_template:4"),
    }
    assert count_card_shapes == [(121, 122), (121, 122)]
    assert len(vision.batch_calls) == 1
    assert vision.batch_calls[0]["source_image"] is page
    assert vision.batch_calls[0]["use_grayscale"] is False
    assert vision.batch_calls[0]["template_images"] == catalog["_template_paths"]


def test_framework_vision_matches_rgb_capture_against_material_template():
    template_path = PLAN_ROOT / "templates" / "inventory" / "materials" / "origin_string.png"
    template_bgr = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
    assert template_bgr is not None
    template_rgb = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2RGB)
    source_rgb = np.zeros((180, 220, 3), dtype=np.uint8)
    source_rgb[45:115, 60:160] = template_rgb
    service = VisionService()

    results = asyncio.run(
        service.find_all_templates_batch_async(
            source_image=source_rgb,
            template_images=[str(template_path)],
            threshold=0.94,
            use_grayscale=False,
        )
    )

    assert len(results) == 1
    assert results[0].count == 1
    assert results[0].matches[0].top_left == (60, 45)
    assert results[0].matches[0].confidence > 0.999


class _CaptureApp:
    def __init__(self, pages):
        self.pages = [np.asarray(page) for page in pages]
        self.index = 0
        self.scroll_calls = 0
        self.drag_calls = 0
        self.drag_requests = []

    def capture(self, rect=None):
        return SimpleNamespace(success=True, image=self.pages[self.index])

    def scroll(self, *_args, **_kwargs):
        self.scroll_calls += 1
        self.index = min(self.index + 1, len(self.pages) - 1)

    def drag(self, *_args, **_kwargs):
        self.drag_calls += 1
        self.drag_requests.append({"args": _args, "kwargs": _kwargs})
        self.index = min(self.index + 1, len(self.pages) - 1)


def _marker_page(marker: int):
    page = np.zeros((720, 1280, 3), dtype=np.uint8)
    page[0, 0, 0] = marker
    return page


def test_read_inventory_items_does_not_stop_when_all_supported_ids_are_found(monkeypatch):
    pages = [_marker_page(1), _marker_page(2), _marker_page(3), _marker_page(4)]
    app = _CaptureApp(pages)
    catalog = inventory_actions.load_inventory_catalog()
    catalog["items"] = [
        item
        for item in catalog["items"]
        if item["item_id"] in {"cactus_energy_lollipop", "stamina_lollipop"}
    ]
    monkeypatch.setattr(inventory_actions, "load_inventory_catalog", lambda *_args, **_kwargs: catalog)
    monkeypatch.setattr(
        inventory_actions,
        "_estimate_scroll_delta",
        lambda *_args: (0, 1.0),
    )
    monkeypatch.setattr(inventory_actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        inventory_actions,
        "scan_inventory_page",
        lambda page, _catalog, _ocr, _vision: [
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
        _PathOnlyVision(),
        max_scrolls=5,
    )

    assert result["pages_scanned"] == 4
    assert result["completion_reason"] == "three_consecutive_scans_without_new_items"
    assert result["consecutive_scans_without_new_items"] == 3
    assert result["scan_complete"] is True
    assert {item["item_id"] for item in result["items"]} == {
        "cactus_energy_lollipop",
        "stamina_lollipop",
    }
    assert app.scroll_calls + app.drag_calls == 3


def test_material_catalog_and_scanner_reuse_card_and_digit_pipeline(monkeypatch, tmp_path):
    item_catalog_payload = json.loads(
        (PLAN_ROOT / "data" / "meta" / "inventory_items.json").read_text(
            encoding="utf-8"
        )
    )
    catalog_path = tmp_path / "inventory_materials.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "category": "materials",
                "default_stack_policy": "merge",
                "layout": item_catalog_payload["layout"],
                "materials": [
                    {
                        "material_id": "sample_material",
                        "name": "测试材料",
                        "template": item_catalog_payload["items"][2]["template"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    catalog = inventory_actions.load_inventory_catalog(
        catalog_path,
        plan_root=PLAN_ROOT,
    )
    app = _CaptureApp([_marker_page(index) for index in range(1, 5)])
    monkeypatch.setattr(
        inventory_actions,
        "load_inventory_catalog",
        lambda *_args, **_kwargs: catalog,
    )
    monkeypatch.setattr(inventory_actions, "_estimate_scroll_delta", lambda *_args: (0, 1.0))
    monkeypatch.setattr(inventory_actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        inventory_actions,
        "scan_inventory_page",
        lambda page, _catalog, _ocr, _vision: [
            {
                "item_id": "sample_material",
                "name": "测试材料",
                "count": 12,
                "card_top_left": [10, int(page[0, 0, 0]) * 100],
            }
        ]
        if int(page[0, 0, 0]) == 1
        else [],
    )

    result = inventory_actions.read_inventory_category(
        app,
        object(),
        _PathOnlyVision(),
        category="materials",
        catalog_path=catalog_path,
        max_scrolls=5,
    )

    assert result["category"] == "materials"
    assert result["supported_material_count"] == 1
    assert result["source"] == "item_template+count_digit_template"
    assert result["materials"] == [
        {"name": "测试材料", "count": 12, "material_id": "sample_material"}
    ]


def test_read_inventory_items_stops_after_three_scans_without_new_items(monkeypatch):
    pages = [_marker_page(1), _marker_page(2), _marker_page(3), _marker_page(4)]
    app = _CaptureApp(pages)
    catalog = inventory_actions.load_inventory_catalog()
    scanned_markers: list[int] = []
    sleeps: list[float] = []
    alignments = iter(((100, 1.0), (0, 1.0), (0, 1.0)))
    monkeypatch.setattr(inventory_actions, "load_inventory_catalog", lambda *_args, **_kwargs: catalog)
    monkeypatch.setattr(inventory_actions, "_estimate_scroll_delta", lambda *_args: next(alignments))
    monkeypatch.setattr(inventory_actions.time, "sleep", sleeps.append)

    def fake_scan(page, _catalog, _ocr, _vision):
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
        _PathOnlyVision(),
        max_scrolls=5,
    )

    assert scanned_markers == [1, 2, 3, 4]
    assert result["pages_scanned"] == 4
    assert result["completion_reason"] == "three_consecutive_scans_without_new_items"
    assert result["consecutive_scans_without_new_items"] == 3
    assert result["scan_complete"] is True
    assert [item["item_id"] for item in result["items"]] == [
        "cactus_energy_lollipop"
    ]
    assert app.scroll_calls + app.drag_calls == 3
    assert all(
        request["kwargs"]["hold_before_release_sec"] == 0.5
        for request in app.drag_requests
    )
    assert 1.0 not in sleeps


def test_same_item_id_at_a_new_physical_position_resets_empty_scan_counter(monkeypatch):
    pages = [_marker_page(index) for index in range(1, 7)]
    app = _CaptureApp(pages)
    catalog = inventory_actions.load_inventory_catalog()
    alignments = iter(((100, 1.0), (100, 1.0), (0, 1.0), (0, 1.0), (0, 1.0)))
    monkeypatch.setattr(inventory_actions, "load_inventory_catalog", lambda *_args, **_kwargs: catalog)
    monkeypatch.setattr(inventory_actions, "_estimate_scroll_delta", lambda *_args: next(alignments))
    monkeypatch.setattr(inventory_actions.time, "sleep", lambda _seconds: None)

    def fake_scan(page, _catalog, _ocr, _vision):
        marker = int(page[0, 0, 0])
        if marker == 1:
            return [
                {
                    "item_id": "purchase_order_book",
                    "name": "进货采购书",
                    "count": 2,
                    "card_top_left": [10, 100],
                }
            ]
        if marker == 2:
            return [
                {
                    "item_id": "purchase_order_book",
                    "name": "进货采购书",
                    "count": 2,
                    "card_top_left": [10, 0],
                }
            ]
        if marker == 3:
            return [
                {
                    "item_id": "purchase_order_book",
                    "name": "进货采购书",
                    "count": 3,
                    "card_top_left": [10, 0],
                }
            ]
        return []

    monkeypatch.setattr(inventory_actions, "scan_inventory_page", fake_scan)

    result = inventory_actions.read_inventory_items(
        app,
        object(),
        _PathOnlyVision(),
        max_scrolls=8,
    )

    assert result["pages_scanned"] == 6
    assert result["matched_stack_count"] == 2
    assert result["completion_reason"] == "three_consecutive_scans_without_new_items"
    assert result["items"] == [
        {"item_id": "purchase_order_book", "name": "进货采购书", "count": 5}
    ]
    assert app.drag_calls == 5
