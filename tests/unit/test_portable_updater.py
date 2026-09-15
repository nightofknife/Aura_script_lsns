"""Portable updater tests use synthetic releases and never touch live processes."""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "portable_updater_under_test", Path(__file__).resolve().parents[2] / "updater/aura_updater.py"
)
updater = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = updater
_SPEC.loader.exec_module(updater)


def make_release(root: Path, tag: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in updater.MANAGED_PATHS:
        path = root / name
        if name in {"runtime", "plans", "models"}:
            path.mkdir()
            (path / "payload.txt").write_text(tag, encoding="utf-8")
        else:
            path.write_text(f"{tag}:{name}", encoding="utf-8")
    (root / "runtime/aura.exe").write_bytes(tag.encode())
    (root / "runtime/AuraResonanceRuntime.exe").write_bytes(tag.encode())
    (root / "BUILD-INFO.json").write_text(
        json.dumps({"release_label": tag, "profile": "cpu"}), encoding="utf-8"
    )
    return root


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for name in updater.MANAGED_PATHS
        for path in ([root / name] if (root / name).is_file() else (root / name).rglob("*"))
        if path.is_file()
    }


def release_server(staged: Path, *, bad_hash=False, traversal=False):
    tag = json.loads((staged / "BUILD-INFO.json").read_text())["release_label"]
    stem = f"AuraResonance-{tag}-win-x64-cpu"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in staged.rglob("*"):
            if path.is_file():
                archive.writestr(f"{stem}/{path.relative_to(staged).as_posix()}", path.read_bytes())
        if traversal:
            archive.writestr(f"{stem}/../../escaped.txt", b"escape")
    payload = buffer.getvalue()
    checksum = "0" * 64 if bad_hash else hashlib.sha256(payload).hexdigest()
    calls = []

    def opener(request, **kwargs):
        calls.append(request.full_url)
        if request.full_url == updater.LATEST_CHECKSUMS_URL:
            return io.BytesIO(f"{checksum}  {stem}.zip\n".encode())
        assert request.full_url == f"{updater.RELEASES_URL}/download/{tag}/{stem}.zip"
        return io.BytesIO(payload)

    return opener, calls


@pytest.fixture(autouse=True)
def block_live_processes(monkeypatch):
    monkeypatch.setattr(updater, "terminate_installed_processes", lambda root: [])


def test_cpu_only_release_uses_version_pinned_asset(tmp_path):
    opener, _ = release_server(make_release(tmp_path / "source", "v1.9.10"))
    release = updater.fetch_latest_release(opener=opener)
    assert release.tag == "v1.9.10"
    assert len(release.assets) == 1
    assert release.assets[0].name.endswith("-cpu.zip")
    assert "/download/v1.9.10/" in release.assets[0].url


def test_current_release_does_not_download_or_replace(tmp_path):
    root = make_release(tmp_path / "installed", "v1.9.10")
    before = snapshot(root)
    opener, calls = release_server(make_release(tmp_path / "source", "v1.9.10"))
    assert updater.perform_update(root, opener=opener) is False
    assert calls == [updater.LATEST_CHECKSUMS_URL]
    assert snapshot(root) == before


def test_cpu_update_replaces_updater_and_preserves_user_files(tmp_path):
    root = make_release(tmp_path / "installed", "v1.9.9")
    staged = make_release(tmp_path / "source", "v1.9.10")
    expected = snapshot(staged)
    retained = ["user-data/player.json", "gui-settings.ini", "logs/session.log", "custom.txt"]
    for name in retained:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"user-content")
    (root / "plans/obsolete.py").write_text("old")
    opener, _ = release_server(staged)
    assert updater.perform_update(root, opener=opener) is True
    assert snapshot(root) == expected
    assert (root / "更新.exe").read_text(encoding="utf-8") == "v1.9.10:更新.exe"
    for name in retained:
        assert (root / name).read_bytes() == b"user-content"


