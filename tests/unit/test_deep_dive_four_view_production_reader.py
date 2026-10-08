import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_four_view_reader as reader


def source(generation=1, timestamp=1., session=7, revision=0):
    return dict(generation_source='atomic_wgc', capture_backend='wgc', session_id=session,
                generation=generation, frame_id=generation, frame_time=timestamp, map_revision=revision)


def samples(icons):
    return [dict(reading=dict(icon_id=icon, confidence=.8, conflict=False),
        source=source(i+1, float(i+1)), view_group=1, scan_epoch='scan1', stable=True)
        for i, icon in enumerate(icons)]


def test_actual_empty_not_unknown():
    result = reader.fuse_node_readings(samples(['white_diamond', None, 'white_diamond']))
    assert result['icon_id'] == 'white_diamond' and result['node_kind'] == 'empty'
    assert result['temporal_frame_count'] == 3 and result['independent_view_count'] == 1


@pytest.mark.parametrize('icons', [('red_single_eye', 'red_single_eye', 'yellow_hex'),
                                   (None, 'red_single_eye', None), (None, None, None)])
def test_conflict_or_insufficient_remains_unknown(icons):
    assert reader.fuse_node_readings(samples(icons))['node_status'] == 'unknown'


@pytest.mark.parametrize('mutate', [
    lambda s: s[1].update(stable=False),
    lambda s: s[1].update(view_group=2),
    lambda s: s[1].update(scan_epoch='old'),
    lambda s: s[1]['source'].update(generation=1),
    lambda s: s[1]['source'].update(frame_time=1),
    lambda s: s[1]['source'].update(session_id=8),
    lambda s: s[1]['source'].update(map_revision=1),
    lambda s: s[1]['source'].update(generation_source='synthetic'),
])
def test_capture_identity_and_stability_fail_closed(mutate):
    values = samples(['white_diamond']*3)
    mutate(values)
    assert reader.fuse_node_readings(values)['node_status'] == 'unknown'


def test_reading_has_no_truth_or_occlusion_selection(monkeypatch):
    monkeypatch.setattr(reader, 'classify_icon', lambda rgb: dict(icon_id='blue_scales', confidence=.83))
    result = reader.read_node(np.zeros((96, 96, 3), np.uint8))
    assert result['icon_id'] == 'blue_scales'
    assert result['node_kind'] == 'shop'
    assert len(result['normalization_readings']) == 4


def test_normalization_confirmed_conflict_cannot_be_voted_away(monkeypatch):
    predictions = iter(['blue_scales', 'blue_scales', 'blue_scales', 'yellow_hex'])
    monkeypatch.setattr(reader, 'classify_icon', lambda rgb: dict(icon_id=next(predictions), confidence=.95))
    assert reader.read_node(np.zeros((96, 96, 3), np.uint8))['icon_id'] is None


def test_outside_quad_never_black_padded_as_actual_empty():
    with pytest.raises(ValueError, match='outside_source'):
        reader.crop_node(np.zeros((100, 100, 3), np.uint8), [[-5, 0], [20, 0], [20, 20], [-5, 20]])
