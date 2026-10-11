"""City scope uses complete native product lists and never assumes their order."""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _trade_goods_investment_vision as native
from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as investment


ROOT = Path(__file__).resolve().parents[2]
FRAME = np.zeros((720, 1280, 3), np.uint8)


def test_native_catalog_covers_every_city_and_exact_known_products():
    payload = json.loads(native.CITY_CATALOG.read_text(encoding="utf-8"))
    cities = json.loads((ROOT / "plans/resonance_pc/data/meta/cities.json").read_text(encoding="utf-8"))
    products = json.loads((ROOT / "plans/resonance_pc/data/meta/products.json").read_text(encoding="utf-8"))
    assert {cid: city["name"] for cid, city in payload["cities"].items()} == cities
    for city in payload["cities"].values():
        assert city["status"] == "known" and city["products"]
        assert len({p["native_goods_id"] for p in city["products"]}) == len(city["products"])
        assert all(products[p["product_id"]] == p["name"] for p in city["products"])
    assert len(payload["cities"]["14"]["products"]) == 10  # Missing from prestige unlock metadata.
    assert len(payload["cities"]["19"]["products"]) == 10
    assert "206" in {p["product_id"] for p in payload["cities"]["19"]["products"]}


@pytest.mark.parametrize("name,expected", [
    ("远星大桥", 10), ("farstar_bridge", 10), ("14", 10),
    ("贡露城", 10), ("格罗努城", 10), (" 七号自由港 ", 10),
    ("7号自由港", 10), ("塔图站", 6), ("沃德镇", 9),
    ("", None), ("   ", None),
])
def test_city_aliases_select_only_their_product_count(name, expected):
    assert native.InvestmentVision(city_name=name).city_product_count == expected


def test_unknown_city_fails_before_capture_or_input():
    app = SimpleNamespace(capture=lambda: pytest.fail("No capture for unknown city"),
                          click=lambda **kwargs: pytest.fail("No click for unknown city"))
    with pytest.raises(native.InvestmentRecognitionError) as error:
        investment.execute_trade_goods_investment_from_shop(
            mode=10, city_name="不存在的城市", app=app, vision=object())
    assert error.value.code == "investment_city_unknown"


@pytest.mark.parametrize("invalid_header", [False, True])
def test_city_scope_bounds_expensive_frame_checks(monkeypatch, invalid_header):
    reader = native.InvestmentVision(city_name="塔图站")
    candidates = [{"x": x, "y": y, "header_score": 1.}
                  for y in (154, 285, 416) for x in (62, 189, 315)]
    headers = list(reversed(candidates))
    if invalid_header:
        headers.append({"x": 2, "y": 154, "header_score": 1.})
    monkeypatch.setattr(reader, "_headers", lambda frame: list(headers))
    monkeypatch.setattr(reader, "_top_partials", lambda *args: [])
    monkeypatch.setattr(reader, "_match_box", lambda *args: {"found": False})
    calls = []
    monkeypatch.setattr(native, "_best", lambda *args: calls.append(None) or (1., (3, 3)))
    cards = reader.detect_cards(FRAME, success_visible=False)
    assert len(cards) == 6
    assert [c["rect"][:2] for c in cards] == [(row["x"], row["y"]) for row in candidates[:6]]
    assert len(calls) == 6 * len(reader._manifest["cards"]["frame_variants"])


def test_processed_city_count_avoids_final_rescan_and_drag(monkeypatch):
    cards = [{"rect": (62 + 127 * i, 154, 114, 129), "partial": False,
              "locked": False, "selected": False} for i in range(3)]
    events = []
    reader = SimpleNamespace(
        city_product_count=3, VIEWPORT=(49, 143, 394, 446),
        read_entry_state=lambda frame: {"availability": "available", "center": [843, 490],
                                        "white_score": .95, "gray_score": .7},
    )

    def make_reader(*, city_name):
        assert city_name == "贡露城"
        return reader

    monkeypatch.setattr(investment, "InvestmentVision", make_reader)
    monkeypatch.setattr(investment, "_wait", lambda app, predicate: (FRAME, predicate(FRAME)))
    monkeypatch.setattr(investment, "_click", lambda *args: None)
    monkeypatch.setattr(investment, "_pause", lambda *args: None)
    monkeypatch.setattr(investment, "_cards", lambda *args: events.append("scan") or (FRAME, cards))
    monkeypatch.setattr(investment, "_select", lambda app, r, card: events.append(card["rect"]) or
                        (FRAME, card, {"current": 10, "preview": 11}))
    monkeypatch.setattr(investment, "_scroll", lambda *args: pytest.fail("No end-of-list drag"))
    monkeypatch.setattr(investment, "_return_shop", lambda *args: events.append("shop"))
    result = investment.execute_trade_goods_investment_from_shop(
        mode=10, city_name="贡露城", app=object(), vision=object())
    assert result["product_count"] == 3 and result["scroll_count"] == 0
    assert events == ["scan", cards[0]["rect"], "scan", cards[1]["rect"],
                      "scan", cards[2]["rect"], "shop"]


def test_city_scoped_investment_preserves_closed_entry_skip_without_input(monkeypatch):
    entry = {"availability": "unavailable", "center": None,
             "white_score": .7, "gray_score": .95}
    reader = SimpleNamespace(city_product_count=10, read_entry_state=lambda frame: entry)
    monkeypatch.setattr(investment, "InvestmentVision", lambda **kwargs: reader)
    monkeypatch.setattr(investment, "_wait", lambda app, predicate: (FRAME, predicate(FRAME)))
    monkeypatch.setattr(investment, "_click", lambda *args: pytest.fail("Closed entry must not click"))
    monkeypatch.setattr(investment, "_cards", lambda *args: pytest.fail("Closed entry must not scan"))
    monkeypatch.setattr(investment, "_return_shop", lambda *args: pytest.fail("Already on shop page"))
    result = investment.execute_trade_goods_investment_from_shop(
        mode=10, city_name="farstar_bridge", app=object(), vision=object())
    assert result["success"] and not result["triggered"]
    assert result["status"] == "skipped" and result["reason"] == "investment_not_available"
    assert result["page_state"] == "shop_page" and result["entry_state"] == entry
