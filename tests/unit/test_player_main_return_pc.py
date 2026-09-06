from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.resonance_pc.src.actions import passenger_pc_actions as passenger
from plans.resonance_pc.src.actions import player_data_pc_actions as player


@pytest.fixture
def clock(monkeypatch):
    now=[0.0]
    monkeypatch.setattr(passenger.time,"monotonic",lambda:now[0])
    monkeypatch.setattr(passenger.time,"sleep",lambda seconds:now.__setitem__(0,now[0]+seconds))
    return now


def fake_ui(monkeypatch, *, initial="profile", success_after=2):
    state=[initial]
    clicks=[]
    def click(*,x,y):
        assert state[0] != "main", "never click on confirmed main screen"
        clicks.append((x,y))
        if len(clicks)>=success_after:
            state[0]="main"
    monkeypatch.setattr(passenger,"_match_template",lambda *args,**kwargs:{"found":state[0]=="main","confidence":.99 if state[0]=="main" else .1})
    monkeypatch.setattr(player,"_match_navigation_template",lambda *args,**kwargs:{"found":state[0]=="profile"})
    return NS(click=click),state,clicks


def test_profile_return_retries_same_point_and_stops_when_main(monkeypatch,clock):
    app,state,clicks=fake_ui(monkeypatch)
    player._close_profile_panel_to_main(app,object())
    assert state[0]=="main"
    assert clicks==[(900,150),(900,150)]


def test_already_main_never_clicks(monkeypatch,clock):
    app,state,clicks=fake_ui(monkeypatch,initial="main")
    player._close_profile_panel_to_main(app,object())
    assert clicks==[]


def test_unknown_page_waits_without_blind_click(monkeypatch,clock):
    app,state,clicks=fake_ui(monkeypatch,initial="unknown")
    with pytest.raises(player.StopTaskException,match="player_data_main_screen_not_restored"):
        player._close_profile_panel_to_main(app,object())
    assert clicks==[]
    assert clock[0]<12


def test_return_has_three_click_limit(monkeypatch,clock):
    app,state,clicks=fake_ui(monkeypatch,success_after=99)
    with pytest.raises(player.StopTaskException):
        player._close_profile_panel_to_main(app,object())
    assert clicks==[(900,150)]*3
    assert clock[0]<12


def test_passenger_keeps_default_exit_point(monkeypatch,clock):
    app,state,clicks=fake_ui(monkeypatch,initial="passenger",success_after=1)
    result=passenger._click_blank_and_confirm_main(app,object(),error_code="test")
    assert result["success"]
    assert clicks==[passenger._SAFE_EXIT_POINT]


def test_source_guard_rechecked_before_each_retry(monkeypatch,clock):
    clicks=[]
    outcomes=iter([False,False,False,True])
    monkeypatch.setattr(passenger,"_wait_main_stable",lambda *args,**kwargs:{"confirmed":next(outcomes)})
    allowed=iter([True,False,False])
    result=passenger._click_blank_and_confirm_main(NS(click=lambda **kwargs:clicks.append(kwargs)),object(),
        error_code="test",exit_point=(900,150),can_exit=lambda:next(allowed))
    assert result["success"]
    assert clicks==[{"x":900,"y":150}]
    assert [a["clicked"] for a in result["attempts"]]==[True,False,False]


def test_two_consecutive_main_matches_required(monkeypatch,clock):
    sequence=iter([True,False,True,True])
    consumed=[]
    def match(*args,**kwargs):
        value=next(sequence)
        consumed.append(value)
        return {"found":value}
    monkeypatch.setattr(passenger,"_match_template",match)
    result=passenger._wait_main_stable(None,None,timeout_sec=3)
    assert result["confirmed"] and result["confirmations"]==2
    assert consumed==[True,False,True,True]


@pytest.mark.parametrize("page",["profile","unknown"])
def test_failure_cleanup_reuses_same_return_helper(monkeypatch,page):
    calls=[]
    app,vision=object(),object()
    monkeypatch.setattr(player,"_close_profile_panel_to_main",lambda *args:calls.append(args))
    player._best_effort_return_to_main(app,None,page,vision)
    assert calls==[(app,vision)]


def test_return_failure_does_not_save_or_repeat_cleanup(tmp_path,monkeypatch):
    service=PersistentDataService(tmp_path)
    old={"status":{"cargo":{"current":1,"max":2}},"metadata":{"updated_at":"old"}}
    service.set(player.USER_INFO_FILE,[],old)
    monkeypatch.setattr(player,"_wait_for_any_marker",lambda *args,**kwargs:[])
    monkeypatch.setattr(player,"_read_region_text",lambda *args:"2/3")
    error=player.StopTaskException("close failed",success=False)
    calls=[]
    def fail(*args):
        calls.append(1)
        raise error
    monkeypatch.setattr(player,"_close_profile_panel_to_main",fail)
    monkeypatch.setattr(player,"_best_effort_return_to_main",lambda *args:pytest.fail("must not retry exhausted close"))
    with pytest.raises(player.StopTaskException) as caught:
        player.resonance_pc_player_data_refresh(stages=["profile"],profile_sections=["cargo"],
            app=NS(click=lambda **kwargs:None),ocr=object(),vision=object(),persistent_data=service)
    assert caught.value is error
    assert calls==[1]
    assert service.read(player.USER_INFO_FILE)==old


def test_cancelled_return_never_clicks(monkeypatch,clock):
    monkeypatch.setattr(passenger,"is_current_task_cancel_requested",lambda:True)
    with pytest.raises(asyncio.CancelledError):
        passenger._click_blank_and_confirm_main(NS(click=lambda **kwargs:pytest.fail("cancelled click")),None,error_code="test")
