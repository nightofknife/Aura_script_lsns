"""Exact finite search pruning agrees with the frozen candidate20 reference."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
import time

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix
from plans.resonance_pc.src.actions._deep_dive_target_face_scan_policy import TargetFacePageScanPolicy
from test_deep_dive_mixed_scan_policy import AXES, cells

REFERENCE = runpy.run_path(str(Path(__file__).parent/'fixtures/deep_dive_page_pair_reference20.py'))['select_page_pair_reference20']


@pytest.mark.parametrize('case', ['unknown', 'known_sprites', 'basis', 'no_pair'])
def test_same_candidate_ids_exact_poses_paths_and_no_pair(case):
    saved = json.loads((Path(__file__).parent/'fixtures/deep_dive_navigation_sources_13.json').read_text(encoding='utf8'))
    pose = saved['sources'][0]['feedback']['semantic_metadata']['pose']
    policy = TargetFacePageScanPolicy(framing=False, page_cosine_range=(.86, .925))  # Frozen21 kernel ablation.
    policy._page_face = 'F'
    policy._page_basis = _matrix(np.array([.09,-.05,.03])) if case == 'basis' else np.eye(3)
    policy.tvec = np.array(pose['tvec'])
    observed = np.array(pose['rotation'])
    rows = [dict(occupant='unknown',node_status='unknown',evidence=[]) for _ in range(54)]
    if case in ('known_sprites','basis'):
        rows = cells()
        for row in rows:
            row['node_status'] = 'known'
            if row['occupant'] in ('player','inspiration','singularity'):
                row['evidence'] = [dict(occupant=row['occupant'],confidence=.85,target_box=[620.,200.,48.,58.],
                    quad=[[600.,180.],[680.,180.],[680.,260.],[600.,260.]],
                    target_point=[644.,229.],cosine=.70)]
                assert policy._sprite_footprint(row) is not None
    if case == 'no_pair':
        policy.tvec = np.array([0.,0.,10000.])
    before = deepcopy(rows)
    threads = cv2.getNumThreads()
    try:
        cv2.setNumThreads(1)
        begun = time.perf_counter()
        old = REFERENCE(policy,observed,rows,AXES)
        old_sec = time.perf_counter()-begun
        begun = time.perf_counter()
        new = policy._select_page_pair(observed,rows,AXES)
        new_sec = time.perf_counter()-begun
    finally:
        cv2.setNumThreads(threads)
    assert (old is None) == (new is None)
    if old is not None:
        assert len(old[0]) == len(new[0]) and len(old[1]) == len(new[1])
        assert all(np.array_equal(x,y) for x,y in zip(old[0]+old[1],new[0]+new[1]))
    assert rows == before
    print(f'{case}: reference={old_sec:.6f}s optimized={new_sec:.6f}s selected={new is not None}')
