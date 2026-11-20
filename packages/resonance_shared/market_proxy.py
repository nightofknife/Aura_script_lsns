"""Shared settings for fetching Columba market prices."""

from __future__ import annotations


def normalize_market_proxy(
    mode: str,
    port: int | None,
    *,
    allow_incomplete: bool = False,
) -> dict[str, str | int | None]:
    if mode not in ("automatic", "manual"):
        raise ValueError("行情连接方式必须为自动或手动代理。")
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("行情代理端口必须是 1–65535 之间的整数。")
    if mode == "manual" and port is None and not allow_incomplete:
        raise ValueError("请输入 1–65535 之间的 HTTP 或混合代理端口。")
    return {"market_proxy_mode": mode, "market_proxy_port": port}
