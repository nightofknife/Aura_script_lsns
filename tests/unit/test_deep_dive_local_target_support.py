"""Actual saved projections reject neighbour ghosts and retain genuine pairs.

These tests inject recorded glyph premises; they are not live recognition.
"""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_layout_vision import LayoutScanner


CASES = json.loads((Path(__file__).parents[1] /
    'fixtures/deep_dive_anchor/actual_local_support_explore04.json').read_text('utf8'))['cases']


@pytest.mark.parametrize('case', CASES, ids=lambda case: str(case['frame_id']))
def test_actual_source_local_face_and_pixel_stability(case):
    scanner = LayoutScanner()
    pose = case['pose']
    scanner.rvec = np.asarray(pose['rvec']).reshape(3, 1)
    scanner.tvec = np.asarray(pose['tvec']).reshape(3, 1)
    scanner.rotation = np.asarray(pose['rotation'])
    scanner.quality = pose['quality']
    scanner._anchor_source = {key: case['source'][key] for key in
        ('source_frame_id', 'source_frame_time', 'source_map_revision')}
    scanner.refine_diagnostic = deepcopy(case['refine_diagnostic'])
    faces = Counter(scanner.refine_diagnostic['accepted_faces'])
    # Re-establish the saved same-frame proof token on its exact recorded pose.
    scanner._record_glyph_anchor(scanner.refine_diagnostic['reason'], list(faces.elements()))
    target = deepcopy(case['target'])
    scanner.last_targets = [target]
    scanner.last_projected = scanner.visible()
    assigned, uncertain = scanner._associated_targets([target], scanner.last_projected)
    if case['expected_accept']:
        assert set(assigned) == {case['nominal_index']}
        evidence = assigned[case['nominal_index']]['association_evidence']
        assert evidence['target_face_glyph_count'] >= 2
        assert evidence['anchor_uncertainty']['ready']
        assert evidence['anchor_uncertainty']['margin_lower'] > .12
    else:
        assert not assigned and case['nominal_index'] in uncertain
        assert 'cell_index' not in scanner.target_mask_observation()[0]
        reason = scanner.entity_association_diagnostic['decisions'][0]['reason']
        assert reason == ('target_anchor_pixel_uncertain' if case['frame_id'] == 348
                          else 'target_face_glyph_support_required')
        scanner.group = 1
        scanner._observe_cells(np.zeros((720, 1280, 3), np.uint8), case['frame_id'], [target])
        assert not any(entry.get('occupant') == 'inspiration'
                       for pool in scanner.evidence for entry in pool)
