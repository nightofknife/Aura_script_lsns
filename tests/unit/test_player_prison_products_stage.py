from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.resonance_pc.src.actions import player_data_pc_actions as player


def _snapshot(**updates):
    return {
        "success": True, "scope": "visible", "visible_complete": True,
        "returned_to_profile": True, "inventory_complete": False,
        "goods": [{"goods_id": "1", "goods": "test", "quantity": 20}],
        "unresolved": [], "refreshed_at": "now", **updates,
    }


def _environment(tmp_path, monkeypatch):
    events = []
    service = PersistentDataService(tmp_path / "install")

    class App:
        def click(self, **kwargs):
            assert kwargs == {"x": player._CLICK_PROFILE[0], "y": player._CLICK_PROFILE[1]}
            events.append("open_profile")

    monkeypatch.setattr(player, "_wait_for_any_marker", lambda *a, **k: events.append("main"))
    monkeypatch.setattr(player, "_wait_for_profile_panel", lambda *a, **k: events.append("profile_ready"))
    monkeypatch.setattr(player, "_close_profile_panel_to_main", lambda *a, **k: events.append("close_profile"))
    monkeypatch.setattr(player, "check_cancelled", lambda: None)
    return events, service, dict(app=App(), ocr=object(), vision=object(), interaction=object(), persistent_data=service)


def test_new_stage_is_optional_and_normalized_in_fixed_order():
    assert player._normalize_stages(None) == ("location", "profile", "inventory", "characters")
    assert player._normalize_stages(["prison_products", "profile", "prison_products"]) == ("profile", "prison_products")


@pytest.mark.parametrize("goods", [None, []])
def test_no_goods_filter_means_all_visible(goods):
    assert player._normalize_prison_product_goods(goods) is None


def test_goods_filter_resolves_names_and_deduplicates():
    products = json.loads((player._PLAN_ROOT / "data/meta/products.json").read_text(encoding="utf-8"))
    identity = next(key for key, value in products.items() if value == "涤纶")
    assert player._normalize_prison_product_goods(["涤纶", identity]) == [identity]


