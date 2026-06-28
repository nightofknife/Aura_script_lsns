# -*- coding: utf-8 -*-
"""日志系统。

核心组件:
- logger: 全局日志实例
- StructuredLogger: 结构化日志
"""

from .core_logger import logger
from .structured import StructuredLogger

__all__ = ['logger', 'StructuredLogger']
