"""An unchanged plus still permits one submit; a confirmed no-effect ends normally."""
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as investment


FRAME = np.zeros((720, 1280, 3), np.uint8)


class App:
    def __init__(self):
        self.clicks = []

    def click(self, **point):
        self.clicks.append(point)


@pytest.fixture
def rig(monkeypatch):
    state = SimpleNamespace(current=0, preview=1, cap=6, enabled=True, adjustments=[], matches=[])
    app = App()

    class Reader:
        VIEWPORT = (33, 130, 410, 460)

        def read_levels(self, frame):
            return {"current": state.current, "preview": state.preview}

        def match(self, frame, name):
            state.matches.append(name)
            return {"found": state.enabled, "center": [1001, 561]}

    reader = Reader()

    def adjust(app, reader, name, current, previous):
        state.adjustments.append((name, previous))
        state.preview = min(previous + 1, state.cap) if name == "plus" else previous - 1
        return FRAME, reader.read_levels(FRAME)

    monkeypatch.setattr(investment, "_capture", lambda app: FRAME)
    monkeypatch.setattr(investment, "_levels", lambda app, reader, current=None: (FRAME, reader.read_levels(FRAME)))
    monkeypatch.setattr(investment, "_adjust", adjust)
    monkeypatch.setattr(investment, "_pause", lambda seconds: None)
    monkeypatch.setattr(investment, "_cancel_check", lambda: None)
    return app, reader, state


def prepare(rig, target=10):
    app, reader, state = rig
    return investment._prepare_upgrade(app, reader, reader.read_levels(FRAME), target, lambda **data: None)


@pytest.mark.parametrize("target", [10, 14, 20])
def test_valid_partial_preview_commits_once_then_requests_exit(rig, target):
    operation = prepare(rig, target)
    assert operation == {"current_level": 0, "upgrade_level": 6, "center": [1001, 561],
                         "stop_after_commit": True}
    assert rig[2].adjustments == [("plus", value) for value in range(1, 7)]


@pytest.mark.parametrize("current", [0, 1, 6])
def test_default_next_level_with_no_plus_progress_gets_one_submission(rig, current):
    state = rig[2]
    state.current, state.preview, state.cap = current, current + 1, current + 1
    operation = prepare(rig)
    assert operation["current_level"] == current and operation["upgrade_level"] == current + 1
    assert operation["stop_after_commit"] is True
    assert state.adjustments == [("plus", current + 1)]


def test_default_preview_at_target_can_be_submitted_without_extra_plus(rig):
    state = rig[2]
    state.current, state.preview, state.cap = 9, 10, 10
    operation = prepare(rig)
    assert operation["current_level"] == 9 and operation["upgrade_level"] == 10
    assert not state.adjustments and not operation["stop_after_commit"]


@pytest.mark.parametrize("target", range(1, 21))
def test_reaching_target_keeps_normal_next_product_processing(rig, target):
    rig[2].cap = target
    operation = prepare(rig, target)
    assert operation["upgrade_level"] == target and not operation["stop_after_commit"]
    assert ("plus", target) not in rig[2].adjustments


def test_oversized_preview_is_reduced_to_target_without_claiming_plus_limit(rig):
    rig[2].preview = 12
    operation = prepare(rig)
    assert operation["upgrade_level"] == 10 and not operation["stop_after_commit"]
    assert rig[2].adjustments == [("minus", 12), ("minus", 11)]


def test_disabled_confirmation_cannot_submit_partial_preview(rig):
    rig[2].enabled = False
    assert prepare(rig) is None


@pytest.mark.parametrize("preview", [None, 0])
def test_invalid_plus_feedback_still_raises_not_normal_limit(rig, monkeypatch, preview):
    monkeypatch.setattr(investment, "_adjust", lambda *args: (FRAME, {"current": 0, "preview": preview}))
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        prepare(rig)
    assert error.value.code == "investment_preview_invalid"


@pytest.mark.parametrize("kind, delayed_preview, clicks, reads", [
    ("plus", 7, 1, 2), ("plus", 8, 1, 2), ("minus", 7, 2, 3),
])
def test_plus_limit_has_readback_but_no_second_click(monkeypatch, kind, delayed_preview, clicks, reads):
    app = App()
    reader = SimpleNamespace(read_levels=lambda frame: {"current": 6},
                             match=lambda frame, name: {"found": True, "center": [1146, 239]})
    observations = []

    def levels(*args):
        observations.append(None)
        return FRAME, {"current": 6, "preview": 7 if len(observations) == 1 else delayed_preview}

    monkeypatch.setattr(investment, "_capture", lambda app: FRAME)
    monkeypatch.setattr(investment, "_levels", levels)
    monkeypatch.setattr(investment, "_pause", lambda seconds: None)
    monkeypatch.setattr(investment, "_cancel_check", lambda: None)
    result = investment._adjust(app, reader, kind, 6, 7)
    assert len(app.clicks) == clicks and len(observations) == reads
    assert result[1]["preview"] == delayed_preview


