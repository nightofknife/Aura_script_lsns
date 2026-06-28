from __future__ import annotations

from typing import Any

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.utils.exceptions import StopTaskException

from ..services.process_manager_service import ProcessManagerService


@action_info(name="start_process", public=True)
@requires_services(process_manager="process_manager")
def start_process(
    process_manager: ProcessManagerService,
    identifier: str,
    executable_path: str | None = None,
    args: list[str] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    res = process_manager.start_process(
        identifier=identifier,
        executable_path=executable_path,
        args=args,
        cwd=cwd,
        env=env,
    )
    if res.get("status") not in ("success", "already_running"):
        raise StopTaskException(f"启动进程失败：{res.get('message')}", success=False)
    return res


@action_info(name="stop_process", public=True)
@requires_services(process_manager="process_manager")
def stop_process(
    process_manager: ProcessManagerService,
    identifier: str,
    force: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any]:
    res = process_manager.stop_process(identifier=identifier, force=force, timeout=timeout)
    if res.get("status") == "error":
        raise StopTaskException(f"停止进程失败：{res.get('message')}", success=False)
    return res


@action_info(name="get_process_status", read_only=True, public=True)
@requires_services(process_manager="process_manager")
def get_process_status(process_manager: ProcessManagerService, identifier: str) -> dict[str, Any]:
    return process_manager.get_process_status(identifier=identifier)


@action_info(name="wait_for_process_exit", public=True)
@requires_services(process_manager="process_manager")
def wait_for_process_exit(
    process_manager: ProcessManagerService,
    identifier: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    res = process_manager.wait_for_exit(identifier=identifier, timeout=timeout)
    if res.get("status") == "timeout":
        raise StopTaskException("等待进程退出超时。", success=False)
    return res
