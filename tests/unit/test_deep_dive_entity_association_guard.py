"""Entity votes require this image's glyph fit; hidden faces can only veto."""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as vision


def proof(scanner, frame=17):
    scanner._anchor_source = dict(source_frame_id=frame, source_frame_time=100.,
                                 source_map_revision=3)
    scanner.refine_diagnostic = dict(scanner._anchor_source, renewed=False)
    scanner._record_glyph_anchor('known_multi_face_grid', ['U', 'F'])


def target(**changes):
    return dict(dict(kind='player', point=[520., 220.], box=[505., 200., 30., 40.],
                     confidence=.9, confirmable=True), **changes)


def synthetic_scanner(monkeypatch, *, hidden_competitor=False, negative=False):
    scanner = vision.LayoutScanner(target_negative_evidence=negative)
    scanner.quality = .95
    scanner.ready = True
    quad = np.array([[500., 200.], [540., 200.], [540., 240.], [500., 240.]])
    visible = [dict(index=0, quad=quad, area=1600., cosine=.8,
                    centre=np.array([520., 220.]))]
    monkeypatch.setattr(scanner, 'visible', lambda *a, **k: visible)

    def project(points, rotation=None):
        n = len(points)
        if n == 216:
            return np.tile(quad, (54, 1))
        if n == 864:
            centres = np.column_stack((np.arange(54) * 200. + 520., np.full(54, 220.)))
            if hidden_competitor:
                centres[30] = centres[0]
            return np.repeat(centres, 16, axis=0)
        return np.tile([520., 220.], (n, 1))

    monkeypatch.setattr(scanner, 'project', project)
    monkeypatch.setattr(scanner, '_read_icon', lambda *a: (None, dict(icon_id=None, confidence=0.)))
    scanner.last_projected = visible
    return scanner, visible


def test_bootstrap_and_previous_frame_only_mask(monkeypatch):
    scanner, visible = synthetic_scanner(monkeypatch)
    proof(scanner)
    scanner._begin_frame()
    scanner._anchor_source = dict(source_frame_id=18, source_frame_time=101., source_map_revision=3)
    scanner.refine_diagnostic = dict(scanner._anchor_source, renewed=False)
    scanner._record_glyph_anchor('reset_bootstrap', ['U', 'F'])
    scanner.last_targets = [target()]
    assigned, uncertain = scanner._associated_targets(scanner.last_targets, visible)
    assert not assigned and 0 in uncertain
    masks = scanner.result()['target_candidate_associations']
    assert masks[0]['confidence'] == .9 and masks[0]['confirmable']
    assert 'cell_index' not in masks[0] and 'object_point' not in masks[0]
    assert scanner.entity_association_diagnostic['decisions'][0]['reason'] == 'same_frame_glyph_pose_not_renewed'


def test_renewal_invalidates_earlier_same_pose_cache_and_preserves_evidence(monkeypatch):
    scanner, visible = synthetic_scanner(monkeypatch)
    scanner._anchor_source = dict(source_frame_id=17, source_frame_time=100., source_map_revision=3)
    targets = [target()]
    assert not scanner._associated_targets(targets, visible)[0]
    proof(scanner)
    assigned, _ = scanner._associated_targets(targets, visible)
    evidence = assigned[0]['association_evidence']
    assert evidence['source_frame_id'] == 17 and evidence['source_frame_time'] == 100.
    assert evidence['source_map_revision'] == 3 and evidence['margin'] > .12
    np.testing.assert_array_equal(evidence['rotation'], scanner.rotation)
    scanner.last_targets = targets
    scanner.group = 1
    scanner._observe_cells(np.zeros((720, 1280, 3), np.uint8), 17, targets)
    saved = deepcopy(scanner.evidence[0][0])
    targets[0]['box'][0] += 1
    assert scanner.evidence[0][0] == saved
    assert scanner.evidence[0][0]['association_evidence'] == evidence
    targets[0]['confidence'] = .59
    assert not scanner._associated_targets(targets, visible)[0]
    scanner.rvec[0, 0] += .001
    assert not scanner._same_frame_entity_pose()


