from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
import zipfile

import pytest

from updater.aura_updater import (
    FAILURE_MESSAGE,
    UpdateError,
    extract_release_archive,
    install_staged_release,
    main,
    parse_checksums,
    perform_update,
    terminate_installed_processes,
    validate_staged_release,
)


def _write_file(path: Path, contents: bytes = b"new") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)


def _make_staged_release(root: Path, *, tag: str = "v2.0.0", profile: str = "cpu") -> None:
    for relative in (
        "runtime/aura.exe",
        "runtime/AuraResonanceRuntime.exe",
        "plans/demo/manifest.yaml",
        "models/demo/model.onnx",
        "config.yaml",
        "AuraResonanceGui.exe",
        "更新.exe",
        "run.ps1",
        "README.md",
        "LICENSE",
        "BUILD-INFO.txt",
    ):
        _write_file(root / relative)
    (root / "BUILD-INFO.json").write_text(
        json.dumps({"release_label": tag, "profile": profile}),
        encoding="utf-8",
    )


class _ByteResponse:
    def __init__(self, contents: bytes) -> None:
        self._stream = io.BytesIO(contents)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def _zip_tree(root: Path) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                handle.write(path, path.relative_to(root.parent).as_posix())
    return stream.getvalue()


def test_release_archive_rejects_path_traversal(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("AuraResonance-v2.0.0-win-x64-cpu/../escape.txt", "bad")

    with pytest.raises(UpdateError, match="Unsafe ZIP entry"):
        extract_release_archive(archive, tmp_path / "staging")

    assert not (tmp_path / "escape.txt").exists()


def test_checksum_parser_accepts_release_format_and_rejects_duplicates():
    digest = hashlib.sha256(b"archive").hexdigest()
    assert parse_checksums(f"{digest}  release.zip\n".encode()) == {"release.zip": digest}

    with pytest.raises(UpdateError, match="duplicate"):
        parse_checksums(f"{digest}  release.zip\n{digest}  RELEASE.ZIP\n".encode())


def test_staged_release_must_match_tag_and_profile(tmp_path):
    _make_staged_release(tmp_path, tag="v2.0.0", profile="gpu")

    validate_staged_release(tmp_path, tag="v2.0.0", profile="gpu")
    with pytest.raises(UpdateError, match="tag and profile"):
        validate_staged_release(tmp_path, tag="v2.0.0", profile="cpu")


def test_install_replaces_program_files_and_preserves_portable_data(tmp_path):
    staged = tmp_path / "staged"
    installed = tmp_path / "installed"
    _make_staged_release(staged)
    _write_file(installed / "runtime" / "old.dll", b"old")
    _write_file(installed / "plans" / "old.txt", b"old")
    _write_file(installed / "更新.exe", b"keep-updater")
    _write_file(installed / "gui-settings.ini", b"keep-settings")
    _write_file(installed / "logs" / "run.log", b"keep-log")

    install_staged_release(staged, installed)

    assert not (installed / "runtime" / "old.dll").exists()
    assert (installed / "runtime" / "aura.exe").read_bytes() == b"new"
    assert (installed / "更新.exe").read_bytes() == b"keep-updater"
    assert (installed / "gui-settings.ini").read_bytes() == b"keep-settings"
    assert (installed / "logs" / "run.log").read_bytes() == b"keep-log"
    assert not (installed / "backup").exists()


def test_process_termination_only_targets_current_installation(tmp_path):
    installed = tmp_path / "Aura"
    targets = [
        installed / "AuraResonanceGui.exe",
        installed / "runtime" / "AuraResonanceRuntime.exe",
        installed / "runtime" / "aura.exe",
    ]
    records = [(100 + index, str(path)) for index, path in enumerate(targets)]
    records.append((999, str(tmp_path / "Other" / "AuraResonanceGui.exe")))
    terminated: list[int] = []

    result = terminate_installed_processes(
        installed,
        process_provider=lambda: records,
        terminator=terminated.append,
    )

    assert result == [100, 101, 102]
    assert terminated == [100, 101, 102]


def test_perform_update_downloads_verifies_installs_and_cleans_work_files(tmp_path):
    installed = tmp_path / "Aura"
    package_root = tmp_path / "package" / "AuraResonance-v2.0.0-win-x64-cpu"
    _make_staged_release(package_root)
    package_bytes = _zip_tree(package_root)
    package_name = "AuraResonance-v2.0.0-win-x64-cpu.zip"
    checksum_bytes = f"{hashlib.sha256(package_bytes).hexdigest()}  {package_name}\n".encode()
    release_payload = json.dumps(
        {
            "tag_name": "v2.0.0",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": package_name,
                    "browser_download_url": f"https://github.com/example/releases/{package_name}",
                },
                {
                    "name": "SHA256SUMS.txt",
                    "browser_download_url": "https://github.com/example/releases/SHA256SUMS.txt",
                },
            ],
        }
    ).encode()
    responses = {
        "https://api.github.com/repos/nightofknife/Aura_script_lsns/releases/latest": release_payload,
        f"https://github.com/example/releases/{package_name}": package_bytes,
        "https://github.com/example/releases/SHA256SUMS.txt": checksum_bytes,
    }

    installed.mkdir()
    (installed / "BUILD-INFO.json").write_text(
        json.dumps({"release_label": "v1.0.0", "profile": "cpu"}),
        encoding="utf-8",
    )
    _write_file(installed / "更新.exe", b"fixed-updater")
    _write_file(installed / "gui-settings.ini", b"settings")
    _write_file(installed / "logs" / "run.log", b"log")

    def opener(request, **_kwargs):
        return _ByteResponse(responses[request.full_url])

    with patch("updater.aura_updater.terminate_installed_processes", return_value=[]):
        assert perform_update(installed, opener=opener) is True

    info = json.loads((installed / "BUILD-INFO.json").read_text(encoding="utf-8"))
    assert info["release_label"] == "v2.0.0"
    assert (installed / "更新.exe").read_bytes() == b"fixed-updater"
    assert (installed / "gui-settings.ini").read_bytes() == b"settings"
    assert (installed / "logs" / "run.log").read_bytes() == b"log"
    assert not (installed / "updates").exists()
    assert not (installed / ".update-work").exists()


def test_updater_failure_only_directs_user_to_manual_download(capsys):
    with patch("updater.aura_updater.perform_update", side_effect=UpdateError("details")):
        assert main([]) == 1

    output = capsys.readouterr().out
    assert FAILURE_MESSAGE in output
    assert "details" not in output
    assert "回滚" not in output
