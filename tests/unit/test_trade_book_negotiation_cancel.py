"""Cancellation races in trading helpers, using fake inputs and a fake clock."""
import asyncio
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import purchase_book_pc_actions as books
from plans.resonance_pc.src.actions import trade_negotiation_pc_actions as negotiation


@pytest.fixture
def cancel_state(monkeypatch):
    state = {"cancelled": False}
    for module in (books, negotiation):
        monkeypatch.setattr(module, "is_current_task_cancel_requested", lambda: state["cancelled"])
    return state


@pytest.mark.parametrize("module", [books, negotiation])
def test_long_wait_stops_on_first_fifty_ms_slice(monkeypatch, cancel_state, module):
    elapsed = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds
        cancel_state["cancelled"] = True

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        module._cancelable_sleep(15)
    assert sleeps == [0.05]


def test_book_cancel_during_pointer_move_prevents_click(monkeypatch, cancel_state):
    clicks = []
    monkeypatch.setattr(books, "_find_template", lambda *a, **kw:
                        SimpleNamespace(found=True, confidence=1.0, center_point=(10, 20)))
    app = SimpleNamespace(
        move_to=lambda *a, **kw: cancel_state.update(cancelled=True),
        click=lambda **kw: clicks.append(kw),
    )
    with pytest.raises(asyncio.CancelledError):
        books._click_template_or_point(app, object(), "unused", [0, 0, 50, 50], (10, 20))
    assert clicks == []


def test_book_quantity_cancel_never_confirms_or_runs_failure_cleanup(monkeypatch, cancel_state):
    phases = []
    clicks = []

    def matched_click(*args, **kwargs):
        phases.append(args[2])
        return {"clicked": True}

    def click(**kwargs):
        clicks.append(kwargs)
        cancel_state["cancelled"] = True

    monkeypatch.setattr(books, "_click_template_or_point", matched_click)
    monkeypatch.setattr(books, "_wait_for_template_state", lambda *a, **kw: {"matched": True})
    monkeypatch.setattr(books, "_attempt_quantity_dialog_cleanup",
                        lambda **kw: pytest.fail("Cancellation must not trigger recovery clicks"))
    with pytest.raises(asyncio.CancelledError):
        books._use_purchase_book_batch(batch_size=10, item_name="book", open_timeout_sec=3,
                                       dialog_timeout_sec=3, click_interval_sec=.2,
                                       app=SimpleNamespace(click=click), vision=object())
    assert len(clicks) == 1
    assert phases == [books._USE_ITEM_BUTTON_TEMPLATE, books._FIRST_ITEM_USE_BUTTON_TEMPLATE]


def test_book_cancel_does_not_start_next_batch(monkeypatch, cancel_state):
    batches = []

    def batch(**kwargs):
        batches.append(kwargs["batch_size"])
        cancel_state["cancelled"] = True
        return {"ok": True, "used": kwargs["batch_size"], "buy_page_ready": True}

    monkeypatch.setattr(books, "_use_purchase_book_batch", batch)
    with pytest.raises(asyncio.CancelledError):
        books.resonance_pc_use_purchase_books(14, app=object(), vision=object())
    assert batches == [10]


def test_negotiation_cancel_after_matching_prevents_click(monkeypatch, cancel_state):
    clicks = []
    monkeypatch.setattr(negotiation, "_confirm_attempts_exhausted", lambda **kw: {"found": False})
    monkeypatch.setattr(negotiation, "_confirm_cap", lambda **kw: {"confirmed": False})

    def found(**kwargs):
        cancel_state["cancelled"] = True
        return {"found": True, "center": [10, 20]}

    monkeypatch.setattr(negotiation, "_wait_for_template_state", found)
    with pytest.raises(asyncio.CancelledError):
        negotiation.execute_bargain_to_cap(requested_to_cap=True,
                                           app=SimpleNamespace(click=lambda **kw: clicks.append(kw)),
                                           vision=object())
    assert clicks == []


def test_negotiation_cancel_during_animation_prevents_next_attempt(monkeypatch, cancel_state):
    clicks = []
    elapsed = [0.0]
    sleeps = []
    monkeypatch.setattr(negotiation, "_confirm_cap", lambda **kw: {"confirmed": False})
    monkeypatch.setattr(negotiation, "_match_template", lambda **kw:
                        {"found": True, "center": [10, 20], "confidence": 1.0})

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds
        cancel_state["cancelled"] = True

    monkeypatch.setattr(negotiation, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        negotiation.execute_raise_to_cap(requested_to_cap=True,
                                         app=SimpleNamespace(click=lambda **kw: clicks.append(kw)),
                                         vision=object())
    assert len(clicks) == 1
    assert sleeps == [0.05]


def test_existing_business_error_is_preserved(monkeypatch, cancel_state):
    monkeypatch.setattr(negotiation, "_confirm_cap", lambda **kw: {"confirmed": False})
    monkeypatch.setattr(negotiation, "_wait_for_template_state", lambda **kw: {"found": False})
    with pytest.raises(negotiation.NegotiationExecutionError) as caught:
        negotiation.execute_raise_to_cap(requested_to_cap=True, app=object(), vision=object())
    assert caught.value.code == "negotiation_button_not_found"
