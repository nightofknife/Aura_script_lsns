# -*- coding: utf-8 -*-
"""结构化日志增强模块

提供结构化日志记录功能，支持 JSON 格式输出，便于日志分析和监控。
"""
import json
import logging
import time
from typing import Any, Dict, Optional
from datetime import datetime


class StructuredLogFormatter(logging.Formatter):
    """结构化日志格式化器

    将日志记录格式化为 JSON 格式，包含所有关键字段和额外的上下文信息。
    """

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录为 JSON 字符串

        Args:
            record: 日志记录对象

        Returns:
            JSON 格式的日志字符串
        """
        # 基础日志字段
        log_data = {
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
            'thread': record.thread,
            'thread_name': record.threadName,
            'process': record.process,
            'process_name': record.processName,
        }

        # 添加 CID (关联 ID)
        if hasattr(record, 'cid'):
            log_data['cid'] = record.cid

        # 添加任务相关信息
        if hasattr(record, 'task_name'):
            log_data['task_name'] = record.task_name
        if hasattr(record, 'plan_name'):
            log_data['plan_name'] = record.plan_name

        # 添加并发控制相关信息
        if hasattr(record, 'concurrency_mode'):
            log_data['concurrency_mode'] = record.concurrency_mode
        if hasattr(record, 'resource_tags'):
            log_data['resource_tags'] = record.resource_tags

        # 添加性能指标
        if hasattr(record, 'duration_ms'):
            log_data['duration_ms'] = record.duration_ms
        if hasattr(record, 'metrics'):
            log_data['metrics'] = record.metrics

        # 添加额外的上下文字段
        if hasattr(record, 'extra_fields'):
            log_data.update(record.extra_fields)

        # 添加异常信息
        if record.exc_info:
            log_data['exception'] = {
                'type': record.exc_info[0].__name__ if record.exc_info[0] else None,
                'message': str(record.exc_info[1]) if record.exc_info[1] else None,
                'traceback': self.formatException(record.exc_info) if record.exc_info else None
            }

        return json.dumps(log_data, ensure_ascii=False, default=str)


class StructuredLogger:
    """结构化日志记录器包装类

    提供便捷的方法来记录带有额外上下文信息的结构化日志。
    """

    def __init__(self, logger: logging.Logger):
        """初始化结构化日志记录器

        Args:
            logger: 底层的 logging.Logger 实例
        """
        self.logger = logger

    def _log_with_context(self, level: int, message: str,
                          extra_fields: Optional[Dict[str, Any]] = None,
                          **kwargs):
        """记录带有额外上下文的日志

        Args:
            level: 日志级别
            message: 日志消息
            extra_fields: 额外的字段字典
            **kwargs: 其他关键字参数（如 exc_info 等）
        """
        extra = kwargs.get('extra', {})
        if extra_fields:
            extra['extra_fields'] = extra_fields
        kwargs['extra'] = extra
        self.logger.log(level, message, **kwargs)

    def info(self, message: str, **fields):
        """记录 INFO 级别的结构化日志

        Args:
            message: 日志消息
            **fields: 额外的字段（如 task_name, duration_ms 等）
        """
        self._log_with_context(logging.INFO, message, extra_fields=fields)

    def debug(self, message: str, **fields):
        """记录 DEBUG 级别的结构化日志"""
        self._log_with_context(logging.DEBUG, message, extra_fields=fields)

    def warning(self, message: str, **fields):
        """记录 WARNING 级别的结构化日志"""
        self._log_with_context(logging.WARNING, message, extra_fields=fields)

    def error(self, message: str, exc_info=None, **fields):
        """记录 ERROR 级别的结构化日志

        Args:
            message: 日志消息
            exc_info: 异常信息（可选）
            **fields: 额外的字段
        """
        self._log_with_context(logging.ERROR, message, extra_fields=fields, exc_info=exc_info)

    def critical(self, message: str, exc_info=None, **fields):
        """记录 CRITICAL 级别的结构化日志"""
        self._log_with_context(logging.CRITICAL, message, extra_fields=fields, exc_info=exc_info)


# 性能监控装饰器
def log_performance(logger: StructuredLogger, operation: str):
    """性能监控装饰器

    自动记录函数执行时间和相关信息

    Args:
        logger: 结构化日志记录器
        operation: 操作名称

    使用示例:
        @log_performance(structured_logger, "task_execution")
        async def execute_task(task_name):
            ...
    """
    def decorator(func):
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = await func(*args, **kwargs)
                duration_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"{operation} completed successfully",
                    operation=operation,
                    duration_ms=round(duration_ms, 2),
                    status="success"
                )
                return result
            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                logger.error(
                    f"{operation} failed",
                    operation=operation,
                    duration_ms=round(duration_ms, 2),
                    status="failed",
                    error_type=type(e).__name__,
                    exc_info=e
                )
                raise

        def sync_wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                duration_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"{operation} completed successfully",
                    operation=operation,
                    duration_ms=round(duration_ms, 2),
                    status="success"
                )
                return result
            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                logger.error(
                    f"{operation} failed",
                    operation=operation,
                    duration_ms=round(duration_ms, 2),
                    status="failed",
                    error_type=type(e).__name__,
                    exc_info=e
                )
                raise

        # 判断是异步还是同步函数
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator
