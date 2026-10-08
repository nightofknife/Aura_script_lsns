"""Asynchronous camera motion cannot create semantic viewpoint evidence."""
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_scan_stream import _accepted_face_observation
from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as scan


def test_current_source_votes_survive_preview_truncation():
    scanner = SimpleNamespace(glyph_anchor_frame_id=42, glyph_anchor_rotation=np.eye(3),
                              evidence=[[{'frame_id':i} for i in range(43)], []])
    observed = dict(refine_diagnostic=dict(renewed=True, source_frame_id=42))
    proof = _accepted_face_observation(scanner, 42, observed)
    assert proof == dict(frame_id=42, rotation=np.eye(3).tolist(), cell_indices=[0])
    scanner.glyph_anchor_rotation[0, 0] = 0.
    assert proof['rotation'][0][0] == 1.


@pytest.mark.parametrize('cause', ['not_renewed', 'old_fit', 'old_anchor', 'paused', 'no_vote'])
def test_old_or_rejected_source_cannot_count_as_new_view(cause):
    scanner = SimpleNamespace(glyph_anchor_frame_id=42, glyph_anchor_rotation=np.eye(3),
                              evidence=[[dict(frame_id=42)]])
    observed = dict(refine_diagnostic=dict(renewed=True, source_frame_id=42))
    if cause == 'not_renewed': observed['refine_diagnostic']['renewed'] = False
    if cause == 'old_fit': observed['refine_diagnostic']['source_frame_id'] = 41
    if cause == 'old_anchor': scanner.glyph_anchor_frame_id = 41
    if cause == 'paused': observed['fusion_paused'] = True
    if cause == 'no_vote': scanner.evidence = [[dict(frame_id=41)]]
    assert _accepted_face_observation(scanner, 42, observed) is None


def test_face_route_does_not_finish_early_from_global_target_readiness(monkeypatch):
    monkeypatch.setattr(scan, 'targets_readiness', lambda *args: dict(ready=True, reason='targets_ready'))
    controller = SimpleNamespace(completed_faces={'F'})
    token = scan._SCAN_CONTROL.set(dict(scan_route='faces', face_policy=controller,
                                      recognition_goal='targets'))
    try:
        assert scan._completion({}) == (False, 'face_scan_incomplete')
        controller.completed_faces = set('URFDLB')
        assert scan._completion({}) == (True, 'targets_ready')
    finally:
        scan._SCAN_CONTROL.reset(token)
