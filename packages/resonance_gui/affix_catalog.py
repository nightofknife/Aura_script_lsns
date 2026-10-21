"""Portable affix catalog access and decimal-only configuration handling."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping

from .paths import resolve_application_root


def decimal_value(value: Any) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)?", value.strip()):
        raise ValueError("数值要求必须填写非负十进制数字。")
    try:
        result = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError("数值要求无效。") from exc
    if not result.is_finite():
        raise ValueError("数值要求无效。")
    return result


def normalize_affix_inputs(inputs: Mapping[str, Any]) -> dict[str, Any]:
    target = inputs.get("target_affix_id")
    if target is not None and (type(target) is not int or target <= 0):
        raise ValueError("请选择有效的目标词缀。")
    comparison = inputs.get("comparison", "gte")
    if comparison not in {"any", "gte", "eq", "max"}:
        raise ValueError("数值比较方式无效。")
    threshold = inputs.get("threshold", "")
    if comparison in {"gte", "eq"}:
        threshold = format(decimal_value(threshold), "f")
    else:
        threshold = ""
    limit = inputs.get("max_film_cost", 20)
    if type(limit) is not int or not 0 <= limit <= 9999:
        raise ValueError("胶卷消耗上限必须是 0～9999 的整数。")
    return {"target_affix_id": target, "comparison": comparison,
            "threshold": threshold, "max_film_cost": limit, "auto_replace": False}


def eligible_affixes(snapshot: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Load the complete offline catalog, independently of equipment or UI rows."""
    relative = Path("plans/resonance_pc/data/meta/affix_refresh.json")
    candidates = [resolve_application_root() / relative, Path.cwd() / relative,
                  Path(__file__).resolve().parents[2] / relative,
                  Path(sys.executable).resolve().parent.parent / relative]
    configured = str(os.environ.get("AURA_BASE_PATH") or "").strip()
    if configured:
        candidates = [Path(configured) / relative]
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [dict(row) for row in payload.get("affixes", [])]
    raise ValueError("找不到词缀目录 affix_refresh.json，请更新资源后重新读取。")
