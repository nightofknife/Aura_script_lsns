from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import cv2
import pytest

from plans.resonance_pc.src.actions import cape_island_investment_pc_actions as actions
from plans.resonance_pc.src.services.city_shop_data_pc_service import ResonancePcCityShopDataService
from plans.aura_base.src.services.vision_service import MatchResult


REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_ROOT = REPO_ROOT / "plans" / "resonance_pc"


class _App:
    def __init__(self):
        self.clicks = []

    def click(self, x=None, y=None, **_kwargs):
        self.clicks.append((int(x), int(y)))


def _match(found=True, confidence=0.99, center=None):
    return MatchResult(
        found=bool(found),
        confidence=float(confidence),
        top_left=(1, 2) if found else None,
        center_point=tuple(center or (66, 67)) if found else None,
        rect=(1, 2, 130, 130) if found else None,
    )


def test_cape_city_mirage_island_coordinate_is_resolved_from_location_data():
    service = ResonancePcCityShopDataService(plan_root=PLAN_ROOT)

    point = service.resolve_shop_point(city_name="海角城", shop_name="蜃息岛")

    assert point["city_key"] == "cape_city"
    assert point["shop_key"] == "mirage_island"
    assert (point["x"], point["y"]) == (112, 425)


def test_page_templates_and_available_card_option_samples_exist():
    page_templates = [
        actions._ISLAND_HOME_TEMPLATE,
        actions._REVENUE_OVERVIEW_TEMPLATE,
        actions._INVESTMENT_TAB_TEMPLATE,
        actions._INVESTMENT_PAGE_TEMPLATE,
        actions._INVEST_BUTTON_TEMPLATE,
        actions._INVESTMENT_SUCCESS_TEMPLATE,
    ]

    assert all((PLAN_ROOT / template).is_file() for template in page_templates)
    assert (PLAN_ROOT / actions._CARD_TEMPLATE_MASK).is_file()
    assert len(actions.CARD_OPTION_TEMPLATES["share"]["bronze"]) == 1
    assert len(actions.CARD_OPTION_TEMPLATES["share"]["silver"]) == 1
    assert len(actions.CARD_OPTION_TEMPLATES["share"]["gold"]) == 1
    assert len(actions.CARD_OPTION_TEMPLATES["ticket"]["bronze"]) == 1
    assert len(actions.CARD_OPTION_TEMPLATES["ticket"]["silver"]) == 1
    assert len(actions.CARD_OPTION_TEMPLATES["tax"]["bronze"]) == 1
    assert len(actions.CARD_OPTION_TEMPLATES["tax"]["silver"]) == 1
    assert actions.CARD_OPTION_TEMPLATES["ticket"]["gold"] == ()
    assert actions.CARD_OPTION_TEMPLATES["tax"]["gold"] == ()
    assert len(actions.CARD_OPTION_TEMPLATES["all"]["bronze"]) == 1
    assert actions.CARD_OPTION_TEMPLATES["all"]["silver"] == ()
    assert len(actions.CARD_OPTION_TEMPLATES["all"]["gold"]) == 1
    assert all(
        (PLAN_ROOT / template).is_file()
        for grades in actions.CARD_OPTION_TEMPLATES.values()
        for templates in grades.values()
        for template in templates
    )


@pytest.mark.parametrize(
    ("target_name", "same_artwork_name", "minimum_margin"),
    [
        ("cape_island_card_ticket_silver.png", "cape_island_card_ticket_bronze.png", 0.02),
        ("cape_island_card_share_gold.png", "cape_island_card_share_bronze.png", 0.005),
        ("cape_island_card_share_silver.png", "cape_island_card_share_bronze.png", 0.02),
    ],
)
def test_masked_color_sqdiff_separates_same_artwork_grades_in_one_match(
    target_name,
    same_artwork_name,
    minimum_margin,
):
    target = cv2.imread(str(PLAN_ROOT / "templates" / target_name), cv2.IMREAD_COLOR)
    same_artwork = cv2.imread(
        str(PLAN_ROOT / "templates" / same_artwork_name),
        cv2.IMREAD_COLOR,
    )
    assert target is not None
    assert same_artwork is not None
    mask = cv2.imread(
        str(PLAN_ROOT / actions._CARD_TEMPLATE_MASK),
        cv2.IMREAD_GRAYSCALE,
    )
    assert mask is not None

    correct_error = cv2.matchTemplate(
        target,
        target,
        actions._CARD_MATCH_METHOD,
        mask=mask,
    )[0, 0]
    wrong_error = cv2.matchTemplate(
        target,
        same_artwork,
        actions._CARD_MATCH_METHOD,
        mask=mask,
    )[0, 0]
    correct_confidence = 1.0 - float(correct_error)
    wrong_confidence = 1.0 - float(wrong_error)

    assert actions._CARD_MATCH_METHOD == cv2.TM_SQDIFF_NORMED
    assert mask.shape == (130, 130)
    assert correct_confidence == pytest.approx(1.0)
    assert correct_confidence - wrong_confidence > minimum_margin


