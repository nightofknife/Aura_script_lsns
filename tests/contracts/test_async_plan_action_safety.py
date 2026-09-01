"""Static safety checks for async plan actions."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_ACTION_ROOTS = tuple(sorted((REPO_ROOT / "plans").glob("*/src/actions")))

_UNSAFE_ACTION_MODULE_SUFFIXES = (
    "aura_base.src.actions.ocr_actions",
    "aura_base.src.actions.vision_actions",
)
_SAFE_SYNC_ACTION_IMPORTS = {"list_templates_in_set"}
_UNSAFE_SERVICE_METHODS = {
    "ocr": {
        "find_text",
        "find_all_text",
        "recognize_text",
        "recognize_all",
    },
    "vision": {
        "find_template",
        "find_all_templates",
        "find_templates_batch",
        "find_all_templates_batch",
        "find_color",
    },
}


class _FunctionCalls(ast.NodeVisitor):
    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.root = root
        self.calls: list[ast.Call] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)


def _module_unsafe_action_aliases(tree: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.endswith(_UNSAFE_ACTION_MODULE_SUFFIXES):
            continue
        for imported in node.names:
            if imported.name == "*" or imported.name in _SAFE_SYNC_ACTION_IMPORTS:
                continue
            aliases.add(imported.asname or imported.name)
    return aliases


def _analyze_module(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    unsafe_action_aliases = _module_unsafe_action_aliases(tree)
    local_calls: dict[str, set[str]] = {}
    direct_reasons: dict[str, set[str]] = {}

    for name, function in functions.items():
        visitor = _FunctionCalls(function)
        visitor.visit(function)
        called_locals: set[str] = set()
        reasons: set[str] = set()
        for call in visitor.calls:
            target = call.func
            if isinstance(target, ast.Name):
                if target.id in functions:
                    called_locals.add(target.id)
                if target.id in unsafe_action_aliases:
                    reasons.add(f"line {call.lineno}: {target.id}()")
                continue
            if not isinstance(target, ast.Attribute) or not isinstance(
                target.value, ast.Name
            ):
                continue
            service_name = target.value.id
            if target.attr in _UNSAFE_SERVICE_METHODS.get(service_name, set()):
                reasons.add(f"line {call.lineno}: {service_name}.{target.attr}()")
        local_calls[name] = called_locals
        direct_reasons[name] = reasons

    unsafe_functions = {
        name for name, reasons in direct_reasons.items() if reasons
    }
    changed = True
    while changed:
        changed = False
        for name, called in local_calls.items():
            if name in unsafe_functions or not (called & unsafe_functions):
                continue
            unsafe_functions.add(name)
            changed = True

    relative_path = path.relative_to(REPO_ROOT).as_posix()
    violations: list[str] = []
    for name, function in functions.items():
        if not isinstance(function, ast.AsyncFunctionDef):
            continue
        if name not in unsafe_functions:
            continue
        reasons = sorted(direct_reasons[name])
        unsafe_helpers = sorted(local_calls[name] & unsafe_functions)
        detail = reasons + [f"unsafe helper: {helper}()" for helper in unsafe_helpers]
        violations.append(
            f"{relative_path}:{function.lineno} async {name}: " + ", ".join(detail)
        )
    return violations


def test_async_plan_actions_do_not_call_sync_vision_or_ocr() -> None:
    violations = [
        violation
        for action_root in PLAN_ACTION_ROOTS
        for path in sorted(action_root.glob("*.py"))
        for violation in _analyze_module(path)
    ]

    assert not violations, (
        "Async plan actions must use async Vision/OCR APIs or explicitly offload "
        "sync calls with asyncio.to_thread:\n" + "\n".join(violations)
    )
