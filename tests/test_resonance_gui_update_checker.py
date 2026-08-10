from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from packages.resonance_gui.update_checker import check_for_update, find_available_update


class _Response:
    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._data


def _write_build_info(root, tag: str = "v1.2.3") -> None:
    (root / "BUILD-INFO.json").write_text(
        json.dumps({"release_label": tag, "profile": "cpu"}),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("latest_tag", "expected"),
    [
        ("v1.2.4", True),
        ("v1.2.3", False),
        ("v1.2.2", False),
        ("v2.0.0", True),
    ],
)
def test_update_checker_compares_formal_semantic_versions(tmp_path, latest_tag, expected):
    _write_build_info(tmp_path)
    payload = {"tag_name": latest_tag, "draft": False, "prerelease": False}

    result = check_for_update(base_path=tmp_path, opener=lambda *_args, **_kwargs: _Response(payload))

    assert result is not None
    assert result.update_available is expected


@pytest.mark.parametrize(
    "payload",
    [
        {"tag_name": "nightly", "draft": False, "prerelease": False},
        {"tag_name": "v1.2.4", "draft": True, "prerelease": False},
        {"tag_name": "v1.2.4", "draft": False, "prerelease": True},
    ],
)
def test_update_checker_ignores_invalid_or_non_formal_releases(tmp_path, payload):
    _write_build_info(tmp_path)

    assert check_for_update(
        base_path=tmp_path,
        opener=lambda *_args, **_kwargs: _Response(payload),
    ) is None


def test_update_checker_skips_network_without_packaged_build_info(tmp_path):
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be used")

    assert check_for_update(base_path=tmp_path, opener=opener) is None
    assert called is False


def test_update_checker_silently_ignores_network_errors(tmp_path):
    _write_build_info(tmp_path)
    with patch(
        "packages.resonance_gui.update_checker.urllib.request.urlopen",
        side_effect=OSError("offline"),
    ):
        assert find_available_update(base_path=tmp_path) == ""
