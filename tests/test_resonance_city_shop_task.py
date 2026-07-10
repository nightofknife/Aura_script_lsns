from pathlib import Path

import inspect
import pytest
import yaml

from plans.resonance.src.actions import city_trade_flow_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_task_data():
    task_path = REPO_ROOT / "plans" / "resonance" / "tasks" / "city_shop.yaml"
    return yaml.safe_load(task_path.read_text(encoding="utf-8"))


def test_city_shop_tasks_are_thin_independent_action_wrappers():
    task_data = _load_task_data()

    expected = {
        "open_city_panel_from_main": "resonance.open_city_panel_from_main",
        "tap_back_once": "resonance.tap_back_once",
        "go_city_main_direct": "resonance.go_city_main_direct",
        "read_city_name_on_city_panel": "resonance.read_city_name_on_city_panel",
        "click_city_shop_by_name": "resonance.click_city_shop_by_name",
        "click_shop_menu_node": "resonance.click_shop_menu_node",
    }
    assert set(task_data) == set(expected)
    for task_name, action_name in expected.items():
        steps = task_data[task_name]["steps"]
        assert list(steps) == ["run"]
        assert steps["run"]["action"] == action_name


def test_city_shop_fixed_coordinates_and_node_formula():
    assert actions._BACK_POINT == (82, 37)
    assert actions._CITY_MAIN_POINT == (198, 37)
    assert (REPO_ROOT / "plans" / "resonance" / actions._BACK_BUTTON_TEMPLATE).is_file()
    assert (REPO_ROOT / "plans" / "resonance" / actions._CITY_MAIN_BUTTON_TEMPLATE).is_file()
    assert actions._SHOP_NODE_X == 1160
    assert actions._SHOP_NODE_FIRST_Y == 324
    assert actions._SHOP_NODE_GAP_Y == 83
    assert actions._SHOP_NODE_FIRST_Y + 2 * actions._SHOP_NODE_GAP_Y == 490
    assert actions._SHOP_ENTRY_SETTLE_SEC == 2.0


def test_city_trade_flow_waits_after_entering_shop_before_menu_click():
    source = inspect.getsource(actions._execute_city_trade_inside_current_city)
    assert "wait_sec=_SHOP_ENTRY_SETTLE_SEC" in source


@pytest.mark.parametrize(
    ("action", "expected_code"),
    [
        (actions.resonance_tap_back_once, "nav_back_button_not_found"),
        (actions.resonance_go_city_main_direct, "nav_city_main_button_not_found"),
    ],
)
def test_nav_button_template_miss_raises_without_click(monkeypatch, action, expected_code):
    class FakeApp:
        def __init__(self):
            self.clicks = []

        def click(self, **kwargs):
            self.clicks.append(kwargs)

    app = FakeApp()

    monkeypatch.setattr(
        actions,
        "_wait_template",
        lambda *args, **kwargs: {"found": False, "confidence": 0.1},
    )

    with pytest.raises(actions.CityTradeFlowError) as exc_info:
        action(app=app, vision=object())

    assert exc_info.value.code == expected_code
    assert app.clicks == []
