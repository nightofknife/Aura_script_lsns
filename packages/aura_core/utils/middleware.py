# -*- coding: utf-8 -*-
"""Action middleware pipeline."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Awaitable, Callable, Dict, List

from ..api import ActionDefinition
from ..context import ExecutionContext
from packages.aura_core.observability.logging.core_logger import logger


class Middleware:
    """Base middleware type."""

    async def handle(
        self,
        action_def: ActionDefinition,
        context: ExecutionContext,
        params: Dict[str, Any],
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        return await next_handler(action_def, context, params)


class MiddlewareManager:
    """Manage and execute middleware chain."""

    def __init__(self):
        self._middlewares: List[Middleware] = []

    def add(self, middleware: Middleware):
        self._middlewares.append(middleware)

    async def process(
        self,
        action_def: ActionDefinition,
        context: ExecutionContext,
        params: Dict[str, Any],
        final_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        if not self._middlewares:
            return await final_handler(action_def, context, params)

        handler = final_handler
        for middleware in reversed(self._middlewares):
            async def wrapper(
                ad: ActionDefinition,
                ctx: ExecutionContext,
                p: Dict[str, Any],
                *,
                _middleware: Middleware = middleware,
                _next_handler: Callable[..., Awaitable[Any]] = handler,
            ) -> Any:
                middleware_handle = _middleware.handle
                if asyncio.iscoroutinefunction(middleware_handle):
                    return await middleware_handle(ad, ctx, p, _next_handler)

                logger.debug(
                    "Running sync middleware in executor: %s",
                    getattr(middleware_handle, "__qualname__", str(middleware_handle)),
                )
                loop = asyncio.get_running_loop()
                sync_callable = partial(
                    middleware_handle,
                    ad,
                    ctx,
                    p,
                    lambda *a, **kw: asyncio.run_coroutine_threadsafe(_next_handler(*a, **kw), loop).result(),
                )
                return await loop.run_in_executor(None, sync_callable)

            handler = wrapper

        return await handler(action_def, context, params)


middleware_manager = MiddlewareManager()
