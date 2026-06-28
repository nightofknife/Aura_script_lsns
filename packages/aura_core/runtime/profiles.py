from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    enable_schedule_loop: bool
    enable_interrupt_loop: bool
    enable_event_triggers: bool


_PROFILES: dict[str, RuntimeProfile] = {
    "embedded_full": RuntimeProfile(
        name="embedded_full",
        enable_schedule_loop=True,
        enable_interrupt_loop=True,
        enable_event_triggers=True,
    ),
    "api_full": RuntimeProfile(
        name="api_full",
        enable_schedule_loop=True,
        enable_interrupt_loop=True,
        enable_event_triggers=True,
    ),
    "tui_manual": RuntimeProfile(
        name="tui_manual",
        enable_schedule_loop=False,
        enable_interrupt_loop=False,
        enable_event_triggers=False,
    ),
}


def resolve_runtime_profile(name: str | None) -> RuntimeProfile:
    if not name:
        return _PROFILES["embedded_full"]
    profile = _PROFILES.get(str(name).strip().lower())
    if profile is None:
        supported = ", ".join(sorted(_PROFILES))
        raise ValueError(f"Unknown runtime profile '{name}'. Supported: {supported}")
    return profile
