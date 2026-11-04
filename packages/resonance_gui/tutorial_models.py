"""Content model shared by the offline help articles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TutorialChapter:
    title: str
    body: str
    action_label: str
    destination: str
    category: str = "快速上手"
    keywords: tuple[str, ...] = ()
