"""Second-batch catalogs and functional banks remain usable after release staging."""
import json
from pathlib import Path

from scripts.release.assemble_release_plans import (
    collect_plan_files,
    stage_plan_tree,
    validate_selected_files,
)


ROOT = Path(__file__).resolve().parents[2]
PLAN = Path("plans/resonance_pc")
BANKS = ("navigation", "passenger", "commerce/trade", "commerce/books",
         "commerce/investment", "recovery")
CATALOGS = (
    "city_identity_templates.json", "depart_button_template.json",
    "trade_product_templates.json", "player_recovery.json", "love_bento.json",
    "bento_consumption.json", "sparkling_water.json",
)
RETIRED_BANKS = (
    "city_identity", "warehouse_navigation", "trailer_service", "trade_products",
    "trade_goods_investment", "player_recovery", "bento_consumption", "sparkling_water",
)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for entry in value.values():
            yield from strings(entry)
    elif isinstance(value, list):
        for entry in value:
            yield from strings(entry)


def test_functional_banks_and_catalogs_survive_release_assembly(tmp_path):
    selected = {destination: source for source, destination in collect_plan_files(ROOT / "plans")}
    prefixes = [PLAN / "templates" / bank for bank in BANKS]
    required = {destination for destination in selected
                if any(destination.is_relative_to(prefix) for prefix in prefixes)}
    required.update(PLAN / "data/meta" / name for name in CATALOGS)
    assert required <= selected.keys()
    for prefix in prefixes:
        assert any(path.is_relative_to(prefix) and path.suffix == ".png" for path in required), prefix
    for bank in RETIRED_BANKS:
        old = PLAN / "templates" / bank
        assert not (ROOT / old).exists()
        assert not any(path.is_relative_to(old) for path in selected)
    files = [(selected[destination], destination) for destination in sorted(required)]
    validate_selected_files(files)
    target = tmp_path / "release/plans"
    assert not target.exists()
    stage_plan_tree(files, target)
    package = target / "resonance_pc"
    for destination in required:
        assert (target / destination.relative_to("plans")).read_bytes() == selected[destination].read_bytes()
    for name in CATALOGS:
        catalog = json.loads((package / "data/meta" / name).read_text(encoding="utf-8"))
        for reference in strings(catalog):
            if reference.startswith("templates/"):
                resolved = (package / reference).resolve()
                assert resolved.is_relative_to(package.resolve())
                assert resolved.exists(), (name, reference)
