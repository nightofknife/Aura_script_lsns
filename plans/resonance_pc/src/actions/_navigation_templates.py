"""Small immutable profiles for navigation artwork shared by PC actions."""
from __future__ import annotations

from dataclasses import dataclass

import cv2


@dataclass(frozen=True)
class NavigationTemplateProfile:
    path: str
    roi: tuple[int, int, int, int]
    threshold: float
    use_grayscale: bool = True
    match_method: int = cv2.TM_CCOEFF_NORMED
    preprocess: str = "none"

    def match_kwargs(self) -> dict:
        return {"threshold": self.threshold, "use_grayscale": self.use_grayscale,
                "match_method": self.match_method, "preprocess": self.preprocess}

    def layout_spec(self) -> dict:
        # Loaders add resolved paths and normalize ROIs on their own copy.
        return {"path": self.path, "roi": list(self.roi), **self.match_kwargs()}


PROFILE_MENU = NavigationTemplateProfile(
    "templates/navigation/global/profile_entry.png", (305, 293, 215, 45), .85,
)
GLOBAL_BACK = NavigationTemplateProfile(
    "templates/navigation/global/back.png", (0, 0, 170, 80), .86,
)
GLOBAL_CITY_MAIN = NavigationTemplateProfile(
    "templates/navigation/global/city_main.png", (140, 0, 130, 80), .86,
)


def navigation_profile_layout(reference: str) -> dict:
    """Expand the one profile reference currently shared with JSON catalogs."""
    if reference != "navigation.profile":
        raise ValueError(f"Unknown navigation template profile: {reference}")
    return PROFILE_MENU.layout_spec()
