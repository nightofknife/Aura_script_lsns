"""Real screenshot ROI replay and control-only resource/task contracts."""
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest
import yaml

from plans.resonance_pc.src.actions import _black_moon_local_prepare_vision as prepare
from plans.resonance_pc.src.actions import _black_moon_local_shop_vision as shop


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/black_moon_prepare"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def frame(row):
    pixels = np.zeros((720, 1280, 3), np.uint8)
    for patch in row["patches"]:
        path = FIXTURES / patch["file"]
        assert sha256(path.read_bytes()).hexdigest() == patch["sha256"]
        x, y, width, height = patch["rect"]
        observed = np.array(Image.open(path).convert("RGB"))
        assert observed.shape == (height, width, 3)
        pixels[y:y + height, x:x + width] = observed
    return pixels


@pytest.mark.parametrize("row", MANIFEST["frames"], ids=lambda row: f"real-frame-{row['number']}")
def test_native_templates_match_all_expected_control_states(row):
    reader = prepare.BlackMoonLocalPrepareVision()
    pixels = frame(row)
    state = reader.observe(pixels)
    matches = {**state["matches"], "shop_entry": reader.match(pixels, "shop_entry")}
    assert {name for name, value in matches.items() if value["found"]} == set(row["expected_matches"])
    for key in ("page_state", "local_selected", "batch_enabled", "ready"):
        assert state[key] == row[key]
    for name in row["expected_matches"]:
        assert matches[name]["score"] >= .90


def test_prepare_reader_never_loads_stock_assets_or_catalogue(monkeypatch):
    shop._resources.cache_clear()
    opened = []
    real_open = shop.Image.open

    def tracked(path, *args, **kwargs):
        opened.append(Path(path).as_posix())
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(shop.Image, "open", tracked)
    assert not hasattr(shop, "_catalog")
    real_read = Path.read_text

    def read_without_products(path, *args, **kwargs):
        assert path.name != "black_moon_local_shop_catalog.json", "Preparation cannot read products"
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_without_products)
    reader = prepare.BlackMoonLocalPrepareVision()
    reader.observe(np.zeros((720, 1280, 3), np.uint8))
    assert opened and not any("/items/" in path or "/cards/" in path for path in opened)
    assert len(reader._data[0]["templates"]) == 6
    assert all(not pixels.flags.writeable for pixels in reader._data[1].values())
    assert {name.split(".")[0] for name in map(lambda path: Path(path).name, opened)} == {
        *prepare.CONTROL_NAMES, "local_tab"}


def test_unknown_templates_do_not_imply_off_or_ready():
    state = prepare.BlackMoonLocalPrepareVision().observe(np.zeros((720, 1280, 3), np.uint8))
    assert state["page_state"] == "unknown"
    assert state["local_selected"] is state["batch_enabled"] is None
    assert state["ready"] is False


def test_standalone_task_has_only_ordered_preparation_nodes():
    task_path = ROOT / "plans/resonance_pc/tasks/black_moon_local_prepare_pc.yaml"
    task = yaml.safe_load(task_path.read_text(encoding="utf-8"))["black_moon_local_prepare_pc"]
    assert task["meta"]["role"] == "internal" and task["meta"]["entry_point"] is False and task["meta"]["concurrency"] == "exclusive"
    assert not task["meta"].get("inputs")
    nodes = task["steps"]
    assert list(nodes) == ["require_resolution", "enter_shop", "select_local", "enable_batch", "verify_ready"]
    previous = "require_resolution"
    for name in list(nodes)[1:]:
        assert nodes[name]["action"] == "resonance_pc.prepare_black_moon_local_shop_step"
        assert nodes[name]["depends_on"] == previous and nodes[name]["params"] == {"step": name}
        previous = name
    manifest = yaml.safe_load((ROOT / "plans/resonance_pc/manifest.yaml").read_text(encoding="utf-8"))
    assert any(row["name"] == "resonance_pc.prepare_black_moon_local_shop_step" for row in manifest["exports"]["actions"])
