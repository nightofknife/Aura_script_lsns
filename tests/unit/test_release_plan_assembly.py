"""External release plans include runtime deep-dive assets, never model research."""
from pathlib import Path

import pytest
import yaml

from scripts.release.assemble_release_plans import (
    collect_plan_files,
    stage_plan_tree,
    validate_selected_files,
)


RUNTIME_ASSETS = (
    "data/deep_dive_events/healing.json",
    "data/deep_dive_vortex/catalog.json",
    "data/four_views_geometry.json",
    "data/models/deep_dive_entities.onnx",
    "data/models/deep_dive_entities.json",
)


def test_assembly_stages_required_runtime_assets_without_model_experiments(tmp_path):
    plans = tmp_path / "source/plans"
    base = plans / "aura_base"
    base.mkdir(parents=True)
    (base / "manifest.yaml").write_text("package: base", encoding="utf-8")
    for relative in ("assets/mumu/scrcpy/server.jar", "assets/mumu/android_touch/arm64/touch",
                     "src/platform/mumu/scrcpy_capture.py"):
        retired = base / relative
        retired.parent.mkdir(parents=True, exist_ok=True)
        retired.write_bytes(b"retired platform fixture")
    package = plans / "resonance_pc"
    allowed = ("manifest.yaml", "data/meta/products.json", "data/input_profiles/client.json",
               "templates/board.png", *RUNTIME_ASSETS)
    excluded = (
        "data/unrelated.json", "data/cache/four_views_geometry.json",
        "data/models/experiment.onnx", "data/models/training_metrics.json",
        "data/models/checkpoint.pt", "data/models/deep_dive_entities.onnx.tmp",
        "data/models/cache/deep_dive_entities.onnx",
        "data/models/state/deep_dive_entities.json",
        "data/models/__pycache__/model.pyc", "data/models/credentials.json",
    )
    contents = {}
    for relative in (*allowed, *excluded):
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        contents[relative] = ("fixture:" + relative).encode() + b"\x00\xff\r\n"
        if relative == "manifest.yaml":
            contents[relative] = b"package: fixture\n"
        path.write_bytes(contents[relative])
    files = collect_plan_files(plans)
    validate_selected_files(files)
    selected = {destination.relative_to("plans/resonance_pc").as_posix()
                for _, destination in files if destination.parts[1] == "resonance_pc"}
    assert selected == set(allowed)
    assert not any("mumu" in destination.parts for _, destination in files)
    destination = tmp_path / "assembled/plans"
    stage_plan_tree(files, destination)
    staged = destination / "resonance_pc"
    assert {path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()} == set(allowed)
    for relative in allowed:
        assert (staged / relative).read_bytes() == contents[relative]


def test_current_deep_dive_model_and_geometry_are_selected_for_release():
    repo = Path(__file__).resolve().parents[2]
    selected = {destination.as_posix(): source for source, destination in collect_plan_files(repo / "plans")}
    validate_selected_files(list((source, Path(destination)) for destination, source in selected.items()))
    for relative in RUNTIME_ASSETS:
        name = "plans/resonance_pc/" + relative
        assert name in selected
        assert selected[name].is_file()
        assert selected[name].stat().st_size > 0


@pytest.mark.parametrize("missing", ["aura_base", "resonance_pc"])
def test_release_requires_every_runtime_plan(tmp_path, missing):
    for name in {"aura_base", "resonance_pc", "aura_benchmark", "resonance"} - {missing}:
        package = tmp_path / name
        package.mkdir()
        (package / "manifest.yaml").write_text("package: fixture", encoding="utf-8")
    with pytest.raises(RuntimeError, match=missing):
        collect_plan_files(tmp_path)


def test_release_ignores_development_and_retired_plans(tmp_path):
    for name in ("aura_base", "resonance_pc", "aura_benchmark", "resonance", "custom_experiment"):
        package = tmp_path / name
        package.mkdir()
        (package / "manifest.yaml").write_text("package: fixture", encoding="utf-8")
        (package / "probe.py").write_text("raise RuntimeError('never import')", encoding="utf-8")
    selected = collect_plan_files(tmp_path)
    assert {dest.parts[1] for _, dest in selected} == {"aura_base", "resonance_pc"}
    with pytest.raises(RuntimeError, match="Non-runtime Plan"):
        validate_selected_files([(tmp_path / "probe.py", Path("plans/aura_benchmark/probe.py"))])


@pytest.mark.parametrize("relative", ["assets/mumu/scrcpy/server.jar", "src/platform/mumu/session.py"])
def test_release_validation_rejects_explicit_retired_platform_payload(tmp_path, relative):
    with pytest.raises(RuntimeError, match="Retired platform"):
        validate_selected_files([(tmp_path / "fixture", Path("plans/aura_base") / relative)])


def test_staging_filters_developer_definitions_and_manifest_aliases(tmp_path):
    plans = tmp_path / "source"
    for name in ("aura_base", "resonance_pc"):
        (plans / name).mkdir(parents=True)
        (plans / name / "manifest.yaml").write_text("package: fixture", encoding="utf-8")
    pc = plans / "resonance_pc"
    (pc / "tasks").mkdir()
    (pc / "src").mkdir()
    algorithm = pc / "src/scan.py"
    algorithm.write_text("def scan(): return 'runtime algorithm'\n", encoding="utf-8")
    definitions = {
        "run": {"meta": {"role": "user_task"}, "steps": {}},
        "helper": {"meta": {"role": "internal"}, "steps": {}},
        "probe": {"meta": {"role": "developer"}, "steps": {}},
    }
    (pc / "tasks/mixed.yaml").write_text(yaml.safe_dump(definitions), encoding="utf-8")
    (pc / "tasks/dev.yaml").write_text(yaml.safe_dump({"calibrate": definitions["probe"]}), encoding="utf-8")
    tasks = [{"id": name, "source": "tasks/mixed.yaml"} for name in definitions]
    tasks += [{"id": "mixed/" + name, "source": "tasks/mixed.yaml"} for name in definitions]
    tasks += [{"id": "calibrate", "source": "tasks/dev.yaml"}]
    (pc / "manifest.yaml").write_text(yaml.safe_dump({"exports": {"tasks": tasks}}), encoding="utf-8")
    files = collect_plan_files(plans)
    assert "plans/resonance_pc/tasks/dev.yaml" not in {dest.as_posix() for _, dest in files}
    destination = tmp_path / "release/plans"
    stage_plan_tree(files, destination)
    staged = destination / "resonance_pc"
    assert set(yaml.safe_load((staged / "tasks/mixed.yaml").read_text("utf-8"))) == {"run", "helper"}
    manifest = yaml.safe_load((staged / "manifest.yaml").read_text("utf-8"))
    assert {task["id"] for task in manifest["exports"]["tasks"]} == {"run", "helper", "mixed/run", "mixed/helper"}
    assert (staged / "src/scan.py").read_bytes() == algorithm.read_bytes()
    assert set(yaml.safe_load((pc / "tasks/mixed.yaml").read_text("utf-8"))) == set(definitions)
