# -*- coding: utf-8 -*-
"""日志系统。

核心组件:
- logger: 全局日志实例
- StructuredLogger: 结构化日志
"""

from .core_logger import logger
from .structured import StructuredLogger

def __getattr__(name):
    if name in {'start_session', 'attach_session', 'get_current_log_path', 'flush', 'close_session'}:
        from . import coordinator
        return getattr(coordinator, name)
    raise AttributeError(name)


__all__ = ['logger', 'StructuredLogger', 'start_session', 'attach_session',
           'get_current_log_path', 'flush', 'close_session']
