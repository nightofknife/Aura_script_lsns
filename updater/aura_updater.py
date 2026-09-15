from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import logging
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import subprocess
import time
import uuid
from typing import Any, Callable, Iterable
import urllib.parse
import urllib.request
import urllib.error
import zipfile


RELEASES_URL = "https://github.com/nightofknife/Aura_script_lsns/releases"
LATEST_CHECKSUMS_URL = f"{RELEASES_URL}/latest/download/SHA256SUMS.txt"
FAILURE_MESSAGE = "更新失败，请前往 GitHub Releases 手动下载最新版本。"
VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
SHA256_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
RELEASE_ASSET_RE = re.compile(
    r"^AuraResonance-(v\d+\.\d+\.\d+)-win-x64-cpu\.zip$",
    re.IGNORECASE,
)
MAX_CHECKSUM_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200_000
MAX_EXTRACTED_BYTES = 16 * 1024 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
LOG = logging.getLogger("AuraUpdater")

MANAGED_PATHS = (
    "runtime",
    "plans",
    "models",
    "config.yaml",
    "AuraResonanceGui.exe",
    "更新.exe",
    "run.ps1",
    "README.md",
    "LICENSE",
    "BUILD-INFO.json",
    "BUILD-INFO.txt",
)
REQUIRED_RELEASE_PATHS = (
    "runtime/aura.exe",
    "runtime/AuraResonanceRuntime.exe",
    "plans",
    "models",
    "config.yaml",
    "AuraResonanceGui.exe",
    "更新.exe",
    "run.ps1",
    "README.md",
    "LICENSE",
    "BUILD-INFO.json",
    "BUILD-INFO.txt",
)
MANAGED_PROCESS_PATHS = (
    "AuraResonanceGui.exe",
    "runtime/AuraResonanceRuntime.exe",
    "runtime/aura.exe",
)


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str


@dataclass(frozen=True)
class LatestRelease:
    tag: str
    assets: tuple[ReleaseAsset, ...]
    checksums: dict[str, str]


def parse_version(value: Any) -> tuple[int, int, int] | None:
    match = VERSION_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _request(url: str, *, accept: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "AuraResonanceUpdater/1",
        },
    )


def _open_url(
    request: urllib.request.Request,
    *,
    opener: Callable[..., Any] | None,
    timeout_sec: float,
):
    open_url = opener or urllib.request.urlopen
    for attempt in range(3):
        try:
            return open_url(request, timeout=max(float(timeout_sec), 0.1))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 2 or (isinstance(exc, urllib.error.HTTPError) and exc.code < 500):
                raise
            LOG.warning("phase=network_retry attempt=%s reason=%s", attempt + 1, exc)
            time.sleep(0.5 * (attempt + 1))


def fetch_latest_release(
    *,
    opener: Callable[..., Any] | None = None,
    timeout_sec: float = 15.0,
) -> LatestRelease:
    request = _request(LATEST_CHECKSUMS_URL, accept="text/plain")
    with _open_url(request, opener=opener, timeout_sec=timeout_sec) as response:
        raw = response.read(MAX_CHECKSUM_BYTES + 1)
    if len(raw) > MAX_CHECKSUM_BYTES:
        raise UpdateError("最新版本校验文件过大")

    checksums = parse_checksums(raw)
    tags = {
        match.group(1)
        for filename in checksums
        if (match := RELEASE_ASSET_RE.fullmatch(filename)) is not None
    }
    if len(tags) != 1:
        raise UpdateError("无法从 SHA256SUMS.txt 唯一确定最新正式版版本号")
    tag = next(iter(tags))
    if parse_version(tag) is None:
        raise UpdateError("SHA256SUMS.txt 中的最新版本号无效")

    asset_names = (f"AuraResonance-{tag}-win-x64-cpu.zip",)
    missing = [name for name in asset_names if name.casefold() not in checksums]
    if missing:
        raise UpdateError("SHA256SUMS.txt 缺少正式版资产：" + ", ".join(missing))

    encoded_tag = urllib.parse.quote(tag, safe="")
    assets = tuple(
        ReleaseAsset(
            name=name,
            url=f"{RELEASES_URL}/download/{encoded_tag}/{urllib.parse.quote(name, safe='')}",
        )
        for name in asset_names
    )
    return LatestRelease(
        tag=tag,
        assets=assets,
        checksums=checksums,
    )