def test_enter_island_retries_the_same_resolved_coordinate(monkeypatch):
    app = _App()
    service = ResonancePcCityShopDataService(plan_root=PLAN_ROOT)
    matches = iter([_match(False), _match(True)])
    sleeps = []

    async def wait_for_image(**_kwargs):
        return next(matches)

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(actions, "wait_for_image", wait_for_image)
    monkeypatch.setattr(actions.asyncio, "sleep", sleep)

    result = asyncio.run(
        actions._enter_island(
            app,
            object(),
            object(),
            service,
            location_file_path="data/meta/location_pc.json",
            page_timeout_sec=0,
            interval_sec=0.05,
            transition_attempts=3,
        )
    )

    assert result["attempts"] == 2
    assert result["settle_sec"] == 2.0
    assert app.clicks == [(112, 425), (112, 425)]
    assert sleeps == [2.0]


def test_open_revenue_overview_retries_only_the_fixed_safe_point(monkeypatch):
    app = _App()
    matches = iter([_match(False), _match(False), _match(True)])
    clock = [100.0]
    wait_timeouts = []

    async def wait_for_image(**_kwargs):
        wait_timeouts.append(_kwargs["timeout"])
        match = next(matches)
        if not match.found:
            clock[0] += _kwargs["timeout"]
        return match

    monkeypatch.setattr(actions, "wait_for_image", wait_for_image)
    monkeypatch.setattr(actions.time, "monotonic", lambda: clock[0])

    result = asyncio.run(
        actions._open_revenue_overview(
            app,
            object(),
            object(),
            page_timeout_sec=12.0,
            interval_sec=0.05,
            transition_attempts=3,
        )
    )

    assert result["attempts"] == 3
    assert app.clicks == [actions._OPEN_REVENUE_SAFE_POINT] * 3
    assert wait_timeouts == pytest.approx([1.5, 1.5, 9.0])


def test_open_revenue_overview_stops_clicking_when_total_timeout_is_exhausted(monkeypatch):
    app = _App()
    clock = [100.0]
    wait_timeouts = []

    async def wait_for_image(**kwargs):
        wait_timeouts.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        return _match(False)

    monkeypatch.setattr(actions, "wait_for_image", wait_for_image)
    monkeypatch.setattr(actions.time, "monotonic", lambda: clock[0])

    with pytest.raises(actions.CapeIslandInvestmentError) as exc_info:
        asyncio.run(
            actions._open_revenue_overview(
                app,
                object(),
                object(),
                page_timeout_sec=2.0,
                interval_sec=0.05,
                transition_attempts=3,
            )
        )

    assert exc_info.value.code == "revenue_overview_timeout"
    assert exc_info.value.detail["attempts"] == 2
    assert app.clicks == [actions._OPEN_REVENUE_SAFE_POINT] * 2
    assert wait_timeouts == pytest.approx([1.5, 0.5])


@pytest.mark.parametrize(
    ("kind", "texts", "expected"),
    [
        ("share", ["32.0%"], 32.0),
        ("share", ["分成 31.6％"], 31.6),
        ("tax", ["−5.0%"], -5.0),
        ("tax", ["0%"], 0.0),
        ("tax", ["5%"], None),
        ("ticket", ["1,500"], 1500),
        ("ticket", ["票价 1480"], 1480),
    ],
)
def test_parse_investment_metric(kind, texts, expected):
    assert actions._parse_metric_value(kind, texts) == expected


def test_metric_caps_use_the_game_limits():
    capped = actions._metric_caps(
        {
            "share_percent": 32.0,
            "ticket_price": 1500,
            "tax_reduction_percent": -5.0,
        }
    )

    assert capped == {"share": True, "ticket": True, "tax": True}


def test_all_boost_requires_every_metric_to_be_below_cap():
    options = [
        {"slot": 1, "category": "share", "grade": "gold"},
        {"slot": 2, "category": "all", "grade": "gold"},
        {"slot": 3, "category": "ticket", "grade": "gold"},
    ]

    assert actions._select_investment_option(
        options,
        {"share": False, "ticket": False, "tax": False},
    )["slot"] == 2
    assert actions._select_investment_option(
        options,
        {"share": False, "ticket": False, "tax": True},
    )["slot"] == 1


def test_all_boost_uses_an_ordinary_grade_instead_of_a_special_top_grade():
    selected = actions._select_investment_option(
        [
            {"slot": 1, "category": "all", "grade": "bronze"},
            {"slot": 2, "category": "share", "grade": "silver"},
            {"slot": 3, "category": "ticket", "grade": "bronze"},
        ],
        {"share": False, "ticket": False, "tax": False},
    )

    assert selected["slot"] == 2


def test_same_grade_prefers_share_then_ticket_then_tax_and_excludes_capped():
    options = [
        {"slot": 1, "category": "tax", "grade": "gold"},
        {"slot": 2, "category": "ticket", "grade": "gold"},
        {"slot": 3, "category": "share", "grade": "gold"},
    ]

    assert actions._select_investment_option(
        options,
        {"share": False, "ticket": False, "tax": False},
    )["slot"] == 3
    assert actions._select_investment_option(
        options,
        {"share": True, "ticket": False, "tax": False},
    )["slot"] == 2


