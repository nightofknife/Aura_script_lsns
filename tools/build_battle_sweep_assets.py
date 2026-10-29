"""Produce sweep controls and their provenance from the installed native UI.

This reads local bundles only. It never captures or controls the game.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"


def build(game_root, devtools_src, pythonlibs):
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(pythonlibs), str(devtools_src)]
    from PIL import Image, ImageChops, ImageDraw
    from _trade_investment_native import inspect_prefab, load_bundle
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image

    data = next(Path(game_root).glob("*_Data"))
    assets = data / "Patch/Asset"
    metadata = data / "il2cpp_data/Metadata/global-metadata.dat"
    destination = PLAN / "templates/battle/sweep"
    destination.mkdir(parents=True, exist_ok=True)
    bundles, prefabs, sources = {}, {}, {}

    def load(relative):
        if relative not in bundles:
            path = assets / relative
            sources[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            bundles[relative] = load_bundle(path, metadata)
        return bundles[relative]

    manifest = next(obj.read_typetree() for obj in load("Asset").objects
                    if obj.type.name == "AssetBundleManifest")
    bundle_names = dict(manifest["AssetBundleNames"])
    bundle_infos = dict(manifest["AssetBundleInfos"])

    def native_nodes(relative):
        if relative not in prefabs:
            env = load(relative)
            # ShowItem embeds VFX with ordinary Transform parents. Those are
            # not canvas controls and have no RectTransform bounds to export.
            rects = {obj.path_id: obj.read_typetree() for obj in env.objects
                     if obj.type.name == "RectTransform"}

            def canvas_chain(pid):
                if pid == 0:
                    return True
                return pid in rects and canvas_chain(rects[pid]["m_Father"]["m_PathID"])

            class CanvasObjects:
                assets = env.assets
                objects = [obj for obj in env.objects
                           if obj.type.name != "RectTransform" or canvas_chain(obj.path_id)]

            prefabs[relative] = {node["path"]: node for node in inspect_prefab(CanvasObjects())["nodes"]}
        return prefabs[relative]

    def control(relative, path):
        node = native_nodes(relative)[path]
        x, y, width, height = node["rect_client"]
        left, top = max(0, x), max(0, y)
        right, bottom = min(1280, x + width), min(720, y + height)
        return {"rect": [left, top, right-left, bottom-top],
                "point": [(left+right)//2, (top+bottom)//2],
                "rect_source": path, "prefab": relative}

    def text(relative, path):
        node = native_nodes(relative)[path]
        tree = next(component["tree"] for component in node["components"]
                    if "m_Text" in component["tree"])
        return {"text": tree["m_Text"], "roi": node["rect_client"],
                "rect_source": path, "prefab": relative}

    def image_control(relative, path, filename):
        record = control(relative, path)
        node = native_nodes(relative)[path]
        tree = next(component["tree"] for component in node["components"]
                    if component["tree"].get("m_Sprite", {}).get("m_PathID"))
        file_id, sprite_id = tree["m_Sprite"]["m_FileID"], tree["m_Sprite"]["m_PathID"]
        env = load(relative)
        wanted = next(iter(env.assets)).externals[file_id-1].path.rsplit("/", 1)[-1] if file_id else None
        candidates = [relative]
        source_id = next(index for index, value in bundle_names.items() if value == relative)
        candidates.extend(bundle_names[index] for index in bundle_infos[source_id]["AssetBundleDependencies"])
        obj, sprite_bundle = None, None
        for candidate in candidates:
            sprites = load(candidate)
            if wanted and not any(asset.name == wanted for asset in sprites.assets):
                continue
            obj = next((obj for obj in sprites.objects
                        if obj.type.name == "Sprite" and obj.path_id == sprite_id), None)
            if obj is not None:
                sprite_bundle = candidate
                break
        if obj is None:
            raise RuntimeError(f"Native sweep sprite not found: {path}")
        sprite = obj.read()
        width, height = record["rect"][2:]
        image = _logical_sprite_image(sprite, obj.version).convert("RGBA").resize(
            (width, height), Image.Resampling.LANCZOS)
        tint = tuple(round(tree["m_Color"][key]*255) for key in ("r", "g", "b", "a"))
        image = ImageChops.multiply(image, Image.new("RGBA", image.size, tint))
        # Exclude dynamically drawn text (label and resource count) from the
        # native button image. OCR supplies those independent identities.
        alpha = image.getchannel("A")
        draw = ImageDraw.Draw(alpha)
        x, y = record["rect"][:2]
        for child in native_nodes(relative).values():
            if child["path"].startswith(path + "/") and any("m_Text" in c["tree"] for c in child["components"]):
                cx, cy, cw, ch = child["rect_client"]
                draw.rectangle((cx-x-3, cy-y-3, cx+cw-x+3, cy+ch-y+3), fill=0)
        image.putalpha(alpha)
        output = destination / f"{filename}.png"
        image.save(output)
        record.update(template=output.relative_to(PLAN).as_posix(), sprite=sprite.m_Name,
                      sprite_bundle=sprite_bundle, sprite_path_id=sprite_id,
                      sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                      native_image_type=tree.get("m_Type"), tint_rgba=list(tint),
                      text_mask="child_text_rectangles_with_three_pixel_guard")
        return record

    gp_profiles = []
    for name, root in (("material", "Battle_Material"), ("equipmentday", "Battle_EquipmentDay"),
                       ("equipmentweek", "Battle_EquipmentWeek")):
        relative = f"ui/chapter/battle_material/battle_{name}.asset"
        prefix = f"/{root}/Group_Right"
        gp_profiles.append({
            "name": name, "page_after": "battle_preparation",
            "anchors": [text(relative, prefix + "/Group_Reward/Txt_R"),
                        text(relative, prefix + "/Group_Details/Img_D/Txt_")],
            "stage_name": text(relative, prefix + "/Group_Details/Txt_Name"),
            "sweep": image_control(relative, prefix + "/Group_Skip/Btn_Skip", f"{name}_sweep"),
            "sweep_label": text(relative, prefix + "/Group_Skip/Btn_Skip/Txt_T"),
            "group_start_label": text(relative, prefix + "/Group_Skip/Btn_Start/Txt_T"),
            "manual_only": image_control(relative, prefix + "/Btn_Start", f"{name}_manual_only"),
            "manual_only_label": text(relative, prefix + "/Btn_Start/Txt_"),
            "availability_source": "GetLevelPassBySelf(levelId); own completion required; PlayerData.lua:2924-2930",
        })
    ct_prefab = "ui/activity/activitysafe/fastbattle.asset"
    ct_profiles = []
    for name, button, anchor_paths, source in (
        ("ct_expel", "/FastBattle/Group_Safa/Group_Level/Group_Information/Group_Battle/Group_Start/Btn_Battle",
         ["/FastBattle/Group_Safa/Group_Level/Group_Information/Group_Progress/Group_Schedule/Group_Right/Group_Reward/Txt_"],
         "UIFastBattleSafeController.ClickBattle:484-536"),
        ("ct_bounty", "/FastBattle/Group_Safa/Group_XS/Group_Information/Group_TZ/Btn_QW",
         ["/FastBattle/Group_Safa/Group_XS/Group_Information/Group_Top/Txt_"],
         "UIFastBattleXSController.ClickBattle:174-205"),
        ("ct_regional", "/FastBattle/Group_Battle/Group_Battle/Group_1/Group_Information/Group_TZ/Btn_QW",
         ["/FastBattle/Group_Battle/Group_Battle/Group_1/Group_Information/Group_Difficult/Btn_Difficult/Txt_"],
         "UIFastBattleBattleController.TryBattle:345-383"),
    ):
        ct_profiles.append({"name": name, "page_after": "mission_preparation",
                            "manual_only": control(ct_prefab, button),
                            "manual_only_label": text(ct_prefab, button + "/Txt_"),
                            "anchors": [text(ct_prefab, path) for path in anchor_paths],
                            "availability_source": source + "; native controllers expose squad entry only, no sweep control"})
    skip = "ui/common/battleskiptips.asset"
    reward = "ui/common/showitem.asset"
    reward_viewport = control(reward, "/ShowItem/ScrollGrid_Items")
    reward_close = control(reward, "/ShowItem/Btn_OK")
    # Select the canvas gap left of the item viewport, not an item button.
    reward_close["point"] = [reward_viewport["rect"][0]//2, 360]
    reward_close["point_derivation"] = "center of canvas margin left of ScrollGrid_Items"
    arrest = "ui/prison/arrestprisoner/arresttips.asset"
    arrest_result = "ui/prison/arrestprisoner/skiparrestsettlement.asset"

    def formation_tab(index):
        record = control(skip, f"/BattleSkipTips/ScrollGrid_Tab/Viewport/Grid/Group_Item_{index-1:03d}/Btn_Tab")
        x, y, width, height = record["rect"]
        # The number Text uses horizontal overflow and a zero-width native
        # rectangle. Its center is 17 px inside the button's left edge.
        record.update(index=index, index_roi=[x+6, y+6, min(40, width-6), min(44, height-6)])
        return record

    catalog = {
        "schema_version": 1, "status": "native_generated_not_live_verified",
        "reference_client": [1280, 720], "sources": sources,
        "gp_profiles": gp_profiles, "unsupported_profiles": ct_profiles,
        "skip": {"title": text(skip, "/BattleSkipTips/Img_Bg/Txt_T"),
                 "confirm": control(skip, "/BattleSkipTips/Btn_Confirm"),
                 "confirm_label": text(skip, "/BattleSkipTips/Btn_Confirm/Txt_T"),
                 "cancel": control(skip, "/BattleSkipTips/Btn_Close"),
                 "cancel_label": text(skip, "/BattleSkipTips/Btn_Close/Txt_T"),
                 "formation_current": text(skip, "/BattleSkipTips/Txt_Team"),
                 "formation_tabs": [formation_tab(index) for index in range(1, 5)],
                 "formation_source": "UIBattleSkipTipsController.Init:5-10 MoveToTop; SetElementTab:12-32 zero-padded index; ClickBtnTab:55-68 updates Txt_Team to curSquadIndex"},
        "reward": {"titles": [text(reward, f"/ShowItem/Group_{height}/Group_Title/Txt_Title")
                              for height in (300, 580, 800, 890)],
                   "identity_text": "获得物品", "viewport": reward_viewport, "close": reward_close,
                   "source": "UIBattleSkipTipsController.TryArrest:326-359; GoBack + CommonTips.OpenShowItem(reward)"},
        "arrest": {"cancel": control(arrest, "/ArrestTips/Btn_Cancel"),
                   "source": "UIArrestTipsViewFunction:30-33; GoBack + Cancel callback",
                   "minimum_text": "最小", "plus_text": "+1",
                   "count_region": [460, 315, 350, 100], "minimum_region": [330, 340, 110, 80],
                   "plus_region": [770, 340, 115, 80],
                   "result_title": text(arrest_result, "/SkipArrestSettlement/Txt_"),
                   "result_close": control(arrest_result, "/SkipArrestSettlement/Btn_")},
        "execution_source": "UIBattleSkipTipsController.StartSkipWin:71-230; single battle.skip_battle per confirmation, no quantity selector",
        "resource_source": "CommonTips.OpenBuyEnergyTips:924-959; no generic sweep ticket; level-specific itemCost still applies",
    }
    output = PLAN / "data/meta/battle_sweep_pc.json"
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return {"catalog": str(output), "profiles": len(gp_profiles)+len(ct_profiles)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, required=True)
    parser.add_argument("--pythonlibs", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.game_root, args.devtools_src, args.pythonlibs), ensure_ascii=False))
