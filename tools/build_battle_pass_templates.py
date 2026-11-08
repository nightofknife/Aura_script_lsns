"""Rebuild the approved battle-pass controls from installed native assets.

This authoring command never captures, matches or controls the running game.
Shared reward/home anchors are reused byte-for-byte from daily runtime assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from shutil import copyfile
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
OUT = PLAN / "development/template_sources/rewards/battle_pass"
NATIVE = PLAN / "templates/rewards/battle_pass"
CATALOG = PLAN / "data/meta/battle_pass_templates_pc.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_entry(native):
    from PIL import Image
    main = native.Prefab("ui/mainui/mainui.asset")
    parent = "/MainUI/Group_Common/Group_TopRight/Btn_Mission"
    icon, icon_source = main.image(parent + "/Img_Icon")
    badge, badge_source = main.image(parent + "/Img_Remind")
    rt = main.trans[main.paths[parent + "/Img_Remind"]]
    x = round(rt["m_AnchorMin"]["x"] * icon.width + rt["m_AnchoredPosition"]["x"] - rt["m_Pivot"]["x"] * badge.width)
    y = round(icon.height - (rt["m_AnchorMin"]["y"] * icon.height + rt["m_AnchoredPosition"]["y"] - rt["m_Pivot"]["y"] * badge.height) - badge.height)
    left, top = min(0, x), min(0, y)
    image = Image.new("RGBA", (max(icon.width, x + badge.width) - left, max(icon.height, y + badge.height) - top))
    image.alpha_composite(icon, (-left, -top))
    image.alpha_composite(badge, (x - left, y - top))
    image = image.resize((round(image.width * 2 / 3), round(image.height * 2 / 3)), Image.Resampling.LANCZOS)
    mask = image.getchannel("A").point(lambda value: 255 if value >= 224 else 0)
    return image, mask, [icon_source, badge_source]


def save_render(key, image, mask, source, recipe, profile):
    import numpy as np
    image_path = NATIVE / f"{key}.png"
    image.save(image_path)
    item = {**profile, "template": image_path.relative_to(PLAN).as_posix(), "mask": None,
            "size": list(image.size), "sha256": sha(image_path)}
    if mask is not None:
        pixels = np.array(mask)
        if np.issubdtype(pixels.dtype, np.floating):
            path = NATIVE / f"{key}.weights.npy"
            np.save(path, pixels.astype(np.float32), allow_pickle=False)
            encoding = "float32_npy"
        else:
            from PIL import Image
            path = NATIVE / f"{key}.mask.png"
            Image.fromarray(pixels.astype(np.uint8)).save(path)
            encoding = "uint8_binary_png"
        item.update(mask=path.relative_to(PLAN).as_posix(), mask_sha256=sha(path), mask_encoding=encoding)
    else:
        item["mask_encoding"] = "none"
    provenance = {"file": item["template"], "mask": item["mask"], "sha256": item["sha256"],
                  "mask_sha256": item.get("mask_sha256"), "source": source, "refinement_recipe": recipe}
    return item, provenance


def reuse_shared(key, profile):
    from PIL import Image
    source = PLAN / "templates/rewards/daily" / f"{key}.png"
    target = NATIVE / source.name
    copyfile(source, target)
    item = {**profile, "template": target.relative_to(PLAN).as_posix(), "mask": None,
            "size": list(Image.open(source).size), "sha256": sha(target)}
    mask = source.with_name(f"{key}.mask.png")
    if profile["mask_encoding"] != "none":
        target_mask = NATIVE / mask.name
        copyfile(mask, target_mask)
        item.update(mask=target_mask.relative_to(PLAN).as_posix(), mask_sha256=sha(target_mask))
    provenance = {"file": item["template"], "mask": item["mask"], "sha256": item["sha256"],
                  "mask_sha256": item.get("mask_sha256"), "source": [{"runtime_file": source.relative_to(ROOT).as_posix(), "sha256": sha(source)}],
                  "note": "Exact reuse of approved daily runtime asset; not regenerated native pixels."}
    return item, provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, required=True)
    parser.add_argument("--pythonlibs", type=Path)
    args = parser.parse_args()
    import build_daily_reward_templates as native
    native.configure(args)
    from _battle_pass_tabs_renderer import render_tab
    from _battle_pass_buttons_renderer import render_button
    for relative in ("ui/mainui.asset", "ui/common.asset", "ui/bp_quest.asset", "ui/bp_quest/quest_slices.asset", "ui/font/originpack.asset"):
        native.asset_table(relative)
    NATIVE.mkdir(parents=True, exist_ok=True)
    recipes = json.loads((OUT / "battle_pass_refinements.json").read_text(encoding="utf-8"))
    profiles = json.loads((OUT / "matching_profiles.json").read_text(encoding="utf-8"))
    templates, sources = {}, {}
    image, mask, source = render_entry(native)
    templates["main_entry_with_red"], sources["main_entry_with_red"] = save_render(
        "main_entry_with_red", image, mask, source, {"mask_alpha_floor": 224, "client_scale": 2 / 3}, profiles["main_entry_with_red"])
    for key, recipe in recipes.items():
        renderer = render_tab if recipe["renderer_family"] == "tabs" else render_button
        image, mask, source = renderer(native, recipe)
        templates[key], sources[key] = save_render(key, image, mask, source, recipe, profiles[key])
    for key in ("reward_title", "reward_exit", "city_main"):
        templates[key], sources[key] = reuse_shared(key, profiles[key])
    write_json(CATALOG, {"schema_version": 1, "client_size": [1280, 720],
        "provenance": "development/template_sources/rewards/battle_pass/native_provenance.json", "templates": templates})
    write_json(OUT / "native_provenance.json", {"kind": "native_battle_pass_templates", "client_reference": [1280, 720],
        "scale": 2 / 3, "sources": native.SOURCE, "templates": sources,
        "limitations": ["Native assets and fonts only; no screenshot-derived template pixels.",
            "Text and layout use approved Pillow reconstruction recipes, not Unity dynamic rendering.",
            "Historical screenshot scores do not verify newly regenerated images or independent live behavior.",
            "Shared reward/home anchors reused byte-for-byte from the daily runtime assets."]})
    print(json.dumps({key: item["size"] for key, item in templates.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
