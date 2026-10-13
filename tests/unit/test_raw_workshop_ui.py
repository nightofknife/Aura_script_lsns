"""Bounded UI integration checks; never run a game or repeat the digit suite.

Fixtures are unchanged original client crops. Reserve-selected polarity and
maximum-slider geometry tests below are synthetic, not live-state evidence.
"""
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _raw_workshop_ui as ui

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures/raw_workshop_ui'
DIGITS = Path(__file__).resolve().parents[1] / 'fixtures/raw_workshop_digits'
RECORDS = {r['name']: r for r in json.loads((FIXTURES / 'manifest.json').read_text(encoding='utf-8'))['records']}
ROOM = '植物纤维原料车间'


def frame(name):
    record = RECORDS[name]
    binary = (FIXTURES / record['image']).read_bytes()
    assert hashlib.sha256(binary).hexdigest() == record['sha256']
    return cv2.imdecode(np.frombuffer(binary, np.uint8), cv2.IMREAD_COLOR)


@pytest.mark.parametrize('name,production,enabled,work', [
    ('idle_blue', 'idle', True, '0/7'),
    ('idle_gray', 'idle', False, '0/7'),
    ('staff_full', 'idle', True, '7/7'),
    ('running', 'running', False, '7/7'),
])
def test_original_snapshots(name, production, enabled, work):
    image = frame(name)
    state = ui.snapshot(image, ROOM)
    assert state is not None
    assert state['production'] == production
    assert state['start_enabled'] is enabled
    assert state['work']['text'] == work
    assert state['reserve']['text'] == '0/2'
    assert state['tab'] == 'work'
    assert ui.room_ready(image, ROOM)
    assert not ui.room_ready(image, '动物纤维原料车间')
    if production == 'running':
        assert not ui.control_visible(image, 'choose_prisoners')


@pytest.mark.parametrize('name,outcome', [('deposit_dialog', 'dialog'), ('deposit_full', 'already_full')])
def test_original_deposit_outcomes(name, outcome):
    image = frame(name)
    assert ui.deposit_outcome(image, ROOM) == outcome
    if outcome == 'dialog':
        assert not ui.room_ready(image, ROOM)
        assert ui.control_enabled(image, 'deposit_confirm')
        assert not ui.deposit_maximum(image)
    else:
        assert ui.deposit_outcome(image, '动物纤维原料车间') is None


@pytest.mark.parametrize('name,button,count,marker', [
    ('selection_empty', 'select_all', '0/7', False),
    ('selection_checked', 'cancel_all', '7/7', True),
])
def test_original_selection_states(name, button, count, marker):
    image = frame(name)
    state = ui.selection_state(image)
    assert state['button'] == button
    assert state['count']['text'] == count
    assert state['confirm_enabled']
    assert state['selected_marker'] is marker
    assert not ui.room_ready(image, ROOM)


@pytest.mark.parametrize('missing', ['deposit_currency', 'deposit_min_marker', 'deposit_max', 'deposit_confirm', 'deposit_cancel'])
def test_incomplete_modal_does_not_authorize_deposit_click(missing):
    image = frame('deposit_dialog')
    x, y, w, h = ui._specs()[missing]['client_bbox_xywh']
    image[y-6:y+h+6, x-8:x+w+8] = 0
    assert ui.deposit_outcome(image, ROOM) is None
    assert not ui.deposit_maximum(image)


def test_blank_unknown_counts_and_wrong_frame_size_fail_closed():
    blank = np.zeros((720, 1280, 3), np.uint8)
    assert ui.snapshot(blank, ROOM) is None
    assert ui.production_state(blank) is None
    assert ui.selection_state(blank) is None
    assert ui.deposit_outcome(blank, ROOM) is None
    assert not ui.control_visible(blank, 'deposit_button')
    for bad in (None, np.zeros((710, 1280, 3), np.uint8), np.zeros((720, 1280), np.uint8)):
        assert ui.snapshot(bad, ROOM) is None
        assert not ui.control_enabled(bad, 'start')
        assert not ui.deposit_maximum(bad)
    image = frame('idle_gray')
    image[282:310, 735:815] = 255
    assert ui.snapshot(image, ROOM) is None  # unknown never means 0/full


