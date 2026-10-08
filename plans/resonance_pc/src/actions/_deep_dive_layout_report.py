"""Write an offline, human-reviewable deep-dive cube scan report."""

from __future__ import annotations

import html
import json
import math
import os
import time
from pathlib import Path
from urllib.parse import quote


_FACES = ("U", "R", "F", "D", "L", "B")
_OCCUPANTS = {
    "none": "无目标", "player": "玩家", "singularity": "奇点",
    "inspiration": "灵感", "unknown": "未知",
}
_STATUSES = {
    "known": "已识别", "confirmed": "已确认", "unknown": "未知",
    "conflict": "冲突", "not_required_target": "目标下方，无需识别",
}
_ICONS = {
    "white_diamond": "白色方框", "blue_scales": "蓝色天平",
    "green_burst": "绿色放射", "purple_ring": "紫色圆环",
    "yellow_hex": "黄色六边形", "red_single_eye": "红色单眼",
    "orange_triple_eye": "橙色三眼",
}


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _confidence(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "未提供"
    return f"{number:.1%}" if math.isfinite(number) and 0 <= number <= 1 else "无效"


def _local_url(value: object, output_dir: Path) -> str | None:
    """Only link filesystem artifacts, never executable or external URLs."""
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    raw = str(value)
    if "://" in raw or raw.lower().startswith(("javascript:", "data:", "file:")):
        return None
    path = Path(raw)
    if not path.is_absolute():
        # Both report-relative filenames and repository-relative paths are used
        # by capture helpers; prefer an existing report-relative artifact.
        candidate = output_dir / path
        path = candidate if candidate.exists() else Path.cwd() / path
    path = path.resolve()
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return None
    try:
        relative = os.path.relpath(path, output_dir).replace("\\", "/")
        return quote(relative, safe="/.")
    except ValueError:
        return path.as_uri()


def _image_link(value: object, label: str, output_dir: Path) -> str:
    url = _local_url(value, output_dir)
    if url is None:
        return f'<span class="muted">{_escape(label)}：无可用截图</span>'
    return f'<a href="{_escape(url)}" target="_blank" rel="noopener">{_escape(label)}</a>'


def _coordinate(value: object) -> str:
    if not isinstance(value, dict):
        return "未确定"
    return f'{value.get("face", "?")} / r{value.get("row", "?")} / c{value.get("col", "?")}'


def write_layout_report(result: dict, output_dir: Path) -> dict:
    """Persist raw scan evidence and a six-face net; coordinates are zero based.

    This function never upgrades a partial scanner result to complete. Missing
    or duplicated coordinates also make the displayed result incomplete.
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "layout.json"
    report_path = output_dir / "report.html"
    stage_started = time.perf_counter()
    serialized = json.dumps(result, ensure_ascii=False, separators=(',', ':'), default=str)
    timings = {'json_encode_sec': time.perf_counter()-stage_started}
    stage_started = time.perf_counter()
    json_path.write_text(serialized, encoding="utf-8")
    timings['json_write_sec'] = time.perf_counter()-stage_started
    stage_started = time.perf_counter()

    cells = {}
    duplicates = set()
    invalid_count = 0
    for cell in result.get("cells", []):
        if not isinstance(cell, dict):
            invalid_count += 1
            continue
        face, row, col = cell.get("face"), cell.get("row"), cell.get("col")
        if face not in _FACES or type(row) is not int or type(col) is not int or row not in range(3) or col not in range(3):
            invalid_count += 1
            continue
        key = (face, row, col)
        if key in cells:
            duplicates.add(key)
        cells[key] = cell

    complete = bool(result.get("layout_complete")) and len(cells) == 54 and not duplicates and not invalid_count
    complete = complete and all(
        cell.get("occupant_status") == "confirmed"
        and cell.get("occupant") in ("none", "player", "singularity", "inspiration")
        and (cell.get("node_status") == "known" and bool(cell.get("icon_id"))
             if cell.get("occupant") == "none" else cell.get("node_status") in ("known", "not_required_target"))
        for cell in cells.values()
    )
    complete = complete and str(result.get("status", "")).lower() not in ("partial", "failed", "error", "cancelled", "canceled")
    heading = "扫描完整 · 等待人工核对" if complete else "扫描未完成 / PARTIAL · 请勿作为完整布局计算"

    face_html = []
    details = []
    for face in _FACES:
        tile_html = []
        for row in range(3):
            for col in range(3):
                key = (face, row, col)
                cell = cells.get(key, {})
                occupant = cell.get("occupant", "unknown")
                style = occupant if occupant in _OCCUPANTS else "unknown"
                label = _OCCUPANTS.get(occupant, "未知")
                occupant_status = cell.get("occupant_status", "unknown")
                node_status = cell.get("node_status", "unknown")
                icon = cell.get("icon_id")
                icon_label = "下方图标无需识别" if node_status == "not_required_target" else _ICONS.get(icon, str(icon or "图标未知"))
                if key in duplicates:
                    label += " · 坐标冲突"
                elif occupant_status != "confirmed":
                    label += " · " + _STATUSES.get(occupant_status, str(occupant_status))
                anchor = f"cell-{face}-{row}-{col}"
                crop_url=next((_local_url(e.get("crop_path"),output_dir) for e in cell.get("evidence",[]) if isinstance(e,dict) and e.get("crop_path")),None)
                crop_preview=f'<img class="cell-crop" src="{_escape(crop_url)}" alt="{face} r{row} c{col} 表面裁切">' if crop_url else ''
                tile_html.append(
                    f'<a class="tile {style}" href="#{anchor}"><small>{face} r{row} c{col}</small>'
                    f'{crop_preview}<strong>{_escape(label)}</strong><span>{_escape(icon_label)}</span>'
                    f'<small>置信度 {_confidence(cell.get("confidence"))}</small></a>'
                )
                evidence = []
                for item in cell.get("evidence", []):
                    if not isinstance(item, dict):
                        continue
                    links = [_image_link(item.get("frame_path"), "原截图", output_dir)]
                    if item.get("overlay_path"):
                        links.append(_image_link(item["overlay_path"], "识别叠加图", output_dir))
                    if item.get("crop_path"):
                        links.append(_image_link(item["crop_path"], "格子裁剪", output_dir))
                    evidence.append(f'<li>帧 {_escape(item.get("frame_id", "?"))} · {" / ".join(links)}'
                                    f' · 置信度 {_confidence(item.get("confidence"))}</li>')
                details.append(
                    f'<details id="{anchor}"><summary>{face} r{row} c{col} — {_escape(label)} / {_escape(icon_label)}</summary>'
                    f'<p>目标状态：{_escape(_STATUSES.get(occupant_status, occupant_status))}；'
                    f'节点状态：{_escape(_STATUSES.get(node_status, node_status))}；'
                    f'逐格置信度：{_confidence(cell.get("confidence"))}</p>'
                    f'<ul>{"".join(evidence) or "<li>无来源证据，不能视为已观察。</li>"}</ul></details>'
                )
        face_html.append(f'<section class="face face-{face}"><h2>{face}</h2><div class="grid">{"".join(tile_html)}</div></section>')

    frame_html = []
    for frame in result.get("frames", []):
        if not isinstance(frame, dict):
            continue
        source = frame.get("overlay_path") or frame.get("path")
        url = _local_url(source, output_dir)
        preview = f'<a href="{_escape(url)}" target="_blank" rel="noopener"><img loading="lazy" src="{_escape(url)}" alt="扫描帧 {_escape(frame.get("frame_id", "?"))}"></a>' if url else "无预览"
        frame_html.append(f'<figure>{preview}<figcaption>帧 {_escape(frame.get("frame_id", "?"))} · '
                          f'{_image_link(frame.get("path"), "原截图", output_dir)}</figcaption></figure>')

    inspirations = "；".join(_coordinate(value) for value in result.get("inspiration_cells", [])) or "尚无确认位置"
    diagnostics = json.dumps(result.get("diagnostics", {}), ensure_ascii=False, indent=2, default=str)
    document = '''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>识海深潜 · 魔方布局扫描</title><style>
*{box-sizing:border-box}body{margin:0;padding:28px;background:#101620;color:#edf1f7;font:15px/1.6 system-ui,sans-serif}
main{max-width:1440px;margin:auto}h1{font-size:25px}h2{font-size:18px;margin:0 0 8px}a{color:#96cbff}p{max-width:1100px}
.banner{padding:16px;border:1px solid #cf9b49;background:#362b1e;border-radius:9px;font-weight:700}.complete{background:#173a32;border-color:#4fb28f}
.muted,small{color:#b4c1d2}.net{display:grid;grid-template-columns:repeat(4,minmax(250px,1fr));gap:12px;min-width:1040px;margin:24px 0}.scroll{overflow-x:auto}
.face{background:#192231;padding:12px;border-radius:10px}.face-U{grid-column:2;grid-row:1}.face-L{grid-column:1;grid-row:2}.face-F{grid-column:2;grid-row:2}.face-R{grid-column:3;grid-row:2}.face-B{grid-column:4;grid-row:2}.face-D{grid-column:2;grid-row:3}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px}.tile{color:inherit;text-decoration:none;display:flex;flex-direction:column;min-height:126px;padding:7px;border:1px solid #64748b;border-radius:5px;overflow-wrap:anywhere;font-size:12px}.tile strong{font-size:14px}.tile:hover{outline:2px solid white}.player{background:#483560;border-color:#c3a8ff}.singularity{background:#5b222f;border-color:#ff879e}.inspiration{background:#504320;border-color:#fadd73}.none{background:#253246}.unknown{background:repeating-linear-gradient(135deg,#303644,#303644 8px,#252c39 8px,#252c39 16px)}
details{padding:12px;border-bottom:1px solid #384455;scroll-margin-top:15px}details:target{outline:2px solid #96cbff}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#192231;padding:16px}.frames{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}figure{margin:0}img{width:100%;border-radius:6px}li{margin:5px 0}
.cell-crop{width:64px;max-width:100%;height:auto;margin:5px auto;image-rendering:auto}
</style><main>'''
    document += f'<h1>识海深潜 · 魔方布局扫描</h1><div class="banner {"complete" if complete else ""}">{heading}</div>'
    document += '<p><strong>坐标系：scan_local（本次扫描的局部坐标）。</strong>面编号 U/R/F/D/L/B、行列 r0–r2 / c0–c2 均由扫描器定义；不同扫描间不保证相同。下图按各面的局部行列展示，不能仅凭展开图边缘判断跨面行列方向。</p>'
    document += f'<p>运行状态：{_escape(result.get("status", "unknown"))}；原因：{_escape(result.get("reason") or "未提供")}<br>'
    document += f'已知格数：{_escape(result.get("known_cells", "未提供"))} / 54；收到有效坐标：{len(cells)} / 54；已观察面：{_escape(result.get("faces_observed", []))}；整体置信度：{_confidence(result.get("confidence"))}<br>'
    document += f'重复坐标：{len(duplicates)}；无效记录：{invalid_count}。<a href="layout.json">下载原始布局 JSON</a></p>'
    document += f'<p><strong>玩家：</strong>{_escape(_coordinate(result.get("player_cell")))}<br><strong>奇点：</strong>{_escape(_coordinate(result.get("singularity_cell")))}<br><strong>灵感：</strong>{_escape(inspirations)}</p>'
    document += '<p class="muted">点击任意格子查看来源证据。无目标表示已确认该格没有三类目标；未知表示尚不能判断。目标下方的节点图标无需识别。</p>'
    document += '<p class="muted">当前为实验识别器，置信度是启发式证据分数，并非校准后的正确率。请结合原截图与叠加图人工判断。</p>'
    document += f'<div class="scroll"><div class="net">{"".join(face_html)}</div></div><h2>逐格证据</h2>{"".join(details)}'
    document += f'<h2>扫描画面</h2><div class="frames">{"".join(frame_html) or "无画面"}</div><h2>诊断信息</h2><pre>{_escape(diagnostics)}</pre></main></html>'
    timings['html_build_sec'] = time.perf_counter()-stage_started
    stage_started = time.perf_counter()
    report_path.write_text(document, encoding="utf-8")
    timings['html_write_sec'] = time.perf_counter()-stage_started
    return {"report_path": str(report_path), "json_path": str(json_path),
            "report_export_timing": timings}
