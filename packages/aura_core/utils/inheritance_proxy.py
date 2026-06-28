# -*- coding: utf-8 -*-
"""提供一个用于实现服务继承的代理类。

此模块中的 `InheritanceProxy` 是 Aura 框架服务扩展机制的核心。
当一个服务（子服务）声明它扩展（`extends`）另一个服务（父服务）时，
框架会创建一个此代理类的实例，将父子服务实例包装起来。
"""

class InheritanceProxy:
    """一个动态代理类，用于无缝地结合父服务和子服务的功能。

    当访问此代理对象的属性或方法时，它会遵循一个特定的查找顺序：
    1.  首先在子服务实例中查找。
    2.  如果子服务中没有找到，则在父服务实例中查找。
    3.  如果两处都找不到，则抛出 `AttributeError`。

    这种机制允许子服务可以覆盖父服务的部分方法，同时又能无缝地
    “继承”父服务提供的所有其他功能，而无需使用传统的类继承。

    Attributes:
        _parent_service (object): 被包装的父服务实例。
        _child_service (object): 被包装的子服务实例。
    """

    def __init__(self, parent_service: object, child_service: object):
        """初始化继承代理。

        Args:
            parent_service: 父服务的实例。
            child_service: 子服务的实例。
        """
        self._parent_service = parent_service
        self._child_service = child_service

    def __getattr__(self, name: str):
        """在获取属性时动态地在子服务和父服务中查找。

        Args:
            name (str): 要获取的属性或方法的名称。

        Returns:
            在子服务或父服务中找到的属性或方法。

        Raises:
            AttributeError: 如果在子服务和父服务中都找不到指定的属性。
        """
        # 优先从子服务获取属性或方法
        if hasattr(self._child_service, name):
            return getattr(self._child_service, name)

        # 如果子服务没有，则从父服务获取
        if hasattr(self._parent_service, name):
            return getattr(self._parent_service, name)

        # 如果都找不到，则抛出标准的 AttributeError
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
