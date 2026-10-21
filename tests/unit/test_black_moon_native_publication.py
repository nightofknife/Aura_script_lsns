"""Native generation publishes a complete bank or preserves the prior bank."""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType

from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[2]


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def generation(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    module = importlib.import_module("build_black_moon_local_shop_templates")
    plan = tmp_path / "plan"
    output = plan / "templates/commerce/shop/black_moon/native"
    monkeypatch.setattr(module, "PLAN_ROOT", plan)
    monkeypatch.setattr(module, "OUTPUT", output)
    catalog_module = ModuleType("aura_resonance_devtools.item_catalog")
    catalog_module.QUALITY_NUMBERS = {"green": 2}
    monkeypatch.setitem(sys.modules, "aura_resonance_devtools.item_catalog", catalog_module)
    catalog = tmp_path / "catalog.json"
    products = [{"item_id": item, "name": item, "quality": "green", "category": "material",
                 "icon_resource_url": f"ui/items/{item}"} for item in ("first", "second")]
    catalog.write_text(json.dumps({"products": products}), encoding="utf-8")

    class Native:
        def __init__(self, *args):
            self.blockers = []
            self.hashes = {"ui/items.asset": "native-hash"}

        def node(self, path):
            return {"rect_client": [0, 0, 120, 119]}

        def named(self, relative, name):
            return (relative, name), name

        def image(self, key, obj):
            return Image.new("RGBA", (120, 119), (83, 155, 211, 255))

        def provenance(self, key, obj):
            return {"bundle": key[0], "name": obj}

        def layer(self, canvas, node, origin):
            canvas.paste((50, 100, 200, 255), (0, 0, 120, 119))
            return {}

    def controls(native):
        return [{"name": name, "file": module.save_image(
            f"{name}.png", Image.new("RGB", (4, 3), (80, 130, 200))), "threshold": .90,
                 "source": {"crop": [0, 0, 4, 3]}}
                for name in module.PREPARE_NAMES]

    def evidence(native):
        module.write_json(module.OUTPUT / "sources/native_ui.json", {"source": "ui"})
        module.write_json(module.OUTPUT / "sources/native_assets.json", {"source": native.hashes})

    monkeypatch.setattr(module, "Native", Native)
    monkeypatch.setattr(module, "manufacture_prepare_controls", controls)
    monkeypatch.setattr(module, "evidence", evidence)
    records = {module.STORE_PREFAB: {"bundle_sha256": "native-prefab"}}
    old = output / "items/old_normal.png"
    old.parent.mkdir(parents=True)
    Image.new("RGB", (4, 3), "red").save(old)
    (output / "manifest.json").write_text(json.dumps({
        "files": {old.relative_to(plan).as_posix(): {"sha256": sha256(old.read_bytes()).hexdigest()}}
    }), encoding="utf-8")
    (output / "sources").mkdir()
    (output / "sources/native_ui.json").write_text('{"old": "ui"}', encoding="utf-8")
    (output / "sources/native_assets.json").write_text('{"old": "assets"}', encoding="utf-8")
    neighbour = plan / "templates/commerce/shop/black_moon/calibrated/keep.png"
    neighbour.parent.mkdir(parents=True)
    Image.new("L", (3, 3), 255).save(neighbour)
    return module, plan, output, catalog, records


def test_missing_catalog_preserves_manifest_resources_and_provenance(generation):
    module, plan, output, catalog, records = generation
    before = snapshot(plan)
    with pytest.raises((ValueError, FileNotFoundError)):
        module.manufacturing(None, None, records, catalog.with_name("missing.json"))
    assert snapshot(plan) == before


def test_partial_extraction_preserves_bank(generation, monkeypatch):
    module, plan, output, catalog, records = generation
    original = module.Native.named

    def named(self, relative, name):
        if name == "second":
            raise ValueError("missing native sprite")
        return original(self, relative, name)

    monkeypatch.setattr(module.Native, "named", named)
    before = snapshot(plan)
    with pytest.raises(ValueError, match="blocker|incomplete"):
        module.manufacturing(None, None, records, catalog)
    assert snapshot(plan) == before


def test_failed_provenance_write_preserves_bank(generation, monkeypatch):
    module, plan, output, catalog, records = generation
    original = module.write_json

    def write(path, value):
        if path.name == "native_assets.json":
            raise OSError("disk full")
        original(path, value)

    monkeypatch.setattr(module, "write_json", write)
    before = snapshot(plan)
    with pytest.raises(OSError, match="disk full"):
        module.manufacturing(None, None, records, catalog)
    assert snapshot(plan) == before


def test_failed_publish_rolls_back_resources_and_manifest(generation, monkeypatch):
    module, plan, output, catalog, records = generation
    original = Path.replace
    failed = False

    def replace(path, target):
        nonlocal failed
        if Path(target) == output / "manifest.json" and not failed:
            failed = True
            raise OSError("publish interrupted")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    before = snapshot(plan)
    with pytest.raises(OSError, match="publish interrupted"):
        module.manufacturing(None, None, records, catalog)
    assert failed
    assert snapshot(plan) == before


def test_read_only_publish_target_does_not_interrupt_other_file_rollback(generation, monkeypatch):
    module, plan, output, catalog, records = generation
    replace, write = Path.replace, Path.write_bytes

    def fail_replace(path, target):
        if Path(target) == output / "manifest.json":
            raise PermissionError("manifest is read-only")
        return replace(path, target)

    def fail_write(path, content):
        if path == output / "manifest.json":
            pytest.fail("An unchanged read-only target must not be rewritten during rollback")
        return write(path, content)

    monkeypatch.setattr(Path, "replace", fail_replace)
    monkeypatch.setattr(Path, "write_bytes", fail_write)
    before = snapshot(plan)
    with pytest.raises(PermissionError, match="read-only"):
        module.manufacturing(None, None, records, catalog)
    assert snapshot(plan) == before


@pytest.mark.parametrize("products", [[], [{"item_id": "same"}, {"item_id": "same"}],
                                     [{"item_id": "../escape"}], [{"item_id": "C:/escape"}]])
def test_invalid_catalog_never_starts_generation(generation, monkeypatch, products):
    module, plan, output, catalog, records = generation
    catalog.write_text(json.dumps({"products": products}), encoding="utf-8")
    monkeypatch.setattr(module, "Native", lambda *args: pytest.fail("Catalog must be validated first"))
    before = snapshot(plan)
    with pytest.raises(ValueError):
        module.manufacturing(None, None, records, catalog)
    assert snapshot(plan) == before


def test_silently_omitted_product_is_not_published(generation, monkeypatch):
    module, plan, output, catalog, records = generation
    original = module.build_items

    def build(native, path, cards, frame, origin):
        original(native, path, cards, frame, origin)
        cards["items"].pop()

    monkeypatch.setattr(module, "build_items", build)
    before = snapshot(plan)
    with pytest.raises(ValueError, match="coverage is incomplete"):
        module.manufacturing(None, None, records, catalog)
    assert snapshot(plan) == before


def test_preparation_blocker_is_not_published(generation, monkeypatch):
    module, plan, output, catalog, records = generation
    original = module.manufacture_prepare_controls

    def controls(native):
        native.blockers.append({"kind": "control_extraction"})
        return original(native)

    monkeypatch.setattr(module, "manufacture_prepare_controls", controls)
    before = snapshot(plan)
    with pytest.raises(ValueError, match="blockers"):
        module.manufacturing(None, None, records, catalog)
    assert snapshot(plan) == before


def test_failed_cleanup_rolls_back_already_published_bank(generation, monkeypatch):
    module, plan, output, catalog, records = generation
    original = module.cleanup_previous_resources

    def cleanup(previous, current):
        original(previous, current)
        raise OSError("cleanup interrupted after deletion")

    monkeypatch.setattr(module, "cleanup_previous_resources", cleanup)
    before = snapshot(plan)
    with pytest.raises(OSError, match="cleanup interrupted"):
        module.manufacturing(None, None, records, catalog)
    assert snapshot(plan) == before


def test_success_publishes_complete_bank_and_is_idempotent(generation):
    module, plan, output, catalog, records = generation
    result = module.manufacturing(None, None, records, catalog)
    assert len(result["templates"]) == 6
    assert {row["item_id"] for row in result["cards"]["items"]} == {"first", "second"}
    assert not (output / "items/old_normal.png").exists()
    assert (plan / "templates/commerce/shop/black_moon/calibrated/keep.png").exists()
    assert result["source"]["bundle_sha256"] == {"ui/items.asset": "native-hash"}
    assert result["catalog"]["sha256"] == sha256(catalog.read_bytes()).hexdigest()
    assert all((plan / ref).is_file() for ref in result["files"])
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == result
    first = snapshot(plan)
    assert module.manufacturing(None, None, records, catalog) == result
    assert snapshot(plan) == first


def test_staging_is_inside_repository_and_removed_on_failure(generation, monkeypatch):
    module, plan, output, catalog, records = generation
    original = module.save_image
    staging = []

    def save(name, image):
        stage = module._STAGED_OUTPUT.get()
        assert stage.is_relative_to(ROOT / ".pytest_tmp")
        staging.append(stage)
        original(name, image)
        raise OSError("staging image failed")

    monkeypatch.setattr(module, "save_image", save)
    before = snapshot(plan)
    with pytest.raises(OSError, match="staging image failed"):
        module.manufacturing(None, None, records, catalog)
    assert snapshot(plan) == before
    assert staging and all(not path.exists() for path in staging)
    assert module._STAGED_OUTPUT.get() is None


@pytest.mark.parametrize("fault", ["missing_control", "orphan", "corrupt", "wrong_size",
                                  "wrong_mode", "duplicate_output", "bad_crop", "empty_mask",
                                  "wrong_mask_size", "wrong_mask_mode"])
def test_invalid_generated_resource_bank_is_not_published(generation, monkeypatch, fault):
    module, plan, output, catalog, records = generation
    original = module.manufacture_prepare_controls

    def controls(native):
        rows = original(native)
        if fault == "missing_control":
            return rows[:-1]
        if fault == "orphan":
            module.save_image("unowned.png", Image.new("L", (2, 2)))
        if fault == "corrupt":
            module._output_path(plan / rows[0]["file"]).write_bytes(b"not an image")
        if fault == "wrong_size":
            module.save_image("shop_entry.png", Image.new("RGB", (3, 3)))
        if fault == "wrong_mode":
            module.save_image("shop_entry.png", Image.new("L", (4, 3), 255))
        if fault == "duplicate_output":
            original_path = module._output_path(plan / rows[1]["file"])
            rows[1]["file"] = rows[0]["file"]
            original_path.unlink()
        if fault == "bad_crop":
            rows[0]["source"]["crop"] = [0, 0, 0, 3]
        if fault in {"empty_mask", "wrong_mask_size", "wrong_mask_mode"}:
            rows[0]["mask_file"] = module.save_image("shop_entry.mask.png", Image.new(
                "RGB" if fault == "wrong_mask_mode" else "L",
                (3, 3) if fault == "wrong_mask_size" else (4, 3),
                0 if fault == "empty_mask" else 255))
        return rows

    monkeypatch.setattr(module, "manufacture_prepare_controls", controls)
    before = snapshot(plan)
    with pytest.raises((ValueError, OSError)):
        module.manufacturing(None, None, records, catalog)
    assert snapshot(plan) == before


@pytest.mark.parametrize("fault", ["wrong_size", "duplicate_output", "disagreed_crop"])
def test_invalid_item_geometry_or_output_alias_never_publishes(generation, monkeypatch, fault):
    module, plan, output, catalog, records = generation
    original = module.build_items

    def items(native, catalog_path, cards, frame, origin):
        original(native, catalog_path, cards, frame, origin)
        row = cards["items"][0]
        if fault == "wrong_size":
            module.save_image("items/first_normal.png", Image.new("RGB", (2, 3)))
        elif fault == "duplicate_output":
            module._output_path(plan / cards["items"][1]["file"]).unlink()
            cards["items"][1]["file"] = row["file"]
        else:
            row["source"]["icon_rect"] = [0, 0, 2, 3]

    monkeypatch.setattr(module, "build_items", items)
    before = snapshot(plan)
    with pytest.raises(ValueError, match="size|Duplicate|crop"):
        module.manufacturing(None, None, records, catalog)
    assert snapshot(plan) == before


@pytest.mark.parametrize("owners,allowed", [
    (("local_tab_selected", "local_tab_unselected"), True),
    (("batch_on", "batch_off"), False),
])
def test_only_proven_local_tab_geometry_can_share_a_native_mask(generation, monkeypatch, owners, allowed):
    module, plan, output, catalog, records = generation
    original = module.manufacture_prepare_controls

    def controls(native):
        rows = original(native)
        shared = module.save_image("shared.mask.png", Image.new("L", (4, 3), 255))
        for row in rows:
            if row["name"] in owners:
                row["mask_file"] = shared
        return rows

    monkeypatch.setattr(module, "manufacture_prepare_controls", controls)
    before = snapshot(plan)
    if allowed:
        result = module.manufacturing(None, None, records, catalog)
        rows = {row["name"]: row for row in result["templates"]}
        assert rows[owners[0]]["mask_file"] == rows[owners[1]]["mask_file"]
        assert rows[owners[0]]["file"] != rows[owners[1]]["file"]
    else:
        with pytest.raises(ValueError, match="Unexpected shared native mask"):
            module.manufacturing(None, None, records, catalog)
        assert snapshot(plan) == before


def test_shipped_native_bank_satisfies_role_geometry_and_identity_validation(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    module = importlib.import_module("build_black_moon_local_shop_templates")
    manifest = json.loads((module.OUTPUT / "manifest.json").read_text(encoding="utf-8"))
    products = module.catalog_products(module.PLAN_ROOT / "data/meta/black_moon_local_shop_catalog.json")
    before = snapshot(module.OUTPUT)
    with module.staged_generation():
        for reference in manifest["files"]:
            source = module.PLAN_ROOT / reference
            target = module._output_path(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        module.validate_generation(manifest, [row["item_id"] for row in products])
    assert snapshot(module.OUTPUT) == before


@pytest.mark.parametrize("kind", ["stage", "publish", "missing_item", "blocker"])
def test_prepare_only_failure_preserves_existing_items_and_controls(generation, monkeypatch, kind):
    module, plan, output, catalog, records = generation
    module.manufacturing(None, None, records, catalog)
    before = snapshot(plan)
    original = module.manufacture_prepare_controls

    def controls(native):
        if kind == "stage":
            original(native)
            raise OSError("prepare image failed")
        if kind == "blocker":
            native.blockers.append({"kind": "control_extraction"})
        return original(native)

    monkeypatch.setattr(module, "manufacture_prepare_controls", controls)
    if kind == "publish":
        replace = Path.replace

        def fail(path, target):
            if Path(target) == output / "manifest.json":
                raise OSError("prepare publish failed")
            return replace(path, target)

        monkeypatch.setattr(Path, "replace", fail)
    if kind == "missing_item":
        (output / "items/first_normal.png").unlink()
        before = snapshot(plan)
    with pytest.raises((OSError, ValueError)):
        module.update_prepare_controls(None, None, records)
    assert snapshot(plan) == before


def test_prepare_only_preserves_items_and_provenance_without_catalog(generation):
    module, plan, output, catalog, records = generation
    module.manufacturing(None, None, records, catalog)
    catalog.unlink()
    items = snapshot(output / "items")
    sources = snapshot(output / "sources")
    module.update_prepare_controls(None, None, records)
    assert snapshot(output / "items") == items
    assert snapshot(output / "sources") == sources
