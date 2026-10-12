"""Offline exits: actual control crops with explicitly synthetic surroundings."""
from pathlib import Path
import json

import numpy as np
from PIL import Image
import pytest
import yaml

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions._black_moon_local_exit_vision import BlackMoonLocalExitVision
from plans.resonance_pc.src.actions import black_moon_local_select_pc_actions as selection

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
ROOT = Path(__file__).resolve().parents[2]


def put(frame, path, rect):
    x, y, w, h = rect
    frame[y:y+h, x:x+w] = np.asarray(Image.open(path).convert('RGB'))


def shop(name):
    result = np.zeros((720, 1280, 3), np.uint8)
    put(result, FIXTURES / 'black_moon_exit' / f'{name}_header.png', (584, 53, 695, 94))
    put(result, FIXTURES / 'black_moon_exit' / f'{name}_footer.png', (1000, 646, 264, 48))
    put(result, FIXTURES / 'black_moon_exit/navigation.png', (0, 0, 270, 80))
    return result


def test_settled_shop_has_zero_total_and_both_exit_buttons():
    reader = BlackMoonLocalExitVision()
    frame = shop('settled')
    assert reader.observe(frame) == {'page_state': 'local_shop', 'shop_ready': True, 'total_zero': True}
    back = reader.button(frame, 'rest_menu')
    main = reader.button(frame, 'main')
    assert back['found'] and back['score'] >= .9
    assert main['found'] and main['score'] >= .9
    assert back['center'][0] < main['center'][0]


def test_actual_pending_shop_does_not_authorize_exit():
    state = BlackMoonLocalExitVision().observe(shop('pending'))
    assert not (state['shop_ready'] and state['total_zero'])


def rest():
    frame = np.zeros((720, 1280, 3), np.uint8)
    meta = json.loads((FIXTURES / 'black_moon_prepare/manifest.json').read_text(encoding='utf-8'))
    for patch in meta['frames'][0]['patches']:
        put(frame, FIXTURES / 'black_moon_prepare' / patch['file'], patch['rect'])
    return frame


def main_frame():
    frame = np.zeros((720, 1280, 3), np.uint8)
    put(frame, FIXTURES / 'depart_button/actual-matched-crop.png', (1150, 641, 106, 42))
    return frame


@pytest.mark.parametrize('factory,target', [(rest, 'rest_menu'), (main_frame, 'main')])
def test_actual_destination_crops_establish_target(factory, target):
    assert BlackMoonLocalExitVision().observe(factory())['page_state'] == target


def test_conflicting_shop_and_destination_evidence_is_unknown():
    frame = shop('settled')
    put(frame, FIXTURES / 'depart_button/actual-matched-crop.png', (1150, 641, 106, 42))
    assert BlackMoonLocalExitVision().observe(frame)['page_state'] == 'unknown'


def test_blank_and_dimmed_loading_frames_are_not_settled():
    reader = BlackMoonLocalExitVision()
    for frame in (np.zeros((720, 1280, 3), np.uint8), (shop('settled') * .5).astype(np.uint8)):
        assert reader.observe(frame)['page_state'] == 'unknown'
        assert not reader.button(frame, 'rest_menu')['found']


@pytest.mark.parametrize('target', ['rest_menu', 'main'])
def test_task_accepts_two_targets_and_defaults_to_rest_menu(target):
    task = yaml.safe_load((ROOT / 'plans/resonance_pc/tasks/black_moon_local_select_pc.yaml').read_text(encoding='utf-8'))['black_moon_local_select_pc']
    validator = InputValidator(None)
    inputs = {'purchase_table': {'purchase_order_book': False}, 'exit_target': target}
    ok, values = validator.validate_inputs_against_meta(task['meta']['inputs'], inputs)
    assert ok and values['exit_target'] == target
    ok, values = validator.validate_inputs_against_meta(task['meta']['inputs'], {'purchase_table': {'purchase_order_book': False}})
    assert ok and values['exit_target'] == 'rest_menu'
    assert not validator.validate_inputs_against_meta(task['meta']['inputs'], {**inputs, 'exit_target': 'wrong'})[0]
    assert task['steps']['wait_purchase']['when'] == '{{ inputs.submit_purchase }}'
    assert task['steps']['exit_shop']['when'] == '{{ inputs.submit_purchase }}'
    assert task['steps']['exit_shop']['params']['purchase_result'] == '{{ nodes.wait_purchase.output }}'
    assert 'nodes.exit_shop.output.success' in task['returns']['success']


def test_action_rejects_invalid_destination_before_input():
    with pytest.raises(ValueError):
        selection.resonance_pc_validate_black_moon_purchase_table({'purchase_order_book': True}, 'wrong')
