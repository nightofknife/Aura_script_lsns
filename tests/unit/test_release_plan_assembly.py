"""External release plans include runtime deep-dive assets, never model research."""
from pathlib import Path

from scripts.release.assemble_release_plans import (
    collect_plan_files,
    stage_plan_tree,
    validate_selected_files,
)


RUNTIME_ASSETS = (
    "data/four_views_geometry.json",
    "data/models/deep_dive_entities.onnx",
    "data/models/deep_dive_entities.json",
)


def test_assembly_stages_required_runtime_assets_without_model_experiments(tmp_path):
    plans = tmp_path / "source/plans"
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
        path.write_bytes(contents[relative])
    files = collect_plan_files(plans)
    validate_selected_files(files)
    selected = {destination.relative_to("plans/resonance_pc").as_posix()
                for _, destination in files}
    assert selected == set(allowed)
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
