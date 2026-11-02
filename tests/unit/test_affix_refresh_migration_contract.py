"""Keep bounded refresh rounds, candidate handoff and failure evidence."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from packages.aura_core.observability import failure_evidence
from plans.resonance_pc.src.actions import _affix_refresh_policy as policy
from plans.resonance_pc.src.actions import _affix_refresh_runtime as runtime
from plans.resonance_pc.src.actions import _affix_refresh_vision as vision
from tools import build_affix_refresh_templates as template_builder


def test_native_backups_belong_to_development_and_retain_runtime_provenance():
    plan = Path(__file__).resolve().parents[2] / "plans/resonance_pc"
    active_root = plan / "templates/inventory/affix_refresh"
    source_root = plan / "development/template_sources/inventory/affix_refresh"
    active = json.loads((active_root / "manifest.json").read_text(encoding="utf-8"))
    sources = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    assert not (active_root / "native").exists()
    assert "native_file" not in json.dumps(active)
    assert all(not file.startswith("native/") for file in active["assets"])
    assert len(sources["assets"]) == 23
    assert (source_root / sources["runtime_manifest"]).resolve() == active_root / "manifest.json"
    assert set(sources["assets"]) == {path.relative_to(source_root).as_posix()
                                      for path in (source_root / "native").rglob("*.png")}
    for file, asset in sources["assets"].items():
        assert hashlib.sha256((source_root / file).read_bytes()).hexdigest() == asset["sha256"]
        assert len(asset["size"]) == 2 and asset["runtime_file"] in active["assets"]
        assert asset["runtime_spec_paths"]
        for pointer in asset["runtime_spec_paths"]:
            original = sources["provenance"]
            for segment in pointer.removeprefix("/").split("/"):
                key = segment.replace("~1", "/").replace("~0", "~")
                original = original[int(key)] if isinstance(original, list) else original[key]
            assert original["native_file"] == file


def test_affix_generator_output_defaults_keep_runtime_and_development_separate(tmp_path):
    output, development = template_builder.output_directories(template_builder.DEFAULT_OUTPUT)
    assert output == template_builder.DEFAULT_OUTPUT.resolve()
    assert development == template_builder.DEFAULT_DEVELOPMENT_OUTPUT.resolve()
    custom = tmp_path / "runtime"
    assert template_builder.output_directories(custom) == (custom, tmp_path / "runtime_sources")
    explicit = tmp_path / "source_archive"
    assert template_builder.output_directories(custom, explicit) == (custom, explicit)
    for invalid in (custom, custom / "native", tmp_path,
                    template_builder.PLAN / "templates/development"):
        with pytest.raises(ValueError, match="outside runtime templates"):
            template_builder.output_directories(custom, invalid)


@pytest.fixture
def rig(monkeypatch):
    catalog = policy.load_catalog()
    target, other = catalog["affixes"][:2]

    def frame(definition=None):
        candidate = (dict(id=definition["id"], value=definition["min_value"], text=definition["description"])
                     if definition else None)
        return dict(valid=True, scene="affix", replace_mode=True,
                    candidate=candidate, controls={"refresh": [100, 100], "dialog_confirm": [200, 200]})

    instance = runtime.AffixRefreshRuntime.__new__(runtime.AffixRefreshRuntime)
    instance.catalog, instance.app = catalog, object()
    instance.state = dict(cid="offline-contract", film_spent=0, film_reserved=0,
                          refresh_count=0, refresh_limit=0, film_limit=0, replaced=False)
    instance.config, instance.last_frame = {}, {}
    current_frames, outcomes, clicks = [], [], []

    async def commit(stage=None, **fields):
        instance.state.update(fields)
        if stage:
            instance.state["stage"] = stage

    async def current():
        return deepcopy(current_frames.pop(0))

    async def wait(predicate, **kwargs):
        result = deepcopy(outcomes.pop(0))
        assert predicate(result), "Fake observation must satisfy the real transaction predicate"
        return result

    async def click(point):
        clicks.append(point)

    async def blocking(call, *args, **kwargs):
        return None

    instance.commit, instance.current, instance.wait, instance.click = commit, current, wait, click
    monkeypatch.setattr(runtime, "_blocking", blocking)
    monkeypatch.setattr(runtime, "_check_cancelled", lambda: None)
    monkeypatch.setattr(runtime, "REFRESH_SETTLE_SECONDS", 0)
    config = dict(target_affix_id=target["id"], comparison="any", threshold="",
                  max_film_cost=1, auto_replace=True)
    return instance, frame, target, other, current_frames, outcomes, clicks, config


@pytest.mark.parametrize("matched", [False, True])
def test_last_round_candidate_is_evaluated_and_never_applied(rig, matched):
    instance, frame, target, other, reads, outcomes, clicks, config = rig
    reads.append(frame(other))
    outcomes.append(frame(target if matched else other))
    result = asyncio.run(instance.run(config))
    assert result["reason"] == ("target_found" if matched else "refresh_limit_reached")
    assert result["film_spent"] == result["refresh_count"] == 1
    assert result["film_reserved"] == 0 and result["result_uncertain"] is False
    assert result["auto_replace"] is result["replaced"] is False
    assert clicks == [[100, 100]] and not outcomes and not reads


def test_zero_round_limit_preserves_current_candidate_without_refresh(rig):
    instance, frame, target, other, reads, outcomes, clicks, config = rig
    reads.append(frame(other))
    result = asyncio.run(instance.run(config | {"max_film_cost": 0}))
    assert result["reason"] == "refresh_limit_reached" and not clicks
    assert result["current_candidate"]["id"] == other["id"]


@pytest.mark.parametrize("cancel", [False, True])
def test_unknown_result_keeps_submitted_round_and_never_retries_or_replaces(rig, cancel):
    instance, frame, target, other, reads, outcomes, clicks, config = rig
    reads.append(frame(other))

    async def unknown(predicate, **kwargs):
        if cancel:
            raise asyncio.CancelledError()
        raise policy.PolicyError("observation_timeout", "unknown refresh outcome")

    instance.wait = unknown
    expected = asyncio.CancelledError if cancel else policy.PolicyError
    with pytest.raises(expected):
        asyncio.run(instance.run(config))
    assert instance.state["refresh_count"] == instance.state["film_spent"] == 1
    assert instance.state["film_reserved"] == 0
    assert instance.state["result_uncertain"] is True and clicks == [[100, 100]]


def test_identical_candidates_and_absent_film_counts_do_not_block_round_limit(rig):
    instance, frame, target, other, reads, outcomes, clicks, config = rig
    reads.append(frame(other))
    outcomes.extend([frame(other), frame(other), frame(other)])
    result = asyncio.run(instance.run(config | {"max_film_cost": 3}))
    assert result["reason"] == "refresh_limit_reached"
    assert result["refresh_count"] == result["film_spent"] == result["refresh_limit"] == 3
    assert clicks == [[100, 100]] * 3 and not reads and not outcomes


def test_refresh_confirmation_belongs_to_the_same_round(rig):
    instance, frame, target, other, reads, outcomes, clicks, config = rig
    reads.append(frame(other))
    outcomes.extend([
        dict(valid=True, scene="refresh_confirm", controls={"dialog_confirm": [200, 200]}),
        frame(target),
    ])
    result = asyncio.run(instance.run(config))
    assert result["reason"] == "target_found" and result["refresh_count"] == 1
    assert result["result_uncertain"] is False and result["replaced"] is False
    assert clicks == [[100, 100], [200, 200]] and not outcomes


def test_replacement_and_legacy_saved_settings_cannot_enable_input(rig):
    instance, frame, target, other, reads, outcomes, clicks, config = rig
    normalized = policy.validate_config(config | {"selected_slot": 0, "snapshot": frame(target)}, instance.catalog)
    assert normalized["auto_replace"] is False and "snapshot" not in normalized
    with pytest.raises(policy.PolicyError) as error:
        runtime._point(frame(target) | {"controls": {"replace": [20, 20]}}, "replace")
    assert error.value.code == "input_disabled" and not clicks


@pytest.fixture
def affix_reader():
    reader = vision.AffixRefreshVision.__new__(vision.AffixRefreshVision)
    reader.affixes = {row["id"]: row for row in policy.load_catalog()["affixes"]}
    return reader


@pytest.mark.parametrize("missing", [(), ("引",), ("顶",), ("引", "顶")])
def test_lightning_damage_aliases_preserve_raw_text_and_full_effect(affix_reader, missing):
    canonical = "自身造成的【引雷】，【落雷】，【轰雷】和【轰顶】的落雷伤害+5%，可叠加"
    raw = canonical
    expected = {}
    for character, source, target in (("引", "【雷】", "【引雷】"), ("顶", "【轰】", "【轰顶】")):
        if character in missing:
            raw = raw.replace(target, source)
            expected[source] = target
    result = affix_reader._affix(raw, tuple(affix_reader.affixes))
    assert result["id"] == 12300797 and result["value"] == "5" and result["text"] == raw
    if expected:
        assert result["corrected_text"] == vision._normal(canonical)
        assert result["ocr_corrections"] == expected
    else:
        assert "corrected_text" not in result and "ocr_corrections" not in result


@pytest.mark.parametrize("raw", [
    "自身造成的【雷】触发概率+5%，可叠加",
    "造成【轰】时，获得0.1点费用，可叠加",
    "自身造成的【雷】，【落雷】，【轰雷】和【轰】的落雷伤害+11%，可叠加",
    "自身造成的【雷】，【落雷】，【轰雷】和【轰】的落雷伤害+5.5%，可叠加",
    "自身造成的【雷】，【雷】，【轰雷】和【轰】的落雷伤害+5%，可叠加",
    "自身造成的【雷】，【落雷】，【轰】和【轰】的落雷伤害+5%，可叠加",
])
def test_lightning_aliases_cannot_change_other_effects_or_legal_values(affix_reader, raw):
    with pytest.raises(ValueError, match="complete native effect and legal value"):
        affix_reader._affix(raw, tuple(affix_reader.affixes))


def test_no_candidate_window_never_reads_film_templates(affix_reader):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    image[295:337, 225:1055] = [64, 123, 255]

    def match(_image, key):
        assert key in {"refresh_label", "no_candidate_label"}
        return {"center": [950, 215]}

    affix_reader.templates = SimpleNamespace(match=match)
    affix_reader._required_template = lambda _image, key: {"center": [0, 0]}
    result = affix_reader._main(image)
    assert result["valid"] and result["candidate"] is None
    assert result["film_remaining"] is None and result["controls"]["refresh"] == [950, 215]


def test_failure_evidence_preserves_full_observed_pixels_without_recapture(affix_reader, monkeypatch):
    pixels = np.zeros((720, 1280, 3), dtype=np.uint8)
    pixels[:, :640] = [201, 20, 70]
    pixels[:, 640:] = [10, 160, 240]
    expected = pixels.copy()
    captures = []
    saved = []

    def capture():
        captures.append(True)
        if len(captures) > 1:
            raise OSError("capture unavailable")
        return {"success": True, "image": pixels}

    def save_png(data):
        saved.append(data)
        return {"status": "saved", "evidence_id": "sample", "screenshot": "errors/session/sample.png"}

    affix_reader.app = SimpleNamespace(capture=capture)
    affix_reader.templates = SimpleNamespace(begin_frame=lambda _image: None)
    affix_reader._dialog = lambda _image: None
    affix_reader._ocr_sequence = 0
    monkeypatch.setattr(failure_evidence, "save_evidence_png", save_png)
    assert affix_reader.observe()["valid"] is False
    pixels[:] = 0  # Simulate a provider reusing its capture buffer.
    evidence = affix_reader.save_failure_evidence(cid="sample", label="candidate", error="unreadable")
    assert evidence["status"] == "saved" and evidence["captured_at"]
    assert len(captures) == len(saved) == 1
    restored = cv2.cvtColor(cv2.imdecode(np.frombuffer(saved[0], dtype=np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
    assert np.array_equal(restored, expected)
    assert affix_reader.observe()["valid"] is False
    evidence = affix_reader.save_failure_evidence(cid="sample", label="candidate", error="capture unavailable")
    assert evidence["status"] == "skipped" and evidence["reason"] == "capture_image_unavailable"
    assert len(saved) == 1  # A failed capture must not save the older frame again.
