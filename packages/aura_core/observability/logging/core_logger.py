# -*- coding: utf-8 -*-
"""Aura 框架的全局日志记录器模块。

此模块提供了一个全局、单例的 `Logger` 类，用于在整个框架中进行
统一的、可配置的日志记录。它支持多种日志输出目标（Handler），
包括控制台、滚动文件、以及用于与 UI 或 API 通信的异步队列。

主要特性:
- **单例模式**: 确保整个应用程序使用同一个日志记录器实例。
- **多处理器支持**: 可以同时将日志输出到控制台、文件和多个队列。
- **自定义日志级别**: 添加了 `TRACE` 级别（级别号 5）用于更详细的调试。
- **异步队列处理器**: 提供了 `AsyncioQueueHandler`，用于从任何线程
  安全地将日志记录发送到 `asyncio.Queue`，非常适合 API 的实时日志流。
- **动态配置**: `setup` 方法允许在运行时配置日志级别、文件路径和队列。
"""
import logging
import os
import queue
import sys
import time
import asyncio
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from typing import Optional, Union


class QueueLogHandler(logging.Handler):
    """一个自定义的日志处理器，它将日志记录发送到一个标准的 `queue.Queue` 中。

    这个处理器是线程安全的，主要用于将日志从工作线程发送到主 UI 线程
    （例如，在一个 Tkinter 应用中）。
    """

    def __init__(self, log_queue: queue.Queue):
        """初始化 QueueLogHandler。

        Args:
            log_queue: 日志记录将被放入的 `queue.Queue` 实例。
        """
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        """当日志被记录时，此方法会被调用。

        它会格式化日志记录，然后将其放入队列中。
        """
        log_entry = self.format(record)
        try:
            self.log_queue.put_nowait(log_entry)
        except queue.Full:
            try:
                self.log_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self.log_queue.put_nowait(log_entry)
            except queue.Full:
                pass


# --- 自定义 TRACE 日志级别 ---
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")

def trace(self, message, *args, **kwargs):
    """为 Logger 类添加 a.trace() 方法。"""
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kwargs)

logging.Logger.trace = trace
# --- TRACE 配置结束 ---


# ?? CID ??????????
_cid_context: ContextVar[str] = ContextVar("aura_cid", default="-")


def set_cid(cid: Optional[str]):
    """????????? CID ?????? token ????"""
    return _cid_context.set(str(cid) if cid else "-")


def reset_cid(token):
    """????? CID ???"""
    try:
        _cid_context.reset(token)
    except Exception:
        pass


def current_cid() -> str:
    """????? CID ?"""
    try:
        return _cid_context.get()
    except Exception:
        return "-"


