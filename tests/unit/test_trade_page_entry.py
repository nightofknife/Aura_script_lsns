"""Observed entry transitions never retry on unknown or already-open pages."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _trade_page_entry as entry
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from packages.aura_core.observability.failure_evidence import evidence_from


def observation(*keys):
    return {key: {'found': key in keys, 'confidence': 1. if key in keys else .1}
            for key in entry.TARGETS}


@pytest.fixture
def rig(monkeypatch):
    state = NS(now=0., clicks=[], cancelled=False, probes=[], evidence=[], behavior=None)
    monkeypatch.setattr(entry, 'time', NS(monotonic=lambda: state.now, time=lambda: 1000+state.now))
    monkeypatch.setattr(entry, 'save_evidence_png', lambda png: state.evidence.append(png) or
                        {'evidence_id': 'entry-frame', 'status': 'saved', 'screenshot': 'errors/test.png'})

    def guard():
        if state.cancelled:
            raise asyncio.CancelledError()

    def pause(seconds):
        state.now += seconds
        guard()

    def click():
        guard()
        state.clicks.append(state.now)
        return {'success': True}

    def observe(started):
        guard()
        state.probes.append(state.now)
        session.frame = np.full((720, 1280, 3), (255, 0, 0), dtype=np.uint8)
        session.captured_at = 1000+state.now
        session.last = state.behavior()
        return session.last

    session = entry.TradePageEntry(kind='sell', app=None, vision=None, click=click,
                                   check_cancelled=guard, sleep=pause, context={'current_city': '岚心城'})
    monkeypatch.setattr(session, 'observe', observe)
    state.session, state.guard, state.pause = session, guard, pause
    state.behavior = lambda: observation('all', 'commit') if state.clicks else observation('shop')
    return state


@pytest.mark.parametrize('kind,keys', [('sell', ('all', 'commit')), ('sell', ('cancel', 'commit')),
                                      ('buy', ('buy',))])
def test_open_page_needs_two_frames_and_no_repeated_click(rig, kind, keys):
    rig.session.kind = kind
    rig.behavior = lambda: observation(*keys) if rig.clicks else observation('shop')
    result = rig.session.run()
    assert result['success'] and result['page_state'] == f'{kind}_page'
    assert len(rig.clicks) == 1
    assert len(rig.probes) == 4


def test_already_open_page_never_clicks(rig):
    rig.behavior = lambda: observation('all', 'commit')
    assert rig.session.run()['clicks'] == []
    assert not rig.clicks


def test_lost_first_click_retries_only_after_menu_and_cooldown(rig):
    rig.behavior = lambda: observation('all', 'commit') if len(rig.clicks) >= 2 else observation('shop')
    assert rig.session.run()['success']
    assert len(rig.clicks) == 2 and rig.clicks[1]-rig.clicks[0] >= entry.RETRY_SECONDS


def test_slow_transition_waits_without_clicking_unknown_page(rig):
    def page():
        if not rig.clicks:
            return observation('shop')
        return observation('all', 'commit') if rig.now-rig.clicks[0] >= 3 else observation()
    rig.behavior = page
    assert rig.session.run()['success'] and len(rig.clicks) == 1


@pytest.mark.parametrize('keys', [(), ('shop', 'commit'), ('shop', 'all', 'commit'),
                                  ('shop', 'buy'), ('all', 'commit', 'buy'), ('all', 'cancel', 'commit')])
def test_unknown_or_conflicting_page_never_authorizes_click(rig, keys):
    rig.behavior = lambda: observation(*keys)
    with pytest.raises(entry.TradePageEntryError) as caught:
        rig.session.run()
    assert caught.value.code == 'sell_page_open_unconfirmed'
    assert not rig.clicks and rig.now == pytest.approx(8.)


def test_partial_sell_controls_conflict_with_buy_marker(rig):
    rig.session.kind = 'buy'
    rig.behavior = lambda: observation('buy', 'commit')
    with pytest.raises(entry.TradePageEntryError) as caught:
        rig.session.run()
    assert caught.value.code == 'buy_page_open_unconfirmed' and not rig.clicks


def test_three_clicks_share_deadline_and_preserve_last_frame(rig):
    rig.behavior = lambda: observation('shop')
    with pytest.raises(entry.TradePageEntryError) as caught:
        rig.session.run()
    assert len(rig.clicks) == 3 and rig.now == pytest.approx(8.)
    assert caught.value.detail['context']['current_city'] == '岚心城'
    assert evidence_from(caught.value)[0]['screenshot'] == 'errors/test.png'
    assert len(rig.evidence) == 1
    image = cv2.imdecode(np.frombuffer(rig.evidence[0], np.uint8), cv2.IMREAD_COLOR)
    assert image[0, 0].tolist() == [0, 0, 255]  # RGB capture encoded faithfully.


def test_single_target_frame_does_not_confirm(rig):
    rig.behavior = lambda: observation('all', 'commit') if len(rig.probes) % 2 else observation()
    with pytest.raises(entry.TradePageEntryError):
        rig.session.run()
    assert not rig.clicks


def test_late_target_observation_cannot_confirm(rig):
    def page():
        if len(rig.probes) == 2:
            rig.now = 9.
        return observation('all', 'commit')
    rig.behavior = page
    with pytest.raises(entry.TradePageEntryError, match='未确认'):
        rig.session.run()
    assert not rig.clicks


@pytest.mark.parametrize('when', ['before', 'observe', 'sleep', 'click'])
def test_cancel_has_no_followup_click_or_failure_screenshot(rig, when):
    if when == 'before':
        rig.cancelled = True
    elif when == 'observe':
        def page():
            rig.cancelled = True
            return observation('shop')
        rig.behavior = page
    elif when == 'sleep':
        def pause(seconds):
            rig.cancelled = True
            rig.guard()
        rig.session.sleep = pause
    else:
        def click():
            rig.cancelled = True
            rig.guard()
        rig.session.click = click
    with pytest.raises(asyncio.CancelledError):
        rig.session.run()
    assert not rig.clicks and not rig.evidence


def test_input_failure_exits_immediately(rig):
    rig.session.click = lambda: NS(success=False)
    with pytest.raises(entry.TradePageEntryError) as caught:
        rig.session.run()
    assert caught.value.code == 'sell_page_input_failed' and rig.now < 1.


@pytest.mark.parametrize('result', [False, {'success': False}, NS(success=False)])
def test_fixed_coordinate_sender_rejects_reported_failure(result):
    with pytest.raises(trade.CityTradeFlowError) as caught:
        trade.resonance_pc_click_shop_menu_node(2, wait_sec=0., app=NS(click=lambda **kwargs: result))
    assert caught.value.code == 'shop_menu_input_failed'


@pytest.mark.parametrize('problem', ['capture', 'shape', 'exception', 'nan', 'debug'])
def test_capture_and_template_errors_never_allow_click(monkeypatch, problem):
    clicks = []
    frame = np.zeros((720, 1280, 3), np.uint8)
    app = NS(capture=lambda **kwargs: NS(success=problem != 'capture',
             image=frame[:10] if problem == 'shape' else frame))
    def find(**kwargs):
        if problem == 'exception':
            raise RuntimeError('bad template')
        return NS(found=False, confidence=float('nan') if problem == 'nan' else .1,
                  debug_info={'error': 'bad template'} if problem == 'debug' else {})
    monkeypatch.setattr(entry, 'save_evidence_png', lambda png: {'evidence_id': 'failure', 'status': 'saved'})
    with pytest.raises(entry.TradePageEntryError) as caught:
        entry.open_trade_page(kind='sell', app=app, vision=NS(find_template=find),
                              click=lambda: clicks.append(True), check_cancelled=lambda: None,
                              sleep=lambda seconds: None)
    assert caught.value.code == ('sell_page_capture_failed' if problem in ('capture', 'shape')
                                else 'sell_page_match_failed')
    assert not clicks


def test_each_observation_uses_one_frame_for_all_templates():
    captures, regions = [], []
    frame = np.zeros((720, 1280, 3), np.uint8)
    app = NS(capture=lambda **kwargs: captures.append(kwargs) or NS(success=True, image=frame))
    def find(**kwargs):
        regions.append(kwargs['source_image'])
        return NS(found=False, confidence=.1, debug_info={})
    session = entry.TradePageEntry(kind='buy', app=app, vision=NS(find_template=find),
                                   click=lambda: None, check_cancelled=lambda: None, sleep=lambda seconds: None)
    session.observe(entry.time.monotonic())
    assert len(captures) == 1 and len(regions) == len(entry.TARGETS)
    assert all(np.shares_memory(image, session.frame) for image in regions)
    assert not np.shares_memory(session.frame, frame)


@pytest.mark.parametrize('kind', ['sell', 'buy'])
def test_city_trade_stops_before_business_when_entry_unconfirmed(monkeypatch, kind):
    business = []
    monkeypatch.setattr(trade, '_enter_exchange', lambda **kwargs: {'menu_ready': {}})
    def open_page(**kwargs):
        if kwargs['kind'] == kind:
            raise trade.CityTradeFlowError(f'{kind}_page_open_unconfirmed', 'entry failed')
        return {'success': True, 'page_state': 'sell_page'}
    monkeypatch.setattr(trade, '_open_trade_page', open_page)
    monkeypatch.setattr(trade, 'resonance_pc_sell_goods_on_sell_page', lambda **kwargs:
                        business.append('sell') or {'success': True, 'page_state': 'shop_page'})
    monkeypatch.setattr(trade, 'resonance_pc_buy_goods_on_buy_page', lambda **kwargs: business.append('buy'))
    with pytest.raises(trade.CityTradeFlowError) as caught:
        trade._execute_city_trade_inside_current_city_scoped(
            current_city='岚心城', buy_products=['goods'], books_used=1,
            sell_raise_to_cap=False, buy_bargain_to_cap=False,
            app=object(), ocr=object(), vision=object(), city_shop_data=object())
    assert caught.value.code == f'{kind}_page_open_unconfirmed'
    assert business == ([] if kind == 'sell' else ['sell'])


def test_wrapper_preserves_source_evidence_and_uses_zero_wait(monkeypatch):
    clicks = []
    monkeypatch.setattr(trade, 'resonance_pc_click_shop_menu_node', lambda **kwargs: clicks.append(kwargs))
    def open_page(**kwargs):
        kwargs['click']()
        error = entry.TradePageEntryError('sell_page_open_unconfirmed', 'entry failed', {})
        error.failure_evidence = [{'evidence_id': 'entry-frame', 'screenshot': 'errors/test.png'}]
        raise error
    monkeypatch.setattr(trade, 'open_trade_page', open_page)
    with pytest.raises(trade.CityTradeFlowError) as caught:
        trade._open_trade_page(kind='sell', app=object(), vision=object())
    assert clicks[0]['node_index'] == 2 and clicks[0]['wait_sec'] == 0.
    assert evidence_from(caught.value)[0]['evidence_id'] == 'entry-frame'


def test_logging_delay_cannot_authorize_a_late_click(rig, monkeypatch):
    def log(*args, **kwargs):
        if len(rig.probes) == 2:
            rig.now = 9.
    monkeypatch.setattr(entry, 'logger', NS(info=log))
    rig.behavior = lambda: observation('shop')
    with pytest.raises(entry.TradePageEntryError):
        rig.session.run()
    assert not rig.clicks


def test_real_framework_saves_the_observed_frame_under_session(rig, monkeypatch, tmp_path):
    from packages.aura_core.observability import failure_evidence
    monkeypatch.setattr(failure_evidence, 'get_log_session', lambda:
                        NS(root=tmp_path, session_id='page-entry-test', available=True))
    monkeypatch.setattr(entry, 'save_evidence_png', failure_evidence.save_evidence_png)
    rig.behavior = lambda: observation()
    with pytest.raises(entry.TradePageEntryError) as caught:
        rig.session.run()
    ref = evidence_from(caught.value)[0]
    path = tmp_path / ref['screenshot']
    assert ref['status'] == 'saved' and path.parent == tmp_path/'errors'/'page-entry-test'
    assert cv2.imread(str(path)).shape == (720, 1280, 3)
    assert len(list(tmp_path.rglob('*.png'))) == 1


class NativeTemplateVision:
    """Use actual template pixels for observation regression, without game input."""
    def find_template(self, *, source_image, template_image, threshold, **kwargs):
        template = cv2.imread(template_image, cv2.IMREAD_GRAYSCALE)
        score = cv2.minMaxLoc(cv2.matchTemplate(cv2.cvtColor(source_image, cv2.COLOR_RGB2GRAY),
                                              template, cv2.TM_CCOEFF_NORMED))[1]
        return NS(found=score >= threshold, confidence=score, debug_info={})


@pytest.mark.parametrize('name,expected', [('shop', 'menu'), ('initial', 'unknown'),
                                         ('preview_10', 'unknown'), ('success', 'unknown')])
def test_native_exchange_and_obstructing_panels(name, expected):
    frame = cv2.cvtColor(cv2.imread(str(Path('tests/fixtures/trade_goods_investment')/f'{name}.png')),
                         cv2.COLOR_BGR2RGB)
    session = entry.TradePageEntry(kind='sell', app=NS(capture=lambda **kwargs: NS(success=True, image=frame)),
                                   vision=NativeTemplateVision(), click=lambda: None,
                                   check_cancelled=lambda: None, sleep=lambda seconds: None)
    assert session.state(session.observe(entry.time.monotonic()), 'sell') == expected


@pytest.mark.parametrize('kind,keys', [('sell', ('all', 'commit')), ('sell', ('cancel', 'commit')),
                                     ('buy', ('buy',))])
def test_page_markers_with_native_template_pixels(kind, keys):
    frame = np.zeros((720, 1280, 3), np.uint8)
    for key in keys:
        name, (x, y, _, _), _ = entry.TARGETS[key]
        template = cv2.cvtColor(cv2.imread(str(entry.ROOT/'templates'/name)), cv2.COLOR_BGR2RGB)
        h, w = template.shape[:2]
        frame[y:y+h, x:x+w] = template
    session = entry.TradePageEntry(kind=kind, app=NS(capture=lambda **kwargs: NS(success=True, image=frame)),
                                   vision=NativeTemplateVision(), click=lambda: None,
                                   check_cancelled=lambda: None, sleep=lambda seconds: None)
    assert session.state(session.observe(entry.time.monotonic()), kind) == 'target'
