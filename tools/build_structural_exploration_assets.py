"""Extract the installed client's structural event icons and auto-mode controls.

This is an asset-production command. It does not capture or control a running
client. Only the requested PNGs and their provenance catalog are written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
TYPES = {
    1: "hetero_branches", 2: "hetero_branches", 3: "echo_buoy",
    4: "birch_buoy", 5: "disordered_roots",
}


def build(game_root: Path, devtools_src: Path, pythonlibs: Path | None = None) -> dict:
    sys.dont_write_bytecode = True
    if pythonlibs is not None:
        sys.path.insert(0, str(pythonlibs))
    sys.path.insert(0, str(devtools_src))
    from PIL import Image, ImageChops
    from _trade_investment_native import inspect_prefab, load_bundle
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image

    data = next(game_root.glob("*_Data"))
    assets = data / "Patch/Asset"
    metadata = data / "il2cpp_data/Metadata/global-metadata.dat"
    target = PLAN / "templates/battle/structural"
    target.mkdir(parents=True, exist_ok=True)
    sources, icons, controls, bundles = {}, [], {}, {}

    def load(relative):
        if relative in bundles:
            return bundles[relative]
        path = assets / relative
        sources[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        bundles[relative] = load_bundle(path, metadata)
        return bundles[relative]

    def save(obj, name, size, relative):
        sprite = obj.read()
        image = _logical_sprite_image(sprite, obj.version).resize(tuple(size), Image.Resampling.LANCZOS)
        path = target / (name + ".png")
        image.save(path)
        return {"template": path.relative_to(PLAN).as_posix(), "sprite": sprite.m_Name,
                "path_id": obj.path_id, "bundle": relative, "size": list(image.size),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    relative = "ui/activity/activityglobalbattle/images.asset"
    for obj in load(relative).objects:
        if obj.type.name != "Sprite":
            continue
        name = obj.read().m_Name
        if name.startswith("WorldScan_event_icon_btn_"):
            index = int(name.rsplit("_", 1)[1])
            record = save(obj, f"event_type_{index}", (35, 35), relative)
            icons.append({"index": index, "type": TYPES[index], **record})
        if obj.path_id == 2592199744137304732:
            controls["event_list_open"] = save(obj, "event_list_open", (40, 20), relative)
            original = Image.open(target / "event_list_open.png")
            path = target / "event_list_closed.png"
            original.transpose(Image.Transpose.FLIP_TOP_BOTTOM).save(path)
            controls["event_list_closed"] = {
                **controls["event_list_open"], "template": path.relative_to(PLAN).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "transform": "flip_vertical",
            }

    relative = "ui/mainui.asset"
    wanted = {"map_btn_off": "detail_auto_off", "map_btn_onBlue": "detail_auto_on"}
    for obj in load(relative).objects:
        if obj.type.name == "Sprite" and obj.read().m_Name in wanted:
            name = wanted[obj.read().m_Name]
            controls[name] = save(obj, name, (76, 33), relative)

    # Record the exact prefab controls behind fixed ROIs; no guessed click points.
    layouts = {}
    for relative in ("ui/activity/activityglobalbattle/worldscan.asset", "ui/chapter/battle_dungeonscan.asset"):
        nodes = inspect_prefab(load(relative))["nodes"]
        for node in nodes:
            path = node["path"]
            if path in {
                "/WorldScan/Group_Btn/Btn_Main",
                "/WorldScan/Group_Btn/ScrollGrid_BtnList/Viewport",
                "/Battle_DungeonScan/Group_Right/Group_Auto/Btn_Auto",
                "/Battle_DungeonScan/Group_Right/Group_Fight/Btn_StartFight",
            }:
                layouts[path] = node["rect_client"]

    catalog = {
        "schema_version": 1,
        "provenance": "installed-client-native-assets",
        "status": "native_generated_not_live_verified",
        "sources": sources,
        "layouts": layouts,
        "icons": sorted(icons, key=lambda value: value["index"]),
        "controls": controls,
        "list_roi": [32, 161, 293, 421],
        "list_icon_roi": [86, 161, 55, 421],
        "icon_match_threshold": 0.78,
        "detail_auto_roi": [739, 605, 83, 43],
        "detail_start_roi": [833, 550, 300, 150],
        "manual_auto_roi": [25, 590, 310, 105],
        "list_title_roi": [25, 85, 310, 80],
        "list_arrow_roi": [50, 103, 52, 34],
        "detail_dismiss_point": [500, 80],
        "detail_dismiss_source": "/WorldScan/Group_Map/ScrollView_Map/Viewport/Content/Btn_CloseDetail",
        "scan_roi": [1000, 555, 240, 130],
    }

    # Auto controls have their own catalog so the legacy event-list reader can
    # continue using its existing fields. Rectangles are Unity prefab bounds at
    # the same 1920x1080 -> 1280x720 scale as the event icons above.
    prefab_nodes = {}

    def nodes(relative):
        if relative not in prefab_nodes:
            prefab_nodes[relative] = {
                node["path"]: node for node in inspect_prefab(load(relative))["nodes"]
            }
        return prefab_nodes[relative]

    def node_control(relative, node_path):
        node = nodes(relative)[node_path]
        x, y, width, height = node["rect_client"]
        left, top = max(0, x), max(0, y)
        right, bottom = min(1280, x + width), min(720, y + height)
        rect = [left, top, right - left, bottom - top]
        return {"rect": rect, "point": [(left + right) // 2, (top + bottom) // 2],
                "rect_native_client": [x, y, width, height],
                "rect_source": node_path, "prefab": relative}

    def background(node, name):
        tree = next(component["tree"] for component in node["components"]
                    if component["tree"].get("m_Sprite", {}).get("m_PathID"))
        sprite_id = tree["m_Sprite"]["m_PathID"]
        relative = "ui/activity/activityglobalbattle/images.asset"
        obj = next(obj for obj in load(relative).objects
                   if obj.type.name == "Sprite" and obj.path_id == sprite_id)
        record = save(obj, name, node["rect_client"][2:], relative)
        # Unity multiplies the sprite RGBA by Image.m_Color. In particular the
        # unselected card's native tint is black; its raw sprite is not enough.
        rgba = Image.open(target / (name + ".png")).convert("RGBA")
        color = tree["m_Color"]
        tint = tuple(round(color[channel] * 255) for channel in ("r", "g", "b", "a"))
        ImageChops.multiply(rgba, Image.new("RGBA", rgba.size, tint)).save(target / (name + ".png"))
        record.update({"sha256": hashlib.sha256((target / (name + ".png")).read_bytes()).hexdigest(),
                       "tint_rgba": list(tint), "rect_source": node["path"]})
        return record

    world_prefab = "ui/activity/activityglobalbattle/worldscan.asset"
    detail_prefab = "ui/chapter/battle_dungeonscan.asset"
    auto_prefix = "/WorldScan/Group_Auto"
    world_nodes = nodes(world_prefab)
    first_card = auto_prefix + "/Group_List/StaticGrid_List/Group_Item_000/Btn_Item"
    selected = background(world_nodes[first_card + "/Group_On/Img_Bg"], "auto_card_selected")
    unselected = background(world_nodes[first_card + "/Group_Off/Img_Bg"], "auto_card_unselected")
    # This inset lies before the icon (whose left edge is x=89), so it contains
    # only the native background and no dynamically drawn number or label.
    state_crop = [6, 5, 56, 49]
    state_templates = {}
    for state, record in (("selected", selected), ("unselected", unselected)):
        state_path = target / f"auto_card_{state}_state.png"
        Image.open(PLAN / record["template"]).crop(tuple(state_crop)).save(state_path)
        state_templates[state] = {
            **record, "template": state_path.relative_to(PLAN).as_posix(),
            "size": [state_crop[2] - state_crop[0], state_crop[3] - state_crop[1]],
            "sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
            "crop_from_background": state_crop,
        }
    native_controls = {
        name: node_control(world_prefab, auto_prefix + suffix)
        for name, suffix in {"setup": "/Btn_Setup", "start": "/Btn_Start", "end": "/Btn_End"}.items()
    }
    native_controls["detail_return"] = node_control(
        detail_prefab, "/Battle_DungeonScan/Group_CommonTopLeft/Btn_Return")
    native_controls["map_return"] = node_control(
        world_prefab, "/WorldScan/Group_CommonTopLeft/Btn_Return")
    native_controls["start"].update({"identity_text": "开启自动讨伐",
                                    "identity_roi": world_nodes[auto_prefix + "/Btn_Start/Txt_T"]["rect_client"]})
    native_controls["end"].update({"identity_text": "关闭自动讨伐",
                                  "identity_roi": world_nodes[auto_prefix + "/Btn_End/Txt_T"]["rect_client"]})
    cards = {}
    native_types = (("disordered_roots", "乱序根须"), ("hetero_branches", "异构厄枝"),
                    ("echo_buoy", "混响浮标"), ("birch_buoy", "桦树浮标"))
    for index, (type_name, label) in enumerate(native_types, 1):
        prefix = auto_prefix + f"/Group_List/StaticGrid_List/Group_Item_{index - 1:03d}/Btn_Item"
        click = node_control(world_prefab, prefix)
        label_node = world_nodes[prefix + "/Txt_Type"]
        priority_node = world_nodes[prefix + "/Group_On/Txt_Index"]
        bg = world_nodes[prefix + "/Group_On/Img_Bg"]
        cards[str(index)] = {
            "native_index": index, "type": type_name, "label": label,
            "click_roi": click["rect"], "point": click["point"],
            "label_roi": label_node["rect_client"], "priority_roi": priority_node["rect_client"],
            "background_roi": bg["rect_client"],
            "state_roi": [bg["rect_client"][0] + state_crop[0],
                          bg["rect_client"][1] + state_crop[1],
                          state_crop[2] - state_crop[0], state_crop[3] - state_crop[1]],
            "state_selected": {**state_templates["selected"], "rect_source": prefix + "/Group_On/Img_Bg"},
            "state_unselected": {**state_templates["unselected"], "rect_source": prefix + "/Group_Off/Img_Bg"},
            "selected": {**selected, "rect_source": prefix + "/Group_On/Img_Bg"},
            "unselected": {**unselected, "rect_source": prefix + "/Group_Off/Img_Bg"},
            "rect_source": {"click": prefix, "label": label_node["path"],
                            "priority": priority_node["path"], "background": bg["path"]},
        }
    backgrounds = [card["background_roi"] for card in cards.values()]
    left, top = min(rect[0] for rect in backgrounds), min(rect[1] for rect in backgrounds)
    right = max(rect[0] + rect[2] for rect in backgrounds)
    bottom = max(rect[1] + rect[3] for rect in backgrounds)
    native_controls["list_panel"] = {
        "rect": [left, top, right - left, bottom - top],
        "point": [(left + right) // 2, (top + bottom) // 2],
        "rect_source": [card["rect_source"]["background"] for card in cards.values()],
        "prefab": world_prefab, "rect_derivation": "union_of_four_native_card_backgrounds",
        "identity_rule": "all_four_labels_in_their_native_row_regions",
    }
    buy_prefab, energy_prefab = "ui/energy/energybuytips.asset", "ui/energy/energy.asset"
    buy_text = nodes(buy_prefab)["/EnergyBuyTips/Txt_Tips"]
    energy_text = nodes(energy_prefab)["/Energy/Group_PickEnergyWay/Group_Energy/Txt_Title/Txt_Tips"]
    energy_pages = {
        "buy_tips": {
            "identity_text": "您当前的澄明度不足，是否补充澄明度？",
            "identity_roi": [0, 0, 1280, 720],
            "identity_native_rect": buy_text["rect_client"],
            "identity_rect_source": buy_text["path"],
            "identity_overflow": "native_text_horizontal_overflow_center_aligned",
            "close": node_control(buy_prefab, "/EnergyBuyTips/Btn_Cancel"),
            "close_handler": "UIEnergyBuyTipsViewFunction.EnergyBuyTips_Btn_Cancel_Click:15-17",
            "ignored_noop_control": "/EnergyBuyTips/Btn_Close",
        },
        "energy": {
            "identity_text": "请选择恢复方式", "identity_roi": [0, 0, 1280, 720],
            "identity_native_rect": energy_text["rect_client"],
            "identity_rect_source": energy_text["path"],
            "close": node_control(energy_prefab, "/Energy/Group_PickEnergyWay/Group_CommonTopLeft/Btn_Return"),
            "close_handler": "UIEnergyViewFunction.Energy_Group_PickEnergyWay_Group_CommonTopLeft_Btn_Return_Click:273-276",
        },
    }
    hud_prefab, hud_sprites = "ui/battle/ui.asset", "ui/battle/pause/img.asset"
    hud_nodes = nodes(hud_prefab)
    hud_objects = {obj.path_id: obj for obj in load(hud_sprites).objects if obj.type.name == "Sprite"}
    hud_prefix = "/UI/Other/Btn_AutoBattle"
    hud_states = {}
    for state, group, children in (
        ("on", "/Group_On", ("/Img_01", "/Img_02")),
        ("off", "/Group_Off", ("/Img_01", "/Img_02")),
        ("disabled", "/Group_Disabled", ("/Img_Disabled",)),
    ):
        group_node = hud_nodes[hud_prefix + group]
        gx, gy, gw, gh = group_node["rect_client"]
        combined = Image.new("RGBA", (gw, gh))
        parts = []
        for child in children:
            node = hud_nodes[hud_prefix + group + child]
            tree = next(component["tree"] for component in node["components"]
                        if component["tree"].get("m_Sprite", {}).get("m_PathID"))
            obj = hud_objects[tree["m_Sprite"]["m_PathID"]]
            part = save(obj, f"hud_auto_{state}_{child.rsplit('_', 1)[-1].lower()}",
                        node["rect_client"][2:], hud_sprites)
            x, y, width, height = node["rect_client"]
            combined.alpha_composite(Image.open(PLAN / part["template"]).convert("RGBA"), (x - gx, y - gy))
            parts.append({**part, "roi": [x, y, width, height], "rect_source": node["path"]})
        composite_path = target / f"hud_auto_{state}.png"
        combined.save(composite_path)
        hud_states[state] = {
            "template": composite_path.relative_to(PLAN).as_posix(),
            "roi": [gx, gy, gw, gh], "size": [gw, gh],
            "rect_source": group_node["path"], "parts": parts,
            "identity_eligible": len(parts) == 2,
            "sha256": hashlib.sha256(composite_path.read_bytes()).hexdigest(),
            "pixel_origin": "native sprite layers alpha-composited at their prefab rectangles",
        }
    catalog["native_auto"] = {
        "schema_version": 1, "types_order": [1, 2, 3, 4],
        "color_matching_required": True, "cards": cards, "controls": native_controls,
        "energy_pages": energy_pages,
        "list_selection_persistence": "immediate; setup toggles visibility; confirm/cancel handlers are empty",
        "selection_state_matching": "native tinted RGBA backgrounds; compare RGB and ignore label/icon/priority overlays",
        "battle_hud": {
            "provided": True, "button": node_control(hud_prefab, hud_prefix),
            "identity_roi": hud_states["on"]["roi"], "states": hud_states,
            "identity_rule": "fixed native ROI plus the on/off combined background and status glyph; both parts must agree",
            "disabled_alone_is_identity": False,
            "speed_control": "not provided; serialized pause/speed controls lie offscreen and their runtime placement was not established",
        },
    }
    path = PLAN / "data/meta/structural_exploration_pc.json"
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"catalog": str(path), "icons": len(icons), "controls": list(controls)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, required=True)
    parser.add_argument("--pythonlibs", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(build(arguments.game_root, arguments.devtools_src, arguments.pythonlibs), ensure_ascii=False))
