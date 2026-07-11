from pathlib import Path

import inspect
import pytest
import yaml

from plans.resonance.src.actions import city_trade_flow_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_buy_task():
    task_path = REPO_ROOT / "plans" / "resonance" / "tasks" / "buy_goods.yaml"
    return yaml.safe_load(task_path.read_text(encoding="utf-8"))["buy_goods"]


def test_buy_goods_task_is_thin_action_wrapper():
    task = _load_buy_task()
    steps = task["steps"]
    input_names = {item["name"] for item in task["meta"]["inputs"]}

    assert input_names == {"product_list", "books_used"}
    assert list(steps) == ["run"]
    assert steps["run"]["action"] == "resonance.buy_goods_on_buy_page"
    assert steps["run"]["params"]["product_list"] == "{{ inputs.product_list }}"
    assert steps["run"]["params"]["books_used"] == "{{ inputs.books_used | default(0) }}"
    assert task["returns"]["page_state"] == "{{ nodes.run.output.page_state }}"
    assert task["returns"]["selected_products"] == "{{ nodes.run.output.selected_products }}"
    assert task["returns"]["missing_products"] == "{{ nodes.run.output.missing_products }}"


def test_buy_goods_action_keeps_settlement_template_fixed_exit_and_book_usage():
    assert (REPO_ROOT / "plans" / "resonance" / "templates" / "buy_settlement_scale_badge.png").is_file()
    assert actions._BUY_SETTLEMENT["template"] == "templates/buy_settlement_scale_badge.png"
    assert actions._BUY_SETTLEMENT["region"] == [520, 240, 320, 320]
    assert actions._SETTLEMENT_EXIT_POINT == (640, 620)
    assert not hasattr(actions, "_BUY_BUTTON_POINT")

    source = inspect.getsource(actions.resonance_buy_goods_on_buy_page)
    assert "resonance_use_purchase_books" in source
    assert "进货采买书" in source
    assert "_close_settlement(app, vision, \"buy\"" in source
    assert "resonance_tap_back_once" in source
    assert "buy_success_returns_to_shop_page" in source
    assert "buy_button_not_found" in source
    assert "fixed-coordinate fallback is disabled" in source


def test_city_trade_flow_forwards_books_used_to_buy_action():
    source = inspect.getsource(actions._execute_city_trade_inside_current_city)

    assert "resonance_click_shop_menu_node(node_index=1" in source
    assert "resonance_buy_goods_on_buy_page" in source
    assert "books_used=int(books_used or 0)" in source


class _FakeApp:
    def __init__(self):
        self.clicks = []

    def click(self, **kwargs):
        self.clicks.append(kwargs)


def _patch_buy_flow_basics(monkeypatch, *, buy_button_found=True):
    monkeypatch.setattr(actions.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(actions, "_capture_text_items", lambda *_args, **_kwargs: [])

    def fake_wait_for_text_hit(_app, _ocr, texts, region, **_kwargs):
        if buy_button_found and "买入" in texts and region == actions._BUY_BUTTON_REGION:
            return {"center": [1010, 650], "text": "买入", "norm_text": "买入", "confidence": 0.99}
        return None

    monkeypatch.setattr(actions, "_wait_for_text_hit", fake_wait_for_text_hit)


def test_buy_goods_skips_back_after_successful_purchase(monkeypatch):
    _patch_buy_flow_basics(monkeypatch)
    monkeypatch.setattr(actions, "_close_settlement", lambda *_args, **_kwargs: {"closed": True})

    def fail_back(**_kwargs):
        raise AssertionError("successful purchase should already be back on shop page")

    monkeypatch.setattr(actions, "resonance_tap_back_once", fail_back)

    result = actions.resonance_buy_goods_on_buy_page(
        product_list=["测试商品"],
        max_scan_rounds=1,
        app=_FakeApp(),
        ocr=object(),
        vision=object(),
        controller=object(),
    )

    assert result["page_state"] == "shop_page"
    assert result["back"]["skipped"] is True


def test_buy_goods_uses_back_after_failed_purchase(monkeypatch):
    _patch_buy_flow_basics(monkeypatch)
    monkeypatch.setattr(actions, "_close_settlement", lambda *_args, **_kwargs: {"closed": False})
    back_calls = []
    monkeypatch.setattr(
        actions,
        "resonance_tap_back_once",
        lambda **kwargs: back_calls.append(kwargs) or {"clicked": True, "page_state": "shop_page"},
    )

    result = actions.resonance_buy_goods_on_buy_page(
        product_list=["测试商品"],
        max_scan_rounds=1,
        app=_FakeApp(),
        ocr=object(),
        vision=object(),
        controller=object(),
    )

    assert result["page_state"] == "shop_page"
    assert result["back"]["clicked"] is True
    assert len(back_calls) == 1


def test_buy_goods_does_not_click_fixed_point_when_buy_button_missing(monkeypatch):
    _patch_buy_flow_basics(monkeypatch, buy_button_found=False)
    monkeypatch.setattr(actions, "_close_settlement", lambda *_args, **_kwargs: {"closed": False})
    app = _FakeApp()

    with pytest.raises(actions.CityTradeFlowError) as exc_info:
        actions.resonance_buy_goods_on_buy_page(
            product_list=[],
            max_scan_rounds=1,
            app=app,
            ocr=object(),
            vision=object(),
            controller=object(),
        )

    assert exc_info.value.code == "buy_button_not_found"
    assert app.clicks == []
