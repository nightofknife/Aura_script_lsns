 # -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""文件监控服务。

此模块定义了 `FileWatcherService`，用于监听文件系统变动并发布事件。
使用 `watchdog` 库来实现跨平台的文件监控。
"""
import asyncio
from typing import Dict, Any, List, Optional
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    Observer = None  # type: ignore
    FileSystemEventHandler = object  # type: ignore

from packages.aura_core.observability.events import Event, EventBus
from packages.aura_core.observability.logging.core_logger import logger
from ..api import service_info

class AuraFileEventHandler(FileSystemEventHandler):
    """自定义的文件事件处理器，将 watchdog 事件转发到 EventBus。"""

    def __init__(self, service: 'FileWatcherService', watch_id: str, events: List[str], recursive: bool):
        self.service = service
        self.watch_id = watch_id
        self.events = events
        self.recursive = recursive
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    def _handle_event(self, event, event_type: str):
        if event.is_directory:
            return

        # 如果只配置了监听特定事件，则过滤
        if self.events and event_type not in self.events:
            return

        payload = {
            "watch_id": self.watch_id,
            "path": event.src_path,
            "event_type": event_type,
            "is_directory": event.is_directory
        }

        # 在主循环中发布事件
        if self.service.event_bus and self.loop:
            asyncio.run_coroutine_threadsafe(
                self.service.event_bus.publish(Event(
                    name=f"file.changed",
                    channel="file_watcher",
                    payload=payload
                )),
                self.loop
            )

    def on_modified(self, event):
        self._handle_event(event, "modified")

    def on_created(self, event):
        self._handle_event(event, "created")

    def on_deleted(self, event):
        self._handle_event(event, "deleted")

    def on_moved(self, event):
        self._handle_event(event, "moved")


@service_info(alias="file_watcher", public=False)
class FileWatcherService:
    """管理文件系统监听的服务。"""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        if not WATCHDOG_AVAILABLE:
            logger.warning(
                "watchdog library is not installed. File watching will be disabled. "
                "Install it with: pip install watchdog"
            )
            self.observer = None
        else:
            self.observer = Observer()
        self.watches: Dict[str, Any] = {}
        self.is_running = False

    def start(self):
        """启动文件监控服务。"""
        if not WATCHDOG_AVAILABLE or self.observer is None:
            logger.debug("FileWatcherService: watchdog not available, skipping start")
            return
        if not self.is_running:
            self.observer.start()
            self.is_running = True
            logger.info("FileWatcherService 已启动。")

    def stop(self):
        """停止文件监控服务。"""
        if not WATCHDOG_AVAILABLE or self.observer is None:
            return
        if self.is_running:
            self.observer.stop()
            self.observer.join()
            self.is_running = False
            logger.info("FileWatcherService 已停止。")

            # 重新创建 Observer 对象，因为线程对象只能启动一次
            # 保存现有的 watches 配置
            old_watches = self.watches.copy()

            # 创建新的 Observer 实例
            self.observer = Observer()
            self.watches.clear()

            # 重新注册之前的 watches
            # 注意：这里只清空 watches 字典，不重新注册
            # 因为 watches 的注册应该由调用方在需要时重新添加
            # 如果需要自动恢复 watches，可以在这里添加逻辑
            logger.debug("Observer 对象已重置，准备下次启动。")

    def add_watch(self, watch_id: str, path: str, events: List[str] = None, recursive: bool = False):
        """添加一个文件/目录监听。

        Args:
            watch_id: 监听器的唯一ID，用于去重和管理。
            path: 要监听的目录路径。
            events: 要监听的事件列表，如 ["created", "modified"]。如果不传则监听所有。
            recursive: 是否递归监听子目录。
        """
        if watch_id in self.watches:
            logger.warning(f"Watch ID '{watch_id}' already exists. Skipping.")
            return

        abs_path = str(Path(path).resolve())
        if not Path(abs_path).exists():
            logger.warning(f"Path '{abs_path}' does not exist. Watch '{watch_id}' skipped.")
            return

        handler = AuraFileEventHandler(self, watch_id, events, recursive)
        watch = self.observer.schedule(handler, abs_path, recursive=recursive)
        self.watches[watch_id] = watch
        logger.info(f"Added file watch: {watch_id} -> {abs_path} (recursive={recursive})")

    def remove_watch(self, watch_id: str):
        """移除一个监听。"""
        if watch_id in self.watches:
            watch = self.watches[watch_id]
            self.observer.unschedule(watch)
            del self.watches[watch_id]
            logger.info(f"Removed file watch: {watch_id}")
