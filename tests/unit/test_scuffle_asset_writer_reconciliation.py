"""A producer reconciles its previous catalog without deleting shared neighbors."""
import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from tools import build_eternal_scuffle_assets as builder


def generate(plan, colors, previous=None):
    assets = {}
    writer = builder.FunctionalAssetWriter(plan, assets, previous_assets=previous)
    rows = []
    for identity, color in sorted(colors.items()):
        ref = writer.write(Image.new("RGB", (3, 3), color),
                           f"templates/inventory/equipment/art/{identity}/icon.png",
                           "equipment/icon", {"source_sha256": str(identity)})
        rows.append({"id": identity, "template": ref})
    catalog = {"assets": assets, "equipment": rows}
    writer.finalize(catalog, plan / "data/meta/eternal_scuffle.json")
    return catalog


def valid_published(plan):
    catalog = json.loads((plan / "data/meta/eternal_scuffle.json").read_text(encoding="utf-8"))
    for relative, row in catalog["assets"].items():
        assert hashlib.sha256((plan / relative).read_bytes()).hexdigest() == row["sha256"]
    assert all(row["template"] in catalog["assets"] for row in catalog["equipment"])
    return catalog


def test_changed_inputs_remove_old_sharing_outputs_and_preserve_other_producer(tmp_path):
    first = generate(tmp_path, {20: "red", 30: "blue"})
    unrelated = tmp_path / "templates/inventory/equipment/art/30/grid.png"
    unrelated.write_bytes(b"another producer owns this file")
    second = generate(tmp_path, {20: "red", 30: "red"}, first["assets"])
    assert [row["id"] for row in second["equipment"]] == [20, 30]
    assert second["equipment"][0]["template"] == second["equipment"][1]["template"]
    assert not (tmp_path / first["equipment"][1]["template"]).exists()
    assert unrelated.read_bytes() == b"another producer owns this file"
    assert valid_published(tmp_path) == second
    assert generate(tmp_path, {20: "red", 30: "red"}, second["assets"]) == second


def test_new_lower_canonical_identity_removes_previous_physical_image(tmp_path):
    first = generate(tmp_path, {20: "red", 30: "red"})
    second = generate(tmp_path, {10: "red", 20: "red", 30: "red"}, first["assets"])
    assert len(second["assets"]) == 1
    assert second["equipment"][0]["template"].endswith("/10/icon.png")
    assert not (tmp_path / first["equipment"][0]["template"]).exists()
    assert len(list(tmp_path.rglob("*.png"))) == 1
    valid_published(tmp_path)


def test_changed_owned_bytes_keep_old_index_valid_until_publish_and_rerun_stable(tmp_path):
    first = generate(tmp_path, {20: "red"})
    assets = {}
    writer = builder.FunctionalAssetWriter(tmp_path, assets, previous_assets=first["assets"])
    new_ref = writer.write(Image.new("RGB", (3, 3), "blue"), first["equipment"][0]["template"], "equipment/icon", {"source_sha256": "changed"})
    assert new_ref != first["equipment"][0]["template"]
    assert valid_published(tmp_path) == first
    catalog = {"assets": assets, "equipment": [{"id": 20, "template": new_ref}]}
    writer.finalize(catalog, tmp_path / "data/meta/eternal_scuffle.json")
    assert not (tmp_path / first["equipment"][0]["template"]).exists()
    second = generate(tmp_path, {20: "blue"}, assets)
    assert second["equipment"][0]["template"] == new_ref
    assert generate(tmp_path, {20: "blue"}, second["assets"]) == second
    valid_published(tmp_path)


@pytest.mark.parametrize("failure", ["missing_reference", "bad_hash", "bad_size", "publish_failure", "interrupted_publish"])
def test_incomplete_or_failed_generation_preserves_previous_complete_index(tmp_path, monkeypatch, failure):
    first = generate(tmp_path, {20: "red", 30: "blue"})
    old_files = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assets = {}
    writer = builder.FunctionalAssetWriter(tmp_path, assets, previous_assets=first["assets"])
    ref = writer.write(Image.new("RGB", (3, 3), "green"), first["equipment"][0]["template"], "equipment/icon", {})
    catalog = {"assets": assets, "equipment": [{"id": 20, "template": ref}]}
    if failure == "missing_reference": catalog["equipment"].append({"id": 30, "template": "templates/missing.png"})
    elif failure == "bad_hash": assets[ref]["sha256"] = "invalid"
    elif failure == "bad_size": assets[ref]["size"] = [1, 1]
    else:
        original = writer._atomic_write
        def fail_catalog(path, payload):
            if path.suffix == ".json":
                assert valid_published(tmp_path) == first
                if failure == "interrupted_publish": raise KeyboardInterrupt()
                raise OSError("simulated catalog publication failure")
            original(path, payload)
        monkeypatch.setattr(writer, "_atomic_write", fail_catalog)
    with pytest.raises((ValueError, OSError, KeyboardInterrupt)):
        writer.finalize(catalog, tmp_path / "data/meta/eternal_scuffle.json")
    assert valid_published(tmp_path) == first
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == old_files


