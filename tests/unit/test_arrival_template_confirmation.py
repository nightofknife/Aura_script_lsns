"""Offline arrival input must be confirmed by valid negative observations."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import numpy as np
import pytest

from plans.resonance_pc.src.actions import city_travel_pc_actions as travel


REGION = [780, 325, 240, 70]


def capture(*, success=True, image=None):
    return NS(success=success, image=np.zeros((70, 240, 3), np.uint8) if image is None else image)


def match(found=True, center=(60, 30), **changes):
    values = dict(found=found, confidence=.98, center_point=center,
                  rect=(40, 20, 40, 20), debug_info={})
    values.update(changes)
    return NS(**values)


class App:
    def __init__(self, captures=()):
        self.frames = list(captures)
        self.clicks = []
        self.captures = []
        self.after_capture = lambda: None
        self.after_click = lambda: None

    def capture(self, *, rect):
        self.captures.append(rect)
        value = self.frames.pop(0) if self.frames else capture()
        self.after_capture()
        if isinstance(value, BaseException):
            raise value
        return value

    def click(self, **point):
        self.clicks.append(point)
        self.after_click()


class Vision:
    def __init__(self, *matches):
        self.matches = list(matches)
        self.calls = []
        self.after_match = lambda: None

    def find_template(self, **options):
        self.calls.append(options)
        value = self.matches.pop(0) if self.matches else match(False)
        self.after_match()
        if isinstance(value, BaseException):
            raise value
        return value


def arrive(app, vision, **changes):
    options = dict(poll_count=4, template='templates/navigation/travel/enter_station.png',
                   region=REGION, threshold=.9, max_click_attempts=3, verify_interval_sec=0)
    options.update(changes)
    return travel._click_arrival_template_until_absent(app, vision, **options)


def test_initial_valid_absence_never_clicks_or_confirms():
    app = App()
    result = arrive(app, Vision(match(False)))
    assert result == dict(confirmed=False, initial_match=result['last_match'],
                          last_match=result['last_match'], click_attempts=0, clicks=[])
    assert not app.clicks


def test_success_requires_two_valid_absent_frames_and_preserves_offsets_and_options():
    app, vision = App(), Vision(match(), match(False), match(False))
    result = arrive(app, vision)
    assert result['confirmed'] and result['click_attempts'] == 1
    assert result['arrival_point'] == {'x': 840, 'y': 355}
    assert app.clicks == [{'x': 840, 'y': 355}]
    assert len(app.captures) == len(vision.calls) == 3
    assert all(rect == tuple(REGION) for rect in app.captures)
    assert all(call['threshold'] == .9 and call['use_grayscale'] is True for call in vision.calls)
    assert result['initial_match']['rect'] == [820, 345, 40, 20]
    assert result['last_match']['found'] is False


@pytest.mark.parametrize('between', [[match(center=(80, 35))],
                                     [match(False), match(center=(80, 35))]])
def test_supplement_relocates_from_fresh_visible_button(between):
    app = App()
    result = arrive(app, Vision(match(), *between, match(False), match(False)))
    assert result['confirmed'] and result['click_attempts'] == 2
    assert app.clicks == [{'x': 840, 'y': 355}, {'x': 860, 'y': 360}]


def test_still_present_button_preserves_click_limit_and_failure_details():
    app = App()
    with pytest.raises(travel.IntercityDestinationError) as caught:
        arrive(app, Vision(*(match() for _ in range(4))))
    assert caught.value.code == 'enter_station_click_not_effective'
    assert len(app.clicks) == len(caught.value.detail['clicks']) == 3
    assert caught.value.detail['poll_count'] == 4


@pytest.mark.parametrize('stage', [0, 1, 2], ids=['initial', 'after_click', 'second_absence'])
@pytest.mark.parametrize('failure', [NS(success=False, image=None), RuntimeError('capture backend failed')],
                         ids=['unsuccessful', 'exception'])
def test_capture_failure_never_confirms_or_reclicks(stage, failure):
    app = App([*(capture() for _ in range(stage)), failure])
    vision = Vision(match(), match(False))
    with pytest.raises(travel.IntercityDestinationError) as caught:
        arrive(app, vision)
    assert caught.value.code == 'arrival_capture_failed'
    assert caught.value.detail['region'] == REGION
    assert len(app.clicks) == (0 if stage == 0 else 1)
    assert len(vision.calls) == stage


@pytest.mark.parametrize('image', [None, [], np.zeros((70, 240), np.uint8),
                                  np.zeros((71, 240, 3), np.uint8),
                                  np.zeros((70, 240, 3), np.float32)])
def test_invalid_capture_stops_after_input_without_matching_or_success(image):
    app = App([capture(), NS(success=True, image=image)])
    vision = Vision(match())
    with pytest.raises(travel.IntercityDestinationError, match='arrival_capture_failed'):
        arrive(app, vision)
    assert len(app.clicks) == len(vision.calls) == 1


@pytest.mark.parametrize('failure', [RuntimeError('matching failed'),
                                    match(False, confidence=float('nan')),
                                    match(False, confidence=float('inf')),
                                    match(False, debug_info={'error': 'template_load_failed'}),
                                    match(None), match('false'),
                                    match(False, center=(float('nan'), 10)),
                                    match(False, center=('1.5', 10)),
                                    match(False, rect=(0, 0, float('inf'), 10)),
                                    match(False, debug_info=None),
                                    NS(found=False)])
def test_invalid_match_cannot_be_used_as_absence(failure):
    app = App()
    with pytest.raises(travel.IntercityDestinationError, match='arrival_template_match_failed'):
        arrive(app, Vision(match(), failure))
    assert len(app.clicks) == 1


def test_missing_center_preserves_existing_structured_error_without_input():
    app = App()
    with pytest.raises(travel.IntercityDestinationError) as caught:
        arrive(app, Vision(match(center=None)))
    assert caught.value.code == 'arrival_template_center_missing'
    assert not app.clicks


def test_unknown_probe_never_authorizes_input_or_confirms(monkeypatch):
    flags = []

    def unknown(**options):
        flags.append(options['require_valid_capture'])
        return {'found': False, 'reason': 'capture_failed'}

    monkeypatch.setattr(travel, '_match_template_in_region', unknown)
    app = App()
    with pytest.raises(travel.IntercityDestinationError, match='arrival_template_match_failed'):
        arrive(app, Vision())
    assert flags == [True] and not app.clicks


@pytest.mark.parametrize('stage', ['before', 'capture', 'match', 'click', 'final_match', 'wait'])
def test_cancellation_never_confirms_or_supplements(monkeypatch, stage):
    cancelled = stage == 'before'
    monkeypatch.setattr(travel, 'is_current_task_cancel_requested', lambda: cancelled)
    app, vision = App(), Vision(match(), match(False), match(False))

    def cancel():
        nonlocal cancelled
        cancelled = True

    if stage == 'capture':
        app.after_capture = cancel
    elif stage == 'match':
        vision.after_match = cancel
    elif stage == 'click':
        app.after_click = cancel
    elif stage == 'final_match':
        vision.after_match = lambda: cancel() if len(vision.calls) == 3 else None
    elif stage == 'wait':
        monkeypatch.setattr(travel.time, 'sleep', lambda seconds: cancel())
    with pytest.raises(asyncio.CancelledError):
        arrive(app, vision, verify_interval_sec=.2 if stage == 'wait' else 0)
    assert len(app.clicks) == (1 if stage in {'click', 'final_match', 'wait'} else 0)


def test_input_exception_is_propagated_without_verification_or_retry():
    app, vision = App(), Vision(match())

    def reject():
        raise RuntimeError('input backend failed')

    app.after_click = reject
    with pytest.raises(RuntimeError, match='input backend failed'):
        arrive(app, vision)
    assert len(app.clicks) == len(vision.calls) == 1


def test_other_matcher_callers_keep_default_capture_failure_contract():
    vision = Vision()
    result = travel._match_template_in_region(App([NS(success=False)]), vision, 'fixture.png', REGION, .9)
    assert result == dict(found=False, template='fixture.png', region=REGION, reason='capture_failed')
    assert not vision.calls


def test_other_matcher_callers_keep_exception_propagation():
    with pytest.raises(RuntimeError, match='capture backend failed'):
        travel._match_template_in_region(App([RuntimeError('capture backend failed')]),
                                         Vision(), 'fixture.png', REGION, .9)
