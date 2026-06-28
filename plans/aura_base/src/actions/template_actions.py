from __future__ import annotations

from pathlib import Path
from typing import Any

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine

from ..services.screen_service import ScreenService
from ..services.vision_service import VisionService


@action_info(name="register_template_library", public=True)
@requires_services(vision="vision")
def register_template_library(
    vision: VisionService,
    engine: ExecutionEngine,
    name: str,
    path: str,
    recursive: bool = False,
    extensions: list[str] | None = None,
) -> bool:
    plan_name = engine.orchestrator.plan_name
    plan_path = engine.orchestrator.current_plan_path
    root_path = Path(path)
    if not root_path.is_absolute():
        root_path = plan_path / root_path
    vision.register_template_library(
        plan_key=plan_name,
        name=name,
        root=root_path,
        recursive=recursive,
        extensions=extensions,
    )
    return True


@action_info(name="unregister_template_library", public=True)
@requires_services(vision="vision")
def unregister_template_library(vision: VisionService, engine: ExecutionEngine, name: str) -> bool:
    plan_name = engine.orchestrator.plan_name
    vision.unregister_template_library(plan_key=plan_name, name=name)
    return True


@action_info(name="list_template_libraries", read_only=True, public=True)
@requires_services(vision="vision")
def list_template_libraries(vision: VisionService, engine: ExecutionEngine) -> dict[str, dict[str, Any]]:
    plan_name = engine.orchestrator.plan_name
    return vision.list_template_libraries(plan_key=plan_name)


@action_info(name="list_capture_backends", read_only=True, public=True)
@requires_services(screen="screen")
def list_capture_backends(screen: ScreenService) -> dict[str, Any]:
    return screen.list_backends()


@action_info(name="set_capture_backend", public=True)
@requires_services(screen="screen")
def set_capture_backend(screen: ScreenService, backend: str) -> bool:
    screen.set_default_backend(backend)
    return True


@action_info(name="screen_selfcheck", read_only=True, public=True)
@requires_services(screen="screen")
def screen_selfcheck(screen: ScreenService) -> dict[str, Any]:
    return screen.self_check()