def _patch_navigation(monkeypatch, metrics):
    async def enter(*_args, **_kwargs):
        return {"attempts": 1}

    async def open_revenue(*_args, **_kwargs):
        return {"attempts": 1}

    async def click_required(*_args, **_kwargs):
        return {"clicked": True, "x": 1, "y": 2}

    async def wait_image(**_kwargs):
        return _match(True)

    async def wait_disappear(**_kwargs):
        return True

    monkeypatch.setattr(actions, "_enter_island", enter)
    monkeypatch.setattr(actions, "_open_revenue_overview", open_revenue)
    monkeypatch.setattr(actions, "_click_template_required", click_required)
    monkeypatch.setattr(actions, "wait_for_image", wait_image)
    monkeypatch.setattr(actions, "wait_for_templates_in_set_to_disappear", wait_disappear)
    monkeypatch.setattr(
        actions,
        "_read_investment_metrics",
        lambda *_args, **_kwargs: {"values": dict(metrics), "ocr_history": {}},
    )


def _run_investment(app=None):
    return asyncio.run(
        actions.resonance_pc_execute_cape_island_investment_from_city_panel(
            app=app or _App(),
            ocr=object(),
            vision=object(),
            resonance_pc_city_shop_data=object(),
            engine=object(),
        )
    )


def test_all_metrics_capped_skips_without_card_templates(monkeypatch):
    metrics = {
        "share_percent": 32.0,
        "ticket_price": 1500,
        "tax_reduction_percent": -5.0,
    }
    _patch_navigation(monkeypatch, metrics)

    result = _run_investment()

    assert result["success"] is True
    assert result["status"] == "skipped"
    assert result["reason"] == "all_metrics_capped"
    assert result["selected_option"] is None
    assert result["page_state"] == "island_investment"


def test_investment_without_card_templates_skips_and_keeps_the_flow_available(monkeypatch):
    metrics = {
        "share_percent": 31.6,
        "ticket_price": 1500,
        "tax_reduction_percent": -5.0,
    }
    _patch_navigation(monkeypatch, metrics)
    async def unclassified(*_args, slot, **_kwargs):
        return {"slot": slot, "category": None, "grade": None, "confidence": 0.0}

    monkeypatch.setattr(actions, "_recognize_card_option", unclassified)
    fake_logger = Mock()
    monkeypatch.setattr(actions, "logger", fake_logger)

    result = _run_investment()

    assert result["status"] == "skipped"
    assert result["reason"] == "no_recognized_eligible_option"
    assert result["degraded"] is True
    assert result["unclassified_slots"] == [1, 2, 3]
    warning_formats = [call.args[0] for call in fake_logger.warning.call_args_list]
    assert any("degraded card selection" in message for message in warning_formats)
    assert any("no_recognized_eligible_option" in message for message in warning_formats)


def test_partial_card_classification_uses_the_best_recognized_eligible_option(monkeypatch):
    metrics = {
        "share_percent": 31.6,
        "ticket_price": 1480,
        "tax_reduction_percent": -5.0,
    }
    _patch_navigation(monkeypatch, metrics)
    options = iter(
        [
            {"slot": 1, "category": None, "grade": None, "confidence": 0.0},
            {"slot": 2, "category": "ticket", "grade": "bronze", "confidence": 0.93},
            {"slot": 3, "category": "tax", "grade": "silver", "confidence": 0.95},
        ]
    )
    async def recognize(*_args, **_kwargs):
        return next(options)

    monkeypatch.setattr(actions, "_recognize_card_option", recognize)

    result = _run_investment()

    assert result["status"] == "invested"
    assert result["selected_option"]["slot"] == 2
    assert result["degraded"] is True
    assert result["unclassified_slots"] == [1]


def test_successful_investment_selects_and_dismisses_result(monkeypatch):
    metrics = {
        "share_percent": 31.6,
        "ticket_price": 1480,
        "tax_reduction_percent": -4.8,
    }
    _patch_navigation(monkeypatch, metrics)
    options = iter(
        [
            {"slot": 1, "category": "tax", "grade": "silver", "confidence": 0.9},
            {"slot": 2, "category": "ticket", "grade": "gold", "confidence": 0.9},
            {"slot": 3, "category": "share", "grade": "gold", "confidence": 0.9},
        ]
    )
    async def recognize(*_args, **_kwargs):
        return next(options)

    monkeypatch.setattr(actions, "_recognize_card_option", recognize)
    fake_logger = Mock()
    monkeypatch.setattr(actions, "logger", fake_logger)
    app = _App()

    result = _run_investment(app)

    assert result["status"] == "invested"
    assert result["selected_option"]["slot"] == 3
    assert app.clicks == [actions._SUCCESS_DISMISS_POINT]
    info_formats = [call.args[0] for call in fake_logger.info.call_args_list]
    assert any("investment metrics" in message for message in info_formats)
    assert sum("investment card slot=" in message for message in info_formats) == 3
    assert any("investment selected" in message for message in info_formats)
    assert any("result status=invested" in message for message in info_formats)
