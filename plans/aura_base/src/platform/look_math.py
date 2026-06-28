from __future__ import annotations

from .contracts import TargetRuntimeError


def resolve_look_direction_vector(direction: str, strength: float) -> tuple[float, float]:
    """Map a cardinal look direction plus strength to a normalized vector."""

    normalized_direction = str(direction or "").strip().lower()
    normalized_strength = float(strength)
    if normalized_strength < 0.0 or normalized_strength > 1.0:
        raise TargetRuntimeError(
            "look_strength_invalid",
            "look_direction strength must be in [0.0, 1.0].",
            {"strength": normalized_strength},
        )

    direction_map = {
        "left": (-normalized_strength, 0.0),
        "right": (normalized_strength, 0.0),
        "up": (0.0, -normalized_strength),
        "down": (0.0, normalized_strength),
    }
    if normalized_direction not in direction_map:
        raise TargetRuntimeError(
            "look_direction_invalid",
            "look_direction direction must be one of: left, right, up, down.",
            {"direction": direction},
        )
    return direction_map[normalized_direction]