def select_asset(release: LatestRelease, suffix: str, *, exact: bool = False) -> ReleaseAsset:
    expected = suffix.casefold()
    matches = [
        asset
        for asset in release.assets
        if (asset.name.casefold() == expected if exact else asset.name.casefold().endswith(expected))
    ]
    if len(matches) != 1:
        raise UpdateError(f"Expected exactly one release asset ending with {suffix!r}")
    return matches[0]


def download_asset(
    asset: ReleaseAsset,
    destination: Path,
    *,
    opener: Callable[..., Any] | None = None,
    timeout_sec: float = 60.0,
    max_bytes: int | None = None,
) -> str:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    if partial.exists():
        partial.unlink()

    digest = hashlib.sha256()
    size = 0
    request = _request(asset.url, accept="application/octet-stream")
    try:
        with _open_url(request, opener=opener, timeout_sec=timeout_sec) as response:
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise UpdateError(f"Release asset is too large: {asset.name}")
                    digest.update(chunk)
                    handle.write(chunk)
        os.replace(partial, destination)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    return digest.hexdigest()


def parse_checksums(contents: bytes) -> dict[str, str]:
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UpdateError("SHA256SUMS.txt is not valid UTF-8") from exc

    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = SHA256_RE.fullmatch(line)
        if match is None:
            raise UpdateError("SHA256SUMS.txt contains an invalid line")
        filename = match.group(2).strip()
        if filename != Path(filename).name:
            raise UpdateError("SHA256SUMS.txt contains an invalid filename")
        key = filename.casefold()
        if key in result:
            raise UpdateError("SHA256SUMS.txt contains a duplicate filename")
        result[key] = match.group(1).lower()
    return result


def expected_checksum(checksums: dict[str, str], asset: ReleaseAsset) -> str:
    digest = checksums.get(asset.name.casefold())
    if digest is None:
        raise UpdateError(f"SHA256SUMS.txt does not contain {asset.name}")
    return digest


def _safe_zip_parts(filename: str) -> tuple[str, ...]:
    normalized = filename.replace("\\", "/")
    if not normalized or normalized.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", normalized):
        raise UpdateError(f"Unsafe ZIP entry: {filename!r}")
    pure = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
        raise UpdateError(f"Unsafe ZIP entry: {filename!r}")
    return tuple(pure.parts)


def _validate_zip_member(info: zipfile.ZipInfo) -> tuple[str, ...]:
    parts = _safe_zip_parts(info.filename)
    unix_mode = info.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise UpdateError(f"Unsupported ZIP entry type: {info.filename!r}")
    return parts


def extract_release_archive(
    archive_path: Path,
    destination: Path,
    *,
    expected_top_level: str | None = None,
) -> Path:
    archive_path = archive_path.resolve()
    destination = destination.resolve()
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    seen: set[str] = set()
    members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    top_levels: set[str] = set()
    total_size = 0
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
            raise UpdateError("Release ZIP has an invalid number of entries")
        for info in infos:
            parts = _validate_zip_member(info)
            key = "/".join(parts).rstrip("/").casefold()
            if key in seen:
                raise UpdateError(f"Release ZIP contains a duplicate entry: {info.filename!r}")
            seen.add(key)
            top_levels.add(parts[0])
            total_size += int(info.file_size)
            if total_size > MAX_EXTRACTED_BYTES:
                raise UpdateError("Release ZIP expands beyond the supported size")
            members.append((info, parts))

        if len(top_levels) != 1:
            raise UpdateError("Release ZIP must contain exactly one top-level directory")
        top_level = next(iter(top_levels))
        if expected_top_level is not None and top_level.casefold() != expected_top_level.casefold():
            raise UpdateError("Release ZIP top-level directory does not match its asset name")

        for info, parts in members:
            target = destination.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)

    extracted_root = destination / top_level
    if not extracted_root.is_dir():
        raise UpdateError("Release ZIP top-level entry is not a directory")
    return extracted_root


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Invalid JSON file: {path.name}") from exc
    if not isinstance(payload, dict):
        raise UpdateError(f"Invalid JSON object: {path.name}")
    return payload


