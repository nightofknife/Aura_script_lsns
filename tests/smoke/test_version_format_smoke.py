"""Smoke checks for the canonical user-facing release version format."""

from __future__ import annotations

import json
from pathlib import Path
import re

from packages.resonance_gui import __version__
from packages.resonance_gui.update_checker import _canonical_version, current_version_label


CANONICAL_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def test_source_version_label_uses_v_prefix(tmp_path: Path) -> None:
    label = current_version_label(base_path=tmp_path)

    assert label == __version__
    assert CANONICAL_VERSION_RE.fullmatch(label)


def test_legacy_numeric_packaged_version_is_normalized_for_display(tmp_path: Path) -> None:
    (tmp_path / "BUILD-INFO.json").write_text(
        json.dumps({"release_label": "1.8.11"}),
        encoding="utf-8",
    )

    assert current_version_label(base_path=tmp_path) == "v1.8.11"


def test_release_tags_are_normalized_for_update_messages() -> None:
    assert _canonical_version("v1.8.11") == "v1.8.11"
    assert _canonical_version("1.8.12") == "v1.8.12"


def test_release_workflow_requires_v_prefixed_versions() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert '- "v*.*.*"' in workflow
    assert "Release version must use vX.X.X format" in workflow
    assert "does not match application version" in workflow
    assert '--title "Aura Resonance $tag"' in workflow
