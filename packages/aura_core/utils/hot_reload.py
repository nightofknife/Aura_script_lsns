# -*- coding: utf-8 -*-
"""Hot reload policy using watchdog."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # type: ignore
    Observer = None  # type: ignore

from packages.aura_core.observability.logging.core_logger import logger


class HotReloadHandler(FileSystemEventHandler):
    def __init__(self, scheduler: Any):
        self._scheduler = scheduler
        self._loop = scheduler._loop

    def on_modified(self, event):
        if not self._loop or not self._loop.is_running():
            logger.warning("Event loop unavailable, skip hot reload.")
            return

        if event.is_directory:
            return

        file_path = Path(event.src_path)
        if file_path.name.startswith(".") or "__pycache__" in file_path.parts:
            return

        if file_path.suffix == ".yaml" and "tasks" in file_path.parts:
            logger.info("[Hot Reload] Task file changed: %s", file_path.name)
            asyncio.run_coroutine_threadsafe(
                self._scheduler.reload_task_file(file_path),
                self._loop,
            )
        elif file_path.suffix == ".py":
            logger.info("[Hot Reload] Python file changed: %s", file_path.name)
            asyncio.run_coroutine_threadsafe(
                self._scheduler.reload_plugin_from_py_file(file_path),
                self._loop,
            )


class HotReloadPolicy:
    def __init__(self, scheduler: Any):
        self._scheduler = scheduler

    def enable(self) -> dict:
        if not self._scheduler._loop or not self._scheduler._loop.is_running():
            return {"status": "error", "message": "Scheduler is not running, cannot enable hot reload."}

        observer = self._scheduler._hot_reload_observer
        if observer and observer.is_alive():
            return {"status": "already_enabled", "message": "Hot reloading is already active."}

        logger.info("Enabling hot reload watcher...")
        event_handler = HotReloadHandler(self._scheduler)
        self._scheduler._hot_reload_observer = Observer()
        plans_path = str(self._scheduler.base_path / "plans")
        self._scheduler._hot_reload_observer.schedule(event_handler, plans_path, recursive=True)
        self._scheduler._hot_reload_observer.start()
        logger.info("Hot reload started; watching %s", plans_path)
        return {"status": "enabled", "message": "Hot reloading has been enabled."}

    def disable(self) -> dict:
        observer = self._scheduler._hot_reload_observer
        if observer and observer.is_alive():
            logger.info("Disabling hot reload watcher...")
            observer.stop()
            observer.join()
            self._scheduler._hot_reload_observer = None
            logger.info("Hot reload watcher stopped.")
            return {"status": "disabled", "message": "Hot reloading has been disabled."}

        return {"status": "not_active", "message": "Hot reloading was not active."}

    def is_enabled(self) -> bool:
        observer = self._scheduler._hot_reload_observer
        return bool(observer and observer.is_alive())