def load_installed_release(root: Path) -> tuple[str, str]:
    info = _load_json(root / "BUILD-INFO.json")
    tag = str(info.get("release_label") or "").strip()
    profile = str(info.get("profile") or "").strip().lower()
    if parse_version(tag) is None or profile != "cpu":
        raise UpdateError("更新器仅支持带有正式 vX.X.X 版本号的 CPU 完整包")
    return tag, profile


def validate_staged_release(root: Path, *, tag: str, profile: str) -> None:
    info = _load_json(root / "BUILD-INFO.json")
    if (
        parse_version(info.get("release_label")) != parse_version(tag)
        or str(info.get("profile") or "").strip().lower() != profile
    ):
        raise UpdateError("Downloaded release does not match the selected tag and profile")
    for relative in REQUIRED_RELEASE_PATHS:
        path = root.joinpath(*PurePosixPath(relative).parts)
        valid = path.is_dir() if relative in {"plans", "models"} else path.is_file()
        if not valid:
            raise UpdateError(f"Downloaded release is missing {relative}")


def _normalize_windows_path(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(Path(path).resolve())))


def _confined_child(root: Path, *parts: str) -> Path:
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*parts)
    for parent in (candidate, *candidate.parents):
        if parent == resolved_root:
            break
        if parent.is_symlink() or parent.is_junction():
            raise UpdateError(f"更新路径不能穿过链接：{parent}")
    candidate = candidate.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise UpdateError("Updater work path is outside the installation directory") from exc
    return candidate


def managed_process_targets(root: Path) -> set[str]:
    return {
        _normalize_windows_path(root.joinpath(*PurePosixPath(relative).parts))
        for relative in MANAGED_PROCESS_PATHS
    }


def _iter_windows_process_paths() -> Iterable[tuple[int, str]]:
    if os.name != "nt":
        return ()

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise UpdateError("Could not enumerate running processes")
    records: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while has_entry:
            pid = int(entry.th32ProcessID)
            if pid and pid != os.getpid():
                process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if process:
                    try:
                        size = wintypes.DWORD(32768)
                        buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                            records.append((pid, buffer.value))
                    finally:
                        kernel32.CloseHandle(process)
            has_entry = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return records


def _terminate_windows_pid(pid: int, expected_path: str | None = None) -> None:
    if os.name != "nt":
        return
    PROCESS_TERMINATE = 0x0001
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    process = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE | 0x1000, False, int(pid))
    if not process:
        if ctypes.get_last_error() == 87:  # Process already exited.
            return
        raise UpdateError("Could not open an Aura process for termination")
    try:
        size = wintypes.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
            raise UpdateError(f"无法确认进程 {pid} 的程序路径")
        if expected_path is not None and _normalize_windows_path(image.value) != expected_path:
            return  # PID was reused by another program.
        if not kernel32.TerminateProcess(process, 1):
            raise UpdateError("Could not terminate an Aura process")
        if kernel32.WaitForSingleObject(process, 10_000) != 0:
            raise UpdateError(f"Aura 进程 {pid} 未在限定时间内退出")
    finally:
        kernel32.CloseHandle(process)


def terminate_installed_processes(
    root: Path,
    *,
    process_provider: Callable[[], Iterable[tuple[int, str]]] | None = None,
    terminator: Callable[[int], None] | None = None,
) -> list[int]:
    targets = managed_process_targets(root)
    provide = process_provider or _iter_windows_process_paths
    terminated: list[int] = []
    for pid, image_path in provide():
        if int(pid) == os.getpid():
            continue
        if _normalize_windows_path(image_path) in targets:
            if terminator is None:
                _terminate_windows_pid(int(pid), _normalize_windows_path(image_path))
            else:
                terminator(int(pid))
            LOG.info("phase=process_stopped pid=%s path=%s", pid, image_path)
            terminated.append(int(pid))
    if process_provider is None:
        remaining = [(pid, path) for pid, path in _iter_windows_process_paths()
                     if _normalize_windows_path(path) in targets]
        if remaining:
            raise UpdateError(f"安装目录仍有 Aura 进程运行：{remaining}")
    return terminated


