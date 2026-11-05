"""Investment list gestures pass through the real mode-aware input facades."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.aura_base.src.services import app_provider_service as base_app
from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as investment
from plans.resonance_pc.src.services.bridge_app_service import ResonancePcAppService
from plans.resonance_pc.src.services.bridge_controller_service import ResonancePcControllerService
from plans.resonance_pc.src.services.input_bridge_service import ResonancePcInputBridgeService


@pytest.fixture
def gesture_rig(monkeypatch):
    monkeypatch.delenv("AURA_GUI_INPUT_BRIDGE", raising=False)

    def create(mode, stop_setting=True):
        state = NS(now=0., page=0, captures=0, system=[], native=[], gestures=[],
                   cancelled=False, cancel_after_drag=False, fail_move=False, grid_checks=[])
        config = {"resonance_pc.input.mode": mode}
        if stop_setting is not None:
            config["resonance_pc.input.stop_inertia"] = stop_setting

        def advance(seconds):
            state.now += seconds

        def finish(start_y, end_y):
            state.gestures.append((start_y, end_y))
            state.page = 0 if end_y > start_y else 20
            state.cancelled = state.cancel_after_drag

        def move(x, y, *, duration):
            state.system.append(("move", x, y, duration))
            if duration == 0:
                state.start_y = y
            else:
                state.end_y = y
                if state.fail_move:
                    raise RuntimeError("movement failed")
                advance(duration)

        def down(*, button):
            state.system.append(("down", button))

        def up(*, button):
            state.system.append(("up", button))
            finish(state.start_y, state.end_y)

        runtime = NS(config=config, move_to=move, mouse_down=down, mouse_up=up)
        screen = NS(target_runtime=runtime)
        bridge = ResonancePcInputBridgeService(screen)
        bridge._viewport = {"width": 1280, "height": 720}
        # Replace native readiness and transport only; retain drag policy and facades.
        monkeypatch.setattr(bridge, "_ready", lambda: None)

        def execute(op, payload, capability, **kwargs):
            assert op == "drag" and capability == "direct_drag"
            state.native.append(dict(payload))
            advance(payload["duration"] + payload["hold"])
            finish(payload["y"], payload["to_y"])

        monkeypatch.setattr(bridge, "_execute", execute)
        controller = ResonancePcControllerService(screen, bridge)
        app = ResonancePcAppService(screen, controller)
        state.app, state.controller = app, controller

        def capture():
            state.captures += 1
            return NS(success=True, image=state.frame())

        monkeypatch.setattr(app, "capture", capture)
        monkeypatch.setattr(investment, "time", NS(monotonic=lambda: state.now, sleep=advance))
        monkeypatch.setattr(base_app, "time", NS(sleep=advance))
        monkeypatch.setattr(investment, "is_current_task_cancel_requested", lambda: state.cancelled)
        state.frame = lambda: np.full((720, 1280, 3), state.page, np.uint8)

        def cards(page):
            identities = range(1, 10) if page == 0 else range(7, 13)
            return [{"rect": (62 + 127 * (offset % 3), 154 + 132 * (offset // 3), 115, 129),
                     "partial": False, "occluded": False, "identity": identity,
                     "investment_index": identity}
                    for offset, identity in enumerate(identities)]

        def validate(rows, *, first_index):
            state.grid_checks.append(first_index)
            assert [card["identity"] for card in rows] == list(range(first_index, first_index + len(rows)))

        state.reader = NS(
            VIEWPORT=(49, 143, 394, 446),
            metadata={"cards": {"viewport": (49, 143, 394, 446), "size": (115, 129)}},
            match=lambda frame, name: {"found": name == "page_anchor"},
            locate_cards=lambda frame, **kwargs: cards(int(frame[0, 0, 0])),
            fingerprint=lambda frame, card: card["identity"],
            same_fingerprint=lambda first, second: first == second,
            validate_card_grid=validate,
        )
        state.cards = lambda: cards(state.page)
        return state

    return create


@pytest.mark.parametrize("mode,stop_setting", [
    ("system", True), ("bridge", None), ("bridge", True), ("bridge", False),
])
@pytest.mark.parametrize("direction", ["next", "top"])
def test_investment_list_uses_selected_input_mode(gesture_rig, direction, mode, stop_setting):
    state = gesture_rig(mode, stop_setting)
    if direction == "top":
        state.page = 20
        _, cards = investment._reset_list_top(state.app, state.reader, state.frame(), state.cards())
        assert [card["identity"] for card in cards] == list(range(1, 10))
        assert len(state.gestures) == 3  # Reached the top, then two stationary spans.
        assert state.gestures[-1] != state.gestures[-2]
        assert state.grid_checks == [1]
    else:
        _, cards, overlap = investment._scroll(state.app, state.reader, state.frame(), state.cards())
        assert [card["investment_index"] for card in cards] == list(range(7, 13))
        assert [card["identity"] for card in overlap] == [7, 8, 9]
        assert len(state.gestures) == 1
        assert state.captures == 2  # Keep the real consecutive layout observations.
        assert state.grid_checks == [7]
    if mode == "system":
        assert not state.native
        assert [step[0] for step in state.system] == ["move", "down", "move", "up"] * len(state.gestures)
        assert not state.controller._held_mouse_buttons
    else:
        assert not state.system
        assert len(state.native) == len(state.gestures)
        assert all(payload["stop_inertia"] is (stop_setting is not False) for payload in state.native)
        assert all(payload["duration"] == .6 and payload["hold"] == .15 for payload in state.native)


def test_system_mode_still_rejects_an_explicit_native_inertia_request(gesture_rig):
    state = gesture_rig("system")
    with pytest.raises(TargetRuntimeError) as error:
        state.app.drag(100, 300, 100, 200, stop_inertia=True)
    assert error.value.code == "input_capability_unsupported"
    assert not state.system and not state.native


@pytest.mark.parametrize("after_drag", [False, True])
def test_cancelled_scroll_never_reads_or_selects_the_next_page(gesture_rig, after_drag):
    state = gesture_rig("system")
    state.cancelled, state.cancel_after_drag = not after_drag, after_drag
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        investment._scroll(state.app, state.reader, state.frame(), state.cards())
    assert error.value.code == "trade_cancelled"
    assert len(state.gestures) == int(after_drag)
    assert state.captures == 0 and not state.grid_checks
    assert not state.controller._held_mouse_buttons


def test_system_drag_failure_releases_the_mouse_and_does_not_observe_a_result(gesture_rig):
    state = gesture_rig("system")
    state.fail_move = True
    with pytest.raises(RuntimeError, match="movement failed"):
        investment._scroll(state.app, state.reader, state.frame(), state.cards())
    assert state.system[-1] == ("up", "left")
    assert not state.controller._held_mouse_buttons and state.captures == 0


def test_system_scroll_still_requires_unique_product_overlap(gesture_rig):
    state = gesture_rig("system")
    state.reader.same_fingerprint = lambda *args: False
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        investment._scroll(state.app, state.reader, state.frame(), state.cards())
    assert error.value.code == "investment_scroll_overlap_unconfirmed"
    assert len(state.gestures) == 1 and not state.grid_checks