def test_hidden_face_can_veto_but_never_receive_positive(monkeypatch):
    scanner, visible = synthetic_scanner(monkeypatch, hidden_competitor=True)
    proof(scanner)
    assigned, uncertain = scanner._associated_targets([target()], visible)
    assert not assigned and {0, 30} <= uncertain
    decision = scanner.entity_association_diagnostic['decisions'][0]
    assert decision['reason'] == 'all54_normal_corridor_competitor'
    assert decision['competitor_index'] == 30 and decision['margin'] == 0.
    scanner.group = 1
    scanner._observe_cells(np.zeros((720, 1280, 3), np.uint8), 17, [target()])
    assert not any(scanner.evidence)


@pytest.mark.parametrize('changes', [dict(confidence=.59), dict(confirmable=False)])
def test_original_positive_thresholds_remain(monkeypatch, changes):
    scanner, visible = synthetic_scanner(monkeypatch)
    proof(scanner)
    assert not scanner._associated_targets([target(**changes)], visible)[0]


@pytest.mark.parametrize('packet,flag,renewed', [([], True, True),
    ({'targets': [], 'model_executed': False, 'coverage_valid': True}, True, True),
    ({'targets': [], 'model_executed': True, 'coverage_valid': False}, True, True),
    ({'targets': [], 'model_executed': True, 'coverage_valid': True}, False, True),
    ({'targets': [], 'model_executed': True, 'coverage_valid': True}, True, False)])
def test_no_independent_negative_without_all_proofs(monkeypatch, packet, flag, renewed):
    scanner, _ = synthetic_scanner(monkeypatch, negative=flag)
    if renewed:
        proof(scanner)
    targets = scanner._target_observation(packet)
    scanner.group = 1
    scanner._observe_cells(np.zeros((720, 1280, 3), np.uint8), 17, targets)
    assert not scanner.evidence[0]


def test_model_negative_requires_three_views_and_never_invents_node(monkeypatch):
    scanner, _ = synthetic_scanner(monkeypatch, negative=True)
    for group in (1, 2, 3):
        scanner._begin_frame()
        proof(scanner, frame=group)
        scanner._target_observation(dict(targets=[], model_executed=True, coverage_valid=True))
        scanner.group = group
        scanner._observe_cells(np.zeros((720, 1280, 3), np.uint8), group, [])
        row = scanner.result()['cells'][0]
        assert row['occupant'] == ('none' if group == 3 else 'unknown')
    assert row['occupant_status'] == 'confirmed'
    assert row['icon_id'] is None and row['node_status'] == 'unknown'
    assert row['evidence'][0]['occupancy_negative_evidence']['coverage_valid']
    # Extra occupancy-only views must not vote for an imaginary None node.
    for group in (4, 5, 6):
        scanner.evidence[0].append(dict(frame_id=group, group=group, occupant='none',
                                      icon_id='blue_scales', confidence=.9))
    row = scanner.result()['cells'][0]
    assert row['icon_id'] == 'blue_scales' and row['node_status'] == 'known'


def test_even_tiny_weak_box_overlap_blocks_model_negative(monkeypatch):
    scanner, _ = synthetic_scanner(monkeypatch, negative=True)
    proof(scanner)
    weak = target(point=[900., 500.], box=[545., 239., 1., 1.], confidence=.05, confirmable=False)
    scanner._target_observation(dict(targets=[weak], model_executed=True, coverage_valid=True))
    scanner.group = 1
    scanner._observe_cells(np.zeros((720, 1280, 3), np.uint8), 17, [weak])
    assert not scanner.evidence[0]


@pytest.mark.parametrize('bad', ['nonfinite', 'offscreen', 'hud', 'back_camera', 'oblique'])
def test_independent_negative_rejects_invalid_full_corridor(monkeypatch, bad):
    scanner, visible = synthetic_scanner(monkeypatch, negative=True)
    if bad == 'oblique':
        visible[0]['cosine'] = .47
    if bad == 'back_camera':
        scanner.tvec[2, 0] = -42.
    original_project = scanner.project
    def project(points, rotation=None):
        result = original_project(points, rotation)
        if len(points) == 16:
            if bad == 'nonfinite':
                result[7] = [np.nan, 220.]
            elif bad == 'offscreen':
                result[7] = [941., 220.]
            elif bad == 'hud':
                result[7] = [640., 534.]
        return result
    monkeypatch.setattr(scanner, 'project', project)
    proof(scanner)
    scanner._target_observation(dict(targets=[], model_executed=True, coverage_valid=True))
    scanner.group = 1
    scanner._observe_cells(np.zeros((720, 1280, 3), np.uint8), 17, [])
    assert not scanner.evidence[0]


