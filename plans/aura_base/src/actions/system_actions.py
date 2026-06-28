from __future__ import annotations

from typing import Any

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.events import Event, EventBus
from packages.aura_core.observability.logging.core_logger import logger

from ..services.app_provider_service import AppProviderService


@action_info(name="publish_event", public=True)
@requires_services(event_bus="core/event_bus")
async def publish_event(
    event_bus: EventBus,
    name: str,
    payload: dict[str, Any] | None = None,
    source: str | None = None,
    channel: str = "global",
) -> bool:
    try:
        event_payload = dict(payload or {})
        if source and "source" not in event_payload:
            event_payload["source"] = source
        new_event = Event(name=name, channel=channel, payload=event_payload)
        await event_bus.publish(new_event)
        return True
    except Exception as exc:
        logger.error("发布事件 '%s' 时失败: %s", name, exc, exc_info=True)
        return False


@action_info(name="get_window_size", read_only=True, public=True)
@requires_services(app="app")
def get_window_size(app: AppProviderService) -> tuple[int, int] | None:
    return app.get_window_size()


@action_info(name="focus_window", public=True)
@requires_services(app="app")
def focus_window(app: AppProviderService) -> bool:
    return app.focus()


@action_info(name="focus_window_with_input", public=True)
@requires_services(app="app")
def focus_window_with_input(app: AppProviderService, click_delay: float = 0.3) -> bool:
    return app.focus_with_input(click_delay)


@action_info(name="file_read", read_only=True, public=True)
def file_read(engine: ExecutionEngine, file_path: str) -> str | None:
    try:
        full_path = engine.orchestrator.current_plan_path / file_path
        if not full_path.is_file():
            logger.error("文件读取失败：'%s' 不存在或不是一个文件。", file_path)
            return None
        return full_path.read_text("utf-8")
    except Exception as exc:
        logger.error("读取文件 '%s' 时发生错误: %s", file_path, exc)
        return None


@action_info(name="file_write", public=True)
def file_write(engine: ExecutionEngine, file_path: str, content: str, append: bool = False) -> bool:
    try:
        full_path = engine.orchestrator.current_plan_path / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with full_path.open(mode, encoding="utf-8") as handle:
            handle.write(content)
        return True
    except Exception as exc:
        logger.error("写入文件 '%s' 时发生错误: %s", file_path, exc)
        return False
