from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from plans.resonance_pc.src.actions import cape_island_investment_pc_actions as actions
from plans.resonance_pc.src.services.city_shop_data_pc_service import ResonancePcCityShopDataService


REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_ROOT = REPO_ROOT / "plans" / "resonance_pc"


class _App:
    def __init__(self):
        self.clicks = []

    def click(self, x=None, y=None, **_kwargs):
        self.clicks.append((int(x), int(y)))


def _match(found=True, confidence=0.99, center=None):
    result = {"found": found, "confidence": confidence}
    if center is not None:
        result["center"] = list(center)
    return result


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
    assert len(actions.CARD_OPTION_TEMPLATES["share"]["bronze"]) == 1
    assert len(actions.CARD_OPTION_TEMPLATES["ticket"]["bronze"]) == 1
    assert len(actions.CARD_OPTION_TEMPLATES["tax"]["bronze"]) == 1
    assert len(actions.CARD_OPTION_TEMPLATES["tax"]["silver"]) == 1
    assert actions.CARD_OPTION_TEMPLATES["share"]["silver"] == ()
    assert actions.CARD_OPTION_TEMPLATES["share"]["gold"] == ()
    assert actions.CARD_OPTION_TEMPLATES["ticket"]["silver"] == ()
    assert actions.CARD_OPTION_TEMPLATES["ticket"]["gold"] == ()
    assert actions.CARD_OPTION_TEMPLATES["tax"]["gold"] == ()
    assert actions.CARD_OPTION_TEMPLATES["all"]["rainbow"] == ()
    assert all(
        (PLAN_ROOT / template).is_file()
        for grades in actions.CARD_OPTION_TEMPLATES.values()
        for templates in grades.values()
        for template in templates
    )


def test_enter_island_retries_the_same_resolved_coordinate(monkeypatch):
    app = _App()
    service = ResonancePcCityShopDataService(plan_root=PLAN_ROOT)
    matches = iter([_match(False), _match(True)])
    monkeypatch.setattr(actions, "_wait_template", lambda *_args, **_kwargs: next(matches))

    result = actions._enter_island(
        app,
        object(),
        service,
        location_file_path="data/meta/location_pc.json",
        page_timeout_sec=0,
        interval_sec=0.05,
        transition_attempts=3,
    )

    assert result["attempts"] == 2
    assert app.clicks == [(112, 425), (112, 425)]


def test_open_revenue_overview_retries_only_the_fixed_safe_point(monkeypatch):
    app = _App()
    matches = iter([_match(False), _match(False), _match(True)])
    monkeypatch.setattr(actions, "_wait_template", lambda *_args, **_kwargs: next(matches))

    result = actions._open_revenue_overview(
        app,
        object(),
        page_timeout_sec=0,
        interval_sec=0.05,
        transition_attempts=3,
    )

    assert result["attempts"] == 3
    assert app.clicks == [actions._OPEN_REVENUE_SAFE_POINT] * 3


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


def test_rainbow_wins_when_any_metric_is_not_capped():
    selected = actions._select_investment_option(
        [
            {"slot": 1, "category": "share", "grade": "gold"},
            {"slot": 2, "category": "all", "grade": "rainbow"},
            {"slot": 3, "category": "ticket", "grade": "gold"},
        ],
        {"share": True, "ticket": False, "tax": True},
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
    monkeypatch.setattr(actions, "_enter_island", lambda *_args, **_kwargs: {"attempts": 1})
    monkeypatch.setattr(actions, "_open_revenue_overview", lambda *_args, **_kwargs: {"attempts": 1})
    monkeypatch.setattr(
        actions,
        "_click_template_required",
        lambda *_args, **_kwargs: {"clicked": True, "x": 1, "y": 2},
    )
    monkeypatch.setattr(
        actions,
        "_wait_template",
        lambda *_args, **kwargs: _match(bool(kwargs.get("should_exist", True))),
    )
    monkeypatch.setattr(
        actions,
        "_read_investment_metrics",
        lambda *_args, **_kwargs: {"values": dict(metrics), "ocr_history": {}},
    )


def test_all_metrics_capped_skips_without_card_templates(monkeypatch):
    metrics = {
        "share_percent": 32.0,
        "ticket_price": 1500,
        "tax_reduction_percent": -5.0,
    }
    _patch_navigation(monkeypatch, metrics)

    result = actions.resonance_pc_execute_cape_island_investment_from_city_panel(
        app=_App(),
        ocr=object(),
        vision=object(),
        resonance_pc_city_shop_data=object(),
    )

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
    monkeypatch.setattr(
        actions,
        "CARD_OPTION_TEMPLATES",
        {
            "share": {"bronze": (), "silver": (), "gold": ()},
            "ticket": {"bronze": (), "silver": (), "gold": ()},
            "tax": {"bronze": (), "silver": (), "gold": ()},
            "all": {"rainbow": ()},
        },
    )
    fake_logger = Mock()
    monkeypatch.setattr(actions, "logger", fake_logger)

    result = actions.resonance_pc_execute_cape_island_investment_from_city_panel(
        app=_App(),
        ocr=object(),
        vision=object(),
        resonance_pc_city_shop_data=object(),
    )

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
    monkeypatch.setattr(actions, "_classify_card", lambda *_args, **_kwargs: next(options))

    result = actions.resonance_pc_execute_cape_island_investment_from_city_panel(
        app=_App(),
        ocr=object(),
        vision=object(),
        resonance_pc_city_shop_data=object(),
    )

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
    monkeypatch.setattr(actions, "_configured_card_template_count", lambda: 4)
    options = iter(
        [
            {"slot": 1, "category": "tax", "grade": "silver", "confidence": 0.9},
            {"slot": 2, "category": "ticket", "grade": "gold", "confidence": 0.9},
            {"slot": 3, "category": "share", "grade": "gold", "confidence": 0.9},
        ]
    )
    monkeypatch.setattr(actions, "_classify_card", lambda *_args, **_kwargs: next(options))
    fake_logger = Mock()
    monkeypatch.setattr(actions, "logger", fake_logger)
    app = _App()

    result = actions.resonance_pc_execute_cape_island_investment_from_city_panel(
        app=app,
        ocr=object(),
        vision=object(),
        resonance_pc_city_shop_data=object(),
    )

    assert result["status"] == "invested"
    assert result["selected_option"]["slot"] == 3
    assert app.clicks == [actions._SUCCESS_DISMISS_POINT]
    info_formats = [call.args[0] for call in fake_logger.info.call_args_list]
    assert any("investment metrics" in message for message in info_formats)
    assert sum("investment card slot=" in message for message in info_formats) == 3
    assert any("investment selected" in message for message in info_formats)
    assert any("result status=invested" in message for message in info_formats)
