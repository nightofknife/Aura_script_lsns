import hashlib

import numpy as np
import pytest

from research import deep_dive_four_view_node_reader as reader


def actual_crop():
    rgb = np.zeros((96, 96, 3), np.uint8)
    rgb[20:76, 30:66] = (15, 220, 30)
    return rgb


def test_identity_input_preserves_actual_pixels_and_hash(monkeypatch):
    rgb = actual_crop()
    seen = []
    def classify(image):
        seen.append(image.copy())
        return {'icon_id': None, 'confidence': 0.0}
    monkeypatch.setattr(reader, 'classify_icon', classify)
    result = reader.read_four_view_node(rgb)
    assert np.array_equal(seen[0], rgb)
    for item, image in zip(result['normalization_readings'], seen):
        assert item['input_sha256'] == hashlib.sha256(image.tobytes()).hexdigest()
    assert np.count_nonzero(seen[-1]) < np.count_nonzero(seen[0])
    assert result['reading']['icon_id'] is None


def test_conflicting_classes_revoke_confirmation_even_with_high_confidence(monkeypatch):
    results = iter([('green_burst', .83), (None, 0), ('yellow_hex', .99), ('green_burst', .81)])
    def classify(_):
        name, confidence = next(results)
        return dict(icon_id=name, confidence=confidence)
    monkeypatch.setattr(reader, 'classify_icon', classify)
    result = reader.read_four_view_node(actual_crop())
    assert result['conflict'] is True
    assert result['reading'] == dict(icon_id=None, confidence=0.0)
    assert result['unknown_reason'] == 'conflicting_normalizations'


def test_agreeing_results_use_conservative_confidence(monkeypatch):
    results = iter([('green_burst', .83), (None, 0), ('green_burst', .79), ('green_burst', .81)])
    def classify(_):
        name, confidence = next(results)
        return dict(icon_id=name, confidence=confidence)
    monkeypatch.setattr(reader, 'classify_icon', classify)
    result = reader.read_four_view_node(actual_crop())
    assert result['reading'] == dict(icon_id='green_burst', confidence=.79)
    assert result['accepted_classes'] == ['green_burst']
    assert not result['target_ownership_confirmed']
    assert not result['rotation_arrival_confirmed']


def test_occlusion_metadata_does_not_choose_or_suppress_reading(monkeypatch):
    monkeypatch.setattr(reader, 'classify_icon', lambda _: dict(icon_id='white_diamond', confidence=.7))
    result = reader.read_four_view_node(actual_crop(), annotation_occluded=True)
    assert result['annotation_occluded']
    assert result['reading']['icon_id'] == 'white_diamond'


def test_no_class_stays_unknown_instead_of_becoming_empty(monkeypatch):
    monkeypatch.setattr(reader, 'classify_icon', lambda _: dict(icon_id=None, confidence=0))
    result = reader.read_four_view_node(actual_crop())
    assert result['accepted_classes'] == []
    assert result['unknown_reason'] == 'no_confirmed_class'
    assert result['reading']['icon_id'] is None


@pytest.mark.parametrize('scales', [(), (0,), (1.2,), (float('nan'),), (True,), tuple([.5]*9)])
def test_invalid_normalization_is_rejected(scales):
    with pytest.raises(ValueError):
        reader.read_four_view_node(actual_crop(), scales=scales)


def test_crop_size_is_explicit():
    with pytest.raises(ValueError):
        reader.read_four_view_node(np.zeros((95, 96, 3), np.uint8))
