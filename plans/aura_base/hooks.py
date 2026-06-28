# -*- coding: utf-8 -*-
from pathlib import Path
from typing import Any, Dict

from packages.aura_core.api import register_hook, service_registry
from packages.aura_core.observability.logging.core_logger import logger


@register_hook("plan.after_load")
def auto_register_template_libraries(plan_name: str, plan_path: Path, config: Dict[str, Any], **kwargs):
    vision = service_registry.get_service_instance("vision")
    if not vision:
        logger.warning("Template library registration skipped: vision service not available.")
        return

    templates_cfg = (config or {}).get("templates") or {}
    libraries = templates_cfg.get("libraries") or []
    if not isinstance(libraries, list):
        logger.warning("Invalid templates.libraries config for plan '%s'.", plan_name)
        return

    for item in libraries:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        path_value = item.get("path")
        if not name or not path_value:
            continue
        root_path = Path(path_value)
        if not root_path.is_absolute():
            root_path = Path(plan_path) / root_path
        vision.register_template_library(
            plan_key=plan_name,
            name=name,
            root=root_path,
            recursive=bool(item.get("recursive", False)),
            extensions=item.get("extensions"),
        )
