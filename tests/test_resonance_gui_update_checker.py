from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from packages.resonance_gui.update_checker import (
    LATEST_CHECKSUMS_URL,
    check_for_update,
    find_available_update,
)


class _Response:
    def __init__(self, contents: bytes) -> None:
        self._data = contents

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._data if size < 0 else self._data[:size]


def _checksum_manifest(
    tag: str,
    *,
    gpu_tag: str | None = None,
    overlay_tag: str | None = None,
) -> bytes:
    gpu = gpu_tag or tag
    overlay = overlay_tag or tag
    digest = "a" * 64
    return (
        f"{digest}  AuraResonance-{tag}-win-x64-cpu.zip\n"
        f"{digest}  AuraResonance-{gpu}-win-x64-gpu.zip\n"
        f"{digest}  AuraResonance-{overlay}-nvidia-cu13-overlay.zip\n"
    ).encode()


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
    requested_urls: list[str] = []

    def opener(request, **_kwargs):
        requested_urls.append(request.full_url)
        return _Response(_checksum_manifest(latest_tag))

    result = check_for_update(base_path=tmp_path, opener=opener)

    assert result is not None
    assert result.update_available is expected
    assert requested_urls == [LATEST_CHECKSUMS_URL]
    assert "api.github.com" not in requested_urls[0]


@pytest.mark.parametrize(
    "contents",
    [
        b"not a checksum manifest\n",
        _checksum_manifest("v1.2.4", gpu_tag="v1.2.5"),
        _checksum_manifest("v1.2.4", overlay_tag="v1.2.5"),
        ("a" * 64 + "  AuraResonance-v1.2.4-win-x64-cpu.zip\n").encode(),
        ("a" * 64 + "  AuraResonance-nightly-win-x64-cpu.zip\n").encode(),
    ],
)
def test_update_checker_ignores_invalid_checksum_manifests(tmp_path, contents):
    _write_build_info(tmp_path)

    assert check_for_update(
        base_path=tmp_path,
        opener=lambda *_args, **_kwargs: _Response(contents),
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