@pytest.mark.parametrize("failure", ["hash", "traversal"])
def test_invalid_download_leaves_old_installation_untouched(tmp_path, monkeypatch, failure):
    root = make_release(tmp_path / "installed", "v1.9.9")
    before = snapshot(root)
    opener, _ = release_server(
        make_release(tmp_path / "source", "v1.9.10"),
        bad_hash=failure == "hash", traversal=failure == "traversal",
    )
    monkeypatch.setattr(updater, "terminate_installed_processes", lambda root: pytest.fail("preflight stopped app"))
    with pytest.raises(updater.UpdateError):
        updater.perform_update(root, opener=opener)
    assert snapshot(root) == before
    assert not (root / "escaped.txt").exists()


def test_replace_failure_rolls_back_all_managed_paths(tmp_path, monkeypatch):
    root = make_release(tmp_path / "installed", "v1.9.9")
    staged = make_release(tmp_path / "source", "v1.9.10")
    before = snapshot(root)
    original_replace = os.replace
    failed = False

    def fail_once(source, target):
        nonlocal failed
        if Path(source) == staged / "plans" and not failed:
            failed = True
            raise PermissionError("injected replacement failure")
        return original_replace(source, target)

    monkeypatch.setattr(updater.os, "replace", fail_once)
    with pytest.raises(updater.UpdateError):
        updater.install_staged_release(staged, root)
    assert failed
    assert snapshot(root) == before
    assert updater.recover_pending_updates(root) is False


