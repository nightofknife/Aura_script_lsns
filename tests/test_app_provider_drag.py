from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, call, patch

from plans.aura_base.src.services.app_provider_service import AppProviderService


def _build_app() -> AppProviderService:
    return AppProviderService(screen=Mock(), controller=Mock(), target_runtime=Mock())


def test_drag_default_releases_without_hold():
    app = _build_app()

    with patch("plans.aura_base.src.services.app_provider_service.time.sleep") as sleep:
        app.drag(10, 20, 30, 40, duration=0.25)

    assert app.controller.move_to.call_args_list == [
        call(10, 20, 0.0),
        call(30, 40, duration=0.25),
    ]
    app.controller.mouse_down.assert_called_once_with("left")
    app.controller.mouse_up.assert_called_once_with("left")
    sleep.assert_not_called()


def test_drag_holds_at_endpoint_before_release():
    app = _build_app()
    events: list[tuple[object, ...]] = []
    app.move_to = Mock(side_effect=lambda *args, **kwargs: events.append(("move_start", args, kwargs)))
    app.controller.mouse_down.side_effect = lambda button: events.append(("mouse_down", button))
    app.controller.move_to.side_effect = lambda *args, **kwargs: events.append(("move_end", args, kwargs))
    app.controller.mouse_up.side_effect = lambda button: events.append(("mouse_up", button))

    with patch(
        "plans.aura_base.src.services.app_provider_service.time.sleep",
        side_effect=lambda seconds: events.append(("sleep", seconds)),
    ):
        app.drag(10, 20, 30, 40, duration=0.25, hold_before_release_sec=0.5)

    assert [event[0] for event in events] == ["move_start", "mouse_down", "move_end", "sleep", "mouse_up"]
    assert events[3] == ("sleep", 0.5)


def test_drag_async_holds_at_endpoint_before_release():
    app = _build_app()
    app.move_to_async = AsyncMock()
    app.controller.mouse_down_async = AsyncMock()
    app.controller.move_to_async = AsyncMock()
    app.controller.mouse_up_async = AsyncMock()

    with patch("plans.aura_base.src.services.app_provider_service.asyncio.sleep", new=AsyncMock()) as sleep:
        asyncio.run(app.drag_async(10, 20, 30, 40, duration=0.25, hold_before_release_sec=0.5))

    app.controller.move_to_async.assert_awaited_once_with(30, 40, duration=0.25)
    sleep.assert_awaited_once_with(0.5)
    app.controller.mouse_up_async.assert_awaited_once_with("left")