def execute(rig, monkeypatch, mode=10, product_levels=None, product_caps=None, product_effects=None):
    app, reader, state = rig
    events = []
    cards = [{"rect": (50 + index * 120, 150, 114, 128), "partial": False,
              "locked": False, "selected": False} for index in range(3)]
    monkeypatch.setattr(investment, "InvestmentVision", lambda: reader)
    monkeypatch.setattr(investment, "_wait", lambda app, predicate: (FRAME, {"center": [843, 490]}))
    monkeypatch.setattr(investment, "_idle", lambda *args: None)
    monkeypatch.setattr(investment, "_cards", lambda *args: (FRAME, cards))
    monkeypatch.setattr(investment, "_scroll", lambda *args: None)

    def select(app, reader, card):
        events.append("select")
        state.selected_index = cards.index(card)
        if product_levels is not None:
            state.current = product_levels[state.selected_index]
            state.preview = state.current + 1
        if product_caps is not None:
            state.cap = product_caps[state.selected_index]
        return FRAME, card, reader.read_levels(FRAME)

    def commit(app, reader, operation):
        events.append(("commit", operation["upgrade_level"]))
        if getattr(state, "submission_effective", True) is False:
            return None
        if product_effects is not None and not product_effects[state.selected_index]:
            return None
        state.current = operation["upgrade_level"]
        state.preview = state.current + 1
        if product_levels is not None:
            product_levels[state.selected_index] = state.current
        return {"confirmed": True, "current_level": state.current}

    monkeypatch.setattr(investment, "_select", select)
    monkeypatch.setattr(investment, "_commit", commit)
    monkeypatch.setattr(investment, "_return_shop", lambda *args: events.append("return_shop"))
    result = investment.execute_trade_goods_investment_from_shop(
        mode=mode, city_name="city", app=app, vision=object())
    return result, events


def test_partial_commit_ends_visit_without_reselecting_or_advancing_product(rig, monkeypatch):
    result, events = execute(rig, monkeypatch)
    assert events == ["select", ("commit", 6), "return_shop"]
    assert result["success"] and result["status"] == "invested"
    assert result["reason"] == "no_further_upgrade" and result["page_state"] == "shop_page"
    assert result["transaction_count"] == 1 and result["upgraded_levels"] == 6
    assert result["product_count"] == 1 and result["scroll_count"] == 0


def test_no_effect_first_plus_can_upgrade_one_level_then_return_shop(rig, monkeypatch):
    rig[2].current, rig[2].preview, rig[2].cap = 6, 7, 7
    result, events = execute(rig, monkeypatch)
    assert events == ["select", ("commit", 7), "return_shop"]
    assert result["success"] and result["status"] == "invested"
    assert result["transaction_count"] == 1 and result["upgraded_levels"] == 1


def test_ineffective_submission_ends_normally_without_repeat_or_next_product(rig, monkeypatch):
    state = rig[2]
    state.current, state.preview, state.cap = 6, 7, 7
    state.submission_effective = False
    result, events = execute(rig, monkeypatch)
    assert events == ["select", ("commit", 7), "return_shop"]
    assert result["success"] and result["status"] == "skipped"
    assert result["reason"] == "no_further_upgrade" and result["transaction_count"] == 0


@pytest.mark.parametrize("target", range(1, 10))
def test_target_below_unlock_level_stops_after_first_product(rig, monkeypatch, target):
    result, events = execute(rig, monkeypatch, mode=target,
                             product_levels=[0, 0, 0], product_caps=[20, 20, 20])
    assert events == ["select", ("commit", target), "return_shop"]
    assert result["mode"] == result["target_level"] == target
    assert result["reason"] == "next_product_locked" and result["product_count"] == 1


def test_low_target_can_advance_past_an_already_unlocked_product(rig, monkeypatch):
    result, events = execute(rig, monkeypatch, mode=1,
                             product_levels=[10, 0, 0], product_caps=[20, 20, 20])
    assert events == ["select", "select", ("commit", 1), "return_shop"]
    assert result["product_count"] == 2 and result["transaction_count"] == 1


@pytest.mark.parametrize("partial", [10, 11, 12, 13])
def test_partial_upgrade_after_unlock_allows_only_one_next_submission(rig, monkeypatch, partial):
    result, events = execute(rig, monkeypatch, mode=14,
                             product_levels=[0, 0, 0], product_caps=[partial, 20, 20])
    assert events == ["select", ("commit", partial), "select", ("commit", 14), "return_shop"]
    assert result["reason"] == "next_product_attempted"
    assert result["transaction_count"] == 2 and result["product_count"] == 2


