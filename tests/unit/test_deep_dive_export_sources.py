"""Saved-source export cache preserves pixels, provenance and failure order."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as scan
from plans.resonance_pc.src.actions._deep_dive_layout_report import write_layout_report


@pytest.fixture(autouse=True)
def single_cv_thread():
    old = cv2.getNumThreads()
    cv2.setNumThreads(1)
    yield
    cv2.setNumThreads(old)


def source(directory, tag):
    yy, xx = np.indices((128, 160))
    pixels = np.stack(((xx + tag * 11) % 256, (yy * 2 + tag * 7) % 256,
                       (xx + yy * 3 + tag * 13) % 256), axis=2).astype(np.uint8)
    path = directory / f'source_{tag}.png'
    assert cv2.imwrite(str(path), pixels)
    return path, pixels


def cell(index, path, shift=0):
    return dict(face='URFDLB'[index // 9], row=(index % 9) // 3, col=index % 3,
        occupant='none', node_status='known', icon_id='green_burst',
        evidence=[dict(frame_id=101, frame_time=12.345, source_session_id=123,
            frame_path=str(path), quad=[[5+shift, 7], [102+shift, 9], [108+shift, 103], [8+shift, 108]],
            confidence=.91, association_evidence=dict(source_map_revision=0)),
            dict(frame_id=99, frame_path=str(path), confidence=.80)])


@pytest.mark.parametrize('absolute', [False, True])
def test_shared_source_real_pixels_and_metadata_match_independent_warps(tmp_path, monkeypatch, absolute):
    source_path, pixels = source(tmp_path, 1)
    path = source_path if absolute else Path(source_path.name)
    result = dict(cells=[cell(0, path), cell(1, path, 9), cell(2, path, 21)],
                  run_id='source-parity', status='partial')
    original = deepcopy(result)
    imread = cv2.imread
    calls = []
    def read(path, *a, **kw):
        calls.append(path)
        return imread(path, *a, **kw)
    monkeypatch.setattr(cv2, 'imread', read)
    scan._write_cell_crops(result, tmp_path)
    assert calls == [str(source_path)]
    for i, c in enumerate(result['cells']):
        proof = c['evidence'][0]
        quad = np.asarray(proof['quad'], np.float32)
        transform = cv2.getPerspectiveTransform(quad, np.float32(((0, 0), (95, 0), (95, 95), (0, 95))))
        expected = cv2.warpPerspective(pixels, transform, (96, 96))
        assert np.array_equal(imread(str(tmp_path / proof['crop_path'])), expected)
        expected_cell = deepcopy(original['cells'][i])
        expected_cell['evidence'][0]['crop_path'] = f'cells/U_0_{i}.png'
        assert c == expected_cell
    assert np.array_equal(imread(str(source_path)), pixels)


def test_eight_source_lru_touches_then_evicts_without_reordering_cells(tmp_path, monkeypatch):
    sources = [source(tmp_path, i)[0] for i in range(9)]
    order = list(range(8)) + [0, 8, 1, 0]
    result = dict(cells=[cell(i, sources[tag]) for i, tag in enumerate(order)])
    imread = cv2.imread
    reads, writes = [], []
    imwrite = cv2.imwrite
    def read(path, *a, **kw):
        reads.append(Path(path))
        return imread(path, *a, **kw)
    def write(path, image, *a, **kw):
        writes.append(Path(path).name)
        return imwrite(path, image, *a, **kw)
    monkeypatch.setattr(cv2, 'imread', read)
    monkeypatch.setattr(cv2, 'imwrite', write)
    scan._write_cell_crops(result, tmp_path)
    assert reads == sources[:8] + [sources[8], sources[1]]
    assert Counter(reads)[sources[0]] == 1  # touching0 preserves it across eviction
    assert Counter(reads)[sources[1]] == 2
    assert writes == [f"{c['face']}_{c['row']}_{c['col']}.png" for c in result['cells']]


def test_imwrite_failure_stops_before_later_decode_and_metadata_update(tmp_path, monkeypatch):
    a = source(tmp_path, 1)[0]
    b = source(tmp_path, 2)[0]
    result = dict(cells=[cell(0, a), cell(1, a), cell(2, b)])
    original = deepcopy(result)
    imread, imwrite = cv2.imread, cv2.imwrite
    reads, writes = [], []
    def read(path, *a, **kw):
        reads.append(path)
        return imread(path, *a, **kw)
    def write(path, image, *a, **kw):
        writes.append(Path(path).name)
        return False if len(writes) == 2 else imwrite(path, image, *a, **kw)
    monkeypatch.setattr(cv2, 'imread', read)
    monkeypatch.setattr(cv2, 'imwrite', write)
    with pytest.raises(OSError, match='cell_crop_save_failed'):
        scan._write_cell_crops(result, tmp_path)
    assert reads == [str(a)]
    assert writes == ['U_0_0.png', 'U_0_1.png']
    assert result['cells'][0]['evidence'][0]['crop_path'] == 'cells/U_0_0.png'
    assert result['cells'][1:] == original['cells'][1:]
    assert not (tmp_path / 'cells/U_0_2.png').exists()


def test_export_cache_is_per_invocation_and_missing_source_stays_unwritten(tmp_path, monkeypatch):
    valid, _ = source(tmp_path, 1)
    missing = tmp_path / 'missing.png'
    result = dict(cells=[cell(0, valid), cell(1, missing), cell(2, missing)])
    imread = cv2.imread
    calls = []
    def read(path, *a, **kw):
        calls.append(path)
        return imread(path, *a, **kw)
    monkeypatch.setattr(cv2, 'imread', read)
    scan._write_cell_crops(result, tmp_path)
    scan._write_cell_crops(result, tmp_path)
    assert calls == [str(valid), str(missing)] * 2
    assert all('crop_path' not in c['evidence'][0] for c in result['cells'][1:])


def test_compact_report_json_roundtrips_all_original_provenance(tmp_path):
    path, _ = source(tmp_path, 1)
    result = dict(status='partial', reason='目标未齐', layout_complete=False,
        run_id='full-provenance', cells=[cell(0, path)],
        frames=[dict(frame_id=101, path=str(path), frame_time=12.345,
                     session_id=2**127+51, capture_backend='wgc')],
        diagnostics=dict(refine=dict(renewed=True, accepted_faces=dict(U=7, R=2))),
        custom_unknown_fields=dict(nested=[True, None, 1.23456789012345, '图案\n来源'],
                                   association=dict(rotation=[[1., 0., 0.]], source_map_revision=0)))
    original = deepcopy(result)
    returned = write_layout_report(result, tmp_path)
    text = Path(returned['json_path']).read_text(encoding='utf8')
    assert json.loads(text) == original
    assert result == original
    assert '\n' not in text  # compact file; embedded newlines remain JSON escapes
    assert 'PARTIAL' in Path(returned['report_path']).read_text(encoding='utf8')
