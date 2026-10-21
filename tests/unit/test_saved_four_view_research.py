"""Saved-frame research uses registered current crops with explicit inference config."""
from argparse import Namespace
from types import SimpleNamespace

import numpy as np
import pytest

from research.detector_config import ProbeConfig, detector_config
from research import read_saved_four_view as saved


def test_cpu_is_default_and_directml_requires_all_explicit_inputs(tmp_path):
    assert ProbeConfig().get("resonance_pc.deep_dive.entity_detector.execution_provider") == "cpu"
    with pytest.raises(ValueError, match="requires explicit"):
        ProbeConfig(runtime_site=tmp_path)
    python = tmp_path / "python.exe"
    python.write_bytes(b"fixture")
    config = detector_config(Namespace(dml_runtime_site=tmp_path, dml_python=python,
                                      dml_device=0, dml_ort_version="1.24.4"))
    assert config.get("resonance_pc.deep_dive.entity_detector.execution_provider") == "dml_worker"
    assert config.get("resonance_pc.deep_dive.entity_detector.dml_device_id") == 0
    assert config.get("unrelated", "fallback") == "fallback"


def test_saved_reader_passes_actual_registered_crops_without_manual_labels(monkeypatch):
    pixels = np.zeros((720, 1280, 3), dtype=np.uint8)
    pixels[10:40, 10:40] = (120, 31, 87)
    quad = [[10, 10], [40, 10], [40, 40], [10, 40]]
    calls = []
    def align(rgb, *, reference_view):
        assert rgb is pixels and reference_view == 2
        return {"results": [{"proposal_accepted": True, "reference_panel": "F",
                             "projected_reference_cells": [{"row": 0, "col": 1, "quad": quad,
                                                            "expected_icon": "must_not_enter_classifier"}]}]}
    def read(crop):
        calls.append(crop.copy())
        return {"reading": {"icon_id": "green_burst"}}
    monkeypatch.setattr(saved, "read_four_view_node", read)
    result = saved.read_saved_view(pixels, SimpleNamespace(align=align), 2)
    assert result["status"] == "read"
    assert len(calls) == 1 and calls[0].shape == (96, 96, 3)
    assert calls[0][48, 48].tolist() == [120, 31, 87]
    assert result["cells"][0]["result"] == {"reading": {"icon_id": "green_burst"}}
    assert "expected_icon" not in result["cells"][0]


@pytest.mark.parametrize("panels", [[], [{"proposal_accepted": False}]])
def test_geometry_rejection_prevents_classification(monkeypatch, panels):
    monkeypatch.setattr(saved, "read_four_view_node", lambda crop: pytest.fail("no accepted geometry"))
    result = saved.read_saved_view(np.zeros((720, 1280, 3), np.uint8),
                                  SimpleNamespace(align=lambda *a, **k: {"results": panels}), 1)
    assert result == {"status": "geometry_rejected", "cells": []}
