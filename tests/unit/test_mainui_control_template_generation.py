"""Opt-in native reproduction using local extraction dependencies, without game input.

Set AURA_NATIVE_GAME_DATA, AURA_NATIVE_PYTHONLIBS, and AURA_NATIVE_DEVTOOLS_SRC
to exercise the complete production generator. CI without local native resources
still exercises the catalog/resource contracts in the navigation asset tests.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
CATALOGS = ("data/meta/mainui_control_templates.json", "data/meta/depart_button_template.json")


def test_native_generator_reproduces_four_current_artworks_without_old_pixels(tmp_path):
    names = ("AURA_NATIVE_GAME_DATA", "AURA_NATIVE_PYTHONLIBS", "AURA_NATIVE_DEVTOOLS_SRC")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip("Native resource reproduction requires " + ", ".join(missing))
    game_data, pythonlibs, devtools_src = (Path(os.environ[name]).resolve() for name in names)
    source = tmp_path / "recipes-only"
    target = tmp_path / "generated"
    recipes = {}
    for reference in CATALOGS:
        recipes[reference] = json.loads((PLAN / reference).read_text(encoding="utf-8"))
    shared = recipes[CATALOGS[0]]
    departure = recipes[CATALOGS[1]]
    entries = {key: departure if "catalog" in entry else entry
               for key, entry in shared["controls"].items()}
    # A nondefault setting in every profile catches a generator that silently
    # restores hardcoded ROI/threshold defaults while refreshing image metadata.
    for index, entry in enumerate(entries.values()):
        entry["match"]["threshold"] = 0.81 + index * 0.01
    for reference, recipe in recipes.items():
        path = source / reference
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
    preview = target / "preview.png"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools/build_mainui_control_templates.py"),
         "--game-data", str(game_data), "--pythonlibs", str(pythonlibs),
         "--devtools-src", str(devtools_src), "--catalog-root", str(source),
         "--output", str(target), "--preview", str(preview)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not (source / "templates").exists()
    produced = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}
    expected_templates = {entry["template"] for entry in entries.values()}
    assert len(expected_templates) == 4
    assert produced == expected_templates | set(CATALOGS) | {"preview.png"}
    generated_shared = json.loads((target / CATALOGS[0]).read_text(encoding="utf-8"))
    generated_depart = json.loads((target / CATALOGS[1]).read_text(encoding="utf-8"))
    generated_entries = {key: generated_depart if "catalog" in entry else entry
                         for key, entry in generated_shared["controls"].items()}
    for key, original in entries.items():
        entry = generated_entries[key]
        assert entry["match"] == original["match"]
        image = target / entry["template"]
        assert image.read_bytes() == (PLAN / entry["template"]).read_bytes(), key
        assert sha256(image.read_bytes()).hexdigest() == entry["sha256"]
    # Every recorded bundle really belongs to this installed source; raw font
    # data are recorded only by digest and never emitted among generator files.
    for relative, expected_hash in generated_depart["source_hashes"].items():
        assert sha256((game_data / "Patch/Asset" / relative).read_bytes()).hexdigest() == expected_hash
    assert {font["language"] for font in generated_depart["fonts"]} == {"chinese", "english"}
    assert all(len(font["font_sha256"]) == 64 for font in generated_depart["fonts"])
