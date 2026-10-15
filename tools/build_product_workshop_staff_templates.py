"""Build template-only workshop staffing assets from the installed game's UI.

All generated scenes are synthetic native-prefab reconstructions, never live
screenshots. The builder does not start the game, execute Lua, or send input.
Run from the repository root. Requires the sibling asset devtools and UnityPy.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
PLAN = REPO / "plans/resonance_pc"
ASSETS = PLAN / "templates/product_workshop_staff"
FIXTURES = REPO / "tests/fixtures/product_workshop_staff"
SCALE = 2 / 3
RESOURCES = {
    "editor": "ui/prison/manage/prisonedit.asset",
    "selection": "ui/prison/manage/prisonerlistmanage.asset",
    "prompt": "ui/common/prompt/prompt.asset",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rgba(value: dict) -> tuple[int, ...]:
    return tuple(round(value.get(k, 1) * 255) for k in "rgba")


def nine_slice(src, size, border):
    w, h = size
    l, b, r, t = [max(0, round(getattr(border, k, 0))) for k in "xyzw"]
    if not any((l, b, r, t)):
        return src.resize(size, Image.Resampling.LANCZOS)
    l, r = min(l, src.width // 2, w // 2), min(r, src.width // 2, w // 2)
    t, b = min(t, src.height // 2, h // 2), min(b, src.height // 2, h // 2)
    sx, sy = [0, l, src.width-r, src.width], [0, t, src.height-b, src.height]
    dx, dy = [0, l, w-r, w], [0, t, h-b, h]
    out = Image.new("RGBA", size)
    for j in range(3):
        for i in range(3):
            if sx[i+1] <= sx[i] or sy[j+1] <= sy[j] or dx[i+1] <= dx[i] or dy[j+1] <= dy[j]:
                continue
            part = src.crop((sx[i], sy[j], sx[i+1], sy[j+1])).resize(
                (dx[i+1]-dx[i], dy[j+1]-dy[j]), Image.Resampling.LANCZOS)
            out.alpha_composite(part, (dx[i], dy[j]))
    return out


class NativePrefab:
    def __init__(self, resource, data, deps, scope, api):
        self.resource, self.data, self.scope = resource, data, scope
        self.load_bundle, self.sprite_image = api
        with contextlib.redirect_stdout(io.StringIO()):
            self.env = self.load_bundle(data / "Patch/Asset" / resource, data / "il2cpp_data/Metadata/global-metadata.dat")
        original_find = self.env.find_file
        self.dependencies = set()
        def find_file(name, is_dependency=True):
            if name.lower() not in self.env.cabs:
                candidate = deps.get(name.lower())
                if candidate and (data / "Patch/Asset" / candidate).exists():
                    self.env.load_file(str(data / "Patch/Asset" / candidate), is_dependency=True)
                    self.dependencies.add(candidate)
            return self.env.cabs.get(name.lower()) or original_find(name, is_dependency=is_dependency)
        self.env.find_file = find_file
        self.objects, self.trees = {}, {}
        for obj in self.env.objects:
            try:
                self.trees[obj.path_id] = (obj.type.name, obj.read_typetree())
                self.objects[obj.path_id] = obj
            except Exception:
                continue
        self.roots = [pid for pid, (typ, t) in self.trees.items() if typ == "RectTransform" and not t["m_Father"]["m_PathID"]]
        self.fonts, self.sprites, self.nodes = {}, {}, {}
        self.errors = []

    def render(self, active=None, text=None):
        active, text = active or {}, text or {}
        canvas = Image.new("RGBA", (1920,1080), (25,31,40,255))
        self.nodes = {}
        def match(mapping, path, fallback):
            for suffix, value in sorted(mapping.items(), key=lambda item: -len(item[0])):
                if path.endswith(suffix):
                    return value
            return fallback
        def composite(layer, box, clip):
            x,y = box[:2]
            l,t,r,b = max(x,clip[0],0), max(y,clip[1],0), min(x+layer.width,clip[2],1920), min(y+layer.height,clip[3],1080)
            if r>l and b>t:
                canvas.alpha_composite(layer.crop((l-x,t-y,r-x,b-y)), (l,t))
        def preferred(pid,path):
            _,rt=self.trees[pid]
            go=self.trees[rt["m_GameObject"]["m_PathID"]][1]
            for entry in go.get("m_Component",[]):
                cid=entry["component"]["m_PathID"];ct=self.trees.get(cid,("",{}))[1]
                if "m_Text" in ct:
                    raw=match(text,path,ct["m_Text"]);plain=re.sub(r"<[^>]+>","",str(raw))
                    fp=self.objects[cid].read().m_FontData.m_Font
                    fkey=fp.path_id
                    if fkey not in self.fonts:
                        dest=self.scope/"fonts"/(str(fkey)+".ttf");dest.parent.mkdir(parents=True,exist_ok=True)
                        dest.write_bytes(bytes(fp.read().m_FontData))
                        self.fonts[fkey]={"path":dest,"sha256":digest(dest),"font_path_id":fkey,"font_file_id":fp.file_id}
                    font=ImageFont.truetype(str(self.fonts[fkey]["path"]),ct["m_FontData"]["m_FontSize"])
                    return font.getlength(plain),sum(font.getmetrics())
                if "m_Sprite" in ct:
                    sp=self.objects[cid].read().m_Sprite
                    if sp.path_id:
                        sprite=sp.read()
                        return sprite.m_Rect.width,sprite.m_Rect.height
            layout=next((self.trees.get(e["component"]["m_PathID"],("",{}))[1] for e in go.get("m_Component",[]) if "m_ChildControlWidth" in self.trees.get(e["component"]["m_PathID"],("",{}))[1]),None)
            if layout:
                sizes=[]
                for child in rt.get("m_Children",[]):
                    cp=child["m_PathID"]
                    childgo=self.trees[self.trees[cp][1]["m_GameObject"]["m_PathID"]][1]
                    childpath=path+"/"+childgo["m_Name"]
                    if match(active,childpath,childgo.get("m_IsActive",1)):
                        sizes.append(preferred(cp,childpath))
                padding=layout.get("m_Padding",{})
                return (sum(s[0] for s in sizes)+max(0,len(sizes)-1)*layout.get("m_Spacing",0)+padding.get("m_Left",0)+padding.get("m_Right",0),max([s[1] for s in sizes] or [0])+padding.get("m_Top",0)+padding.get("m_Bottom",0))
            return max(0,rt["m_SizeDelta"]["x"]),max(0,rt["m_SizeDelta"]["y"])
        def walk(pid, parent, path="", enabled=True, clip=(0,0,1920,1080), forced=None):
            _, rt = self.trees[pid]
            go = self.trees[rt["m_GameObject"]["m_PathID"]][1]
            path += "/" + go.get("m_Name", "?")
            enabled = enabled and bool(match(active,path,go.get("m_IsActive",1)))
            if not enabled:
                return
            px,py,pw,ph=parent
            amin,amax,pivot,ap,sd = [rt[k] for k in ("m_AnchorMin","m_AnchorMax","m_Pivot","m_AnchoredPosition","m_SizeDelta")]
            w,h = pw*(amax["x"]-amin["x"])+sd["x"], ph*(amax["y"]-amin["y"])+sd["y"]
            if w<=0 or h<=0:
                cw,ch=preferred(pid,path)
                w,h=max(w,cw),max(h,ch)
            x = px+pw*(amin["x"]+(amax["x"]-amin["x"])*pivot["x"])+ap["x"]-w*pivot["x"]
            y = py+ph*(amin["y"]+(amax["y"]-amin["y"])*pivot["y"])+ap["y"]-h*pivot["y"]
            if forced is not None:
                x,y,w,h=forced
            box=(round(x),round(1080-y-h),round(x+w),round(1080-y))
            node={"path":path,"box_xyxy":[round(v*SCALE) for v in box],"components":[]}
            self.nodes[path] = node
            if go.get("m_Name")=="Viewport":
                clip=(max(clip[0],box[0]),max(clip[1],box[1]),min(clip[2],box[2]),min(clip[3],box[3]))
            for entry in go.get("m_Component",[]):
                cid=entry["component"]["m_PathID"]
                ct=self.trees.get(cid,("",{}))[1]
                if not ct.get("m_Enabled",1):
                    continue
                if "m_Sprite" in ct and 0<w<5000 and 0<h<5000:
                    color=rgba(ct.get("m_Color",{}))
                    if color[3]:
                        try:
                            sp=self.objects[cid].read().m_Sprite
                            if sp.path_id:
                                ck=(sp.file_id,sp.path_id)
                                if ck not in self.sprites:
                                    sprite=sp.read()
                                    self.sprites[ck]=(self.sprite_image(sprite,sp.deref().version),sprite.m_Border,sprite.m_Name)
                                src,border,name=self.sprites[ck]
                                size=(max(1,round(w)),max(1,round(h)))
                                layer=nine_slice(src,size,border) if ct.get("m_Type")==1 else src.resize(size,Image.Resampling.LANCZOS)
                                if color!=(255,255,255,255):
                                    layer=ImageChops.multiply(layer,Image.new("RGBA",size,color))
                                composite(layer,box,clip)
                                node["components"].append({"sprite":name,"sprite_file_id":sp.file_id,"sprite_path_id":sp.path_id})
                        except Exception as exc:
                            self.errors.append({"path":path,"error":"sprite_"+type(exc).__name__})
                raw=match(text,path,ct.get("m_Text",ct.get("m_text"))) if ("m_Text" in ct or "m_text" in ct) else None
                if raw and w>0 and h>0:
                    fd=ct.get("m_FontData",{})
                    size=int(fd.get("m_FontSize",ct.get("m_fontSize",24)))
                    plain=re.sub(r"<[^>]+>","",str(raw))
                    try:
                        pointer=self.objects[cid].read().m_FontData.m_Font
                        fkey=pointer.path_id
                        if fkey not in self.fonts:
                            content=bytes(pointer.read().m_FontData)
                            if not content:
                                raise ValueError("empty native font")
                            dest=self.scope / "fonts" / (str(fkey)+".ttf")
                            dest.parent.mkdir(parents=True,exist_ok=True)
                            dest.write_bytes(content)
                            self.fonts[fkey]={"path":dest,"sha256":digest(dest),"font_path_id":fkey,"font_file_id":pointer.file_id}
                        font=ImageFont.truetype(str(self.fonts[fkey]["path"]),size)
                    except Exception as exc:
                        raise RuntimeError(f"Native font extraction failed: {path}") from exc
                    text_layer=Image.new("RGBA",canvas.size)
                    draw=ImageDraw.Draw(text_layer)
                    bounds=draw.textbbox((0,0),plain,font=font)
                    tw,th=bounds[2]-bounds[0],bounds[3]-bounds[1]
                    # Explicit prefab best-fit is needed for the room title.
                    while fd.get("m_BestFit") and (tw>w or th>h) and size>int(fd.get("m_MinSize",2)):
                        size-=1; font=ImageFont.truetype(str(self.fonts[fkey]["path"]),size)
                        bounds=draw.textbbox((0,0),plain,font=font);tw,th=bounds[2]-bounds[0],bounds[3]-bounds[1]
                    row,col=divmod(fd.get("m_Alignment",4),3)
                    tx,ty=box[0]+(w-tw)*col/2,box[1]+(h-th)*row/2-bounds[1]
                    color=rgba(ct.get("m_Color",{}))
                    rich=re.fullmatch(r"<color=#FFB800>(\d+)</color>(/\d+)",str(raw))
                    if rich:
                        draw.text((tx,ty),rich[1],font=font,fill=(255,184,0,255))
                        draw.text((tx+font.getlength(rich[1]),ty),rich[2],font=font,fill=color)
                    else:
                        draw.text((tx,ty),plain,font=font,fill=color)
                    composite(text_layer,(0,0,1920,1080),clip)
                    node["ink_box_xyxy"]=[round(v*SCALE) for v in (tx+bounds[0],ty+bounds[1],tx+bounds[2],ty+bounds[3])]
                    node["components"].append({"text":plain,"font_path_id":fkey,"font_size_native":size,"font_size_prefab":fd.get("m_FontSize"),"font_data":fd,"color":color})
            layout=next((self.trees.get(e["component"]["m_PathID"],("",{}))[1] for e in go.get("m_Component",[]) if "m_ChildControlWidth" in self.trees.get(e["component"]["m_PathID"],("",{}))[1]),None)
            cursor=x+(layout.get("m_Padding",{}).get("m_Left",0) if layout else 0)
            if layout:
                total_width,_=preferred(pid,path)
                cursor+=(w-total_width)*(layout.get("m_ChildAlignment",4)%3)/2
            for child in rt.get("m_Children",[]):
                cp=child["m_PathID"]
                if cp in self.trees and self.trees[cp][0]=="RectTransform":
                    forced_child=None
                    if layout:
                        childgo=self.trees[self.trees[cp][1]["m_GameObject"]["m_PathID"]][1]
                        cw,ch=preferred(cp,path+"/"+childgo["m_Name"])
                        forced_child=(cursor,y+(h-ch)/2,cw,ch)
                        cursor+=cw+layout.get("m_Spacing",0)
                    walk(cp,(x,y,w,h),path,enabled,clip,forced_child)
        for pid in self.roots:
            walk(pid,(0,0,1920,1080))
        return canvas.resize((1280,720),Image.Resampling.LANCZOS).convert("RGB"),dict(self.nodes)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root",type=Path,default=Path("D:/game/Resonance/雷索纳斯_Data"))
    parser.add_argument("--devtools-src",type=Path,default=REPO.parent/"Aura_script_lsns_devtools/src")
    parser.add_argument("--unitypy-dir",type=Path,default=REPO/".pytest_tmp/cuberogue_internal/unitypy")
    parser.add_argument("--dependency-index",type=Path,default=REPO/".pytest_tmp/reforming_assets_20261004/dependency_candidates.json")
    parser.add_argument("--scope",type=Path,default=REPO/".pytest_tmp/product_staff_implementation_20261015/assets")
    args=parser.parse_args()
    args.scope=args.scope.resolve()
    if not args.scope.is_relative_to(REPO.resolve()):
        parser.error("--scope must stay inside this repository")
    args.scope.mkdir(parents=True,exist_ok=True)
    for key in ("TEMP","TMP","TMPDIR"):
        os.environ[key]=str(args.scope)
    for path in (args.devtools_src,args.unitypy_dir):
        sys.path.insert(0,str(path))
    from aura_resonance_devtools.unity_bundle import load_bundle,_logical_sprite_image
    deps=json.loads(args.dependency_index.read_text("utf-8"))
    ASSETS.mkdir(parents=True,exist_ok=True);FIXTURES.mkdir(parents=True,exist_ok=True)
    models={key:NativePrefab(resource,args.data_root,deps,args.scope,(load_bundle,_logical_sprite_image)) for key,resource in RESOURCES.items()}
    catalog={"schema_version":1,"reference_client":[1280,720],"kind":"synthetic_native_game_assets","live_calibrated":False,
             "provenance":{"sources":[{"resource":r,"sha256":digest(args.data_root/"Patch/Asset"/r)} for r in RESOURCES.values()]},
             "limitations":["No live screenshot or live acceptance evidence","Native images/fonts/layout; Pillow rasterization differs from Unity","Dynamic lists, animation, shader and masks are not simulated; example counts are synthetic"],
             "templates":{},"scenes":{},"counters":{}}
    scenes={}
    def scene(key,model,active,text):
        image,nodes=models[model].render(active,text)
        image.save(FIXTURES/(key+".png"))
        scenes[key]=(image,nodes,model)
        catalog["scenes"][key]={"image":"tests/fixtures/product_workshop_staff/"+key+".png","kind":"synthetic_native_game_assets","resource":RESOURCES[model],"active_overrides":active,"text_overrides":text,"sha256":digest(FIXTURES/(key+".png"))}
        return image,nodes
    edit_active={"/PrisonerListManage":False,"/Group_Windows":False,"/Group_Lock":False,"/Group_UnLockEffect":False,"/Group_Edit/Group_Detail":False,"/Img_QuickEditON":False,"/Img_QuickEditOFF":True}
    for number in (1,2):
        scene(f"editor_{number}","editor",edit_active,{"/Group_WorkshopInfo/Txt_Workshop":f"{number}号生产车间","/Group_TotalNum/Txt_TotalNumCount":"10"})
    scene("editor_1_after","editor",edit_active,{"/Group_WorkshopInfo/Txt_Workshop":"1号生产车间","/Group_TotalNum/Txt_TotalNumCount":"15"})
    select_active={"/Group_Screen":False,"/Group_NoPrison":False,"/Btn_All":True,"/Btn_AllCancel":False}
    scene("selection_empty","selection",select_active,{"/Group_Detail/Group_Prisoner/Txt_Num":"<color=#FFB800>10</color>/15"})
    scene("selection_full","selection",{**select_active,"/Btn_All":False,"/Btn_AllCancel":True},{"/Group_Detail/Group_Prisoner/Txt_Num":"<color=#FFB800>15</color>/15"})
    prompt_text={"/Group_Prompt/TextIcon_Prompt":"布局已变更，是否保存？"}
    scene("save_prompt","prompt",{"/Txt_Prompt":False,"/Group_DayTip":False,"/Group_Tip":False,"/Group_Tips":False,"/Btn_SkipSafe":False,"/Group_Prompt":True,"/Group_Prompt/Img_Icon":False},prompt_text)
    def template(key,scene_key,suffix,pad=0,click_suffix=None):
        image,nodes,model=scenes[scene_key]
        matches=[n for p,n in nodes.items() if p.endswith(suffix)]
        if len(matches)!=1:
            raise ValueError((key,suffix,len(matches)))
        node=matches[0]; l,t,r,b=node.get("ink_box_xyxy",node["box_xyxy"])
        l,t,r,b=max(0,l-pad),max(0,t-pad),min(1280,r+pad),min(720,b+pad)
        if r<=l or b<=t:
            raise ValueError((key,"invalid native bounds",node))
        path=ASSETS/(key+".png");image.crop((l,t,r,b)).save(path)
        target=next((n for p,n in nodes.items() if click_suffix and p.endswith(click_suffix)),node)
        a,c,d,e=target["box_xyxy"]
        catalog["templates"][key]={"image":"templates/product_workshop_staff/"+path.name,"bbox":[l,t,r-l,b-t],"search_region":[max(0,l-12),max(0,t-12),min(1280,r+12)-max(0,l-12),min(720,b+12)-max(0,t-12)],"click":[round((a+d)/2),round((c+e)/2)],"threshold":0.90,"source_nodes":[node],"resource":RESOURCES[model],"scene":scene_key,"sha256":digest(path)}
    for num in (1,2):
        template(f"room_{num}_title",f"editor_{num}","/Group_WorkshopInfo/Txt_Workshop")
        # Shared suffix characters otherwise make wrong room numbers correlate
        # above 0.96. Match the native numbered prefix, alongside the quick
        # button that independently identifies this as the editing page.
        item=catalog["templates"][f"room_{num}_title"]
        path=PLAN/item["image"]
        image=Image.open(path);image.crop((0,0,min(30,image.width),image.height)).save(path)
        item["bbox"][2]=min(30,item["bbox"][2])
        item["search_region"][2]=item["bbox"][2]+24
        item["sha256"]=digest(path)
        item["crop_note"]="Numbered title prefix; shared suffix excluded to reject the other room"
    template("quick","editor_1","/Img_QuickEditOFF/Group_/Txt_Quick",2,"/Group_Edit/Btn_QuickEdit")
    template("editor_marker","editor_1","/Group_Top/Group_TotalNum/Txt_TotalNum")
    template("editor_count_label","editor_1","/Group_Top/Group_TotalNum/Txt_TotalNum")
    template("selection_marker","selection_empty","/Group_Detail/Group_Prisoner/Txt_")
    template("selection_count_label","selection_empty","/Group_Detail/Group_Prisoner/Txt_")
    template("select_all","selection_empty","/Group_Detail/Btn_All/Txt_T",2,"/Group_Detail/Btn_All")
    template("cancel_all","selection_full","/Group_Detail/Btn_AllCancel/Txt_T",2,"/Group_Detail/Btn_AllCancel")
    template("confirm","selection_empty","/Group_Detail/Btn_Confirm/Txt_T",2,"/Group_Detail/Btn_Confirm")
    template("back","editor_1","/Group_CommonTopLeft/Btn_Return")
    template("save_prompt","save_prompt","/Group_Prompt/TextIcon_Prompt")
    template("apply","save_prompt","/Btn_Confirm/Group_/Txt_Confirm",2,"/Btn_Confirm")
    template("discard","save_prompt","/Btn_Cancel/Group_/Txt_Cancel",2,"/Btn_Cancel")
    # The user-observed save branch returns to the physical room page.
    physical_catalog=json.loads((PLAN/"data/meta/reforming_center_templates_pc.json").read_text("utf-8"))
    physical_markers=physical_catalog["rooms"]["1号生产车间"]["destination_markers"]
    saved=Image.new("RGB",(1280,720),(25,31,40))
    back_spec=catalog["templates"]["back"]
    saved.paste(Image.open(PLAN/back_spec["image"]).convert("RGB"),tuple(back_spec["bbox"][:2]))
    for marker in physical_markers:
        src=PLAN/marker["image"]
        l,t,w,h=marker["source_bbox_xywh"]
        saved.paste(Image.open(src).convert("RGB"),(l,t))
        if "workshop_edit" in marker["image"]:
            dest=ASSETS/"saved_marker.png";Image.open(src).save(dest)
            catalog["templates"]["saved_marker"]={"image":"templates/product_workshop_staff/saved_marker.png","bbox":[l,t,w,h],"search_region":marker["search_region"],"click":[round(l+w/2),round(t+h/2)],"threshold":0.90,"source_image":marker["image"],"source_id":marker["source_id"],"sha256":digest(dest)}
    saved.save(FIXTURES/"saved.png")
    catalog["scenes"]["saved"]={"image":"tests/fixtures/product_workshop_staff/saved.png","kind":"synthetic_composite_of_existing_live_template_crops","source_catalog":"data/meta/reforming_center_templates_pc.json","sha256":digest(FIXTURES/"saved.png")}
    def counter(key,scene_key,suffix,format_,style):
        _,nodes,model=scenes[scene_key]
        node=next(n for p,n in nodes.items() if p.endswith(suffix))
        l,t,r,b=node["box_xyxy"]
        l,t,r,b=max(0,l),max(0,t),min(1280,r),min(720,b)
        catalog["counters"][key]={"region":[l,t,r-l,b-t],"format":format_,"style":style,"source_node":node}
    counter("selection","selection_empty","/Group_Detail/Group_Prisoner/Txt_Num","fraction","selection")
    counter("editor","editor_1","/Group_Top/Group_TotalNum/Txt_TotalNumCount","integer","list")
    catalog["editor_digits"]=[]
    editor_font=models["editor"].fonts[-4884400286118422667]["path"]
    for character in "0123456789":
        font=ImageFont.truetype(str(editor_font),26)
        glyph=Image.new("RGB",(64,64));ImageDraw.Draw(glyph).text((12,4),character,font=font,fill="white")
        glyph=glyph.resize((round(64*SCALE),round(64*SCALE)),Image.Resampling.LANCZOS)
        path=ASSETS/("editor_digit_"+character+".png");glyph.save(path)
        catalog["editor_digits"].append({"character":character,"image":"templates/product_workshop_staff/"+path.name,"font_path_id":-4884400286118422667,"font_size_native":26,"scale":SCALE,"sha256":digest(path)})
    catalog["provenance"]["fonts"]=[{"font_path_id":key,"sha256":font["sha256"]} for model in models.values() for key,font in model.fonts.items()]
    logic_names=("CommonTips.lua","UIPrompt/UIPromptLuaBehaviour.lua","UIPrisonEdit/UIPrisonEditController.lua","UIPrisonEdit/UIPrisonEditViewFunction.lua")
    catalog["provenance"]["logic_sources"]=[{"script":name,"sha256":digest(args.data_root/"Patch/Script"/name)} for name in logic_names]
    catalog["provenance"]["text_bindings"]={"room_title":{"text_id":80600296,"binding":"CurRoomIndex","examples":[1,2]},"save_prompt":{"text_id":80600202,"node":"/Prompt/Group_Prompt/TextIcon_Prompt"},"button_labels":"native prefab defaults; nil yesTxt/noTxt in OnPrompt","example_counts":"synthetic inputs, not player data"}
    catalog["provenance"]["dependencies"]=[{"resource":r,"sha256":digest(args.data_root/"Patch/Asset"/r)} for r in sorted(set().union(*(m.dependencies for m in models.values())))]
    catalog["render_errors"]={k:m.errors for k,m in models.items()}
    dest=PLAN/"data/meta/product_workshop_staff_templates_pc.json"
    dest.write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (FIXTURES/"manifest.json").write_text(json.dumps({"kind":catalog["kind"],"live_calibrated":False,"scenes":catalog["scenes"],"counters":catalog["counters"],"limitations":catalog["limitations"]},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"templates":len(catalog["templates"]),"scenes":len(catalog["scenes"]),"catalog":str(dest),"errors":{k:len(m.errors) for k,m in models.items()}}))


if __name__=="__main__":
    main()