class CIDLogFilter(logging.Filter):
    """????????? cid ????? formatter ??? key"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.cid = current_cid()
        except Exception:
            record.cid = "-"
        return True


class AsyncioQueueHandler(logging.Handler):
    """一个自定义的日志处理器，它将日志记录放入一个 `asyncio.Queue` 中。

    这个处理器是线程安全的，它允许从任何线程（同步或异步）中将日志记录
    安全地发送到一个在特定事件循环中运行的 `asyncio.Queue`。
    这是实现向 WebSocket 客户端流式传输实时日志的关键。
    """

    def __init__(self, log_queue: asyncio.Queue):
        """初始化 AsyncioQueueHandler。

        Args:
            log_queue: 日志记录将被放入的 `asyncio.Queue` 实例。
        """
        super().__init__()
        self.log_queue = log_queue
        self.loop = None

    def emit(self, record):
        """当日志被记录时，此方法会被调用。

        它会捕获当前运行的事件循环（如果需要），并将格式化后的日志记录
        通过 `call_soon_threadsafe` 放入 `asyncio.Queue`。
        """
        if self.loop is None:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # 如果没有正在运行的事件循环，则无法发送日志

        log_entry = {
            'name': record.name,
            'msg': record.getMessage(),
            'args': record.args,
            'levelname': record.levelname,
            'levelno': record.levelno,
            'pathname': record.pathname,
            'filename': record.filename,
            'module': record.module,
            'exc_info': record.exc_info,
            'exc_text': record.exc_text,
            'stack_info': record.stack_info,
            'lineno': record.lineno,
            'funcName': record.funcName,
            'created': record.created,
            'msecs': record.msecs,
            'relativeCreated': record.relativeCreated,
            'thread': record.thread,
            'threadName': record.threadName,
            'processName': record.processName,
            'process': record.process,
        }
        # 为了向后兼容，同时保留 'message' 键
        log_entry['message'] = log_entry['msg']

        try:
            self.loop.call_soon_threadsafe(self.log_queue.put_nowait, log_entry)
        except Exception:
            # 忽略将日志放入队列时可能发生的任何异常（例如队列已满）
            pass


class Logger:
    """Aura 框架的单例日志记录器类。

    通过 `logger = Logger()` 获取全局唯一的实例。
    提供了标准的日志记录方法（`info`, `debug`, `error` 等），
    以及一个 `setup` 方法用于在运行时进行详细配置。
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        """实现单例模式。"""
        if not cls._instance:
            cls._instance = super(Logger, cls).__new__(cls, *args, **kwargs)
            logger_obj = logging.getLogger("AuraFramework")
            logger_obj.setLevel(TRACE_LEVEL_NUM)
            logger_obj.propagate = False
            logger_obj.addFilter(CIDLogFilter())
            # 默认创建一个控制台处理器，作为未调用 setup 时的后备
            if not any(h.name == "console" for h in logger_obj.handlers):
                console_handler = logging.StreamHandler(sys.stdout)
                console_handler.set_name("console")
                console_handler.setLevel(logging.INFO)
                console_formatter = logging.Formatter(
                    '%(asctime)s - %(levelname)-8s - [cid:%(cid)s] - %(message)s',
                    datefmt='%H:%M:%S'
                )
                console_handler.setFormatter(console_formatter)
                logger_obj.addHandler(console_handler)

            cls._instance.logger = logger_obj
        return cls._instance

    def _get_handler(self, name: str) -> Optional[logging.Handler]:
        """(私有) 根据名称获取一个已注册的处理器。"""
        for handler in self.logger.handlers:
            if handler.name == name:
                return handler
        return None

    def setup(self,
              log_dir: str = None,
              task_name: str = None,
              ui_log_queue: queue.Queue = None,
              api_log_queue: Union[queue.Queue, asyncio.Queue, None] = None,
              console_level: Optional[int] = logging.INFO):
        """配置日志记录器。

        此方法可以被多次调用以添加或修改日志处理器。

        Args:
            log_dir (str, optional): 日志文件存放的目录。
            task_name (str, optional): 当前任务的名称，用于生成日志文件名。
            ui_log_queue (queue.Queue, optional): 用于传统 UI 更新的同步队列。
            api_log_queue (asyncio.Queue, optional): 用于 API/WebSocket
                实时日志流的异步队列。
            console_level (int, optional): 控制台输出的日志级别。
                如果设置为 `None`，则会移除控制台处理器。
        """
        # --- 控制台处理器管理 ---
        console_handler = self._get_handler("console")
        if console_level is None:
            if console_handler:
                self.logger.removeHandler(console_handler)
        elif console_handler:
            console_handler.setLevel(console_level)

        # --- 传统 UI 队列处理器 ---
        if ui_log_queue:
            ui_handler = self._get_handler("ui_queue")
            if isinstance(ui_handler, QueueLogHandler):
                ui_handler.log_queue = ui_log_queue
            elif ui_handler is None:
                queue_handler = QueueLogHandler(ui_log_queue)
                queue_handler.set_name("ui_queue")
                queue_handler.setLevel(logging.DEBUG)
                ui_formatter = logging.Formatter('%(asctime)s - %(levelname)-8s - [cid:%(cid)s] - %(message)s', datefmt='%H:%M:%S')
                queue_handler.setFormatter(ui_formatter)
                self.logger.addHandler(queue_handler)

        # --- API WebSocket 日志流处理器 ---
        if api_log_queue:
            api_handler = self._get_handler("api_queue")
            if api_handler and hasattr(api_handler, "log_queue"):
                api_handler.log_queue = api_log_queue  # type: ignore[attr-defined]
            else:
                if isinstance(api_log_queue, queue.Queue):
                    api_queue_handler = QueueLogHandler(api_log_queue)
                    api_queue_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)-8s - [cid:%(cid)s] - %(message)s', datefmt='%H:%M:%S'))
                else:
                    api_queue_handler = AsyncioQueueHandler(api_log_queue)
                api_queue_handler.set_name("api_queue")
                api_queue_handler.setLevel(logging.DEBUG)
                self.logger.addHandler(api_queue_handler)
                self.info("API log streaming queue is connected.")

        # --- 文件处理器 ---
        if log_dir and task_name:
            old_file_handler = self._get_handler("task_file")
            if old_file_handler:
                self.logger.removeHandler(old_file_handler)
                old_file_handler.close()

            os.makedirs(log_dir, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            base_task_name = os.path.basename(task_name)
            safe_base_name = base_task_name.replace('/', '_').replace('\\', '_')
            log_file_path = os.path.join(log_dir, f"{safe_base_name}_{timestamp}.log")

            file_handler = RotatingFileHandler(
                log_file_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
            )
            file_handler.set_name("task_file")
            file_handler.setLevel(logging.DEBUG)
            file_formatter = logging.Formatter(
                '%(asctime)s - %(levelname)-8s - [cid:%(cid)s] - %(name)s - %(module)s.%(funcName)s:%(lineno)d - %(message)s'
            )
            file_handler.setFormatter(file_formatter)
            self.logger.addHandler(file_handler)
            self.info(f"File logging is configured. Log file: {log_file_path}")

    def update_api_queue(self, new_queue: Union[queue.Queue, asyncio.Queue]):
        """在运行时更新或添加 API 日志队列。

        这对于在 API 服务器重新连接时更新 WebSocket 的日志队列非常有用。

        Args:
            new_queue: 新的 `asyncio.Queue` 实例。
        """
        api_handler = self._get_handler("api_queue")
        if api_handler and isinstance(api_handler, AsyncioQueueHandler):
            api_handler.log_queue = new_queue  # type: ignore[assignment]
            self.info("API log queue has been updated.")
        elif new_queue:
            if isinstance(new_queue, queue.Queue):
                api_queue_handler = QueueLogHandler(new_queue)
                api_queue_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)-8s - [cid:%(cid)s] - %(message)s', datefmt='%H:%M:%S'))
            else:
                api_queue_handler = AsyncioQueueHandler(new_queue)
            api_queue_handler.set_name("api_queue")
            api_queue_handler.setLevel(logging.DEBUG)
            self.logger.addHandler(api_queue_handler)
            self.info("New API log streaming queue is connected.")

    def trace(self, message, *args, **kwargs):
        """Log TRACE level messages."""
        self.logger.trace(message, *args, **kwargs)

    def debug(self, message, *args, **kwargs):
        """Log DEBUG level messages."""
        self.logger.debug(message, *args, **kwargs)

    def info(self, message, *args, **kwargs):
        """Log INFO level messages."""
        self.logger.info(message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        """Log WARNING level messages."""
        self.logger.warning(message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        """Log ERROR level messages."""
        self.logger.error(message, *args, **kwargs)

    def critical(self, message, *args, **kwargs):
        """Log CRITICAL level messages."""
        self.logger.critical(message, *args, **kwargs)

    def exception(self, message, *args, **kwargs):
        """Log an ERROR message with exception context."""
        self.logger.exception(message, *args, **kwargs)

logger = Logger()

# 结构化日志支持
def get_structured_logger(name: str = "AuraFramework"):
    """获取结构化日志记录器

    Args:
        name: logger 名称

    Returns:
        StructuredLogger 实例
    """
    from .structured import StructuredLogger
    return StructuredLogger(logging.getLogger(name))

structured_logger = get_structured_logger()
