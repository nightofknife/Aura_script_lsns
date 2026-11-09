"""Daily activity claims rescan the page with or without an overlay."""

from types import SimpleNamespace

import numpy as np
import pytest

from plans.aura_base.src.services.interaction_service import InteractionService
from plans.aura_base.src.services.observation_service import Frame, Observation
from plans.resonance_pc.src.actions import _daily_reward_flow
from plans.resonance_pc.src.actions._daily_reward_vision import RewardPage
from tests.interaction_clock import install_clock


@pytest.mark.parametrize(
    ("activity_clicks", "overlays"),
    [
        ([(441, 177)], [False]),
        ([(441, 177), (562, 177)], [True, False]),
        ([(441, 177), (441, 177)], [False, False]),
    ],
)
def test_daily_activity_rescans_for_remaining_rewards(monkeypatch, activity_clicks, overlays):
    entry = (199, 30)
    dismiss = (500, 500)
    exit_to_main = (197, 37)
    pending = list(zip(activity_clicks, overlays))
    pixels = np.zeros((630, 640, 3), dtype=np.uint8)
    page = "main"
    clicks = []

    def observe(_scope):
        frame = Frame.from_image(pixels)
        controls = {
            "main": {"main_entry_with_red": entry},
            "reward": {"reward_exit": dismiss},
            "daily": {
                "claimable": None,
                "activity_gold": pending[0][0] if pending else None,
                "city_main": exit_to_main,
            },
        }[page]
        return RewardPage(frame, page, {
            name: Observation("present" if point is not None else "absent", frame.id, client_center=point)
            for name, point in controls.items()
        })

    def click(*, x, y):
        nonlocal page
        point = (x, y)
        clicks.append(point)
        if page == "main":
            assert point == entry
            page = "daily"
        elif page == "reward":
            assert point == dismiss
            page = "daily"
        elif pending:
            expected, has_overlay = pending.pop(0)
            assert point == expected
            page = "reward" if has_overlay else "daily"
        else:
            assert point == exit_to_main
            page = "main"
        return True

    monkeypatch.setattr(_daily_reward_flow.DailyRewardFlow, "reader_type",
                        staticmethod(lambda _: SimpleNamespace(observe=observe)))
    with install_clock(monkeypatch, _daily_reward_flow):
        flow = _daily_reward_flow.DailyRewardFlow(SimpleNamespace(click=click), None, InteractionService())
        result = flow.run()

    expected = [entry]
    for point, has_overlay in zip(activity_clicks, overlays):
        expected.append(point)
        if has_overlay:
            expected.append(dismiss)
    assert clicks == [*expected, exit_to_main]
    assert result["status"] == "collected"
    assert result["claim_clicks"] == len(activity_clicks)
    assert result["reward_overlays"] == sum(overlays)
    assert result["returned_to_main"] is True
