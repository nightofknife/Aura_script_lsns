from pathlib import Path
import cv2
import pytest
from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime
from plans.resonance_pc.src.actions._deep_dive_single_run_vision import observe

IMAGES=Path(__file__).resolve().parents[2]/'docs/developer-guide/images/resonance-pc-deep-dive-simple'


def frame(name):
    return cv2.cvtColor(cv2.imread(str(IMAGES/name)),cv2.COLOR_BGR2RGB)


@pytest.mark.parametrize('kind,file,scene',[
    ('move','51-move-button-selected.png','choose_move'),
    ('rotate','52-rotate-button-selected.png','choose_rotate'),
])
def test_button_retry_then_options_without_toggling(monkeypatch,kind,file,scene):
    now=[100.];monkeypatch.setattr(runtime.time,'time',lambda:now[0])
    s={'phase':'player_ready' if kind=='move' else 'rotate_ready','random_seed':1,
       'choices_made':0,'turns_completed':0,'last_choice':None,'move_history':[]}
    idle=observe(frame('50-mode-buttons-idle.png'))
    assert idle['scene']=='board'
    assert runtime._advance_state(s,idle)[0]=='click_'+kind+'_button'
    now[0]=101.99
    assert runtime._advance_state(s,idle)==('wait',None)
    now[0]=102.
    assert runtime._advance_state(s,idle)[0]=='retry_'+kind+'_button'
    selected=observe(frame(file));assert selected['scene']==scene
    assert runtime._advance_state(s,selected)[0]=='click_'+kind+'_option'
    assert 'mode_button_pending' not in s
    assert s['choices_made']==1 and s['move_history']==[]


@pytest.mark.parametrize('kind',['move','rotate'])
def test_blue_button_waits_for_candidates_does_not_toggle(monkeypatch,kind):
    now=[100.];monkeypatch.setattr(runtime.time,'time',lambda:now[0])
    s={'phase':'player_ready' if kind=='move' else 'rotate_ready'}
    idle=observe(frame('50-mode-buttons-idle.png'))
    runtime._advance_state(s,idle);now[0]=103.
    unknown={**idle,'scene':'unknown','cyan_pixels':{'move':0,'rotate':0}}
    unknown['cyan_pixels'][kind]=9000
    assert runtime._advance_state(s,unknown)==('wait',None)
    assert s['mode_button_pending']['clicks']==1
    assert runtime._advance_state(s,{'valid':False,'scene':'capture_failed'})==('wait',None)


@pytest.mark.parametrize('kind',['move','rotate'])
def test_mode_button_three_attempt_limit(monkeypatch,kind):
    now=[100.];monkeypatch.setattr(runtime.time,'time',lambda:now[0])
    s={'phase':'player_ready' if kind=='move' else 'rotate_ready'}
    idle=observe(frame('50-mode-buttons-idle.png'));runtime._advance_state(s,idle)
    for t in [102.,104.]:
        now[0]=t;assert runtime._advance_state(s,idle)[0]=='retry_'+kind+'_button'
    now[0]=106.
    assert runtime._advance_state(s,idle)[0]=='mode_button_blocked'
    assert s['mode_button_pending']['clicks']==3