@pytest.mark.skipif(os.name != "nt", reason="requires Windows sharing semantics")
def test_windows_locked_file_rolls_back_earlier_replacements(tmp_path):
    from ctypes import wintypes

    root = make_release(tmp_path / "installed", "v1.9.9")
    staged = make_release(tmp_path / "source", "v1.9.10")
    before = snapshot(root)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    # FILE_SHARE_READ only: real handle denies delete/rename, but permits assertion reads.
    handle = kernel32.CreateFileW(str(root / "config.yaml"), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(updater.UpdateError):
            updater.install_staged_release(staged, root)
        assert snapshot(root) == before
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.parametrize("crash_point", ["before_backup", "after_backup", "after_install", "during_restore"])
def test_interrupted_transaction_recovers_idempotently(tmp_path, monkeypatch, crash_point):
    class PowerLoss(BaseException):
        pass

    root = make_release(tmp_path / "installed", "v1.9.9")
    staged = make_release(tmp_path / "source", "v1.9.10")
    before = snapshot(root)
    original_replace = os.replace
    crashed = False

    def interrupt(source, target):
        nonlocal crashed
        source, target = Path(source), Path(target)
        if not crashed and crash_point == "before_backup" and source == root / "plans":
            crashed = True
            raise PowerLoss()
        if crash_point == "during_restore" and source == staged / "plans":
            raise OSError("trigger rollback")
        result = original_replace(source, target)
        hit = (
            crash_point == "after_backup" and source == root / "plans"
            or crash_point == "after_install" and source == staged / "plans"
            or crash_point == "during_restore" and source.parent.name == "backup" and target == root / "plans"
        )
        if hit and not crashed:
            crashed = True
            raise PowerLoss()
        return result

    monkeypatch.setattr(updater.os, "replace", interrupt)
    with pytest.raises(PowerLoss):
        updater.install_staged_release(staged, root)
    assert crashed
    monkeypatch.setattr(updater.os, "replace", original_replace)
    assert updater.recover_pending_updates(root) is True
    assert snapshot(root) == before
    assert updater.recover_pending_updates(root) is False
    assert snapshot(root) == before


def test_missing_managed_path_fails_before_any_replacement(tmp_path):
    root = make_release(tmp_path / "installed", "v1.9.9")
    staged = make_release(tmp_path / "source", "v1.9.10")
    (staged / "更新.exe").unlink()
    before = snapshot(root)
    with pytest.raises(updater.UpdateError):
        updater.install_staged_release(staged, root)
    assert snapshot(root) == before


@pytest.mark.skipif(os.name != "nt", reason="requires Windows updater lock")
def test_installation_lock_rejects_concurrency_and_releases_after_error(tmp_path):
    root = tmp_path / "installed"
    with pytest.raises(RuntimeError, match="owner failed"):
        with updater.update_lock(root):
            with pytest.raises(updater.UpdateError):
                with updater.update_lock(root):
                    pytest.fail("second updater acquired lock")
            raise RuntimeError("owner failed")
    with updater.update_lock(root):
        pass


def test_pending_update_is_recovered_before_any_network_request(tmp_path, monkeypatch):
    class PowerLoss(BaseException):
        pass

    root = make_release(tmp_path / "installed", "v1.9.9")
    staged = make_release(tmp_path / "source", "v1.9.10")
    before = snapshot(root)
    original_replace = os.replace

    def interrupt(source, target):
        result = original_replace(source, target)
        if Path(source) == staged / "runtime":
            raise PowerLoss()
        return result

    monkeypatch.setattr(updater.os, "replace", interrupt)
    with pytest.raises(PowerLoss):
        updater.install_staged_release(staged, root)
    monkeypatch.setattr(updater.os, "replace", original_replace)

    def disallow_network(*args, **kwargs):
        pytest.fail("download began before recovery was acknowledged")

    with pytest.raises(updater.UpdateError, match="已恢复"):
        updater.perform_update(root, opener=disallow_network)
    assert snapshot(root) == before
    assert updater.recover_pending_updates(root) is False


def test_failed_rollback_retains_backup_for_later_recovery(tmp_path, monkeypatch):
    root = make_release(tmp_path / "installed", "v1.9.9")
    staged = make_release(tmp_path / "source", "v1.9.10")
    before = snapshot(root)
    original_replace = os.replace

    def fail_install_and_restore(source, target):
        source, target = Path(source), Path(target)
        if source == staged / "plans":
            raise OSError("install failed")
        if source.parent.name == "backup" and target == root / "runtime":
            raise PermissionError("restore temporarily blocked")
        return original_replace(source, target)

    monkeypatch.setattr(updater.os, "replace", fail_install_and_restore)
    with pytest.raises(updater.UpdateError, match="回滚未完成"):
        updater.install_staged_release(staged, root)
    journals = list((root / ".update-work/transactions").glob("*/transaction.json"))
    assert len(journals) == 1
    state = json.loads(journals[0].read_text(encoding="utf-8"))
    assert state["status"] == "rollback_failed"
    assert (journals[0].parent / "backup/runtime/payload.txt").read_text() == "v1.9.9"
    monkeypatch.setattr(updater.os, "replace", original_replace)
    assert updater.recover_pending_updates(root) is True
    assert snapshot(root) == before


@pytest.mark.parametrize("crash_point", ["after_updater_backup", "after_updater_install"])
def test_updater_remains_present_across_interruption_and_recovery(tmp_path, monkeypatch, crash_point):
    class PowerLoss(BaseException):
        pass

    root = make_release(tmp_path / "installed", "v1.9.9")
    staged = make_release(tmp_path / "source", "v1.9.10")
    before = snapshot(root)
    original_replace = os.replace

    def interrupt(source, target):
        source, target = Path(source), Path(target)
        # The root updater must remain available before and after every rename.
        assert (root / "更新.exe").is_file()
        result = original_replace(source, target)
        assert (root / "更新.exe").is_file()
        if (
            crash_point == "after_updater_backup" and target.name == "更新.exe" and target.parent.name == "backup"
            or crash_point == "after_updater_install" and source == staged / "更新.exe"
        ):
            raise PowerLoss()
        return result

    monkeypatch.setattr(updater.os, "replace", interrupt)
    with pytest.raises(PowerLoss):
        updater.install_staged_release(staged, root)
    assert (root / "更新.exe").is_file()
    expected_tag = "v1.9.9" if crash_point == "after_updater_backup" else "v1.9.10"
    assert (root / "更新.exe").read_text(encoding="utf-8") == f"{expected_tag}:更新.exe"

    def observe_recovery(source, target):
        assert (root / "更新.exe").is_file()
        result = original_replace(source, target)
        assert (root / "更新.exe").is_file()
        return result

    monkeypatch.setattr(updater.os, "replace", observe_recovery)
    assert updater.recover_pending_updates(root) is True
    assert snapshot(root) == before
    assert updater.recover_pending_updates(root) is False
