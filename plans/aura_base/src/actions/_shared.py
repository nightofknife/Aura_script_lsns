from __future__ import annotations

import asyncio
import ast
import operator
import time
from pathlib import Path

from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger

from ..services.vision_service import VisionService

_ALLOWED_MATH_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_ALLOWED_MATH_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_MAX_MATH_EXPRESSION_LENGTH = 256
_MAX_MATH_AST_NODES = 64


def resolve_template_path(engine: ExecutionEngine, vision: VisionService, template: str) -> str:
    plan_path = engine.orchestrator.current_plan_path
    plan_name = engine.orchestrator.plan_name
    return str(vision.resolve_template(plan_name, template, plan_path))


def expand_template_paths(engine: ExecutionEngine, vision: VisionService, templates_ref: str) -> list[Path]:
    plan_path = engine.orchestrator.current_plan_path
    plan_name = engine.orchestrator.plan_name
    return vision.expand_templates(plan_name, templates_ref, plan_path)


async def poll_until(timeout: float, interval: float, probe, predicate):
    deadline = time.monotonic() + max(float(timeout), 0.0)
    poll_interval = max(float(interval), 0.0)

    try:
        last_result = await asyncio.to_thread(probe)
        while not predicate(last_result):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, last_result
            await asyncio.sleep(min(poll_interval, remaining))
            last_result = await asyncio.to_thread(probe)
        return True, last_result
    except asyncio.CancelledError:
        logger.info("poll_until cancelled timeout=%s interval=%s", timeout, interval)
        raise


def _evaluate_math_ast(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("math_compute only supports numeric constants.")
        return value

    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_MATH_UNARYOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op(_evaluate_math_ast(node.operand))

    if isinstance(node, ast.BinOp):
        op = _ALLOWED_MATH_BINOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        return op(_evaluate_math_ast(node.left), _evaluate_math_ast(node.right))

    raise ValueError(f"Unsupported expression node: {type(node).__name__}")


def safe_math_compute(expression: str) -> float | int:
    normalized = str(expression or "").strip()
    if not normalized:
        raise ValueError("Expression is empty.")
    if len(normalized) > _MAX_MATH_EXPRESSION_LENGTH:
        raise ValueError("Expression is too long.")

    allowed_chars = "0123456789.+-*/() "
    if not all(char in allowed_chars for char in normalized):
        raise ValueError("Expression contains unsupported characters.")

    parsed = ast.parse(normalized, mode="eval")
    if sum(1 for _ in ast.walk(parsed)) > _MAX_MATH_AST_NODES:
        raise ValueError("Expression is too complex.")
    return _evaluate_math_ast(parsed.body)
