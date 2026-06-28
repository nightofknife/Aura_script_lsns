# -*- coding: utf-8 -*-
"""Aura 框架的 Manifest 系统。

此模块提供了 manifest.yaml 的数据结构定义、解析和生成功能。

核心组件:
- schema: PluginManifest 和相关数据类定义
- parser: manifest.yaml 文件解析器
- generator: 从代码自动生成 manifest.yaml
"""

from .schema import (
    PluginManifest,
    PackageInfo,
    DependencySpec,
    Exports,
    ExportedService,
    ExportedAction,
    ExportedTask,
    LifecycleHooks,
    BuildConfig,
    TrustInfo,
    ConfigurationSpec,
    ResourceMapping,
)

from .parser import ManifestParser
from .generator import ManifestGenerator

__all__ = [
    # 数据类
    'PluginManifest',
    'PackageInfo',
    'DependencySpec',
    'Exports',
    'ExportedService',
    'ExportedAction',
    'ExportedTask',
    'LifecycleHooks',
    'BuildConfig',
    'TrustInfo',
    'ConfigurationSpec',
    'ResourceMapping',
    # 工具类
    'ManifestParser',
    'ManifestGenerator',
]
