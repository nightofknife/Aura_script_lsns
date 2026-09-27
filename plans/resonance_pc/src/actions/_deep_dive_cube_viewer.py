"""Standalone, offline cube atlas viewer; no inference or consistency claims."""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import os
from pathlib import Path
from urllib.parse import quote

from ._deep_dive_layout_vision import BASES, DISTANCE, HALF_TILE, K, SEED_R, SEED_T


def write_cube_viewer(layout, output_path, consistency=None, evidence_root=None):
    """Write one HTML with embedded crops and local evidence links.

    ``layout`` is a scanner layout dictionary. ``evidence_root`` is its run
    directory (the directory containing layout.json). Consistency is displayed
    verbatim as supplied by the caller; it is never inferred by this renderer.
    """
    output = Path(output_path).resolve()
    root = Path(evidence_root or output.parent).resolve()
    data = {key: deepcopy(value) for key, value in layout.items()
            if key not in ('frames', 'actions', 'reset', 'streaming', 'diagnostics')}
    for cell in data.get("cells", []):
        if not isinstance(cell, dict):
            continue
        for item in cell.get("evidence", []):
            if not isinstance(item, dict):
                continue
            for key in ("crop_path", "frame_path", "overlay_path"):
                value = item.get(key)
                if not isinstance(value, str) or not value:
                    continue
                path = Path(value)
                path = (root / path).resolve() if not path.is_absolute() else path.resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    continue
                item[key + "_url"] = quote(os.path.relpath(path, output.parent).replace('\\', '/'), safe='/')
                if key == "crop_path" and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[path.suffix.lower()]
                    item["crop_data"] = "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    payload = dict(layout=data, consistency=consistency, bases=BASES, distance=DISTANCE,
                   halfTile=HALF_TILE, k=K.tolist(), seedR=SEED_R.reshape(-1).tolist(),
                   seedT=SEED_T.reshape(-1).tolist())
    encoded = json.dumps(payload, ensure_ascii=False, default=str).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_HTML.replace("__PAYLOAD__", encoded), encoding="utf-8")
    return str(output)


_HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>魔方布局 · 人工对照</title>
<link rel="icon" href="data:,">
<style>
*{box-sizing:border-box}body{margin:0;background:#10141c;color:#e4eaf4;font:14px system-ui,sans-serif}header{padding:20px 26px;border-bottom:1px solid #303b4c}h1{font-size:22px;margin:0 0 8px}p{line-height:1.65;margin:8px 0;color:#adbcd0}main{display:grid;grid-template-columns:minmax(400px,1fr) 350px;gap:16px;padding:18px}button,select{background:#253349;color:#ecf3ff;border:1px solid #526783;padding:8px 12px;border-radius:6px;cursor:pointer}button:hover{background:#3b506f}nav{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px}canvas{display:block;width:100%;height:580px;background:radial-gradient(ellipse,#263249,#111720);border:1px solid #36445b;border-radius:10px;touch-action:none;cursor:grab}aside{background:#19212e;border:1px solid #36445b;border-radius:10px;padding:18px;overflow-wrap:anywhere}h2{font-size:17px}#crop{width:192px;height:192px;image-rendering:auto;border:1px solid #526783;display:none}a{color:#8fc7ff}#evidence li{margin:9px 0}.net{display:grid;grid-template-columns:repeat(3,minmax(115px,1fr));gap:12px;margin-top:18px}.face{background:#19212e;padding:10px;border-radius:8px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}.tile{padding:5px 2px;min-height:55px;font-size:11px}.tile.active{outline:2px solid #fff}.tile img{width:34px;height:34px;display:block;margin:auto}pre{white-space:pre-wrap;color:#bdcde0;font-size:12px}.legend span{margin-right:16px}.warning{color:#ffd581}@media(max-width:850px){main{grid-template-columns:1fr}canvas{height:430px}.net{grid-template-columns:repeat(2,1fr)}}
</style><header><h1>魔方布局 · 人工对照</h1><p>拖动旋转 · 滚轮缩放 · 点击格子查看坐标与证据。立体目标为示意标记；节点裁切来自扫描，图标朝向不代表移动或旋转规则。</p><div class="legend"><span style="color:#c6acff">● 玩家</span><span style="color:#ffe27b">◎ 灵感</span><span style="color:#ff7182">✦ 奇点</span><span>？ 未知 / 冲突保留</span></div></header>
<main><section><nav><button id="reset">重置视角</button><button id="minus">− 缩小</button><button id="plus">＋ 放大</button><select id="mode"><option value="crop">真实裁切</option><option value="symbol">示意图标</option></select><span id="faces"></span></nav><canvas id="cube" aria-label="可旋转魔方布局"></canvas><p id="summary"></p><p>行列编号从 0 到 2：从每面的外侧正视，列向右、行向下，背面不镜像。侧壁和立体目标为示意；请用真实裁切与原截图核对游戏。</p><div class="net" id="net"></div></section><aside><h2 id="selection">点击一个格子</h2><p id="details">选择左侧立体格子或六面索引。</p><img id="crop" alt="扫描器保存的真实格子裁切"><p id="cropNote"></p><ul id="evidence"></ul><h2>扫描一致性</h2><p id="consistencySummary"></p><details><summary>查看核对明细</summary><pre id="consistency"></pre></details><p class="warning">多次结果一致，仍需对照游戏确认准确性。目标下方的图标未推测。</p></aside></main>
<script type="application/json" id="payload">__PAYLOAD__</script><script>
'use strict';
const D=JSON.parse(document.getElementById('payload').textContent),F=['U','R','F','D','L','B'];
const $=id=>document.getElementById(id),cv=$('cube'),ctx=cv.getContext('2d'),cells=[],tiles=new Map(),textures=new Map();
const names={none:'无目标',player:'玩家',singularity:'奇点',inspiration:'灵感',unknown:'未知'}, icons={white_diamond:'白色方框',blue_scales:'蓝色天平',green_burst:'绿色放射',purple_ring:'紫色圆环',yellow_hex:'黄色六边形',red_single_eye:'红色单眼',orange_triple_eye:'橙色三眼'};
const statuses={confirmed:'已确认',known:'已识别',unknown:'未知',conflict:'冲突',not_required_target:'无需识别'};
const colors={player:'#bea0ff',inspiration:'#ffda55',singularity:'#ff526d'},key=c=>`${c.face}:${c.row}:${c.col}`;
const grouped=new Map();for(const c of D.layout.cells||[]){if(!c||!F.includes(c.face)||!Number.isInteger(c.row)||!Number.isInteger(c.col)||c.row<0||c.row>2||c.col<0||c.col>2)continue;const k=key(c);grouped.set(k,[...(grouped.get(k)||[]),c]);}
for(const f of F)for(let r=0;r<3;r++)for(let c=0;c<3;c++){let items=grouped.get(`${f}:${r}:${c}`)||[],v={face:f,row:r,col:c,occupant:'unknown',node_status:'unknown',occupant_status:'unknown',...items[0]};if(items.length>1)v={...v,occupant_status:'conflict',node_status:'conflict',coordinate_conflict:true};cells.push(v);}
const add=(a,b)=>a.map((x,i)=>x+b[i]),scale=(a,s)=>a.map(x=>x*s),dot=(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0),cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]],mul=(a,b)=>a.map(row=>[0,1,2].map(i=>dot(row,b.map(r=>r[i])))),apply=(m,p)=>m.map(r=>dot(r,p));
function rotation(v){const a=Math.hypot(...v);if(!a)return [[1,0,0],[0,1,0],[0,0,1]];const n=scale(v,1/a),c=Math.cos(a),s=Math.sin(a),W=[[0,-n[2],n[1]],[n[2],0,-n[0]],[-n[1],n[0],0]];return W.map((row,i)=>row.map((w,j)=>(i===j?c:0)+(1-c)*n[i]*n[j]+s*w));}
let R=rotation(D.seedR),zoom=1,selected=null,hit=[],pointer=null;
function center(c){const[n,u,v]=D.bases[c.face];return add(scale(n,D.distance),add(scale(u,c.col-1),scale(v,c.row-1)));}
function project(p){let q=add(apply(R,p),D.seedT),s=Math.min(cv.width/1280,cv.height/720)*zoom;return [cv.width/2+s*D.k[0][0]*q[0]/q[2],cv.height/2+s*D.k[1][1]*q[1]/q[2],q[2]];}
function poly(points){ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));ctx.closePath();}
function iconLabel(c){return ['player','singularity','inspiration'].includes(c.occupant)?'下方图标无需识别':c.node_status==='known'?(icons[c.icon_id]||c.icon_id||'未知'):'图标 '+(c.node_status||'未知');}
function symbol(c){const t=document.createElement('canvas');t.width=t.height=96;const g=t.getContext('2d');g.fillStyle='#17202e';g.fillRect(0,0,96,96);g.lineWidth=5;g.strokeStyle='#eaf2ff';g.fillStyle='#f2f5ff';g.textAlign='center';g.textBaseline='middle';g.font='bold 15px sans-serif';const target=colors[c.occupant];if(target){g.fillStyle=target;g.font='12px sans-serif';g.fillText('无需识别',48,79);return t;}
if(c.node_status!=='known'||c.coordinate_conflict){g.fillStyle='#ffd581';g.font='bold 36px sans-serif';g.fillText('?',48,48);return t;}
const line=(x,y,X,Y)=>{g.beginPath();g.moveTo(x,y);g.lineTo(X,Y);g.stroke();};
switch(c.icon_id){case 'white_diamond':g.save();g.translate(48,48);g.rotate(Math.PI/4);g.strokeRect(-22,-22,44,44);g.restore();break;case 'blue_scales':g.strokeStyle='#57bcff';line(48,19,48,75);line(20,33,76,33);line(28,34,17,58);line(28,34,39,58);line(58,58,69,34);line(80,58,69,34);line(16,59,40,59);line(57,59,81,59);line(30,76,66,76);break;case 'green_burst':g.strokeStyle='#34f8ba';for(let i=0;i<10;i++){let a=i*Math.PI/5;line(48+Math.cos(a)*10,48+Math.sin(a)*10,48+Math.cos(a)*32,48+Math.sin(a)*32);}break;case 'purple_ring':g.strokeStyle='#b777ed';g.beginPath();g.ellipse(48,48,24,32,0,0,Math.PI*2);g.stroke();break;case 'yellow_hex':g.strokeStyle='#ffdf57';g.beginPath();for(let i=0;i<6;i++){let a=i*Math.PI/3;g.lineTo(48+30*Math.cos(a),48+30*Math.sin(a));}g.closePath();g.stroke();break;case 'red_single_eye':case 'orange_triple_eye':g.strokeStyle=c.icon_id==='red_single_eye'?'#ff6670':'#ffac54';for(const [x,y,s] of(c.icon_id==='red_single_eye'?[[48,48,1]]:[[32,33,.55],[64,33,.55],[48,65,.55]])){g.beginPath();g.ellipse(x,y,30*s,16*s,0,0,Math.PI*2);g.stroke();g.beginPath();g.arc(x,y,7*s,0,Math.PI*2);g.stroke();}break;default:g.fillText('?',48,48);}return t;}
for(const c of cells){textures.set(key(c),{symbol:symbol(c)});const e=(c.evidence||[]).find(e=>e.crop_data);if(e){const im=new Image();im.onload=()=>{textures.get(key(c)).crop=im;draw();};im.src=e.crop_data;}}
// Two affine triangles keep the surface's TL/TR/BR/BL order on every face.
function triangle(img,dst,src){const [a,b,c]=dst,[A,B,C]=src;const det=(B[0]-A[0])*(C[1]-A[1])-(C[0]-A[0])*(B[1]-A[1]);const ux=((b[0]-a[0])*(C[1]-A[1])-(c[0]-a[0])*(B[1]-A[1]))/det, vx=((c[0]-a[0])*(B[0]-A[0])-(b[0]-a[0])*(C[0]-A[0]))/det,uy=((b[1]-a[1])*(C[1]-A[1])-(c[1]-a[1])*(B[1]-A[1]))/det,vy=((c[1]-a[1])*(B[0]-A[0])-(b[1]-a[1])*(C[0]-A[0]))/det;ctx.save();poly(dst);ctx.clip();ctx.transform(ux,uy,vx,vy,a[0]-ux*A[0]-vx*A[1],a[1]-uy*A[0]-vy*A[1]);ctx.drawImage(img,0,0,96,96);ctx.restore();}
function draw(){
ctx.clearRect(0,0,cv.width,cv.height);hit=[];
const surfaces=[],markers=[],inner=1+D.halfTile,signs=[[-1,-1],[1,-1],[1,1],[-1,1]];
const camera=p=>add(apply(R,p),D.seedT);
function surface(points,normal,type,c=null){
 const midpoint=scale(points.reduce((a,p)=>add(a,p),[0,0,0]),1/points.length),q=camera(midpoint),rn=apply(R,normal);
 if(dot(rn,q)>=0)return;
 surfaces.push({points:points.map(project),depth:q[2],normal:rn,type,c});
}
// An opaque central cube closes the gaps; each node extends out from it.
for(const f of F){const[n,u,v]=D.bases[f],p=scale(n,inner);
 surface(signs.map(([x,y])=>add(p,add(scale(u,x*inner),scale(v,y*inner)))),n,'core');
}
for(const c of cells){
 const[n,u,v]=D.bases[c.face],p=center(c),outer=signs.map(([x,y])=>add(p,add(scale(u,x*D.halfTile),scale(v,y*D.halfTile)))),back=outer.map(q=>add(q,scale(n,inner-D.distance)));
 surface(outer,n,'tile',c);
 // Top/right/bottom/left wall normals follow the same row/column bases.
 const normals=[scale(v,-1),u,v,scale(u,-1)];
 for(let i=0;i<4;i++){const j=(i+1)%4;surface([outer[i],back[i],back[j],outer[j]],normals[i],'wall',c);}
 if(colors[c.occupant]&&dot(apply(R,n),camera(p))<0){const tip=add(p,scale(n,.65));markers.push({type:'marker',c,p,n,tip,depth:camera(tip)[2]});}
}
// Sort each wall separately so nearer blocks correctly cover deeper walls.
const commands=[...surfaces,...markers].sort((a,b)=>b.depth-a.depth);
for(const item of commands){
 const {c,type}=item;
 if(type==='marker'){
  const a=project(item.p),b=project(item.tip),size=Math.max(8,Math.min(22,12*zoom));
  ctx.strokeStyle=colors[c.occupant];ctx.fillStyle=colors[c.occupant];ctx.lineWidth=3;
  ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke();
  if(c.occupant==='player'){
   // A tapered body and round head, aligned with the projected face normal.
   const dx=b[0]-a[0],dy=b[1]-a[1],len=Math.hypot(dx,dy),axis=len>2?[dx/len,dy/len]:[0,-1],side=[-axis[1],axis[0]],neck=[b[0]-axis[0]*size*.6,b[1]-axis[1]*size*.6];
   ctx.beginPath();ctx.moveTo(a[0]+side[0]*size*.75,a[1]+side[1]*size*.75);ctx.lineTo(neck[0]+side[0]*size*.32,neck[1]+side[1]*size*.32);ctx.lineTo(neck[0]-side[0]*size*.32,neck[1]-side[1]*size*.32);ctx.lineTo(a[0]-side[0]*size*.75,a[1]-side[1]*size*.75);ctx.closePath();ctx.fill();ctx.strokeStyle='#eadfff';ctx.lineWidth=1;ctx.stroke();ctx.beginPath();ctx.arc(b[0],b[1],size*.63,0,Math.PI*2);ctx.fill();ctx.stroke();
  }else if(c.occupant==='singularity'){
   ctx.beginPath();for(let i=0;i<16;i++){const r=i%2?size*.35:size,t=i*Math.PI/8;ctx.lineTo(b[0]+r*Math.cos(t),b[1]+r*Math.sin(t));}ctx.closePath();ctx.fill();ctx.strokeStyle='#ffafb7';ctx.lineWidth=1;ctx.stroke();
  }else{ctx.beginPath();ctx.arc(b[0],b[1],size,0,Math.PI*2);ctx.stroke();ctx.beginPath();ctx.arc(b[0],b[1],size*.75,0,Math.PI*2);ctx.lineWidth=1;ctx.stroke();}
  ctx.fillStyle='#fff';ctx.font='12px system-ui';ctx.fillText(names[c.occupant]+(c.occupant_status==='confirmed'?'':'?'),b[0]+size+4,b[1]);continue;
 }
 const corners=item.points;poly(corners);
 if(type!=='tile'){
  const light=Math.max(0,-dot(item.normal,[.3,.45,.84])),shade=Math.round((type==='core'?14:27)+light*22);
  ctx.fillStyle=`rgb(${shade},${shade+5},${shade+14})`;ctx.fill();ctx.strokeStyle=type==='core'?'#1a2230':'#495367';ctx.lineWidth=.8;ctx.stroke();
  if(c)hit.push({key:key(c),points:corners});continue;
 }
 const tex=textures.get(key(c)),img=$('mode').value==='crop'&&tex.crop&&!colors[c.occupant]?tex.crop:tex.symbol;
 triangle(img,[corners[0],corners[1],corners[2]],[[0,0],[96,0],[96,96]]);triangle(img,[corners[0],corners[2],corners[3]],[[0,0],[96,96],[0,96]]);
 poly(corners);ctx.strokeStyle=selected===key(c)?'#fff':c.occupant_status!=='confirmed'||c.node_status==='conflict'?'#e7b766':'#8390a5';ctx.lineWidth=selected===key(c)?3:1.5;ctx.stroke();ctx.fillStyle='#fff';ctx.font='12px system-ui';ctx.fillText(`${c.face}${c.row}${c.col}`,corners[0][0]+3,corners[0][1]+14);hit.push({key:key(c),points:corners});
}
}
function select(k){selected=k;const c=cells.find(c=>key(c)===k);$('selection').textContent=`${c.face} 面 · 行 ${c.row} · 列 ${c.col}`;$('details').textContent=`目标：${names[c.occupant]||c.occupant}（${statuses[c.occupant_status]||c.occupant_status}）；${iconLabel(c)}；节点状态：${statuses[c.node_status]||c.node_status}；置信度：${c.confidence==null?'未提供':Math.round(c.confidence*100)+'%'}${c.coordinate_conflict?'；坐标重复冲突':''}`;const ev=c.evidence||[],e=ev.find(e=>e.crop_data);$('crop').style.display=e?'block':'none';if(e)$('crop').src=e.crop_data;$('cropNote').textContent=colors[c.occupant]?'此裁切仅含格子表面；浮空目标请点原截图核对。':'真实格子表面裁切。';$('evidence').replaceChildren();for(const item of ev){const li=document.createElement('li');li.textContent=`帧 ${item.frame_id??'?'} `;for(const [field,label]of[['frame_path_url','原截图'],['overlay_path_url','叠加图'],['crop_path_url','裁切']])if(item[field]){const a=document.createElement('a');a.href=item[field];a.textContent=label+' ';a.target='_blank';a.rel='noopener';li.append(a);}$('evidence').append(li);}if(!ev.length)$('evidence').textContent='无来源证据';for(const [id,t]of tiles)t.classList.toggle('active',id===k);draw();}
for(const f of F){const b=document.createElement('button');b.textContent=f;b.onclick=()=>{const[n,u,v]=D.bases[f];R=[u.slice(),v.slice(),scale(n,-1)];draw();};$('faces').append(b);const sec=document.createElement('section');sec.className='face';const h=document.createElement('h2');h.textContent=f+' · 外侧正视';sec.append(h);const grid=document.createElement('div');grid.className='grid';for(const c of cells.filter(c=>c.face===f)){const t=document.createElement('button');t.className='tile';t.textContent=`${c.row},${c.col} ${c.occupant==='none'?(icons[c.icon_id]||'未知'):(names[c.occupant]||'?')}${c.occupant_status==='confirmed'?'':' ?'}`;const preview=(c.evidence||[]).find(e=>e.crop_data);if(preview){const im=document.createElement('img');im.src=preview.crop_data;im.alt=iconLabel(c);t.prepend(im);}t.title=iconLabel(c);t.onclick=()=>select(key(c));tiles.set(key(c),t);grid.append(t);}sec.append(grid);$('net').append(sec);}
function point(e){const r=cv.getBoundingClientRect();return [(e.clientX-r.left)*cv.width/r.width,(e.clientY-r.top)*cv.height/r.height];}cv.onpointerdown=e=>{pointer={id:e.pointerId,p:point(e),distance:0};cv.setPointerCapture(e.pointerId);};cv.onpointermove=e=>{if(!pointer||pointer.id!==e.pointerId)return;const p=point(e),dx=p[0]-pointer.p[0],dy=p[1]-pointer.p[1];pointer.distance+=Math.hypot(dx,dy);R=mul(rotation([-dy*.008,dx*.008,0]),R);pointer.p=p;draw();};cv.onpointerup=e=>{if(!pointer)return;if(pointer.distance<5){const p=point(e);for(const h of [...hit].reverse()){poly(h.points);if(ctx.isPointInPath(...p)){select(h.key);break;}}}pointer=null;};cv.onpointercancel=()=>pointer=null;cv.onwheel=e=>{e.preventDefault();zoom=Math.min(3,Math.max(.4,zoom*Math.exp(-e.deltaY*.001)));draw();};$('plus').onclick=()=>{zoom=Math.min(3,zoom*1.2);draw();};$('minus').onclick=()=>{zoom=Math.max(.4,zoom/1.2);draw();};$('reset').onclick=()=>{R=rotation(D.seedR);zoom=1;draw();};$('mode').onchange=draw;new ResizeObserver(()=>{cv.width=Math.round(cv.clientWidth);cv.height=Math.round(cv.clientHeight);draw();}).observe(cv);
$('consistencySummary').textContent=D.consistency&&typeof D.consistency==='object'&&D.consistency.all_consistent===true?`${D.consistency.run_count} 次扫描结果一致 · ${D.consistency.pairwise_comparisons} 组成对比较 · 布局差异 ${D.consistency.total_pairwise_field_differences}。`:'请查看核对明细。';
$('consistency').textContent=D.consistency==null?'未提供多次扫描一致性结论。':typeof D.consistency==='string'?D.consistency:JSON.stringify(D.consistency,null,2);$('summary').textContent=`已确认 ${D.layout.known_cells??'未提供'} / 54 格 · ${D.layout.layout_complete===true?'六面完整':'布局尚未完整'}。`;draw();
const first=D.layout.player_cell||{face:'F',row:1,col:1};select(key(first));
</script></html>'''
