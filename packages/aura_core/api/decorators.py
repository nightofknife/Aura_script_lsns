# -*- coding: utf-8 -*-
"""Aura public decorators."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, Mapping, Optional


def action_info(
    name: Optional[str] = None,
    read_only: bool = False,
    public: bool = True,
    description: str | None = None,
    visibility: str = "public",
    timeout: int | None = None,
):
    """Attach action metadata to a function."""

    def decorator(func: Callable) -> Callable:
        resolved_name = name or func.__name__
        desc = _resolve_description(func, description)
        service_deps = getattr(func, "_service_dependencies", {})
        parameters = _extract_action_parameters(func, service_deps)

        meta = {
            "name": resolved_name,
            "read_only": read_only,
            "public": public,
            "services": service_deps,
            "description": desc,
            "visibility": visibility,
            "timeout": timeout,
            "parameters": parameters,
            "service_deps": list(service_deps.values()),
            "is_async": inspect.iscoroutinefunction(func),
            "source_file": _safe_get_source_file(func),
            "source_function": func.__name__,
        }

        setattr(func, "_aura_action_meta", meta)
        setattr(func, "__aura_action__", meta)
        return func

    return decorator


def requires_services(*args: str, **kwargs: str):
    """Declare service dependencies for an action function."""

    def decorator(func: Callable) -> Callable:
        dependencies: Dict[str, str] = {}
        for alias, service_id in kwargs.items():
            dependencies[alias] = service_id
        for service_id in args:
            default_alias = str(service_id).split("/")[-1]
            if default_alias in dependencies:
                raise NameError(
                    f"Duplicate injected alias '{default_alias}' in @requires_services for '{func.__name__}'."
                )
            dependencies[default_alias] = service_id
        setattr(func, "_service_dependencies", dependencies)
        return func

    return decorator


def service_info(
    alias: Optional[str] = None,
    public: bool = True,
    description: str | None = None,
    visibility: str = "public",
    singleton: bool = True,
    config_schema: Dict[str, Any] | None = None,
    replace: str | None = None,
    deps: Mapping[str, str] | None = None,
):
    """Attach service metadata to a class."""

    def decorator(cls: type) -> type:
        resolved_alias = alias or _infer_service_alias(cls.__name__)
        if not resolved_alias or not isinstance(resolved_alias, str):
            raise TypeError("Service alias must resolve to a non-empty string.")

        desc = _resolve_description(cls, description)
        dependency_map = dict(deps or {})

        meta = {
            "alias": resolved_alias,
            "public": public,
            "description": desc,
            "visibility": visibility,
            "singleton": singleton,
            "config_schema": config_schema,
            "replace": replace,
            "deps": dependency_map,
            "source_file": _safe_get_source_file(cls),
            "source_class": cls.__name__,
        }

        setattr(cls, "_aura_service_meta", meta)
        setattr(cls, "__aura_service__", meta)
        return cls

    return decorator


def register_hook(name: str):
    """Register a lifecycle hook name on a function."""

    def decorator(func: Callable) -> Callable:
        setattr(func, "_aura_hook_name", name)
        return func

    return decorator


def _resolve_description(obj: Any, explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    desc = inspect.getdoc(obj) or ""
    return desc.split("\n\n")[0].split("Args:")[0].strip()


def _safe_get_source_file(obj: Any) -> str | None:
    try:
        return inspect.getsourcefile(obj) or inspect.getfile(obj)
    except Exception:
        module_name = getattr(obj, "__module__", None)
        if not module_name:
            return None
        module = inspect.getmodule(obj)
        return getattr(module, "__file__", None)


def _extract_action_parameters(func: Callable, service_deps: Mapping[str, str]) -> list[dict[str, Any]]:
    sig = inspect.signature(func)
    parameters = []
    for param_name, param in sig.parameters.items():
        if param_name in {"context", "engine"}:
            continue
        if param_name in service_deps:
            continue

        if param.annotation != inspect.Parameter.empty:
            type_name = getattr(param.annotation, "__name__", str(param.annotation))
            if isinstance(type_name, str) and type_name.endswith("Service"):
                continue

        param_info = {
            "name": param_name,
            "type": _get_type_string(param.annotation),
            "required": param.default == inspect.Parameter.empty,
            "default": param.default if param.default != inspect.Parameter.empty else None,
        }
        param_desc = _extract_param_description(func, param_name)
        if param_desc:
            param_info["description"] = param_desc
        parameters.append(param_info)
    return parameters


def _get_type_string(annotation) -> str:
    if annotation == inspect.Parameter.empty:
        return "Any"
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    return str(annotation)


def _extract_param_description(func, param_name: str) -> str:
    docstring = inspect.getdoc(func)
    if not docstring:
        return ""

    lines = docstring.split("\n")
    in_args_section = False
    for i, line in enumerate(lines):
        if "Args:" in line or "Parameters:" in line:
            in_args_section = True
            continue

        if in_args_section:
            if line.strip().startswith(f"{param_name}:"):
                desc = line.split(":", 1)[1].strip()
                j = i + 1
                while j < len(lines) and lines[j].startswith("        "):
                    desc += " " + lines[j].strip()
                    j += 1
                return desc
            if line.strip() and not line.startswith(" "):
                break

    return ""


def _infer_service_alias(class_name: str) -> str:
    if not class_name:
        return ""
    base = class_name[:-7] if class_name.endswith("Service") and len(class_name) > 7 else class_name
    result = []
    for idx, ch in enumerate(base):
        if ch.isupper() and idx > 0 and (not base[idx - 1].isupper()):
            result.append("_")
        result.append(ch.lower())
    return "".join(result)
