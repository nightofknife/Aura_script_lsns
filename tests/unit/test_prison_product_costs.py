"""Cost recursion, native identity mapping and price-basis regression."""
from fractions import Fraction
import json
from pathlib import Path

import pytest

from tools.build_prison_product_costs import estimate_costs

ROOT = Path(__file__).resolve().parents[2]


def test_chained_material_cost_is_not_double_counted_and_uses_normal_external_price():
    recipes = [
        {"product": 1, "id": 10, "name": "原料", "perNum": 2, "perTime": 60,
         "costList": [{"id": 11400001, "num": 101}]},
        {"product": 2, "id": 20, "name": "成品", "perNum": 4, "perTime": 60,
         "costList": [{"id": 1, "num": 3}, {"id": 3, "num": 2}]},
    ]
    products, external = estimate_costs(recipes, {1: {"name": "材料"}, 2: {"name": "货物"},
                                               3: {"name": "外购", "price": 2700}}, {"11": "材料", "22": "货物"})
    assert Fraction(products["11"]["unit_direct_cost_exact"]) == Fraction(101, 2)
    assert products["11"]["unit_direct_cost"] == 51
    assert Fraction(products["22"]["unit_direct_cost_exact"]) == Fraction(11103, 8)
    assert products["22"]["unit_direct_cost"] == 1388
    assert external["3"]["unit_price"] == 2700


@pytest.mark.parametrize("price", [None, 0, -1, True])
def test_missing_external_cost_never_becomes_zero(price):
    recipe = {"product": 1, "id": 10, "name": "配方", "perNum": 2, "perTime": 60,
              "costList": [{"id": 3, "num": 1}]}
    with pytest.raises(ValueError, match="Missing normal"):
        estimate_costs([recipe], {1: {"name": "成品"}, 3: {"name": "外购", "price": price}}, {"11": "成品"})


def test_catalog_covers_native_recipes_with_explicit_unestimated_components():
    catalog = json.loads((ROOT / "plans/resonance_pc/data/meta/prison_product_costs_pc.json").read_text(encoding="utf-8"))
    products = catalog["products"]
    assert len(products) == 57
    assert products["113"]["name"] == "涤纶"
    assert Fraction(products["113"]["unit_direct_cost_exact"]) * 20 == 1010
    assert products["113"]["unit_direct_cost"] * 20 == 1020
    assert catalog["external_ingredients"]["82900015"]["unit_price"] == 2700
    assert catalog["external_ingredients"]["82900181"]["unit_price"] == 2520
    assert catalog["assumptions"]["operating_overhead"] is None
    assert catalog["assumptions"]["game_inventory_tax_basis"] is None
    for row in products.values():
        assert type(row["unit_direct_cost"]) is int
        cost = sum(Fraction(item["unit_direct_cost_exact"]) * item["quantity"] for item in row["inputs"])
        assert cost / row["base_output"] == Fraction(row["unit_direct_cost_exact"])
