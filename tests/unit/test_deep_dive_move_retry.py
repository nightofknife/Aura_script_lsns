import pytest
from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime


def setup(monkeypatch):
    clock=[100.];monkeypatch.setattr(runtime.time,'time',lambda:clock[0])
    state={'phase':'move_options','turns_completed':0,'choices_made':0,'random_seed':0,
           'move_history':[],'last_choice':None}
    o={'valid':True,'scene':'choose_move','options':[{'point':[428,308]}]}
    assert runtime._advance_state(state,o)==('click_move_option',[428,308])
    return state,clock


def test_retry_once_after_two_seconds_relocates_same_tile(monkeypatch):
    s,clock=setup(monkeypatch)
    o={'valid':True,'scene':'choose_move','options':[{'point':[640,260]},{'point':[428,363]}]}
    clock[0]=101.99
    assert runtime._advance_state(s,o)==('wait',None)
    clock[0]=102.
    assert runtime._advance_state(s,o)==('retry_move_option',[428,363])
    for t in [103.,105.,125.]:
        clock[0]=t
        assert runtime._advance_state(s,o)==('wait',None)
    assert s['choices_made']==1 and s['move_history']==[]
    entry={'valid':True,'scene':'event_entry','event_type':'healing'}
    assert runtime._advance_state(s,entry)==('transition',None)


@pytest.mark.parametrize('o',[
    {'valid':False,'scene':'capture_failed'},
    {'valid':True,'scene':'unknown'},
    {'valid':True,'scene':'ambiguous_event_entry'},
    {'valid':True,'scene':'reward_obtained'},
    {'valid':True,'scene':'choose_move','options':[{'point':[800,400]}]},
])
def test_no_retry_on_other_pages_or_missing_target(monkeypatch,o):
    s,clock=setup(monkeypatch);clock[0]=103.
    assert runtime._advance_state(s,o)==('wait',None)
    assert not s['pending_move'].get('retry_at')


def test_preview_arriving_at_retry_time_wins(monkeypatch):
    s,clock=setup(monkeypatch);clock[0]=102.
    assert runtime._advance_state(s,{'valid':True,'scene':'event_entry','event_type':'empty'})==('transition',None)
    assert not s['pending_move'].get('retry_at')
