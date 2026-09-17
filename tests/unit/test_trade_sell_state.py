from types import SimpleNamespace
import pytest
from plans.resonance_pc.src.actions import _trade_sell_state as m


def observation(**found):
    return {k: {'found': found.get(k, False), 'confidence': 1., 'center': (1, 1)} for k in m.TARGETS}


class Failure(Exception):
    pass


class Rig(m.SellSession):
    def __init__(self, monkeypatch, probe):
        self.clock = 0.
        self.inputs = []
        self.probe = probe
        self.cancelled = False
        self.rewinds = 0
        super().__init__(None, None, self.check_cancel, self.error)
        monkeypatch.setattr(m.time, 'monotonic', lambda: self.clock)

    def error(self, code, message, detail):
        raise Failure(code)

    def check_cancel(self):
        if self.cancelled:
            raise Failure('cancelled')

    def pause(self, seconds):
        self.check_cancel()
        self.clock += seconds

    def observe(self, keys, frame=None):
        self.check_cancel()
        return self.probe(self)

    def click(self, hit):
        self.check_cancel()
        self.inputs.append(self.clock)
        return {'clicked': True}

    def rewind(self):
        self.rewinds += 1


@pytest.mark.parametrize('kind', ['empty_cargo', 'no_sellable_goods'])
def test_skip_has_positive_evidence_and_never_selects(monkeypatch, kind):
    rig=Rig(monkeypatch,lambda r: observation(all=True,commit=True,
                empty=kind=='empty_cargo',local=kind=='no_sellable_goods'))
    assert rig.select()['status']==kind
    assert not rig.inputs
    assert rig.rewinds==(kind=='no_sellable_goods')


def test_selected_with_local_leftovers_is_not_skipped(monkeypatch):
    rig=Rig(monkeypatch,lambda r: observation(cancel=True,commit=True,local=True))
    assert rig.select()['status']=='selected'
    assert not rig.inputs and not rig.rewinds


def test_selection_retries_lost_click(monkeypatch):
    rig=Rig(monkeypatch,lambda r: observation(all=len(r.inputs)<2,cancel=len(r.inputs)>=2,commit=True))
    assert rig.select()['status']=='selected'
    assert len(rig.inputs)==2


def test_selection_never_falls_back_to_empty(monkeypatch):
    rig=Rig(monkeypatch,lambda r: observation(all=True,commit=True))
    with pytest.raises(Failure,match='sell_all_selection_unconfirmed'):
        rig.select()
    assert len(rig.inputs)==3


def test_commit_retries_then_waits_without_extra_clicks(monkeypatch):
    def probe(r):
        transitioning=len(r.inputs)>=2
        return observation(commit=not transitioning,cancel=not transitioning,
                           settlement=transitioning and r.clock-r.inputs[-1]>5.)
    rig=Rig(monkeypatch,probe)
    assert rig.submit()['transition_confirmed']
    assert len(rig.inputs)==2


def test_commit_lost_click_does_not_return_or_skip(monkeypatch):
    rig=Rig(monkeypatch,lambda r: observation(commit=True,cancel=True))
    with pytest.raises(Failure,match='sell_click_unconfirmed'):
        rig.submit()
    assert len(rig.inputs)==3


def test_transition_timeout_does_not_reclick_reappearing_button(monkeypatch):
    rig=Rig(monkeypatch,lambda r: observation(commit=not r.inputs or r.clock>2.,cancel=True))
    with pytest.raises(Failure,match='sell_settlement_timeout'):
        rig.submit()
    assert len(rig.inputs)==1


def test_fast_settlement_prevents_duplicate_input(monkeypatch):
    rig=Rig(monkeypatch,lambda r: observation(settlement=True,commit=True,cancel=True))
    rig.submit()
    assert not rig.inputs


def test_return_requires_actual_shop_and_clicks_back_only_once(monkeypatch):
    rig=Rig(monkeypatch,lambda r: observation(all=True,commit=True,back=True))
    with pytest.raises(Failure,match='sell_return_unconfirmed'):
        rig.return_to_shop(False)
    assert len(rig.inputs)==1


def test_cancel_during_probe_prevents_click(monkeypatch):
    def probe(r):
        r.cancelled=True
        return observation(commit=True,cancel=True)
    rig=Rig(monkeypatch,probe)
    with pytest.raises(Failure,match='cancelled'):
        rig.submit()
    assert not rig.inputs