def test_detector_injection_packet_and_fork(monkeypatch):
    calls = []
    def detector(image):
        calls.append(image)
        return dict(targets=[], model_executed=True, coverage_valid=True)
    scanner = vision.LayoutScanner(detector, target_negative_evidence=True)
    image = np.zeros((720, 1280, 3), np.uint8)
    monkeypatch.setattr(scanner, '_bootstrap', lambda image: False)
    scanner.observe(image, 5)
    assert calls == [image]
    assert scanner._target_coverage['source_frame_id'] == 5
    fork = scanner.fork_semantic()
    assert fork._target_detector is detector and fork.target_negative_evidence
    assert fork._entity_anchor_proof is None and not fork._target_coverage
    monkeypatch.setattr(fork, '_refine_centres', lambda *a: proof(fork, 6))
    monkeypatch.setattr(fork, '_atlas_alignment', lambda *a, **k: True)
    monkeypatch.setattr(fork, '_observe_cells', lambda *a: None)
    monkeypatch.setattr(vision.time, 'monotonic', lambda: 100.)
    fork.semantic_view(image, 6, fork.pose_snapshot(), source_frame_time=100.)
    assert len(calls) == 2
    with pytest.raises(TypeError):
        fork._target_observation(None)


def test_icon_cache_never_aliases_another_source_or_classifier(monkeypatch):
    scanner = vision.LayoutScanner()
    calls = []
    monkeypatch.setattr(vision, '_crop', lambda image, q: image.copy())
    monkeypatch.setattr(vision, 'classify_icon', lambda image: calls.append(image[0, 0, 0]) or {})
    image = np.zeros((2, 2, 3), np.uint8)
    q = np.zeros((4, 2))
    scanner._read_icon(image, q)
    scanner._read_icon(image, q)
    scanner._read_icon(image.copy(), q)
    assert len(calls) == 2
    monkeypatch.setattr(vision, 'classify_icon', lambda image: dict(icon_id='replacement'))
    assert scanner._read_icon(image, q)[1]['icon_id'] == 'replacement'


_BOSS_CASES=json.loads((Path(__file__).parents[1]/'fixtures/deep_dive_anchor/boss_bbox_center_20261001.json').read_text())['cases']


@pytest.mark.parametrize('case', _BOSS_CASES, ids=lambda case: str(case['frame_id']))
def test_saved_boss_box_centre_wrong_neighbor_is_mask_only(case):
    scanner=vision.LayoutScanner()
    pose=case['pose']
    scanner.rvec=np.array(pose['rvec']).reshape(3,1)
    scanner.rotation=np.array(pose['rotation'])
    scanner.tvec=np.array(pose['tvec']).reshape(3,1)
    source=case['actual_source']
    scanner._anchor_source={key:source[key] for key in ('source_frame_id','source_frame_time','source_map_revision')}
    scanner.refine_diagnostic=dict(scanner._anchor_source,renewed=False)
    scanner._record_glyph_anchor('saved_current_frame_fixture',['D','B'])
    index=case['candidate_index']
    target=deepcopy(case['target'])
    scanner.last_targets=[target]
    scanner.last_projected=scanner.visible()
    # Exercise the original front-facing thresholds as well as both guards.
    assigned,uncertain=scanner._associated_targets([target],scanner.last_projected)
    if case['expected_index'] is None:
        assert not assigned and index in uncertain
        decision=scanner.entity_association_diagnostic['decisions'][0]
        assert decision['reason']=='bbox_center_height_boundary_ambiguous'
        assert decision['margin']>.12  # The old point-only test accepted it.
        reliability=decision['anchor_center_reliability']
        assert reliability['height_sample']==0
        assert reliability['competitor_index']==31
        assert reliability['margin']<.12
        assert 'cell_index' not in scanner.target_mask_observation()[0]
    else:
        assert set(assigned)=={case['expected_index']}
        if case['frame_id']==353:
            # A valid upper-height endpoint remains eligible when the centre
            # uncertainty cannot support any rival surface corridor.
            evidence=assigned[index]['association_evidence']['anchor_center_reliability']
            assert evidence['height_sample']==15 and evidence['margin']>.12
