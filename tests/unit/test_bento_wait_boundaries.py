"""Bento waits retain the session's consumption deadline and error priority."""
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import bento_consumption_pc_actions as bento


def session_clock(monkeypatch, *, deadline=1.0, cancel_after_sleep=False):
    now = [0.0]
    sleeps = []
    cancelled = [False]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        cancelled[0] = cancel_after_sleep

    monkeypatch.setattr(bento, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    monkeypatch.setattr(bento, "is_current_task_cancel_requested", lambda: cancelled[0])
    session = object.__new__(bento.BentoConsumptionSession)
    session.deadline = deadline
    session.layout = {"poll_interval_sec": 0.08}
    return session, now, sleeps


def test_wait_cannot_extend_consumption_session_deadline(monkeypatch):
    session, now, sleeps = session_clock(monkeypatch, deadline=0.12)
    with pytest.raises(bento.BentoConsumptionError) as caught:
        session.sleep(3.0)
    assert caught.value.code == "bento_timeout"
    assert now[0] == pytest.approx(0.12)
    assert sleeps == pytest.approx([0.05, 0.05, 0.02])


def test_wait_preserves_cancellation_priority_at_session_deadline(monkeypatch):
    session, now, sleeps = session_clock(monkeypatch, deadline=0.05, cancel_after_sleep=True)
    with pytest.raises(bento.BentoConsumptionError) as caught:
        session.sleep(1.0)
    assert caught.value.code == "bento_cancelled"
    assert sleeps == [0.05]


def test_default_wait_uses_scene_poll_interval(monkeypatch):
    session, now, sleeps = session_clock(monkeypatch)
    session.sleep()
    assert now[0] == pytest.approx(0.08)
    assert sleeps == pytest.approx([0.05, 0.03])


@pytest.mark.parametrize("seconds", [0.0, -1.0])
def test_nonpositive_wait_still_checks_expired_session(monkeypatch, seconds):
    session, now, sleeps = session_clock(monkeypatch, deadline=0.0)
    with pytest.raises(bento.BentoConsumptionError) as caught:
        session.sleep(seconds)
    assert caught.value.code == "bento_timeout"
    assert sleeps == []