def test_stale_cleanup_rejects_unsafe_previous_paths_and_modified_owned_files(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        builder.FunctionalAssetWriter(tmp_path, {}, previous_assets={"templates/../../outside.png": {}})
    first = generate(tmp_path, {20: "red", 30: "blue"})
    foreign_change = tmp_path / first["equipment"][1]["template"]
    foreign_change.write_bytes(b"another actor changed this file")
    with pytest.raises(ValueError, match="changed externally"):
        generate(tmp_path, {20: "red"}, first["assets"])
    assert foreign_change.read_bytes() == b"another actor changed this file"


def test_full_build_defers_control_writes_before_later_source_failure(tmp_path, monkeypatch):
    first = generate(tmp_path, {20: "red"})
    action = tmp_path / "src/actions/_eternal_scuffle_policy.py"
    action.parent.mkdir(parents=True)
    action.write_text("# generation fails before ranking\n", encoding="utf-8")
    source = tmp_path / "review"
    source.mkdir()
    Image.new("RGB", (2, 2), "green").save(source / "control.png")
    row = {"id": "H01", "group": "entry", "kind": "template", "file": "control.png", "box": [0, 0, 2, 2], "estimated_client_box": [0, 0, 2, 2], "source": "screen.png", "label": "home"}
    # Missing D01 is a source error occurring after the first control was emitted.
    (source / "manifest.json").write_text(json.dumps({"templates": [row], "sources": [{"source": "screen.png", "sha256": "source"}]}), encoding="utf-8")
    monkeypatch.setattr(builder, "PLAN", tmp_path)
    with pytest.raises(StopIteration):
        builder.build(tmp_path / "research", tmp_path / "metadata", source)
    assert valid_published(tmp_path) == first
    assert not (tmp_path / "templates/battle").exists()


def test_existing_scuffle_controls_have_only_functional_paths_and_shared_mask_profiles():
    plan = Path(__file__).resolve().parents[2] / "plans/resonance_pc"
    catalog = json.loads((plan / "data/meta/eternal_scuffle.json").read_text(encoding="utf-8"))
    mask_refs = set()
    for quality in ("UR", "SSR", "SR", "R"):
        control = catalog["controls"]["occupied_" + quality]
        assert control["threshold"] == .90
        assert control["match_method"] == "TM_SQDIFF_NORMED"
        assert control["source"].endswith(quality)
        mask_refs.add(control["mask"])
    assert mask_refs == {"templates/inventory/equipment/frames/scuffle/occupied_mask.png"}
    metadata = catalog["assets"][next(iter(mask_refs))]
    assert {source["source_sha256"] for source in metadata["source_variants"]} == {
        catalog["controls"]["occupied_" + quality]["source_sha256"] for quality in ("UR", "SSR", "SR", "R")}
    for key, row in catalog["controls"].items():
        if not key.startswith("occupied_"):
            assert row["template"].startswith("templates/battle/scuffle/controls/")
    assert not (plan / "templates/eternal_scuffle").exists()
    assert not list((plan / "templates").glob("battle_*.png"))


def test_card_mask_sharing_does_not_merge_future_different_alpha_profiles(tmp_path):
    source = tmp_path / "source/ui"
    source.mkdir(parents=True)
    for index, quality in enumerate(("UR", "SSR", "SR", "R")):
        image = Image.new("RGBA", (90, 90), (17, 38, 61, 255))
        image.putpixel((22 + index, 0), (17, 38, 61, 0))
        image.save(source / f"RandomBattle_Equipment_{quality}.png")
    catalog = {"assets": {}, "controls": {}}
    builder.add_occupied_templates(catalog, source.parent, tmp_path / "plan")
    refs = [row["mask"] for row in catalog["controls"].values()]
    assert len(set(refs)) == 4
    for index, ref in enumerate(refs):
        with Image.open(tmp_path / "plan" / ref) as mask:
            assert mask.getpixel((22 + index, 0)) == 0
            assert mask.getpixel((22 + (index + 1) % 4, 0)) == 255
