"""Produce affix-refresh text templates from the installed native UI fonts.

This asset importer only reads client bundles and catalog metadata. It never
captures the running game, sends input, or evaluates a matcher. Runtime readers
need the output directory only, not UnityPy, the game files, or the original fonts.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_PREFAB = "ui/characterinfo/weapon/group_strengthen_splited/group_windows.asset"
PROMPT_PREFAB = "ui/common/prompt/prompt.asset"
FONT_BUNDLE = "ui/font/originpack.asset"
REFERENCE_SHA256 = "92fa5fa4004d7e2e182f012a2d6861be40521cc3e672c2f8f7b21fceb4369069"
LIVE_REFERENCE_SHA256 = "79f88d5be6db7b855f7cbb4a994af759ed6f991335caf918e97c84dca482e2f0"
MODAL_REFERENCE_SHA256 = "867ea7732813998ad77acf1c2b019191a381268b143453e557a4286633fe8de5"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--pythonlibs", type=Path, default=ROOT / ".codex_tmp/item_quality_research/pythonlibs")
    parser.add_argument("--output", type=Path, default=ROOT / "plans/resonance_pc/templates/inventory/affix_refresh")
    parser.add_argument("--catalog", type=Path, default=ROOT / "plans/resonance_pc/data/meta/affix_refresh.json")
    parser.add_argument("--reference-screenshot", type=Path, action="append", default=[],
                        help="Approved reference screenshot, repeat to retain calibration from multiple recorded SHA256 sources")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(args.pythonlibs.resolve()))
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from _trade_investment_native import load_bundle

    game_data = args.game_data.resolve()
    asset_root = game_data / "Patch/Asset"
    metadata = game_data / "il2cpp_data/Metadata/global-metadata.dat"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))

    def bundle(relative):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return load_bundle(asset_root / relative, metadata)

    def text_styles(relative):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            trees = {obj.path_id: obj.read_typetree() for obj in bundle(relative).objects
                     if obj.type.name in ("GameObject", "RectTransform", "MonoBehaviour")}
        transforms = {key: row for key, row in trees.items() if "m_AnchoredPosition" in row}

        @lru_cache(maxsize=None)
        def node_path(key):
            row = transforms.get(key)
            return (node_path(row["m_Father"]["m_PathID"]) + "/" +
                    trees[row["m_GameObject"]["m_PathID"]]["m_Name"]) if row else ""

        paths = {row["m_GameObject"]["m_PathID"]: node_path(key) for key, row in transforms.items()}
        return {paths[row["m_GameObject"]["m_PathID"]]: row
                for row in trees.values() if "m_FontData" in row}

    main_styles, prompt_styles = text_styles(MAIN_PREFAB), text_styles(PROMPT_PREFAB)
    fonts = {}
    for obj in bundle(FONT_BUNDLE).objects:
        if obj.type.name == "Font":
            font = obj.read()
            fonts[obj.path_id] = {"name": font.m_Name, "data": bytes(font.m_FontData)}

    @lru_cache(maxsize=None)
    def font_for(font_id, size):
        return ImageFont.truetype(io.BytesIO(fonts[font_id]["data"]), size)

    def glyph(text, tree, mode, background=None):
        style = tree["m_FontData"]
        font_id, size = style["m_Font"]["m_PathID"], style["m_FontSize"]
        if style["m_FontStyle"] != 0:
            raise ValueError("Affix template requires a native regular-style font face")
        font = font_for(font_id, size)
        # Use the project's native-size-then-2/3 renderer convention. Cache the
        # opened font face so 176 equipment names do not repeatedly decode it.
        mask = Image.new("L", (max(500, round(font.getlength(text)) + 40), size * 3), 0)
        ImageDraw.Draw(mask).text((15, size), text, font=font, fill=255, anchor="lt")
        mask = mask.resize((round(mask.width * 2/3), round(mask.height * 2/3)), Image.Resampling.BILINEAR)
        alpha = np.asarray(mask).astype(float)[:, :, None] / 255
        tint = [round(tree["m_Color"][key]*255) for key in ("r", "g", "b")]
        if background is None:
            background = [255,255,255] if mode == "dark" else [35,35,35]
        rgb = np.rint(alpha*np.array(tint)+(1-alpha)*np.array(background)).astype(np.int16)
        minimum, maximum = rgb.min(axis=2), rgb.max(axis=2)
        if mode == "yellow":
            r,g,b = rgb[:,:,0],rgb[:,:,1],rgb[:,:,2]
            selected = (r>=170)&(g>=110)&(b<=130)&((r-b)>=65)
        elif mode == "dark":
            selected = (maximum<=150)&((maximum-minimum)<=65)
        else:
            selected = (minimum>=180)&((maximum-minimum)<=65)
        pixels = np.where(selected,255,0).astype(np.uint8)
        points = np.argwhere(pixels)
        if points.size == 0:
            raise ValueError(f"Native font rendered no foreground: {text}")
        top, left = points.min(axis=0)
        bottom, right = points.max(axis=0)+1
        tight = pixels[top:bottom, left:right]
        return np.pad(tight, 2), {
            "font": fonts[font_id]["name"], "font_path_id": font_id,
            "font_sha256": hashlib.sha256(fonts[font_id]["data"]).hexdigest(),
            "native_font_size": size, "native_tint": tint,
            "glyph_size": [int(right-left), int(bottom-top)], "padding": 2,
        }

    assets = {}

    def save(file, pixels):
        path = output / file
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(pixels, mode="L").save(path)
        assets[file] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size": [int(pixels.shape[1]), int(pixels.shape[0])]}

    def item(key, text, source, roi, mode="bright", *, prompt=False):
        tree = (prompt_styles if prompt else main_styles)[source]
        backgrounds = {"replacement_header": [64,123,255], "replace_label": [64,123,255],
                       "no_candidate_label": [69,0,0], "no_candidate_hint": [69,0,0],
                       "refresh_label": [255,255,255], "film_label": [255,255,255],
                       "confirm_label": [255,184,0], "cancel_label": [35,35,35],
                       "level_prefix": [51,51,51]}
        pixels, provenance = glyph(text, tree, mode, backgrounds.get(key))
        file = f"tokens/{key}.png"
        save(file, pixels)
        return {"file": file, "roi": roi, "mode": mode, "threshold": .90,
                "text": text, "prefab": PROMPT_PREFAB if prompt else MAIN_PREFAB,
                "prefab_node": source, **provenance}

    main = "/Group_Windows/"
    tokens = {}
    specs = [
        ("replacement_header", "请选择以下其中一个随机词缀", "Txt_Tips", [240,299,450,30], "bright"),
        ("no_candidate_label", "暂无替换词缀", "Img_NoHaveChangeEntry/Txt_NoHaveChangeEntry", [240,205,230,55], "bright"),
        ("no_candidate_hint", "需提升装备等级或使用道具", "Img_NoHaveChangeEntry/Txt_Condition", [470,205,320,55], "bright"),
        ("refresh_label", "刷新词缀", "Btn_Add/Txt_", [884,203,148,30], "dark"),
        ("film_label", "剩余胶卷数量：", "Btn_Add/Txt_RemainingNum", [874,229,165,25], "dark"),
        ("confirm_label", "确认", "Btn_Confirm/Img_Use/Txt_Confirm", [650,606,186,38], "dark"),
        ("cancel_label", "取消", "Btn_Cancel/Txt_Cancel", [444,606,186,38], "bright"),
        ("level_prefix", "LV", "Group_Equipment_Row/Txt_EquipmentLvNum/Txt_EquipmentLv", [995,75,67,32], "yellow"),
        ("replace_label", "剩余可替换次数", "Txt_RemainingChangeNum", [705,298,170,30], "bright"),
    ]
    for key, text, source, roi, mode in specs:
        tokens[key] = item(key, text, main+source, roi, mode)
    for key in ("refresh", "replace"):
        tokens[key+"_prompt"] = item(
            key+"_prompt", catalog["dialog_prompts"][key+"_confirm"],
            "/Prompt/Txt_Prompt", [220,180,840,250], prompt=True)
    tokens["dialog_confirm"] = item("dialog_confirm", "确认", "/Prompt/Btn_Confirm/Group_/Txt_Confirm",
                                      [650,465,615,75], prompt=True)
    tokens["dialog_cancel"] = item("dialog_cancel", "取消", "/Prompt/Btn_Cancel/Group_/Txt_Cancel",
                                     [15,465,615,75], prompt=True)

    equipment = []
    title_node = main+"Group_Equipment_Row/Txt_EquipmentName"
    for row in catalog["equipment"]:
        file = f"equipment/{row['id']}.png"
        pixels, provenance = glyph(row["name"], main_styles[title_node], "bright", [51,51,51])
        save(file, pixels)
        equipment.append({"id": row["id"], "name": row["name"], "file": file,
                          "roi": [372,75,590,31], "mode": "bright", "threshold": .90,
                          "prefab_node": title_node, **provenance})

    digits = {}
    for key, node, roi, mode, maximum in (
        ("level", "Group_Equipment_Row/Txt_EquipmentLvNum", [995,75,67,32], "yellow", 3),
        ("film", "Btn_Add/Txt_RemainingNum", [874,229,151,25], "dark", 7),
        ("replace", "Txt_RemainingChangeNum", [705,298,170,30], "yellow", 7),
    ):
        files, glyph_sizes = {}, {}
        for digit in range(10):
            file = f"digits/{key}/{digit}.png"
            # The replacement counter colors its numerical span via rich text;
            # its parent Text component itself remains white.
            tree = dict(main_styles[main+node])
            if key == "replace":
                tree["m_Color"] = {"r": 1., "g": 184/255, "b": 0., "a": 1.}
            background = {"level": [51,51,51], "film": [255,255,255], "replace": [64,123,255]}[key]
            pixels, provenance = glyph(str(digit), tree, mode, background)
            save(file, pixels)
            files[str(digit)] = file
            glyph_sizes[str(digit)] = provenance["glyph_size"]
        digits[key] = {"files": files, "roi": roi, "mode": mode, "min_digits": 1,
                       "max_digits": maximum, "threshold": .90, "padding": 2,
                       "glyph_sizes": glyph_sizes, "prefab_node": main+node,
                       "font": provenance["font"], "font_path_id": provenance["font_path_id"],
                       "native_font_size": provenance["native_font_size"],
                       "normalization": "tight glyph bounds; two black pixels of padding; no stretching"}

    references = []
    for reference_path in args.reference_screenshot:
        source_hash = hashlib.sha256(reference_path.read_bytes()).hexdigest()
        if source_hash not in (REFERENCE_SHA256, LIVE_REFERENCE_SHA256, MODAL_REFERENCE_SHA256):
            raise ValueError("This reference crop map is scoped to the approved starting screenshot SHA256")
        screenshot = Image.open(reference_path).convert("RGB")
        is_modal = source_hash == MODAL_REFERENCE_SHA256
        expected_size = (1280,720) if is_modal else (1282,752)
        if screenshot.size != expected_size:
            raise ValueError(f"The approved reference must retain its original {expected_size} dimensions")
        client_box = [0,0,1280,720] if is_modal else [1,31,1281,751]
        rgb = np.array(screenshot.crop(tuple(client_box))).astype(np.int16)
        reference = {"sha256": source_hash, "client_box": client_box,
                     "kind": "authorized_run_refresh_confirmation_capture" if is_modal else "user_supplied_starting_screenshot",
                     "scope": "Refresh confirmation prompt and two button labels only" if is_modal else "Fixed labels, equipment title, and individual visible digit glyphs only"}
        references.append(reference)

        def capture_glyph(roi, mode):
            x,y,width,height = roi
            crop = rgb[y:y+height,x:x+width]
            low,high = crop.min(axis=2),crop.max(axis=2)
            if mode == "bright":
                selected = (low>=180)&((high-low)<=65)
            elif mode == "dark":
                selected = (high<=150)&((high-low)<=65)
            else:
                r,g,b = crop[:,:,0],crop[:,:,1],crop[:,:,2]
                selected = (r>=170)&(g>=110)&(b<=130)&((r-b)>=65)
            ys,xs = np.nonzero(selected)
            if len(xs) == 0:
                raise ValueError("The approved crop contains no foreground glyph")
            foreground = np.uint8(selected[ys.min():ys.max()+1,xs.min():xs.max()+1])*255
            return np.pad(foreground,2), [int(xs.min()),int(ys.min()),int(xs.max()-xs.min()+1),int(ys.max()-ys.min()+1)]

        def preserve_native(file):
            native_file = "native/"+file
            path = output/native_file
            path.parent.mkdir(parents=True,exist_ok=True)
            if native_file not in assets:
                path.write_bytes((output/file).read_bytes())
                assets[native_file] = dict(assets[file])
            return native_file

        def capture_spec(spec, roi):
            native_file = preserve_native(spec["file"])
            pixels,bounds = capture_glyph(roi,spec["mode"])
            save(spec["file"],pixels)
            spec.update(native_file=native_file, kind="approved_reference_glyph",
                        reference_sha256=source_hash, reference_client_crop=roi,
                        reference_foreground_bounds=bounds,
                        glyph_size=bounds[2:],padding=2)

        if is_modal:
            for key, roi in {
                "refresh_prompt": [455,342,435,36],
                "dialog_confirm": [958,486,56,35],
                "dialog_cancel": [320,486,56,35],
            }.items():
                capture_spec(tokens[key],roi)
            continue

        # Crops contain complete label glyphs and exclude neighboring numbers,
        # equipment icons, button borders, and the pill's curved dark exterior.
        label_crops = {
            "replacement_header": [248,304,220,24],
            "no_candidate_label": [246,215,115,27],
            "no_candidate_hint": [560,215,185,28],
            "refresh_label": [900,206,100,28],
            "film_label": [880,233,111,18],
            "confirm_label": [720,612,50,23],
            "cancel_label": [515,612,50,23],
            "level_prefix": [1015,80,20,24],
            "replace_label": [716,306,116,23],
        }
        if source_hash == LIVE_REFERENCE_SHA256:
            # These two labels are absent while a pending candidate is visible.
            label_crops.pop("no_candidate_label")
            label_crops.pop("no_candidate_hint")
        for key, roi in label_crops.items():
            capture_spec(tokens[key],roi)
        equipment_id = 11800258 if source_hash == REFERENCE_SHA256 else 11800007
        name = next(row for row in equipment if row["id"] == equipment_id)
        capture_spec(name,[372,75,590,31])
        # Every sample is one segmented digit. No whole-number balance or
        # level image is ever used as a constant-value recognition shortcut.
        digit_samples = {
            "level": {"5": [1038,84,7,12], "9": [1047,84,7,12]},
            "film": {"8": [994,236,7,12], "5": [1012,236,7,12]},
            "replace": {"9": [838,311,7,12], "5": [856,311,7,12]},
        }
        if source_hash == LIVE_REFERENCE_SHA256:
            digit_samples = {
                "level": {"6": [1038,84,7,12], "7": [1047,84,7,12]},
                "film": {"4": [1011,236,8,12]},
                "replace": {},
            }
        for kind, samples in digit_samples.items():
            bank = digits[kind]
            bank.setdefault("reference_samples", {})
            for digit, roi in samples.items():
                file = bank["files"][digit]
                native_file = preserve_native(file)
                pixels,bounds = capture_glyph(roi,bank["mode"])
                save(file,pixels)
                bank["glyph_sizes"][digit] = bounds[2:]
                bank["reference_samples"][digit] = {
                    "native_file": native_file, "reference_sha256": source_hash,
                    "reference_client_crop": roi, "glyph_size": bounds[2:],
                    "kind": "single_digit_from_approved_reference"}

    manifest = {"schema_version": 1, "reference_client": [1280,720],
                "tokens": tokens, "equipment": equipment, "digits": digits,
                "image_format": "L8 binary foreground (0 background, 255 glyph), tight + 2px padding",
                "foreground_modes": {"bright": "minRGB>=180 and ptpRGB<=65",
                                     "dark": "maxRGB<=150 and ptpRGB<=65",
                                     "yellow": "R>=170,G>=110,B<=130,R-B>=65"},
                "source_files": {relative: {"sha256": hashlib.sha256((asset_root/relative).read_bytes()).hexdigest()}
                                 for relative in (MAIN_PREFAB, PROMPT_PREFAB, FONT_BUNDLE)},
                "assets": assets, "status": "native_generated_with_scoped_reference_glyphs" if references else "native_generated_unvalidated",
                "reference": references[0] if references else None, "references": references,
                "limitations": ["Font rendering and matching have not been evaluated against screenshots or the running game.",
                                "Native text and shapes target the 1280x720 Simplified Chinese client only.",
                                "Equipment titles must match complete foreground bounds, not a substring of a longer title."]}
    if references:
        manifest["limitations"][0] = (
            "Reference glyphs are calibrated to the recorded source screenshots and any explicitly listed modal capture; "
            "other equipment titles, unseen digits, selection states, and replacement-confirmation wording retain unvalidated native rendering.")
    (output/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(f"Produced {len(tokens)} label templates, {len(equipment)} equipment names, and 30 digit templates.")


if __name__ == "__main__":
    main()
