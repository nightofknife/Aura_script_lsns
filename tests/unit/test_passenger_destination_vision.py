"""Real destination-card replay plus guarded, OCR-free passenger selection."""
from pathlib import Path
from types import SimpleNamespace as NS
import json

import numpy as np
from PIL import Image
import pytest

from plans.resonance_pc.src.actions import passenger_pc_actions as p
from plans.resonance_pc.src.actions import _passenger_destination_vision as d


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/passenger_destination'


def test_all_city_badges_on_actual_destination_list():
    image = np.array(Image.open(FIXTURE / 'user_destination_list.png').convert('RGB'))
    visible = set(json.loads((FIXTURE / 'sources.json').read_text('utf-8'))['fully_visible_city_keys'])
    found = {}
    for entry in d.load_catalog():
        result = d.probe_destination(image, entry, p._DESTINATION_CITY_REGION, lambda: None)
        if result['found']:
            found[entry['city_key']] = result
    assert set(found) == visible
    assert max(abs(a-b) for a,b in zip(found['clarity_data_center_administration_bureau']['center'], [776,450])) <= 3
    assert max(abs(a-b) for a,b in zip(found['brcl_outpost']['center'], [776,135])) <= 3


def test_background_templates_on_actual_second_stop_frame_reject_clipped_cards():
    image = np.array(Image.open(FIXTURE / 'freeport_to_clarity_list.png').convert('RGB'))
    source = json.loads((FIXTURE / 'freeport_to_clarity_sources.json').read_text('utf-8'))
    results = {row['city_key']: d.probe_destination(image, row, p._DESTINATION_CITY_REGION, lambda: None)
               for row in d.load_catalog()}
    assert {key for key, hit in results.items() if hit['found']} == set(source['fully_visible_city_keys'])
    assert all(not results[key]['found'] for key in source['clipped_city_keys'])
    target = results['clarity_data_center_administration_bureau']
    assert target['center'] == [1119, 200]
    assert target['confidence'] > .93


def test_plain_background_windows_do_not_create_false_hit_or_need_a_mask(monkeypatch):
    image = np.array(Image.open(FIXTURE / 'user_destination_list.png').convert('RGB'))
    image[:250, :112] = 0
    original = d.cv2.matchTemplate
    calls = []
    def match(*args, **kwargs):
        calls.append((len(args), kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(d.cv2, 'matchTemplate', match)
    present = d.probe_destination(image, d.resolve_destination('澄明数据中心'),
                                  p._DESTINATION_CITY_REGION, lambda: None)
    absent = d.probe_destination(image, d.resolve_destination('freeport'),
                                 p._DESTINATION_CITY_REGION, lambda: None)
    assert present['found'] and abs(present['center'][1] - 450) <= 3
    assert not absent['found']
    assert present['nonfinite_locations_ignored'] == 0
    assert calls == [(3, {}), (3, {})]
    assert all('mask' not in row for row in d.load_catalog())


@pytest.mark.parametrize('score', [float('nan'), float('inf')])
def test_invalid_match_map_does_not_supply_click(monkeypatch, score):
    monkeypatch.setattr(d.cv2, 'matchTemplate', lambda *a, **k: np.full((391, 601), score, np.float32))
    with pytest.raises(d.DestinationVisionError, match='No usable'):
        d.probe_destination(np.zeros((500, 710, 3), np.uint8), d.resolve_destination('freeport'),
                            p._DESTINATION_CITY_REGION, lambda: None)


@pytest.fixture
def search(monkeypatch):
    state = NS(clicks=[], drags=[], hits=[], captures=0)
    monkeypatch.setattr(p, '_check_cancelled', lambda: None)
    monkeypatch.setattr(p.time, 'sleep', lambda _: None)
    monkeypatch.setattr(p, '_capture_text_items', lambda *a, **k: pytest.fail('destination used OCR'))
    def capture(*a):
        state.captures += 1
        return np.zeros((500, 710, 3), np.uint8)
    monkeypatch.setattr(d, 'capture_list', capture)
    monkeypatch.setattr(d, 'probe_destination', lambda *a: state.hits.pop(0))
    monkeypatch.setattr(p, '_drag', lambda app, start, end: state.drags.append((start, end)))
    state.run = lambda steps=12: p._select_destination_city('七号自由港',
        NS(click=lambda **kw: state.clicks.append(kw)), max_search_steps=steps)
    return state


def test_selection_requires_two_stable_positions_and_uses_no_ocr(search):
    search.hits = [{'found': True, 'center': [738, 300]}, {'found': True, 'center': [737, 301]}]
    result = search.run()
    assert result['success'] and result['confirmations'] == 2
    assert search.clicks == [{'x': 737, 'y': 301}] and not search.drags


def test_moving_or_disappearing_hit_is_not_clicked_or_scrolled(search):
    search.hits = [{'found': True, 'center': [738, 300]}, {'found': True, 'center': [738, 320]},
                   {'found': True, 'center': [738, 320]}, {'found': False}]
    with pytest.raises(p.PassengerPcError, match='destination_not_found'):
        search.run(steps=2)
    assert not search.clicks and not search.drags


def test_visual_list_boundary_reverses_search_then_stops(search):
    search.hits = [{'found': False}] * 12
    with pytest.raises(p.PassengerPcError, match='destination_not_found'):
        search.run()
    assert search.drags == [p._DESTINATION_DRAG_UP] * 2 + [p._DESTINATION_DRAG_DOWN] * 3
    assert not search.clicks and search.captures == 6


def test_unknown_destination_fails_before_capture(monkeypatch):
    monkeypatch.setattr(d, 'capture_list', lambda *a: pytest.fail('unknown city searched'))
    with pytest.raises(p.PassengerPcError, match='destination_not_found'):
        p._select_destination_city('unknown city', object(), max_search_steps=12)


def test_drag_holds_endpoint_half_second_before_release(monkeypatch):
    calls = []
    monkeypatch.setattr(p, '_check_cancelled', lambda: None)
    monkeypatch.setattr(p.time, 'sleep', lambda _: None)
    p._drag(NS(move_to=lambda **k: None, drag=lambda **k: calls.append(k)),
            (1160, 525), (1160, 180))
    assert calls[0]['hold_before_release_sec'] == .5
    assert (calls[0]['end_x'], calls[0]['end_y']) == (1160, 180)


def test_failed_capture_is_not_treated_as_list_boundary():
    with pytest.raises(d.DestinationVisionError, match='capture'):
        d.capture_list(NS(capture=lambda **k: NS(success=False)), p._DESTINATION_CITY_REGION, lambda: None)
