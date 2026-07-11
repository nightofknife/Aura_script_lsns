from pathlib import Path

import inspect
import yaml

from plans.resonance.src.actions import city_trade_flow_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_sell_goods_task_is_thin_action_wrapper():
    task_path = REPO_ROOT / "plans" / "resonance" / "tasks" / "sell_goods.yaml"
    task = yaml.safe_load(task_path.read_text(encoding="utf-8"))["sell_all_goods"]
    steps = task["steps"]

    assert list(steps) == ["run"]
    assert steps["run"]["action"] == "resonance.sell_goods_on_sell_page"
    assert task["returns"]["page_state"] == "{{ nodes.run.output.page_state }}"
    assert task["returns"]["sold_confirmed"] == "{{ nodes.run.output.sold_confirmed }}"
    assert task["returns"]["sell_result"] == "{{ nodes.run.output.sell_result }}"


def test_sell_goods_action_uses_scale_template_and_returns_to_shop_page():
    assert (REPO_ROOT / "plans" / "resonance" / "templates" / "sell_settlement_scale_badge.png").is_file()
    assert actions._SELL_SETTLEMENT["template"] == "templates/sell_settlement_scale_badge.png"
    assert actions._SELL_SETTLEMENT["region"] == [930, 240, 300, 300]
    assert actions._SETTLEMENT_EXIT_POINT == (640, 620)

    source = inspect.getsource(actions.resonance_sell_goods_on_sell_page)
    assert "_close_settlement(app, vision, \"sell\"" in source
    assert "resonance_tap_back_once" in source
    assert "sell_success_returns_to_shop_page" in source
    assert '"page_state": "shop_page"' in source
    assert "empty_or_no_result" in source


class _FakeApp:
    def __init__(self):
        self.clicks = []

    def click(self, **kwargs):
        self.clicks.append(kwargs)


def test_sell_goods_skips_back_after_successful_sale(monkeypatch):
    monkeypatch.setattr(actions.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(actions, "_wait_and_click_text", lambda *_args, **_kwargs: {"clicked": True})
    monkeypatch.setattr(actions, "_close_settlement", lambda *_args, **_kwargs: {"closed": True})

    def fail_back(**_kwargs):
        raise AssertionError("successful sale should already be back on shop page")

    monkeypatch.setattr(actions, "resonance_tap_back_once", fail_back)

    result = actions.resonance_sell_goods_on_sell_page(app=_FakeApp(), ocr=object(), vision=object())

    assert result["page_state"] == "shop_page"
    assert result["sold_confirmed"] is True
    assert result["back"]["skipped"] is True


def test_sell_goods_uses_back_when_sale_not_confirmed(monkeypatch):
    monkeypatch.setattr(actions.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(actions, "_wait_and_click_text", lambda *_args, **_kwargs: {"clicked": False})
    back_calls = []
    monkeypatch.setattr(
        actions,
        "resonance_tap_back_once",
        lambda **kwargs: back_calls.append(kwargs) or {"clicked": True, "page_state": "shop_page"},
    )

    result = actions.resonance_sell_goods_on_sell_page(app=_FakeApp(), ocr=object(), vision=object())

    assert result["page_state"] == "shop_page"
    assert result["sold_confirmed"] is False
    assert result["back"]["clicked"] is True
    assert len(back_calls) == 1
