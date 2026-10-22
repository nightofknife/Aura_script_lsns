"""Remaining functional banks survive release collection and runtime selection."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from plans.resonance_pc.src.actions import cape_island_investment_pc_actions as cape
from plans.resonance_pc.src.actions import _rubbish_recycling_vision as recycling
from scripts.release.assemble_release_plans import collect_plan_files, stage_plan_tree, validate_selected_files


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
RETIRED = ("black_moon_local_shop", "black_moon_local_select", "eternal_scuffle", "reforming_center", "product_workshop_staff")
BANKS = ("battle", "navigation/rooms", "workshop/staffing", "workshop/production/controls", "workshop/collection", "commerce/shop/black_moon/native", "commerce/shop/black_moon/calibrated", "commerce/investment/cape_island", "recycling")


def test_no_task_banks_or_loose_root_png_remain():
    assert not list((PLAN / "templates").glob("*.png"))
    for name in RETIRED:
        assert not (PLAN / "templates" / name).exists(), name


def test_closeout_assets_and_contract_survive_real_release_staging(tmp_path):
    selected = {destination: source for source, destination in collect_plan_files(ROOT / "plans")}
    assets = {p for bank in BANKS for p in (PLAN / "templates" / bank).rglob("*") if p.is_file()}
    assert assets
    required = {path.relative_to(ROOT) for path in assets}
    assert required <= selected.keys(), sorted(required - selected.keys())
    files = [(selected[path], path) for path in sorted(required)]
    validate_selected_files(files)
    target = tmp_path / "release/plans"
    stage_plan_tree(files, target)
    for original in assets:
        staged = target / original.relative_to(ROOT / "plans")
        assert staged.read_bytes() == original.read_bytes()
        if staged.suffix == ".png":
            with Image.open(staged) as image:
                image.load()
    contract = json.loads((ROOT / "packaging/release-contract.json").read_text(encoding="utf8"))
    serialized = json.dumps(contract)
    assert "plans/resonance_pc/" + cape._INCOME_ENTRY_TEMPLATE in serialized
    assert "plans/resonance_pc/" + cape._INCOME_ENTRY_MASK in serialized
    for reference in (recycling._ENTRY_TEMPLATE, recycling._RECYCLE_ALL_TEMPLATE, recycling._NO_RUBBISH_TEMPLATE, recycling.GLOBAL_BACK.path):
        assert (PLAN / reference).is_file()


@pytest.mark.parametrize("category,grade,template", [(category, grade, template) for category, grades in cape.CARD_OPTION_TEMPLATES.items() for grade, templates in grades.items() for template in templates])
def test_cape_glob_and_runtime_metadata_preserve_card_identity(monkeypatch, category, grade, template):
    assert set(PLAN.glob(cape._CARD_TEMPLATE_SET)) == {PLAN / entry for grades in cape.CARD_OPTION_TEMPLATES.values() for templates in grades.values() for entry in templates}
    captured = {}
    def find(**kwargs):
        captured.update(kwargs)
        return {"template": template, "match": SimpleNamespace(found=True, confidence=0.95)}
    monkeypatch.setattr(cape, "find_best_template_in_set", find)
    monkeypatch.setattr(cape, "_match_payload", lambda *args, **kwargs: kwargs)
    result = asyncio.run(cape._recognize_card_option(None, None, None, 1, cape._CARD_ICON_REGIONS[0]))
    assert (result["category"], result["grade"]) == (category, grade)
    assert captured["templates_ref"] == cape._CARD_TEMPLATE_SET
    assert captured["mask"] == cape._CARD_TEMPLATE_MASK
    assert captured["threshold"] == 0.84
    assert captured["region"] == cape._CARD_ICON_REGIONS[0]