def test_color_is_required_for_enabled_and_running_not_gray_identity_only():
    image = frame('idle_blue')
    gray = cv2.cvtColor(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    assert ui.control_visible(gray, 'start')
    assert ui.production_state(gray) == 'idle'
    assert not ui.control_enabled(gray, 'start')
    running = frame('running')
    running_gray = cv2.cvtColor(cv2.cvtColor(running, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    assert ui.production_state(running_gray) is None


def _set_fraction_synthetic(image, label, text, *, selected):
    """Synthetic counter-layout integration, using pre-existing native glyphs."""
    hit = ui._labels(image)[label]
    x, y, w, h = hit['rect']
    shade = 255 if selected else 20
    image[y+2:y+h-2, x+w-1:x+w+79] = shade
    color = 'black' if selected else 'white'
    sample = cv2.imread(str(DIGITS / f'synth_list_{color}_{text.replace("/", "_")}.png'))
    ink = ui.digits.foreground(sample)
    _, mask = cv2.threshold(ink, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    points = cv2.findNonZero(mask)
    sx, sy, sw, sh = cv2.boundingRect(points)
    glyph = ink[sy:sy+sh, sx:sx+sw]
    glyph = cv2.cvtColor(glyph, cv2.COLOR_GRAY2BGR)
    glyph = 255-glyph if selected else glyph
    image[y+7:y+7+sh, x+w+3:x+w+3+sw] = glyph


def test_synthetic_two_digit_counts_and_shifted_label_not_fixed_7_7_roi():
    image = frame('idle_gray')
    # Layout group shifts left as the centered label+fraction widens.
    x, y, w, h = ui._labels(image)['work']['rect']
    original = image[y:y+h, x:x+w].copy()
    image[y:y+h, x-12:x+w] = 255
    image[y:y+h, x-12:x-12+w] = original
    _set_fraction_synthetic(image, 'work', '15/15', selected=True)
    _set_fraction_synthetic(image, 'reserve', '10/10', selected=False)
    state = ui.snapshot(image, ROOM)
    assert state is not None
    assert state['work']['text'] == '15/15'
    assert state['reserve']['text'] == '10/10'
    assert state['work']['full'] and state['reserve']['full']


def test_synthetic_reserve_tab_polarity_no_fabricated_runtime_asset():
    image = frame('idle_gray')
    for hit in ui._labels(image).values():
        x, y, w, h = hit['rect']
        image[y:y+h, x:x+w] = 255-image[y:y+h, x:x+w]
    _set_fraction_synthetic(image, 'work', '15/15', selected=False)
    _set_fraction_synthetic(image, 'reserve', '10/10', selected=True)
    state = ui.snapshot(image, ROOM)
    assert state is not None and state['tab'] == 'reserve'
    assert state['work']['text'] == '15/15'
    assert state['reserve']['text'] == '10/10'


@pytest.mark.parametrize('knob_center,expected', [(12, False), (145, False), (273, True)])
def test_synthetic_slider_geometry_not_live_maximum_evidence(knob_center, expected):
    image = frame('deposit_dialog')
    image[375:405, 495:785] = 20
    image[388:391, 501:774] = 255
    image[377:402, 495+knob_center-6:495+knob_center+7] = 255
    assert ui.deposit_maximum(image) is expected


def test_missing_selection_confirmation_or_fraction_fails_closed():
    image = frame('selection_empty')
    image[650:690, 1110:1210] = 0
    assert ui.selection_state(image) is None
    image = frame('selection_empty')
    image[650:685, 175:255] = 0
    assert ui.selection_state(image) is None


def test_return_overview_state_reuses_saved_map_without_outline_detection(monkeypatch):
    path = FIXTURES.parent / "reforming_center_templates/safety_boundary_20261013/animal_left_limit.png"
    image = cv2.imread(str(path))
    monkeypatch.setattr(ui.rooms, "selected_rectangles", lambda *args: pytest.fail("No outline checks requested"))
    state = ui.overview_state(image)
    assert state["page"] == "reforming_center_overview"
    assert "动物纤维原料车间" in state["anchors"]
    assert state == ui.overview_state(image.copy())
    assert "score" not in str(state)


@pytest.mark.parametrize("name", ["idle_gray", "running", "deposit_dialog", "selection_empty"])
def test_return_overview_state_rejects_workshop_and_overlay_pages(name):
    assert ui.overview_state(frame(name)) is None


def test_return_overview_state_rejects_blank_and_bad_shape():
    assert ui.overview_state(np.zeros((720, 1280, 3), np.uint8)) is None
    assert ui.overview_state(None) is None
