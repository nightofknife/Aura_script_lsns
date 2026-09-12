"""Target-only matching contracts, including an offline real screenshot."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest
from PIL import Image

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions._bento_target_locator import find_target_on_frame
from plans.resonance_pc.src.actions.love_bento_pc_actions import load_love_bento_catalog


@pytest.fixture(scope='module')
def real_catalog():
    vision = VisionService()
    vision._submit_to_loop_and_wait = asyncio.run
    return load_love_bento_catalog(vision)


@pytest.fixture
def catalog(real_catalog):
    return copy.deepcopy(real_catalog)


def cached_meal(catalog, role_id=10000071, days=5):
    role = next(row for row in catalog['roles'] if row['id'] == role_id)
    return {'kind': 'love_bentos', 'role_id': role['id'],
            'food_id': role['food_ids'][0], 'remaining_days': days}


def hit(point=None, score=1.0, found=True, error=None):
    return NS(top_left=point, confidence=score, found=found,
              debug_info={'error': error} if error else {})


class FakeVision:
    def __init__(self, points=((15, 236),)):
        self.anchors = NS(matches=[hit(point) for point in points], debug_info={})
        self.result = hit()
        self.calls = []

    def find_all_templates(self, **kwargs):
        self.calls.append(kwargs)
        return self.anchors

    def find_template(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
def frame(catalog):
    _, _, w, h = catalog['scanner']['capture_roi']
    return np.zeros((h, w, 3), dtype=np.uint8)


@pytest.mark.parametrize('role_id,food_id,days,expected', [
    (10000071, 83300015, 5, [15, 236]),
    (10000112, 83300014, 3, [250, 236]),
    (10000389, 83300014, 3, [484, 236]),
])
def test_real_cached_target_positions(catalog, role_id, food_id, days, expected):
    vision = VisionService()
    vision._submit_to_loop_and_wait = asyncio.run
    x, y, w, h = catalog['scanner']['capture_roi']
    with Image.open(Path('tests/fixtures/bento_consumption/love_usable.png')) as image:
        frame = np.array(image.convert('RGB').crop((x, y, x+w, y+h)))
    meal = cached_meal(catalog, role_id, days)
    assert meal['food_id'] == food_id
    before = copy.deepcopy((catalog, meal))
    assert find_target_on_frame(frame, meal, catalog, vision) == expected
    assert (catalog, meal) == before
    assert find_target_on_frame(frame, {**meal, 'remaining_days': 9}, catalog, vision) is None


def test_only_requested_templates_and_exact_matching_options(catalog, frame):
    vision = FakeVision()
    meal = cached_meal(catalog)
    before = frame.copy()
    assert find_target_on_frame(frame, meal, catalog, vision) == [15, 236]
    rows = [next(r for r in catalog[section] if r[key] == value)
            for section, key, value in [('items', 'id', meal['food_id']),
                                       ('roles', 'id', meal['role_id']), ('days', 'value', 5)]]
    assert [call['template_image'] for call in vision.calls] == [
        catalog['scanner']['anchor_template'], *(row['resolved'] for row in rows)]
    anchor, *fields = vision.calls
    assert anchor['mask_image'] == catalog['scanner']['anchor_mask']
    assert anchor['match_method'] == cv2.TM_SQDIFF_NORMED
    assert anchor['threshold'] == catalog['scanner']['anchor_threshold']
    assert anchor['source_image'] is frame
    assert [call['use_grayscale'] for call in fields] == [False, True, True]
    assert [call['mask_image'] for call in fields] == [None, None, rows[2]['resolved_mask']]
    for call, field in zip(fields, ('food', 'role', 'days')):
        assert call['threshold'] == catalog['scanner'][field+'_threshold']
        assert call['match_method'] == cv2.TM_CCOEFF_NORMED
        assert call['preprocess'] == 'none'
        x1, y1, x2, y2 = catalog['geometry'][field+'_crop_xyxy']
        margin = catalog['scanner']['roi_margin']
        assert call['source_image'].shape == (y2-y1+2*margin, x2-x1+2*margin, 3)
        assert np.shares_memory(call['source_image'], frame)
    np.testing.assert_array_equal(frame, before)


def test_no_anchors_returns_none(catalog, frame):
    vision = FakeVision(points=())
    assert find_target_on_frame(frame, cached_meal(catalog), catalog, vision) is None
    assert len(vision.calls) == 1


@pytest.mark.parametrize('section', ['food', 'role', 'days'])
@pytest.mark.parametrize('found,below', [(False, False), (True, True)])
def test_missing_target_does_not_substitute(catalog, frame, section, found, below):
    vision = FakeVision()
    def match(**kwargs):
        vision.calls.append(kwargs)
        index = ('food', 'role', 'days').index(section)+2
        return hit(score=kwargs['threshold']-0.001 if below else 1, found=found) if len(vision.calls) == index else hit()
    vision.find_template = match
    assert find_target_on_frame(frame, cached_meal(catalog), catalog, vision) is None


def test_multiple_visible_matches_are_ambiguous(catalog, frame):
    vision = FakeVision(points=((484, 236), (15, 236)))
    with pytest.raises(ValueError, match='ambiguous'):
        find_target_on_frame(frame, cached_meal(catalog), catalog, vision)
    assert len(vision.calls) == 7
    assert len({call['template_image'] for call in vision.calls}) == 4


def test_exact_catalog_thresholds_are_accepted(catalog, frame):
    vision = FakeVision()
    vision.anchors.matches[0].confidence = catalog['scanner']['anchor_threshold']
    vision.find_template = lambda **kwargs: hit(score=kwargs['threshold'])
    assert find_target_on_frame(frame, cached_meal(catalog), catalog, vision) == [15, 236]


def test_later_matching_failure_does_not_return_first_target(catalog, frame):
    vision = FakeVision(points=((15, 236), (250, 236)))
    def match(**kwargs):
        vision.calls.append(kwargs)
        return hit(error='backend failed') if len(vision.calls) == 5 else hit()
    vision.find_template = match
    with pytest.raises(ValueError, match='matching failed'):
        find_target_on_frame(frame, cached_meal(catalog), catalog, vision)


@pytest.mark.parametrize('score', [float('nan'), float('inf')])
def test_nonfinite_anchor_scores_fail(catalog, frame, score):
    vision = FakeVision()
    vision.anchors.matches[0].confidence = score
    with pytest.raises(ValueError, match='non-finite'):
        find_target_on_frame(frame, cached_meal(catalog), catalog, vision)


def test_low_confidence_anchor_is_not_used(catalog, frame):
    vision = FakeVision()
    vision.anchors.matches[0].confidence = catalog['scanner']['anchor_threshold']-0.001
    assert find_target_on_frame(frame, cached_meal(catalog), catalog, vision) is None
    assert len(vision.calls) == 1


@pytest.mark.parametrize('point', [(-20, 236), (600, 236), (15, -5), (15, 460),
                                  (-6, 236), (15, 362)])
def test_clipped_field_or_margin_is_not_matched(catalog, frame, point):
    vision = FakeVision(points=(point,))
    assert find_target_on_frame(frame, cached_meal(catalog), catalog, vision) is None
    assert len(vision.calls) == 1


def test_partial_capture_is_not_padded(catalog, frame):
    vision = FakeVision()
    assert find_target_on_frame(frame[:420], cached_meal(catalog), catalog, vision) is None
    assert len(vision.calls) == 1


@pytest.mark.parametrize('bad_frame', [None, np.zeros((0, 0, 3)), np.zeros((10, 10)),
                                     np.zeros((10, 10, 4))])
def test_capture_failure(catalog, bad_frame):
    vision = FakeVision()
    with pytest.raises(ValueError, match='capture failed'):
        find_target_on_frame(bad_frame, cached_meal(catalog), catalog, vision)
    assert not vision.calls


@pytest.mark.parametrize('score', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_day_scores_fail(catalog, frame, score):
    vision = FakeVision()
    def match(**kwargs):
        return hit(score=score) if kwargs['mask_image'] is not None else hit()
    vision.find_template = match
    with pytest.raises(ValueError, match='non-finite'):
        find_target_on_frame(frame, cached_meal(catalog), catalog, vision)


@pytest.mark.parametrize('stage', ['anchor', 'field'])
@pytest.mark.parametrize('failure', ['error', 'none', 'exception'])
def test_matching_failures_raise(catalog, frame, stage, failure):
    vision = FakeVision()
    def fail(**kwargs):
        if failure == 'exception':
            raise RuntimeError('backend failed')
        return None if failure == 'none' else NS(debug_info={'error': 'backend failed'})
    setattr(vision, 'find_all_templates' if stage == 'anchor' else 'find_template', fail)
    with pytest.raises(ValueError, match='matching failed'):
        find_target_on_frame(frame, cached_meal(catalog), catalog, vision)


@pytest.mark.parametrize('section,key,meal_key', [('items', 'id', 'food_id'),
    ('roles', 'id', 'role_id'), ('days', 'value', 'remaining_days')])
@pytest.mark.parametrize('damage', ['missing_row', 'duplicate_row', 'unresolved', 'missing_identity'])
def test_requested_metadata_required(catalog, frame, section, key, meal_key, damage):
    meal = cached_meal(catalog)
    row = next(r for r in catalog[section] if r[key] == meal[meal_key])
    if damage == 'missing_row':
        catalog[section].remove(row)
    elif damage == 'duplicate_row':
        catalog[section].append(copy.deepcopy(row))
    elif damage == 'unresolved':
        del row['resolved']
    else:
        del meal[meal_key]
    vision = FakeVision()
    with pytest.raises(ValueError, match='metadata'):
        find_target_on_frame(frame, meal, catalog, vision)
    assert not vision.calls


@pytest.mark.parametrize('damage', ['day_mask', 'incompatible_food'])
def test_required_mask_and_role_food_compatibility(catalog, frame, damage):
    meal = cached_meal(catalog)
    if damage == 'day_mask':
        next(r for r in catalog['days'] if r['value'] == 5).pop('resolved_mask')
    else:
        next(r for r in catalog['roles'] if r['id'] == meal['role_id'])['food_ids'] = []
    vision = FakeVision()
    with pytest.raises(ValueError, match='metadata'):
        find_target_on_frame(frame, meal, catalog, vision)
    assert not vision.calls


def test_unrequested_metadata_is_not_resolved_or_matched(catalog, frame):
    meal = cached_meal(catalog)
    for section, key, value in [('items', 'id', meal['food_id']),
                               ('roles', 'id', meal['role_id']), ('days', 'value', 5)]:
        for row in catalog[section]:
            if row[key] != value:
                row.pop('resolved')
                row.pop('resolved_mask', None)
    assert find_target_on_frame(frame, meal, catalog, FakeVision()) == [15, 236]