def test_no_effect_after_unlock_still_allows_one_next_attempt(rig, monkeypatch):
    result, events = execute(rig, monkeypatch, mode=14, product_levels=[10, 0, 0],
                             product_caps=[11, 1, 20], product_effects=[False, True, True])
    assert events == ["select", ("commit", 11), "select", ("commit", 1), "return_shop"]
    assert result["transaction_count"] == 1 and result["upgraded_levels"] == 1
    assert result["reason"] == "next_product_attempted" and result["product_count"] == 2


def test_already_complete_product_does_not_spend_the_single_next_attempt(rig, monkeypatch):
    result, events = execute(rig, monkeypatch, mode=14,
                             product_levels=[0, 14, 0], product_caps=[12, 20, 20])
    assert events == ["select", ("commit", 12), "select", "select", ("commit", 14), "return_shop"]
    assert result["product_count"] == 3 and result["transaction_count"] == 2


def test_ineffective_final_attempt_is_not_repeated(rig, monkeypatch):
    result, events = execute(rig, monkeypatch, mode=14, product_levels=[0, 0, 0],
                             product_caps=[12, 20, 20], product_effects=[True, False, True])
    assert events == ["select", ("commit", 12), "select", ("commit", 14), "return_shop"]
    assert result["transaction_count"] == 1 and result["reason"] == "next_product_attempted"


@pytest.mark.parametrize("target", [10, 11, 14, 20])
def test_complete_unlocked_products_continue_normally(rig, monkeypatch, target):
    result, events = execute(rig, monkeypatch, mode=target,
                             product_levels=[0, 0, 0], product_caps=[20, 20, 20])
    assert events == ["select", ("commit", target)] * 3 + ["return_shop"]
    assert result["transaction_count"] == 3 and result["reason"] == "all_products_at_target"


def test_submission_without_any_observation_is_still_an_error(rig, monkeypatch):
    app, reader, state = rig
    operation = prepare(rig)
    monkeypatch.setattr(investment, "_COMMIT_TIMEOUT", 0.)
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        investment._commit(app, reader, operation)
    assert error.value.code == "investment_commit_unconfirmed"
    assert len(app.clicks) == 1 and state.current == 0


@pytest.fixture
def commit_rig(monkeypatch):
    state = SimpleNamespace(now=0., observations=[], index=0, current=1, preview=2, active=None)
    app = App()

    class Reader:
        def match(self, frame, name):
            if name == "confirm_enabled":
                return {"found": True, "center": [1001, 561]}
            return {"found": bool(state.active.get("toast"))}

        def read_levels(self, frame):
            if state.active.get("unknown"):
                raise investment.InvestmentRecognitionError("level_uncertain", "offline sample")
            return {"current": state.active["current"], "preview": state.active.get("preview", state.preview)}

    def capture(app):
        state.active = state.observations[min(state.index, len(state.observations)-1)]
        state.index += 1
        return FRAME

    monkeypatch.setattr(investment, "_capture", capture)
    monkeypatch.setattr(investment, "_levels", lambda *args: (FRAME, {"current": state.current, "preview": state.preview}))
    monkeypatch.setattr(investment.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(investment, "_pause", lambda seconds: setattr(state, "now", state.now + seconds))
    monkeypatch.setattr(investment, "_COMMIT_TIMEOUT", .9)
    monkeypatch.setattr(investment, "_cancel_check", lambda: None)
    reader = Reader()
    operation = lambda: {"current_level": state.current, "upgrade_level": state.preview}
    state.run = lambda: investment._commit(app, reader, operation())
    return app, state


@pytest.mark.parametrize("current", [0, 1, 9])
def test_unchanged_actual_level_after_single_submit_is_normal_limit(commit_rig, current):
    app, state = commit_rig
    state.current, state.preview = current, current + 1
    state.observations = [{"current": current, "preview": current + 1}]
    assert state.run() is None
    assert len(app.clicks) == 1 and state.now >= .9 and state.index >= 2


def test_one_level_success_after_delayed_feedback_is_confirmed(commit_rig):
    app, state = commit_rig
    state.observations = [{"current": 1}, {"current": 1}, {"current": 2}, {"current": 2}]
    result = state.run()
    assert result["confirmed"] and result["current_level"] == 2
    assert len(app.clicks) == 1 and state.index == 4


@pytest.mark.parametrize("observations", [
    [{"unknown": True}], [{"current": 3}],
    [{"toast": True}, {"current": 1}],
    [{"current": 1}, {"current": 1}, {"unknown": True}],
    [{"unknown": True}, {"unknown": True}, {"unknown": True}, {"unknown": True}, {"current": 1}],
])
def test_unreadable_changed_or_contradictory_feedback_is_not_a_normal_limit(commit_rig, observations):
    app, state = commit_rig
    state.observations = observations
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        state.run()
    assert error.value.code == "investment_commit_unconfirmed"
    assert len(app.clicks) == 1