def _remove_managed_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _journal_write(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _tree_hash(path: Path) -> dict[str, str]:
    if not path.exists():
        raise UpdateError(f"更新文件缺失：{path}")
    if path.is_symlink() or path.is_junction():
        raise UpdateError(f"更新路径不能是链接：{path}")
    files = [path] if path.is_file() else sorted(path.rglob("*"))
    result = {}
    for item in files:
        if item.is_symlink() or item.is_junction():
            raise UpdateError(f"更新路径不能包含链接：{item}")
        if item.is_file():
            with item.open("rb") as stream:
                result[item.relative_to(path).as_posix() if item != path else "."] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def _rollback(install_root: Path, journal: Path, state: dict) -> None:
    state["status"] = "rolling_back"
    _journal_write(journal, state)
    errors = []
    for entry in reversed(state["entries"]):
        if entry["state"] in {"pending", "restored"}:
            continue
        name = entry["name"]
        target = _confined_child(install_root, name)
        backup = _confined_child(journal.parent, "backup", name)
        try:
            if backup.exists():
                entry["state"] = "restoring"
                _journal_write(journal, state)
                if not (backup.is_file() and target.is_file()):
                    _remove_managed_target(target)
                os.replace(backup, target)
            elif entry["had_old"]:
                # A rename is atomic: either the backup exists, or the original
                # never moved / was already restored before the last journal write.
                if entry["state"] not in {"backing_up", "restoring"} or not target.exists():
                    raise UpdateError(f"缺少恢复备份：{name}")
            else:
                _remove_managed_target(target)
            entry["state"] = "restored"
            _journal_write(journal, state)
            LOG.info("phase=restored path=%s", name)
        except Exception as exc:
            LOG.exception("phase=rollback_failed path=%s", name)
            errors.append(f"{name}: {exc}")
    state["status"] = "rollback_failed" if errors else "rolled_back"
    state["rollback_errors"] = errors
    _journal_write(journal, state)
    if errors:
        raise UpdateError(f"回滚未完成，备份保留在 {journal.parent}：" + "; ".join(errors))


def recover_pending_updates(root: Path) -> bool:
    recovered = False
    transactions = _confined_child(root, ".update-work", "transactions")
    for journal in sorted(transactions.glob("*/transaction.json")):
        state = _load_json(journal)
        if state.get("schema") != 1 or state.get("install_root") != str(root.resolve()):
            raise UpdateError(f"无法识别更新记录：{journal}")
        entries = state.get("entries", [])
        if [entry.get("name") for entry in entries] != list(MANAGED_PATHS):
            raise UpdateError(f"更新记录的文件范围无效：{journal}")
        if state.get("status") in {"installing", "rolling_back", "rollback_failed"}:
            LOG.warning("phase=recover_interrupted transaction=%s", journal.parent.name)
            _rollback(root, journal, state)
            recovered = True
    return recovered


def install_staged_release(staged_root: Path, install_root: Path) -> None:
    staged_root, install_root = staged_root.resolve(), install_root.resolve()
    hashes = {}
    entries = []
    for name in MANAGED_PATHS:
        source = _confined_child(staged_root, name)
        target = _confined_child(install_root, name)
        if not source.exists():
            raise UpdateError(f"Downloaded release is missing managed path {name}")
        hashes[name] = _tree_hash(source)
        entries.append({"name": name, "had_old": target.exists(), "state": "pending"})
    transaction = _confined_child(install_root, ".update-work", "transactions", uuid.uuid4().hex)
    (transaction / "backup").mkdir(parents=True)
    journal = transaction / "transaction.json"
    state = {"schema": 1, "install_root": str(install_root), "status": "installing", "entries": entries}
    _journal_write(journal, state)
    try:
        for entry in entries:
            name = entry["name"]
            target = _confined_child(install_root, name)
            backup = _confined_child(transaction, "backup", name)
            entry["state"] = "backing_up"
            _journal_write(journal, state)
            if entry["had_old"]:
                if name == "更新.exe":
                    # Keep a runnable updater at the root even if power is lost
                    # between creating its backup and installing the new EXE.
                    temporary_backup = backup.with_suffix(".tmp")
                    with target.open("rb") as src, temporary_backup.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
                    os.replace(temporary_backup, backup)
                else:
                    os.replace(target, backup)
            entry["state"] = "installing"
            _journal_write(journal, state)
            os.replace(_confined_child(staged_root, name), target)
            entry["state"] = "installed"
            _journal_write(journal, state)
            LOG.info("phase=installed path=%s", name)
        for name, expected in hashes.items():
            if _tree_hash(install_root / name) != expected:
                raise UpdateError(f"安装后文件校验失败：{name}")
        state["status"] = "completed"
        _journal_write(journal, state)
        LOG.info("phase=completed backup=%s", transaction / "backup")
    except Exception as exc:
        LOG.exception("phase=install_failed")
        try:
            _rollback(install_root, journal, state)
        except Exception as rollback_error:
            raise UpdateError(f"安装失败：{exc}；{rollback_error}") from exc
        raise UpdateError(f"安装失败，已恢复旧版本：{exc}") from exc
    # Retain this successful backup; older successful versions can now go.
    for old_journal in transaction.parent.glob("*/transaction.json"):
        if old_journal == journal:
            continue
        try:
            if _load_json(old_journal).get("status") == "completed":
                old_root = _confined_child(transaction.parent, old_journal.parent.name)
                shutil.rmtree(old_root)
        except Exception:
            LOG.warning("phase=old_backup_cleanup_deferred path=%s", old_journal.parent)


def _download_and_verify(
    asset: ReleaseAsset,
    destination: Path,
    checksums: dict[str, str],
    *,
    opener: Callable[..., Any] | None,
) -> Path:
    LOG.info("phase=download asset=%s", asset.name)
    actual = download_asset(asset, destination, opener=opener, max_bytes=MAX_DOWNLOAD_BYTES)
    if actual.lower() != expected_checksum(checksums, asset):
        destination.unlink(missing_ok=True)
        raise UpdateError(f"SHA-256 verification failed for {asset.name}")
    return destination


@contextmanager
def update_lock(root: Path):
    """An OS-owned lock survives stale files but is released on process exit."""
    import msvcrt
    lock_path = _confined_child(root, ".update-work", "update.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise UpdateError("此安装目录已有更新正在进行，请等待其完成") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def perform_update(
    root: Path,
    *,
    opener: Callable[..., Any] | None = None,
) -> bool:
    root = root.resolve()
    with update_lock(root):
        _cleanup_idle_helpers(root)
        transactions = _confined_child(root, ".update-work", "transactions")
        pending = any(_load_json(p).get("status") in {"installing", "rolling_back", "rollback_failed"}
                      for p in transactions.glob("*/transaction.json"))
        if pending:
            terminate_installed_processes(root)
        if recover_pending_updates(root):
            raise UpdateError("已恢复上次中断的更新，请重新运行更新器检查新版")
        return _perform_update_locked(root, opener=opener)


def _perform_update_locked(root: Path, *, opener: Callable[..., Any] | None) -> bool:
    current_tag, profile = load_installed_release(root)
    LOG.info("phase=check_version installed=%s profile=%s", current_tag, profile)
    release = fetch_latest_release(opener=opener)
    current_version = parse_version(current_tag)
    latest_version = parse_version(release.tag)
    if latest_version is None or current_version is None:
        raise UpdateError("Release version is invalid")
    if latest_version <= current_version:
        LOG.info("phase=already_current latest=%s", release.tag)
        return False

    work_root = _confined_child(root, ".update-work", "downloads", uuid.uuid4().hex)
    download_root = work_root
    checksums = release.checksums

    main_asset = select_asset(release, f"-win-x64-{profile}.zip")
    main_archive = _download_and_verify(
        main_asset,
        download_root / main_asset.name,
        checksums,
        opener=opener,
    )
    expected_main_root = main_asset.name[:-4]
    staged_root = extract_release_archive(
        main_archive,
        work_root / "staging",
        expected_top_level=expected_main_root,
    )
    validate_staged_release(staged_root, tag=release.tag, profile=profile)
    LOG.info("phase=staged tag=%s", release.tag)
    terminate_installed_processes(root)
    install_staged_release(staged_root, root)
    shutil.rmtree(work_root, ignore_errors=True)
    return True


def self_check() -> None:
    if parse_version("v1.2.3") != (1, 2, 3):
        raise UpdateError("Version parser self-check failed")
    if "更新.exe" not in MANAGED_PATHS or any(p in MANAGED_PATHS for p in ("gui-settings.ini", "logs", "user-data")):
        raise UpdateError("Portable-data preservation self-check failed")


def _launch_helper(root: Path) -> None:
    # Start a standalone copy before downloading or modifying anything. Only
    # the helper acquires the install lock, so there is no lock handoff gap.
    directory = _confined_child(root, ".update-work", "helpers", uuid.uuid4().hex)
    directory.mkdir(parents=True)
    helper = directory / "AuraUpdateHelper.exe"
    shutil.copy2(sys.executable, helper)
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(directory)
    subprocess.Popen([str(helper), "--apply-root", str(root)], cwd=root, env=env,
                     creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)


def _cleanup_idle_helpers(root: Path) -> None:
    helpers = _confined_child(root, ".update-work", "helpers")
    active = {_normalize_windows_path(path) for _, path in _iter_windows_process_paths()}
    active.add(_normalize_windows_path(sys.executable))
    for directory in helpers.glob("*"):
        try:
            directory = _confined_child(helpers, directory.name)
            if not directory.is_dir():
                continue
            # A newly launched starter may still be copying its helper before
            # it appears in the process list. Never race that handoff.
            if time.time() - directory.stat().st_mtime < 86400:
                continue
            if _normalize_windows_path(directory / "AuraUpdateHelper.exe") not in active:
                shutil.rmtree(directory)
        except Exception:
            LOG.warning("phase=helper_cleanup_deferred path=%s", directory)


def _wait_for_updater_exit(root: Path) -> None:
    # A onefile app has a bootloader parent as well as the Python process.
    # Wait for all processes using the original executable, not just one PID.
    original = _normalize_windows_path(root / "更新.exe")
    deadline = time.monotonic() + 30
    while any(_normalize_windows_path(path) == original for _, path in _iter_windows_process_paths()):
        if time.monotonic() >= deadline:
            raise UpdateError("原更新器仍在运行，请关闭其他更新窗口后重试")
        time.sleep(0.2)


def _setup_log(root: Path) -> Path:
    path = _confined_child(root, "logs", "updater", f"update-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    return path


def _write_console_line(message: str, ascii_fallback: str) -> None:
    try:
        sys.stdout.write(message + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(ascii_fallback + "\n")
    sys.stdout.flush()


def _describe_error(error: BaseException) -> tuple[str, str]:
    details = str(error).strip() or "没有提供错误详情"
    name = type(error).__name__
    message = f"失败原因：{name}: {details}"
    ascii_details = details.encode("ascii", "backslashreplace").decode("ascii")
    return message, f"Failure reason: {name}: {ascii_details}"


def _wait_for_enter_after_failure() -> None:
    try:
        interactive = bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, OSError):
        interactive = False
    if not interactive:
        return
    _write_console_line("按回车键关闭窗口。", "Press Enter to close this window.")
    try:
        sys.stdin.readline()
    except (EOFError, OSError):
        return


def _report_failure(error: BaseException, *, wait_for_enter: bool) -> None:
    message, fallback = _describe_error(error)
    _write_console_line(message, fallback)
    _write_console_line(
        FAILURE_MESSAGE,
        "Update failed. Please download the latest release from GitHub Releases.",
    )
    if wait_for_enter:
        _wait_for_enter_after_failure()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aura 独立更新器")
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--apply-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_check:
        try:
            self_check()
        except Exception as exc:
            _report_failure(exc, wait_for_enter=False)
            return 1
        _write_console_line("更新器自检通过。", "Aura updater self-check passed.")
        return 0

    root = args.apply_root.resolve() if args.apply_root else application_root()
    log_path = None
    try:
        if getattr(sys, "frozen", False) and args.apply_root is None:
            _launch_helper(root)
            return 0
        log_path = _setup_log(root)
        if args.apply_root:
            _wait_for_updater_exit(root)
        _write_console_line("正在检查最新正式版……", "Checking the latest formal release...")
        updated = perform_update(root)
        if updated:
            _write_console_line("更新完成。", "Update completed.")
        else:
            _write_console_line("当前已是最新正式版。", "The installed release is already current.")
        if args.apply_root:
            _wait_for_enter_after_failure()
        return 0
    except Exception as exc:
        LOG.exception("phase=failed")
        if log_path:
            _write_console_line(f"更新日志：{log_path}", f"Update log: {log_path}")
        _report_failure(exc, wait_for_enter=True)
        return 1
    finally:
        for handler in list(LOG.handlers):
            handler.close()
            LOG.removeHandler(handler)


if __name__ == "__main__":
    raise SystemExit(main())
