"""Native font/sprite tab renderer; no screenshot pixels are consumed."""
from __future__ import annotations
import io
from PIL import Image,ImageDraw,ImageChops

BOTTOM="/BattlePass_Quest/Group_Reward_Quest/Group_Bottom/Img_BG"

def native_components(builder,key):
    page=builder.Prefab("ui/bp_quest/battlepass_quest.asset")
    button="Btn_Bp" if "manual" in key else "Btn_Quest"
    on=key.endswith("_on")
    suffix="On" if on else "Off"
    group="Group_On" if on else "Group_Off"
    branch=f"{BOTTOM}/{button}/{group}"
    icon,icon_source=page.image(branch+f"/Img_Icon{suffix}")
    tree=page.text(branch+f"/Txt_{suffix}")
    font,font_source=builder.font_for(tree)
    return icon,font,tree,{"icon":icon_source,"font":font_source,"text":tree["m_Text"],"selected":on,"branch":branch}

def text_ink(font,text,size,aa,stroke):
    font=font.font_variant(size=round(size*aa))
    alpha=Image.new("L",(500*aa,70*aa))
    draw=ImageDraw.Draw(alpha)
    box=draw.textbbox((0,0),text,font=font,stroke_width=round(stroke*aa))
    draw.text((4*aa-box[0],4*aa-box[1]),text,font=font,fill=255,
        stroke_width=round(stroke*aa),stroke_fill=255)
    box=alpha.getbbox()
    alpha=alpha.crop((box[0]-3*aa,box[1]-3*aa,box[2]+3*aa,box[3]+3*aa))
    return alpha.resize((round(alpha.width/aa),round(alpha.height/aa)),Image.Resampling.LANCZOS)

def icon_ink(icon,size,on):
    resized=icon.resize(tuple(size),Image.Resampling.LANCZOS)
    bg=Image.new("RGBA",resized.size,"white" if on else "black")
    bg.alpha_composite(resized)
    gray=bg.convert("L")
    return ImageChops.invert(gray) if on else gray

def render_tab(builder,recipe):
    key=recipe["template_key"]
    icon,font,tree,source=native_components(builder,key)
    on=source["selected"]
    icon=icon_ink(icon,recipe["icon_size_client"],on)
    text=text_ink(font,tree["m_Text"],recipe["font_size_client"],recipe["aa"],recipe["stroke_client"])
    tx,ty=recipe["text_offset_client"]
    left,top=min(0,tx),min(0,ty)
    size=(max(icon.width,tx+text.width)-left,max(icon.height,ty+text.height)-top)
    alpha=Image.new("L",size)
    alpha.paste(icon,(-left,-top))
    alpha.paste(text,(tx-left,ty-top))
    image=ImageChops.invert(alpha) if on else alpha
    mask=Image.new("L",size)
    draw=ImageDraw.Draw(mask)
    # Regular icon and text rectangles; keep holes/space around the glyphs.
    rectangles=((-left,-top,icon.width-left,icon.height-top),(tx-left,ty-top,tx+text.width-left,ty+text.height-top))
    for box in rectangles:
        draw.rectangle((box[0],box[1],box[2]-1,box[3]-1),fill=255)
    source["mask_rectangles_xyxy"]=[list(box) for box in rectangles]
    return image.convert("RGB"),mask,source
