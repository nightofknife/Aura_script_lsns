"""Static replay of actual scan05; never a claim of current live-frame freshness."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from plans.resonance_pc.src.actions import consciousness_deep_dive_plan_pc_actions as preview
from plans.resonance_pc.src.actions._deep_dive_movement_planner import plan_layout
from plans.resonance_pc.src.actions._deep_dive_target_readiness import targets_readiness


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/deep_dive_four_view/scan05_targets_layout.json'


def recorded_layout():
    return json.loads(FIXTURE.read_text('utf-8'))


def write_layout(path, layout):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(layout), 'utf-8')
    return path


def test_actual_scan05_static_target_proof_loads_without_full_pattern_flag():
    layout = preview._load_completed_layout(FIXTURE)
    proof = layout['_fixture_provenance']
    assert proof['source_run_id'] == '20261006-175436-7261bd0e'
    assert proof['source_sha256'] == '52720bfe888de515efb5c991906743ba7b9c3e14d66e28325419646792ab502f'
    assert layout['layout_complete'] is False and layout['status'] == 'targets_ready'
    assert targets_readiness(layout)['ready']
    result = plan_layout(layout, strategy='chase', turn_budget=5, time_budget_sec=1.)
    assert result['success'] and result['status'] == 'solved'


@pytest.mark.parametrize('mutation', ['flags_only', 'unassociated', 'same_view', 'wrong_player', 'stale_anchor'])
def test_saved_target_flags_cannot_bypass_evidence_readiness(tmp_path, mutation):
    layout = recorded_layout()
    player = next(c for c in layout['cells'] if c['occupant'] == 'player')
    if mutation == 'flags_only':
        for row in layout['cells']:
            row['evidence'] = []
    elif mutation == 'unassociated':
        layout['unassociated_target_candidates'] = [dict(kind='inspiration', confidence=.9, confirmable=True)]
    elif mutation == 'same_view':
        for row in player['evidence']:
            row['group'] = 400
    elif mutation == 'wrong_player':
        layout['player_cell'] = dict(face='U', row=0, col=0)
    elif mutation == 'stale_anchor':
        layout['glyph_anchor_age_sec'] = 2.
    with pytest.raises(ValueError, match='目标证据未通过核验'):
        preview._load_completed_layout(write_layout(tmp_path / 'invalid.json', layout))


def test_latest_actual_target_layout_wins_over_old_complete_layout(tmp_path):
    recent = recorded_layout()
    old = deepcopy(recent)
    old.update(status='completed', recognition_goal='full', layout_complete=True)
    root = tmp_path / 'logs/deep_dive_scan'
    old_path = write_layout(root / 'old/layout.json', old)
    new_path = write_layout(root / 'new/layout.json', recent)
    # This controls file-selection order only; neither mtime makes old RGB fresh.
    os.utime(old_path, (100., 100.))
    os.utime(new_path, (200., 200.))
    selected, layout = preview.load_planning_layout(base_path=tmp_path)
    assert selected == new_path.resolve() and layout['status'] == 'targets_ready'


def test_invalid_new_target_scan_is_skipped_but_explicit_path_fails(tmp_path):
    old = recorded_layout()
    old.update(status='completed', recognition_goal='full', layout_complete=True)
    invalid = recorded_layout()
    invalid['unassociated_target_candidates'] = [dict(kind='player', confidence=.8)]
    root = tmp_path / 'logs/deep_dive_scan'
    old_path = write_layout(root / 'old/layout.json', old)
    invalid_path = write_layout(root / 'bad/layout.json', invalid)
    os.utime(old_path, (100., 100.))
    os.utime(invalid_path, (200., 200.))
    assert preview.load_planning_layout(base_path=tmp_path)[0] == old_path.resolve()
    with pytest.raises(ValueError, match='目标证据未通过核验'):
        preview.load_planning_layout(str(invalid_path), base_path=tmp_path)


@pytest.mark.parametrize('status', ['partial', 'blocked', 'cancelled'])
def test_non_successful_scan_never_reaches_offline_preview(tmp_path, status):
    layout = recorded_layout()
    layout['status'] = status
    with pytest.raises(ValueError, match='需要完整扫描或目标证据'):
        preview._load_completed_layout(write_layout(tmp_path / 'failed.json', layout))
