"""Rebuild daily-reward templates from installed native sprites and fonts.

Sources are installed sprites, prefab styles and embedded fonts. Pillow text
rasterization and the approved layout recipes are deterministic. This asset
authoring command never captures, matches, or controls the running game.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import sys
from shutil import copyfile
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
OUT = PLAN / "development/template_sources/rewards/daily"
NATIVE = PLAN / "templates/rewards/daily"
CATALOG = PLAN / "data/meta/daily_reward_templates_pc.json"
DATA = None
from _trade_investment_native import load_bundle

_logical_sprite_image = None
Image = ImageChops = ImageDraw = ImageFont = None

SCALE = 2 / 3
ASSETS = {}
SOURCE = {}
FONTS = {}
ENVS = {}
ITEMS = {}
OPAQUE_CORE_MASKS = {"main_entry_with_red", "activity_gold", "activity_blue"}
MATCH_PROFILES = {
    "main_entry_with_red": ((145, 0, 110, 80), .90, False, "none"),
    "daily_tab": ((320, 30, 230, 90), .82, False, "none"),
    "claimable": ((120, 580, 1015, 95), .90, False, "none"),
    "activity_gold": ((380, 130, 730, 105), .90, False, "none"),
    "activity_blue": ((380, 130, 730, 105), .90, False, "none"),
    "reward_title": ((475, 155, 340, 115), .90, True, "none"),
    "reward_exit": ((520, 645, 240, 75), .85, True, "blur"),
    "city_main": ((140, 0, 130, 85), .90, False, "none"),
    "incomplete": ((120, 580, 1015, 95), .88, False, "none"),
    "go_to": ((120, 580, 1015, 95), .80, False, "none"),
    "main_entry": ((145, 0, 110, 80), .90, False, "none"),
}
DIAGNOSTIC_ONLY = {"main_entry", "incomplete", "go_to"}


def configure(args):
    global DATA, _logical_sprite_image, Image, ImageChops, ImageDraw, ImageFont
    DATA = args.game_data.resolve()
    if args.pythonlibs is not None:
        sys.path.insert(0, str(args.pythonlibs.resolve()))
    sys.path.insert(0, str(args.devtools_src.resolve()))
    from PIL import Image, ImageChops, ImageDraw, ImageFont
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image as logical_sprite
    _logical_sprite_image = logical_sprite
    for table in (ASSETS, SOURCE, FONTS, ENVS, ITEMS):
        table.clear()


def write_catalog():
    templates = {}
    for key, (roi, threshold, grayscale, preprocess) in MATCH_PROFILES.items():
        item = ITEMS[key]
        entry = {
            "template": item["file"], "mask": item.get("mask"),
            "roi": list(roi), "threshold": threshold,
            "use_grayscale": grayscale, "method": "TM_CCOEFF_NORMED",
            "preprocess": preprocess, "size": item["size"], "sha256": item["sha256"],
        }
        if "mask_sha256" in item:
            entry["mask_sha256"] = item["mask_sha256"]
        if key in DIAGNOSTIC_ONLY:
            entry["diagnostic_only"] = True
        templates[key] = entry
    catalog = {"schema_version": 1, "client_size": [1280, 720],
               "provenance": "development/template_sources/rewards/daily/native_provenance.json",
               "templates": templates}
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def bundle(rel):
    if rel not in ENVS:
        p = DATA / "Patch/Asset" / rel
        ENVS[rel] = load_bundle(p, DATA / "il2cpp_data/Metadata/global-metadata.dat")
        SOURCE[rel] = {"sha256": sha(p)}
    return ENVS[rel]

def asset_table(rel):
    for obj in bundle(rel).objects:
        if obj.type.name in ("Sprite", "Font"):
            ASSETS[obj.path_id] = (rel, obj)

def sprite(pid, size=None, tint=(255, 255, 255, 255)):
    rel, obj = ASSETS[pid]
    s = obj.read()
    im = _logical_sprite_image(s, obj.version).convert("RGBA")
    if size:
        im = im.resize(tuple(max(1, round(v)) for v in size), Image.Resampling.LANCZOS)
    if tint != (255, 255, 255, 255):
        im = ImageChops.multiply(im, Image.new("RGBA", im.size, tint))
    return im, {"bundle": rel, "sprite": s.m_Name, "path_id": pid, "size_native": [s.m_Rect.width, s.m_Rect.height]}

def rgba(c):
    return tuple(round(c.get(k, 1) * 255) for k in "rgba")

class Prefab:
    def __init__(self, rel):
        self.rel = rel
        env = bundle(rel)
        self.trees = {o.path_id: o.read_typetree() for o in env.objects if o.type.name in ("GameObject", "RectTransform", "MonoBehaviour")}
        self.trans = {i: t for i, t in self.trees.items() if "m_AnchoredPosition" in t}
        self.components = defaultdict(list)
        for i, t in self.trees.items():
            if t.get("m_GameObject") and "m_AnchoredPosition" not in t:
                self.components[t["m_GameObject"]["m_PathID"]].append(t)
        self.paths = {self.path(i): i for i in self.trans}

    @lru_cache(None)
    def path(self, i):
        t = self.trans.get(i)
        return (self.path(t["m_Father"]["m_PathID"]) + "/" + self.trees[t["m_GameObject"]["m_PathID"]]["m_Name"]) if t else ""

    def comps(self, path):
        return self.components[self.trans[self.paths[path]]["m_GameObject"]["m_PathID"]]

    def text(self, path):
        return next(c for c in self.comps(path) if "m_FontData" in c)

    def img(self, path):
        return next(c for c in self.comps(path) if "m_Sprite" in c and c["m_Sprite"]["m_PathID"])

    def image(self, path):
        c = self.img(path)
        rt = self.trans[self.paths[path]]
        _, obj = ASSETS[c["m_Sprite"]["m_PathID"]]
        raw = obj.read()
        size = (rt["m_SizeDelta"]["x"] or raw.m_Rect.width,
                rt["m_SizeDelta"]["y"] or raw.m_Rect.height)
        im, info = sprite(c["m_Sprite"]["m_PathID"], size, rgba(c["m_Color"]))
        return im, {"prefab": self.rel, "node": path, **info}

def font_for(tree):
    fd = tree["m_FontData"]
    pid = fd["m_Font"]["m_PathID"]
    if pid not in FONTS:
        rel, obj = ASSETS[pid]
        f = obj.read()
        FONTS[pid] = {"name": f.m_Name, "bytes": bytes(f.m_FontData), "bundle": rel}
    f = FONTS[pid]
    return ImageFont.truetype(io.BytesIO(f["bytes"]), fd["m_FontSize"]), {"font_path_id": pid, "font_name": f["name"], "font_bundle": f["bundle"], "font_sha256": hashlib.sha256(f["bytes"]).hexdigest(), "font_size_native": fd["m_FontSize"], "font_style_prefab": fd["m_FontStyle"]}

def text_layer(tree, box):
    font, info = font_for(tree)
    text = tree["m_Text"]
    im = Image.new("RGBA", box)
    d = ImageDraw.Draw(im)
    bounds = d.textbbox((0, 0), text, font=font)
    w, h = bounds[2]-bounds[0], bounds[3]-bounds[1]
    # Existing SourceHanSansCN-Bold font face already encodes the native bold face.
    d.text(((box[0]-w)/2-bounds[0], (box[1]-h)/2-bounds[1]), text, font=font, fill=rgba(tree["m_Color"]))
    return im, {"text": text, "rasterizer": "Pillow/FreeType; not Unity dynamic atlas", **info}

def save(key, im, provenance, note=""):
    im = im.resize((max(1, round(im.width*SCALE)), max(1, round(im.height*SCALE))), Image.Resampling.LANCZOS)
    # OpenCV interprets nonzero uint8 mask pixels as fully participating.
    # Low-alpha shadow pixels therefore need explicit exclusion, rather than
    # passing the original alpha ramp as though it were fractional weighting.
    im.save(NATIVE / (key + ".png"))
    alpha = im.getchannel("A")
    mask = alpha.point(lambda value: 255 if value >= 224 else 0) if key in OPAQUE_CORE_MASKS else alpha
    mask.save(NATIVE / (key + ".mask.png"))
    ITEMS[key] = {"file": f"templates/rewards/daily/{key}.png", "mask": f"templates/rewards/daily/{key}.mask.png", "size": list(im.size), "sha256": sha(NATIVE/(key+".png")), "source": provenance, "note": note, "status": "native_generated_not_live_validated"}
    ITEMS[key]["mask_sha256"] = sha(NATIVE / (key + ".mask.png"))
    ITEMS[key]["mask_policy"] = {"kind": "binary_opaque_core", "alpha_floor": 224} if key in OPAQUE_CORE_MASKS else {"kind": "source_alpha"}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True,
                        help="Installed game's *_Data directory")
    parser.add_argument("--devtools-src", type=Path, required=True,
                        help="Separate aura-resonance-devtools/src directory")
    parser.add_argument("--pythonlibs", type=Path,
                        help="Optional isolated UnityPy dependency directory")
    args = parser.parse_args()
    if Path.cwd().resolve() != ROOT:
        parser.error("Run from the repository root")
    configure(args)
    OUT.mkdir(parents=True, exist_ok=True)
    NATIVE.mkdir(parents=True, exist_ok=True)
    for rel in ("ui/stagequest/images.asset", "ui/mainui.asset", "ui/common.asset", "ui/font.asset", "ui/font/originpack.asset"):
        asset_table(rel)
    stage = Prefab("ui/stagequest/stagequest.asset")
    mainui = Prefab("ui/mainui/mainui.asset")
    show = Prefab("ui/common/showitem.asset")
    im, info = mainui.image("/MainUI/Group_Common/Btn_StageQuest")
    save("main_entry", im, [info], "Native icon only; red reminder and live scene excluded. MainUI runtime scaling has not been calibrated.")
    entry_path = "/MainUI/Group_Common/Btn_StageQuest"
    badge_path = entry_path + "/Img_Red"
    badge, badge_info = mainui.image(badge_path)
    entry_rt = mainui.trans[mainui.paths[entry_path]]
    badge_rt = mainui.trans[mainui.paths[badge_path]]
    # Child anchors are relative to the parent's pivot; convert Unity's upward
    # Y axis to the PNG's downward Y axis. Retain the badge extending past the
    # parent's right edge instead of clipping it to the original icon image.
    def child_origin(axis, extent, size):
        anchor = badge_rt["m_AnchorMin"][axis]
        pos = badge_rt["m_AnchoredPosition"][axis]
        pivot = badge_rt["m_Pivot"][axis]
        native = anchor * extent + pos - pivot * size
        return native if axis == "x" else extent - native - size
    bx = round(child_origin("x", entry_rt["m_SizeDelta"]["x"], badge.width))
    by = round(child_origin("y", entry_rt["m_SizeDelta"]["y"], badge.height))
    left, top = min(0, bx), min(0, by)
    combined = Image.new("RGBA", (max(im.width, bx + badge.width) - left,
                                  max(im.height, by + badge.height) - top))
    combined.alpha_composite(im, (-left, -top))
    combined.alpha_composite(badge, (bx - left, by - top))
    save("main_entry_with_red", combined, [info, badge_info],
         "Single native composite of the task icon and its red reminder, at the prefab offset. "
         "The red reminder participates in the mask and must be included in color matching. "
         "This candidate gates entry only; absence alone cannot establish a known main screen.")
    ITEMS["main_entry_with_red"]["use_grayscale"] = False
    ITEMS["main_entry_with_red"]["composition_native"] = {
        "entry_size": list(im.size), "red_size": list(badge.size),
        "red_top_left": [bx, by], "canvas_size": list(combined.size),
    }
    # Reuse the established runtime image without claiming it was regenerated
    # from the installed native assets by this producer.
    runtime_home = ROOT / "plans/resonance_pc/templates/navigation/global/city_main.png"
    copyfile(runtime_home, NATIVE / "city_main.png")
    with Image.open(runtime_home) as existing:
        home_size = list(existing.size)
    ITEMS["city_main"] = {"file": "templates/rewards/daily/city_main.png", "size": home_size,
        "sha256": sha(runtime_home), "source": [{"runtime_file": runtime_home.relative_to(ROOT).as_posix(), "sha256": sha(runtime_home)}],
        "note": "Exact reuse of an existing runtime template; not newly generated native pixels.",
        "status": "existing_template_reused_without_new_matching_or_live_validation"}
    gift = "/StageQuest/Group_Daily/Group_Activity/Group_Progress/StaticGrid_Reward/Group_Item_000/"
    for key, node in (("activity_gold", "Btn_Get"), ("activity_blue", "Btn_UnGet")):
        im, info = stage.image(gift+node)
        save(key, im, [info], "Complete native circular state button including gift, color and border; dynamic point label excluded.")
    base = "/StageQuest/Group_Daily/ScrollGrid_Quest/Viewport/Grid/Group_Item_000/"
    for key, branch in (("claimable", "Group_On/Img_Bg"), ("incomplete", "Group_Off/Img_Bg")):
        p=base+branch
        im, info = stage.image(p)
        icon, ii = stage.image(p+"/Img_Icon")
        # Prefab anchors at top-left; child centers are x=112,y=540.5.
        im.alpha_composite(icon, (round(112-icon.width/2), round(540.5-icon.height/2)))
        txt, ti = text_layer(stage.text(p+"/Txt_T"), (78,30))
        im.alpha_composite(txt, (140,526))
        save(key, im.crop((85,513,225,569)), [info,ii,{"prefab":stage.rel,"node":p+"/Txt_T",**ti}], "Native card footer center: state icon and text. Text rasterization differs from Unity; native background texture retained.")
    p=base+"Group_Off/Btn_Jump"
    im, info=stage.image(p)
    icon, ii=stage.image(p+"/Img_Icon")
    im.alpha_composite(icon,(87,round(101-49.5-icon.height/2)))
    txt,ti=text_layer(stage.text(p+"/Txt_T"),(78,30))
    im.alpha_composite(txt,(135,round(101-49.5-15)))
    save("go_to", im.crop((80,22,220,82)),[info,ii,{"prefab":stage.rel,"node":p+"/Txt_T",**ti}],"Blue go-to state; not a claim action.")
    p="/StageQuest/Btn_Daily/Group_On/Img_Bg"
    bg,info=stage.image(p)
    icon,ii= sprite(stage.img(p+"/Img_Icon")["m_Sprite"]["m_PathID"],(50,50),(0,0,0,255))
    txt,ti=text_layer(stage.text(p+"/Txt_T"),(120,36))
    # Title-only horizontal layout (native preferred widths 50+120) excludes hidden tab body.
    tab=bg.crop((45,45,245,105))
    tab.alpha_composite(icon,(7,5));tab.alpha_composite(txt,(62,12))
    tab=tab.rotate(3,resample=Image.Resampling.BICUBIC,expand=True)
    save("daily_tab",tab,[info,ii,{"prefab":stage.rel,"node":p+"/Txt_T",**ti}],"Selected-tab title marker. Prefab Group_On rotation is +3 degrees. Pillow layout/rasterization is a candidate, not Unity-exact.")
    p="/ShowItem/Group_580/Group_Title"
    tree=show.text(p+"/Txt_Title")
    title,ti=text_layer(tree,(240,86))
    icon,ii=show.image(p+"/Img_Title")
    layer=Image.new("RGBA",(350,100));layer.alpha_composite(icon,(0,0));layer.alpha_composite(title,(110,7))
    save("reward_title",layer,[ii,{"prefab":show.rel,"node":p+"/Txt_Title",**ti}],"Native obtained-items icon plus native SourceHanSerifCN-Heavy text. Transparent scene background excluded.")
    p="/ShowItem/Group_Tips/Txt_Tips"
    txt,ti=text_layer(show.text(p),(160,30))
    # Group_Tips has a native warning diamond sprite. Select by actual sibling binding.
    tips=[]
    for node in show.paths:
        if node.startswith('/ShowItem/Group_Tips/') and node!=p:
            try:
                c=show.img(node)
                tips.append((node,c))
            except StopIteration: pass
    if tips:
        node,c=tips[0]
        icon,ii=show.image(node)
        h=max(40,icon.height);out=Image.new('RGBA',(icon.width+8+160,h));out.alpha_composite(icon,(0,(h-icon.height)//2));out.alpha_composite(txt,(icon.width+8,(h-30)//2))
        save('reward_exit',out,[ii,{'prefab':show.rel,'node':p,**ti}],"Native exit hint, separated from blurred scene and particles.")
    else:
        save('reward_exit',txt,[{'prefab':show.rel,'node':p,**ti}],"Native exit text only; no warning icon binding found.")
    from _daily_reward_refinements import apply_refinements
    apply_refinements(sys.modules[__name__])
    result={"kind":"native_daily_reward_templates","client_reference":[1280,720],"scale":SCALE,"sources":SOURCE,"templates":ITEMS,"limitations":["This generator reads native assets only; historical supplied-screenshot calibration results are retained in calibration_summary.json.","Text uses native fonts through Pillow and is not pixel-identical to Unity's dynamic text renderer; selected marker recipes are calibrated on user-supplied screenshots.","Shader, animation, scene blur and particles are intentionally omitted; no live game actions are performed."]}
    (OUT/'native_provenance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    write_catalog()
    print(json.dumps({k:v['size'] for k,v in ITEMS.items()},ensure_ascii=False))

if __name__ == '__main__':
    main()
