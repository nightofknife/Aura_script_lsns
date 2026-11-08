"""Rebuild screenshot-calibrated native reward markers from stored recipes.

No screenshot pixels are used here. Source fonts and sprites come from the
installed native assets; the saved recipe records the calibrated geometry.
"""
from pathlib import Path
import json

import cv2
import numpy as np
from PIL import Image, ImageDraw


def render_title(builder, recipe):
    show = builder.Prefab("ui/common/showitem.asset")
    path = "/ShowItem/Group_580/Group_Title"
    tree = show.text(path + "/Txt_Title")
    native_font, font_source = builder.font_for(tree)
    native_icon, icon_source = show.image(path + "/Img_Title")
    icon = native_icon.resize(tuple(recipe["icon_size"]), Image.Resampling.LANCZOS).getchannel("A")
    aa = recipe["antialias_scale"]
    font = native_font.font_variant(size=round(recipe["font_size_client"] * aa))
    glyph = Image.new("L", (800 * aa, 100 * aa))
    draw = ImageDraw.Draw(glyph)
    cursor = 12 * aa
    for char in tree["m_Text"]:
        draw.text((cursor, 8 * aa), char, font=font, fill=255,
                  stroke_width=round(recipe["stroke_client"] * aa), stroke_fill=255)
        cursor += draw.textlength(char, font=font) + recipe["tracking_client"] * aa
    box = glyph.getbbox()
    glyph = glyph.crop((box[0] - 3 * aa, box[1] - 3 * aa, box[2] + 3 * aa, box[3] + 3 * aa))
    glyph = glyph.resize((round(glyph.width / aa), round(glyph.height / aa)), Image.Resampling.LANCZOS)
    gx, gy = recipe["glyph_offset_from_icon_client"]
    left, top = min(0, gx), min(0, gy)
    alpha = Image.new("L", (max(icon.width, gx + glyph.width) - left,
                            max(icon.height, gy + glyph.height) - top))
    alpha.paste(icon, (-left, -top))
    alpha.paste(glyph, (gx - left, gy - top))
    radius = recipe["context_dilation_radius"]
    mask = cv2.dilate(np.uint8(np.asarray(alpha) > 8) * 255,
                      np.ones((radius * 2 + 1, radius * 2 + 1), np.uint8))
    return alpha.convert("RGB"), Image.fromarray(mask), [icon_source, font_source]


def apply_refinements(builder):
    recipe_path = builder.OUT / "reward_refinements.json"
    if not recipe_path.is_file():
        return
    recipes = json.loads(recipe_path.read_text(encoding="utf-8"))
    for key, recipe in recipes.items():
        if key == "reward_title":
            image, mask, sources = render_title(builder, recipe)
        elif key == "reward_exit":
            from _daily_reward_exit_renderer import render_exit, exit_match_layers
            rgba = render_exit(builder, recipe)
            gray, mask = exit_match_layers(rgba, recipe["halo"], recipe.get("alpha_floor", 32))
            image = gray.convert("RGB")
            sources = builder.ITEMS[key]["source"]
        else:
            raise ValueError(f"Unsupported native reward refinement: {key}")
        geometry = recipe.get("mask_geometry")
        if geometry:
            if geometry["kind"] != "component_rectangles":
                raise ValueError(f"Unsupported regular mask layout: {geometry['kind']}")
            mask = Image.new("L", image.size, 0)
            draw = ImageDraw.Draw(mask)
            for x0, y0, x1, y1 in geometry["rectangles_xyxy"]:
                if not (0 <= x0 < x1 <= image.width and 0 <= y0 < y1 <= image.height):
                    raise ValueError(f"Invalid mask rectangle for {key}")
                draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
        image_path = builder.NATIVE / f"{key}.png"
        mask_path = builder.NATIVE / f"{key}.mask.png"
        image.save(image_path)
        mask.save(mask_path)
        item = builder.ITEMS[key]
        item.update({
            "size": list(image.size), "sha256": builder.sha(image_path),
            "mask_sha256": builder.sha(mask_path), "source": sources,
            "mask_policy": geometry or {"kind": "glyph_and_surrounding_negative_space"},
            "pixel_encoding": "native_alpha_as_luminance_on_black",
            "match": recipe["match"], "refinement_recipe": recipe,
            "status": "native_candidate_calibrated_on_supplied_screenshot",
            "note": "Source sprite/font raster only; no supplied screenshot pixels. "
                    "The context mask retains dark glyph surroundings to avoid flat-white degeneracy. "
                    "Geometry/rasterization were calibrated on the sole positive screenshot.",
        })