@pytest.mark.parametrize("goods", ["涤纶", [""], ["missing_product"], [True], ["67"]])
def test_invalid_goods_rejected_before_observation_or_input(tmp_path, monkeypatch, goods):
    events, service, kwargs = _environment(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        player.resonance_pc_player_data_refresh(stages=["prison_products"], prison_product_goods=goods, **kwargs)
    assert events == []
    assert not service.inspect(player.USER_INFO_FILE)["exists"]


@pytest.mark.parametrize("goods", [None, [], ["涤纶"]])
def test_prison_only_refresh_uses_profile_once_and_persists_visible_scope(tmp_path, monkeypatch, goods):
    events, service, kwargs = _environment(tmp_path, monkeypatch)
    old = {"inventory": {"keep": True}, "recovery": {"keep": True}}
    service.set(player.USER_INFO_FILE, [], old)
    expected = player._normalize_prison_product_goods(goods)

    def read(app, ocr, vision, interaction, selected):
        assert selected == expected
        assert interaction is kwargs["interaction"]
        events.append("prison")
        return _snapshot(requested_goods=selected)

    monkeypatch.setattr(player, "_read_prison_products", read)
    result = player.resonance_pc_player_data_refresh(stages=["prison_products"], prison_product_goods=goods, **kwargs)
    assert events == ["main", "open_profile", "profile_ready", "prison", "close_profile"]
    assert result["metadata"]["executed_stages"] == ["prison_products"]
    assert result["metadata"]["persisted"] is True
    assert "status" not in result and "recovery" not in result
    saved = service.read(player.USER_INFO_FILE)
    assert saved["prison_products"] == result["prison_products"]
    assert saved["prison_products"]["inventory_complete"] is False
    assert saved["inventory"] == old["inventory"]
    assert saved["recovery"] == old["recovery"]
    assert "prison_products" in saved["metadata"]["section_updated_at"]


def test_prison_refresh_runs_after_profile_before_close(tmp_path, monkeypatch):
    events, service, kwargs = _environment(tmp_path, monkeypatch)

    def read_profile(*args, **kwargs):
        events.append("profile_fields")
        return {"cargo": {"current": 1, "max": 10}}

    def read_prison(*args, **kwargs):
        events.append("prison")
        return _snapshot()

    monkeypatch.setattr(player, "_read_profile_stage", read_profile)
    monkeypatch.setattr(player, "_read_prison_products", read_prison)
    result = player.resonance_pc_player_data_refresh(stages=["prison_products", "profile"], profile_sections=["cargo"], **kwargs)
    assert events[-3:] == ["profile_fields", "prison", "close_profile"]
    assert result["metadata"]["executed_stages"] == ["profile", "prison_products"]


def test_unselected_stage_does_not_read_or_validate_prison_goods(tmp_path, monkeypatch):
    events, service, kwargs = _environment(tmp_path, monkeypatch)
    monkeypatch.setattr(player, "_capture_ocr_items", lambda *a, **k: [{"text": "修格里城"}])
    monkeypatch.setattr(player, "_read_prison_products", lambda *a, **k: pytest.fail("unselected prison stage ran"))
    result = player.resonance_pc_player_data_refresh(stages=["location"], prison_product_goods="invalid-but-unselected", **kwargs)
    assert events == ["main"]
    assert "prison_products" not in result


def test_partial_result_replaces_old_prison_snapshot_without_claiming_success_timestamp(tmp_path, monkeypatch):
    events, service, kwargs = _environment(tmp_path, monkeypatch)
    old = {"prison_products": _snapshot(), "metadata": {"section_updated_at": {"prison_products": "old", "location": "keep"}}}
    service.set(player.USER_INFO_FILE, [], old)
    partial = _snapshot(success=False, visible_complete=False, unresolved=[{"reason": "quantity_unknown"}])
    monkeypatch.setattr(player, "_read_prison_products", lambda *a, **k: partial)
    result = player.resonance_pc_player_data_refresh(stages=["prison_products"], **kwargs)
    saved = service.read(player.USER_INFO_FILE)
    assert result["prison_products"]["success"] is False
    assert saved["prison_products"] == partial
    assert saved["metadata"]["section_updated_at"] == {"location": "keep"}
    assert events[-1] == "close_profile"


def test_unconfirmed_return_does_not_close_or_persist(tmp_path, monkeypatch):
    events, service, kwargs = _environment(tmp_path, monkeypatch)
    monkeypatch.setattr(player, "_read_prison_products", lambda *a, **k: _snapshot(returned_to_profile=False))
    monkeypatch.setattr(player, "_best_effort_return_to_main", lambda *a, **k: events.append("observe_cleanup"))
    with pytest.raises(RuntimeError, match="return to profile"):
        player.resonance_pc_player_data_refresh(stages=["prison_products"], **kwargs)
    assert "close_profile" not in events
    assert not service.inspect(player.USER_INFO_FILE)["exists"]


def test_unselected_merge_preserves_snapshot_and_does_not_mutate_existing():
    old = {"prison_products": _snapshot(), "metadata": {"section_updated_at": {"prison_products": "old"}}}
    before = copy.deepcopy(old)
    merged = player._merge_latest(old, {"location": {"current_city": "new"}}, section_updated_at={"location": "now"}, updated_at="now")
    assert merged["prison_products"] == before["prison_products"]
    assert old == before


def test_task_schema_exposes_stage_and_optional_goods_filter():
    task = yaml.safe_load((player._PLAN_ROOT / "tasks/player_data_pc.yaml").read_text(encoding="utf-8"))["player_data_refresh"]
    inputs = {item["name"]: item for item in task["meta"]["inputs"]}
    assert "prison_products" in inputs["stages"]["item"]["enum"]
    assert "prison_products" not in inputs["stages"]["default"]
    assert inputs["prison_product_goods"]["nullable"] is True
    assert task["steps"]["refresh"]["params"]["prison_product_goods"] == "{{ inputs.prison_product_goods }}"
