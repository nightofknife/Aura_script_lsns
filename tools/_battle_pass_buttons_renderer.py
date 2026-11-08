"""Native pass-claim controls; recipes contain geometry, never screenshot pixels."""
from __future__ import annotations

import numpy as np
from PIL import Image


PASS_ROOT = "/BattlePass_Quest/Group_Reward_Quest/"


def _claim_all_body(u, p):
    path = PASS_ROOT + "Group_BattlePass/Btn_TakeAll"
    c = p.img(path)
    pid = c["m_Sprite"]["m_PathID"]
    rel, obj = u.ASSETS[pid]
    sprite = obj.read()
    raw = u._logical_sprite_image(sprite, obj.version).convert("RGBA")
    rt = p.trans[p.paths[path]]
    width, height = (round(rt["m_SizeDelta"][axis]) for axis in ("x", "y"))
    left, bottom, right, top = (round(getattr(sprite.m_Border, axis)) for axis in "xyzw")
    sx, sy = (0, left, raw.width-right, raw.width), (0, top, raw.height-bottom, raw.height)
    dx, dy = (0, left, width-right, width), (0, top, height-bottom, height)
    image = Image.new("RGBA", (width, height))
    for row in range(3):
        for column in range(3):
            part = raw.crop((sx[column], sy[row], sx[column+1], sy[row+1])).resize(
                (dx[column+1]-dx[column], dy[row+1]-dy[row]), Image.Resampling.LANCZOS)
            image.alpha_composite(part, (dx[column], dy[row]))
    tint = u.rgba(c["m_Color"])
    if tint != (255, 255, 255, 255):
        image = u.ImageChops.multiply(image, Image.new("RGBA", image.size, tint))
    text, font_source = u.text_layer(p.text(path+"/Txt_"), (width-12, 40))
    image.alpha_composite(text, (6, round((height-40)/2)))
    image = image.resize((round(width*2/3), round(height*2/3)), Image.Resampling.LANCZOS)
    source = {"prefab": p.rel, "node": path, "sprite_bundle": rel, "sprite": sprite.m_Name,
              "sprite_path_id": pid, "nine_slice_border_native_lbrt": [left,bottom,right,top],
              "text": font_source}
    return image, source


def render_button(u, recipe):
    """Return (image, mask_array_or_None, source_records).

    The red variant's mask is float32 with word weight 1 and reminder weight 4.
    Save/load it as NPY; an 8-bit PNG must not be used as fractional weights.
    """
    p = u.Prefab("ui/bp_quest/battlepass_quest.asset")
    kind = recipe["kind"]
    if kind in {"claim_all", "claim_all_with_red"}:
        body, source = _claim_all_body(u, p)
        x0,y0,x1,y1 = recipe["text_rectangle_xyxy"]
        if kind == "claim_all":
            mask = np.zeros((body.height, body.width), np.uint8)
            mask[y0:y1, x0:x1] = 255
            return body, mask, [source]
        red, red_source = p.image(PASS_ROOT+"Group_BattlePass/Btn_TakeAll/Img_RedPoint")
        red = red.resize(tuple(recipe["red_size_client"]), Image.Resampling.LANCZOS)
        rx, ry = recipe["red_offset_from_body_client"]
        left, top = min(0,rx), min(0,ry)
        image = Image.new("RGBA", (max(body.width,rx+red.width)-left,
                                    max(body.height,ry+red.height)-top))
        image.alpha_composite(body, (-left,-top))
        image.alpha_composite(red, (rx-left,ry-top))
        mask = np.zeros((image.height,image.width), np.float32)
        mask[y0-top:y1-top,x0-left:x1-left] = 1.
        a,b,c,d = recipe["red_inner_rectangle_xyxy"]
        mask[ry-top+b:ry-top+d,rx-left+a:rx-left+c] = recipe["red_mask_weight"]
        return image, mask, [source, red_source]
    raise ValueError(f"Unsupported button recipe: {kind}")
