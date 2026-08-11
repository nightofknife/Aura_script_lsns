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
    LATEST_CHECKSUMS_URL,
    MAX_CHECKSUM_BYTES,
    RELEASES_URL,
    UpdateError,
    _write_console_line,
    extract_release_archive,
    fetch_latest_release,
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


def _release_manifest(
    *,
    cpu_tag: str | None = "v2.0.0",
    gpu_tag: str | None = "v2.0.0",
    overlay_tag: str | None = "v2.0.0",
) -> bytes:
    entries: list[str] = []
    if cpu_tag is not None:
        entries.append(f"{'a' * 64}  AuraResonance-{cpu_tag}-win-x64-cpu.zip")
    if gpu_tag is not None:
        entries.append(f"{'b' * 64}  AuraResonance-{gpu_tag}-win-x64-gpu.zip")
    if overlay_tag is not None:
        entries.append(f"{'c' * 64}  AuraResonance-{overlay_tag}-nvidia-cu13-overlay.zip")
    return ("\n".join(entries) + "\n").encode()


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


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (_release_manifest(cpu_tag=None), "缺少正式版资产"),
        (_release_manifest(gpu_tag=None), "缺少正式版资产"),
        (_release_manifest(overlay_tag=None), "缺少正式版资产"),
        (
            _release_manifest(overlay_tag="v2.0.1"),
            "缺少正式版资产",
        ),
        (
            _release_manifest(gpu_tag="v2.0.1"),
            "唯一确定最新正式版版本号",
        ),
        (
            _release_manifest() + _release_manifest().splitlines(keepends=True)[0],
            "duplicate",
        ),
        (b"x" * (MAX_CHECKSUM_BYTES + 1), "校验文件过大"),
    ],
    ids=[
        "missing-cpu",
        "missing-gpu",
        "missing-overlay",
        "mixed-overlay-version",
        "mixed-cpu-gpu-version",
        "duplicate-asset",
        "oversized-manifest",
    ],
)
def test_fetch_latest_release_rejects_invalid_manifests(contents, message):
    with pytest.raises(UpdateError, match=message):
        fetch_latest_release(opener=lambda *_args, **_kwargs: _ByteResponse(contents))


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
    checksum_bytes = (
        f"{hashlib.sha256(package_bytes).hexdigest()}  {package_name}\n"
        f"{'b' * 64}  AuraResonance-v2.0.0-win-x64-gpu.zip\n"
        f"{'c' * 64}  AuraResonance-v2.0.0-nvidia-cu13-overlay.zip\n"
    ).encode()
    package_url = f"{RELEASES_URL}/download/v2.0.0/{package_name}"
    responses = {
        LATEST_CHECKSUMS_URL: checksum_bytes,
        package_url: package_bytes,
    }

    installed.mkdir()
    (installed / "BUILD-INFO.json").write_text(
        json.dumps({"release_label": "v1.0.0", "profile": "cpu"}),
        encoding="utf-8",
    )
    _write_file(installed / "更新.exe", b"fixed-updater")
    _write_file(installed / "gui-settings.ini", b"settings")
    _write_file(installed / "logs" / "run.log", b"log")

    requested_urls: list[str] = []

    def opener(request, **_kwargs):
        requested_urls.append(request.full_url)
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
    assert requested_urls == [LATEST_CHECKSUMS_URL, package_url]
    assert all("api.github.com" not in url for url in requested_urls)


def test_updater_failure_prints_specific_reason_and_manual_download_fallback(capsys):
    with patch("updater.aura_updater.perform_update", side_effect=UpdateError("details")):
        assert main([]) == 1

    output = capsys.readouterr().out
    assert FAILURE_MESSAGE in output
    assert "UpdateError: details" in output
    assert "回滚" not in output


class _InteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_updater_failure_waits_for_enter_in_interactive_console(monkeypatch, capsys):
    input_stream = _InteractiveInput("\n")
    monkeypatch.setattr("updater.aura_updater.sys.stdin", input_stream)

    with patch("updater.aura_updater.perform_update", side_effect=OSError("disk locked")):
        assert main([]) == 1

    output = capsys.readouterr().out
    assert "OSError: disk locked" in output
    assert "按回车键关闭窗口" in output
    assert input_stream.tell() == 1


def test_self_check_failure_never_waits_for_enter(monkeypatch, capsys):
    input_stream = _InteractiveInput("\n")
    monkeypatch.setattr("updater.aura_updater.sys.stdin", input_stream)

    with patch("updater.aura_updater.self_check", side_effect=UpdateError("broken build")):
        assert main(["--self-check"]) == 1

    output = capsys.readouterr().out
    assert "UpdateError: broken build" in output
    assert "按回车键关闭窗口" not in output
    assert input_stream.tell() == 0


def test_console_output_falls_back_to_ascii_when_code_page_rejects_chinese(monkeypatch):
    output = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr("updater.aura_updater.sys.stdout", output)

    _write_console_line("更新器自检通过。", "Aura updater self-check passed.")

    output.seek(0)
    assert output.read() == "Aura updater self-check passed.\n"
