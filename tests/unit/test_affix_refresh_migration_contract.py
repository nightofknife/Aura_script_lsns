"""Keep the new refresh budget, candidate handoff and uncertain spending."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from plans.resonance_pc.src.actions import _affix_refresh_policy as policy
from plans.resonance_pc.src.actions import _affix_refresh_runtime as runtime
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

    def frame(definition=None, remaining=4):
        candidate = (dict(id=definition["id"], value=definition["min_value"], text=definition["description"])
                     if definition else None)
        return dict(valid=True, scene="affix", replace_mode=True, film_remaining=remaining,
                    candidate=candidate, controls={"refresh": [100, 100], "dialog_confirm": [200, 200]})

    instance = runtime.AffixRefreshRuntime.__new__(runtime.AffixRefreshRuntime)
    instance.catalog, instance.app = catalog, object()
    instance.state = dict(cid="offline-contract", film_spent=0, film_reserved=0,
                          refresh_count=0, film_limit=0, replaced=False)
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
    config = dict(target_affix_id=target["id"], comparison="any", threshold="",
                  max_film_cost=1, auto_replace=True)
    return instance, frame, target, other, current_frames, outcomes, clicks, config


@pytest.mark.parametrize("matched", [False, True])
def test_last_budget_candidate_is_evaluated_and_never_applied(rig, matched):
    instance, frame, target, other, reads, outcomes, clicks, config = rig
    reads.extend([frame(other), frame(other)])
    outcomes.append(frame(target if matched else other, remaining=3))
    result = asyncio.run(instance.run(config))
    assert result["reason"] == ("target_found" if matched else "film_limit_reached")
    assert result["film_spent"] == result["refresh_count"] == 1
    assert result["film_reserved"] == 0 and result["result_uncertain"] is False
    assert result["auto_replace"] is result["replaced"] is False
    assert clicks == [[100, 100]] and not outcomes


def test_zero_budget_preserves_current_candidate_without_refresh(rig):
    instance, frame, target, other, reads, outcomes, clicks, config = rig
    reads.append(frame(other))
    result = asyncio.run(instance.run(config | {"max_film_cost": 0}))
    assert result["reason"] == "film_limit_reached" and not clicks
    assert result["current_candidate"]["id"] == other["id"]


@pytest.mark.parametrize("cancel", [False, True])
def test_unknown_result_keeps_reservation_and_never_retries_or_replaces(rig, cancel):
    instance, frame, target, other, reads, outcomes, clicks, config = rig
    reads.extend([frame(other), frame(other)])

    async def unknown(predicate, **kwargs):
        if cancel:
            raise asyncio.CancelledError()
        raise policy.PolicyError("observation_timeout", "unknown refresh outcome")

    instance.wait = unknown
    expected = asyncio.CancelledError if cancel else policy.PolicyError
    with pytest.raises(expected):
        asyncio.run(instance.run(config))
    assert instance.state["film_spent"] == 0 and instance.state["film_reserved"] == 1
    assert instance.state["result_uncertain"] is True and clicks == [[100, 100]]


def test_replacement_and_legacy_saved_settings_cannot_enable_input(rig):
    instance, frame, target, other, reads, outcomes, clicks, config = rig
    normalized = policy.validate_config(config | {"selected_slot": 0, "snapshot": frame(target)}, instance.catalog)
    assert normalized["auto_replace"] is False and "snapshot" not in normalized
    with pytest.raises(policy.PolicyError) as error:
        runtime._point(frame(target) | {"controls": {"replace": [20, 20]}}, "replace")
    assert error.value.code == "input_disabled" and not clicks
