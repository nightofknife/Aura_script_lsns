# -*- coding: utf-8 -*-
"""上下文持久化层。

此模块提供数据持久化策略和状态存储功能。

核心组件:
- IPersistenceStrategy: 持久化策略接口（文件/StateStore/无）
- StateStoreService: 状态存储服务，全局键值存储（需直接导入避免循环依赖）
"""

from .strategy import IPersistenceStrategy, StateStorePersistence, DatabasePersistence, NoPersistence
from .persistent_data_errors import (
    PersistentDataError,
    PersistentDataNotFoundError,
    PersistentDataReadError,
    PersistentDataValidationError,
    PersistentDataWriteError,
)
from .persistent_data_service import PersistentDataResult, PersistentDataService
# StateStoreService 需要直接导入以避免循环依赖:
# from packages.aura_core.context.persistence.store_service import StateStoreService

__all__ = [
    'IPersistenceStrategy',
    'StateStorePersistence',
    'DatabasePersistence',
    'NoPersistence',
    'PersistentDataError',
    'PersistentDataNotFoundError',
    'PersistentDataReadError',
    'PersistentDataValidationError',
    'PersistentDataWriteError',
    'PersistentDataResult',
    'PersistentDataService',
]
