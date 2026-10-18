"""Calibrated native-template replay; fixtures contain only list/footer pixels."""
from pathlib import Path
import json

import numpy as np
import pytest
from PIL import Image
import yaml

from packages.aura_core.scheduler.validation import InputValidator

from plans.resonance_pc.src.actions._black_moon_local_select_vision import BlackMoonLocalSelectVision, _pixels

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures/black_moon_select'


def frame(name):
    result = np.zeros((720, 1280, 3), np.uint8)
    result[143:643, 594:1278] = np.asarray(Image.open(FIXTURES / f'{name}_list.png').convert('RGB'))
    result[646:694, 1000:1264] = np.asarray(Image.open(FIXTURES / f'{name}_footer.png').convert('RGB'))
    return result


def test_current_real_goods_detected_in_independent_cards():
    reader = BlackMoonLocalSelectVision(['inferior_birch_stone', 'purchase_order_book'])
    image = frame('current')
    cards = reader.cards(image)
    hits = [reader.identify(image, card) for card in cards]
    assert len(cards) in (7, 8)
    assert sum(hit is not None and hit['item_id'] == 'inferior_birch_stone' for hit in hits) == 2
    assert sum(hit is not None and hit['item_id'] == 'purchase_order_book' for hit in hits) == 2
    assert all(card['state'] == 'unselected' for card in cards if not card['partial'])


def test_selected_badge_does_not_make_an_unwanted_card_a_target():
    reader = BlackMoonLocalSelectVision(['purchase_order_book'])
    image = frame('selected')
    cards = reader.cards(image)
    assert sum(card['state'] == 'selected' for card in cards) == 1
    selected = next(card for card in cards if card['state'] == 'selected')
    # This historical selected card is a staff, not a purchase order book.
    assert reader.identify(image, selected) is None
    assert reader.selected_mark(image, selected)['found']
    assert all(not reader.selected_mark(image, card)['found'] for card in cards
               if card['state'] == 'unselected')


def test_selected_badge_moves_with_card_and_is_not_found_on_another_card():
    reader = BlackMoonLocalSelectVision(['purchase_order_book'])
    source = frame('selected')
    selected = next(card for card in reader.cards(source) if card['state'] == 'selected')
    x, y, width, height = selected['rect']
    target = {**selected, 'rect': (600, 450, width, height)}
    moved = np.zeros_like(source)
    moved[450:450 + height, 600:600 + width] = source[y:y + height, x:x + width]
    assert reader.selected_mark(moved, target)['found']
    assert not reader.selected_mark(moved, selected)['found']
    assert not reader.selected_mark(moved, {**target, 'rect': (600, 150, width, height)})['found']


def test_selected_target_identity_is_preserved_when_native_badge_is_present():
    reader = BlackMoonLocalSelectVision(['purchase_order_book'])
    image = frame('current')
    target = next(card for card in reader.cards(image) if reader.identify(image, card))
    x, y, _, _ = target['rect']
    rx, ry, _, _ = reader.meta['selected']['roi_relative']
    badge = _pixels(reader.root, reader.meta['selected']['file'])
    height, width = badge.shape[:2]
    image[y + ry:y + ry + height, x + rx:x + rx + width] = badge
    target = {**target, 'state': 'selected'}
    assert reader.selected_mark(image, target)['found']
    assert reader.identify(image, target)['item_id'] == 'purchase_order_book'


def test_footer_zero_negative_and_purchase_control():
    reader = BlackMoonLocalSelectVision()
    assert reader.total_zero(frame('current'))
    assert not reader.total_zero(frame('selected'))
    assert reader.button(frame('current'))['found']
    assert reader.button(frame('selected'))['found']


@pytest.mark.parametrize('item', ['task_commission_letter', 'order_request'])
def test_disabled_advertising_ticket_is_not_bought_as_similar_paper(item):
    reader = BlackMoonLocalSelectVision([item])
    image = frame('selected')
    card = next(card for card in reader.cards(image) if card['rect'][0] == 938 and abs(card['rect'][1] - 276) <= 2)
    assert reader.identify(image, card) is None


def test_large_bank_coarse_refinement_keeps_real_targets():
    known = BlackMoonLocalSelectVision().known_ids
    reader = BlackMoonLocalSelectVision(known)
    image = frame('current')
    hits = [reader.identify(image, card) for card in reader.cards(image)]
    assert sum(hit is not None and hit['item_id'] == 'inferior_birch_stone' for hit in hits) == 2
    assert sum(hit is not None and hit['item_id'] == 'purchase_order_book' for hit in hits) == 2


def test_zero_and_blank_pixels_do_not_establish_cards_or_selection():
    reader = BlackMoonLocalSelectVision(['purchase_order_book'])
    image = np.zeros((720, 1280, 3), np.uint8)
    assert not reader.cards(image)
    assert not reader.total_zero(image)
    assert not reader.button(image)['found']


def test_all_native_templates_are_recalled_by_coarse_stage():
    base = BlackMoonLocalSelectVision()
    reader = BlackMoonLocalSelectVision(base.known_ids)
    card = {'rect': (600, 210, 327, 120), 'partial': False, 'state': 'unselected'}
    image = np.zeros((720, 1280, 3), np.uint8)
    for key, entry in reader.entries.items():
        image.fill(0)
        template = _pixels(reader.root, entry['file'])
        image[223:319, 634:730] = template
        hit = reader.identify(image, card)
        assert hit is not None and hit['item_id'] == key, key


def test_framework_accepts_complete_purchase_table_and_defaults_missing_to_false():
    root = Path(__file__).resolve().parents[2]
    task = yaml.safe_load((root / 'plans/resonance_pc/tasks/black_moon_local_select_pc.yaml').read_text(encoding='utf-8'))['black_moon_local_select_pc']
    catalog = json.loads((root / 'plans/resonance_pc/data/meta/black_moon_local_shop_catalog.json').read_text(encoding='utf-8'))
    table = {row['item_id']: row['item_id'] in {'inferior_birch_stone', 'purchase_order_book'} for row in catalog['products']}
    validator = InputValidator(None)
    ok, values = validator.validate_inputs_against_meta(task['meta']['inputs'], {'purchase_table': table, 'submit_purchase': True})
    assert ok and values['purchase_table'] == table
    ok, values = validator.validate_inputs_against_meta(task['meta']['inputs'], {'purchase_table': {'purchase_order_book': True}})
    assert ok and sum(values['purchase_table'].values()) == 1
    assert values['submit_purchase'] is False
    assert not validator.validate_inputs_against_meta(task['meta']['inputs'], {'purchase_table': {'unknown': True}})[0]
