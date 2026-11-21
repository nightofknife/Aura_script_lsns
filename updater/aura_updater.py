from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import logging
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import sys
import subprocess
import time
import uuid
from typing import Any, Callable, Iterable, Literal
import urllib.parse
import urllib.request
import urllib.error
import zipfile


from packages.aura_core.release_manifest import (
    EXPERIMENTAL_INDEX_URL,
    LATEST_CHECKSUMS_URL,
    MAX_CHECKSUM_BYTES,
    RELEASES_URL,
    parse_cpu_manifest,
    release_version_key,
)
from packages.aura_core.update_mirror import (
    MIRROR_CA_FILE_ENV, MIRROR_URL_ENV, NETWORK_ERRORS, MirrorClient, MirrorError, MirrorRelease,
)


UpdateChannel = Literal["stable", "experimental"]
CHANNEL_LABELS = {"stable": "正式版", "experimental": "测试版"}
FAILURE_MESSAGE = "更新失败，请前往 GitHub Releases 手动下载最新版本。"
MIRROR_ENDPOINT_RESOURCE = "aura-update-endpoint.dat"
MIRROR_CA_RESOURCE = "aura-update-ca.pem"
MAX_INDEX_BYTES = 4096
MAX_ARCHIVE_ENTRIES = 200_000
MAX_EXTRACTED_BYTES = 16 * 1024 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
MIRROR_DOWNLOAD_ATTEMPTS = 3
WINDOWS_DIRECTORY_PATH_LIMIT = 248
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


class AssetVerificationError(UpdateError):
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
    key = release_version_key(value)
    return key[:3] if key is not None else None


def _filesystem_path(path: Path, *, recursive: bool = False) -> Path:
    if os.name != "nt":
        return path
    path = Path(os.path.abspath(path))
    value = str(path)
    if value.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    # Directory creation has a lower limit than file I/O. For tree operations,
    # use the extended form even if only the descendants need it.
    path_units = len(value.encode("utf-16-le", errors="surrogatepass")) // 2
    if not recursive and path_units < WINDOWS_DIRECTORY_PATH_LIMIT:
        return path
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def _plain_path(path: Path) -> Path:
    value = str(path)
    if value[:8].upper() == "\\\\?\\UNC\\":
        return Path("\\\\" + value[8:])
    if value.startswith("\\\\?\\") and value[5:7] == ":\\":
        return Path(value[4:])
    return path


def _resolve_path(path: Path) -> Path:
    # Keep process paths and the install_root recorded by older updaters comparable.
    return _plain_path(_filesystem_path(path, recursive=True).resolve())


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return _resolve_path(Path(sys.executable)).parent
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


def _fetch_experimental_tag(
    *, opener: Callable[..., Any] | None, timeout_sec: float,
) -> str:
    request = _request(EXPERIMENTAL_INDEX_URL, accept="application/json")
    try:
        with _open_url(request, opener=opener, timeout_sec=timeout_sec) as response:
            contents = response.read(MAX_INDEX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("暂时没有可用的测试版。可以重新运行更新器选择正式版。") from exc
        raise

    if len(contents) > MAX_INDEX_BYTES:
        raise UpdateError("测试版索引过大")
    try:
        index = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("测试版索引无效") from exc
    tag = index.get("tag") if isinstance(index, dict) else None
    version = release_version_key(tag)
    if (
        not isinstance(tag, str)
        or tag != tag.strip()
        or version is None
        or version.stable
    ):
        raise UpdateError("测试版索引缺少有效的 vX.X.X-E.编号 标签")
    return tag


def fetch_latest_release(
    *,
    channel: UpdateChannel = "stable",
    opener: Callable[..., Any] | None = None,
    timeout_sec: float = 15.0,
    pinned_tag: str | None = None,
) -> LatestRelease:
    expected_tag = pinned_tag
    checksums_url = LATEST_CHECKSUMS_URL
    if expected_tag is None and channel == "experimental":
        expected_tag = _fetch_experimental_tag(opener=opener, timeout_sec=timeout_sec)
    if expected_tag is not None:
        encoded_tag = urllib.parse.quote(expected_tag, safe="")
        checksums_url = f"{RELEASES_URL}/download/{encoded_tag}/SHA256SUMS.txt"

    request = _request(checksums_url, accept="text/plain")
    with _open_url(request, opener=opener, timeout_sec=timeout_sec) as response:
        raw = response.read(MAX_CHECKSUM_BYTES + 1)
    if len(raw) > MAX_CHECKSUM_BYTES:
        raise UpdateError("最新版本校验文件过大")

    try:
        tag, checksums = parse_cpu_manifest(raw)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc
    if expected_tag is not None and tag != expected_tag:
        raise UpdateError(f"预期版本为 {expected_tag}，但校验清单列出的是 {tag}")
    version = release_version_key(tag)
    if channel == "stable" and (version is None or not version.stable):
        raise UpdateError("正式版通道返回了测试版清单")

    name = f"AuraResonance-{tag}-win-x64-cpu.zip"
    encoded_tag = urllib.parse.quote(tag, safe="")
    asset = ReleaseAsset(
        name=name,
        url=f"{RELEASES_URL}/download/{encoded_tag}/{urllib.parse.quote(name, safe='')}",
    )
    return LatestRelease(
        tag=tag,
        assets=(asset,),
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
    progress: Callable[[int], None] | None = None,
) -> str:
    destination = _filesystem_path(_resolve_path(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = _filesystem_path(destination.with_name(destination.name + ".part"))
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
                        raise AssetVerificationError(f"Release asset is too large: {asset.name}")
                    digest.update(chunk)
                    handle.write(chunk)
                    if progress is not None:
                        progress(size)
        os.replace(partial, destination)
    except (Exception, KeyboardInterrupt):
        partial.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


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
    if not pure.parts:
        raise UpdateError(f"Unsafe ZIP entry: {filename!r}")
    for part in pure.parts:
        # Extended paths bypass Win32 name normalization. Require ordinary
        # Windows names so extraction and later use agree on each filename.
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or re.search(r'[<>:"|?*\x00-\x1f]', part)
            or PureWindowsPath(part).is_reserved()
        ):
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
    """Extract the validated wrapper's contents directly into destination."""
    archive_path = _filesystem_path(_resolve_path(archive_path))
    destination = _resolve_path(destination)
    staging = _filesystem_path(destination, recursive=True)

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
        if any(len(parts) == 1 and not info.is_dir() for info, parts in members):
            raise UpdateError("Release ZIP top-level entry is not a directory")

        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        for info, parts in members:
            # The archive wrapper is validated above. Stage its contents directly.
            if len(parts) == 1:
                continue
            target = staging.joinpath(*parts[1:])
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)

    return destination


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_filesystem_path(path).read_text(encoding="utf-8-sig"))
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
        raise UpdateError("更新器支持 vX.X.X 或 vX.X.X-E.编号 版本的 CPU 完整包")
    return tag, profile


def validate_staged_release(root: Path, *, tag: str, profile: str) -> None:
    info = _load_json(root / "BUILD-INFO.json")
    if (
        str(info.get("release_label") or "").strip() != tag
        or str(info.get("profile") or "").strip().lower() != profile
    ):
        raise UpdateError("Downloaded release does not match the selected tag and profile")
    for relative in REQUIRED_RELEASE_PATHS:
        path = _filesystem_path(root.joinpath(*PurePosixPath(relative).parts))
        valid = path.is_dir() if relative in {"plans", "models"} else path.is_file()
        if not valid:
            raise UpdateError(f"Downloaded release is missing {relative}")


def _normalize_windows_path(path: Path | str) -> str:
    return os.path.normcase(str(_resolve_path(Path(path))))


def _confined_child(root: Path, *parts: str) -> Path:
    resolved_root = _resolve_path(root)
    candidate = resolved_root.joinpath(*parts)
    for parent in (candidate, *candidate.parents):
        if parent == resolved_root:
            break
        filesystem_parent = _filesystem_path(parent)
        if filesystem_parent.is_symlink() or filesystem_parent.is_junction():
            raise UpdateError(f"更新路径不能穿过链接：{parent}")
    candidate = _resolve_path(candidate)
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
    path = _filesystem_path(path, recursive=True)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _journal_write(path: Path, state: dict) -> None:
    path = _filesystem_path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    # Retry only this atomic journal replacement, never the update transaction.
    # Windows watchers can briefly deny delete sharing on the existing journal.
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            break
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 4:
                raise
            time.sleep(0.05)


def _tree_hash(path: Path) -> dict[str, str]:
    path = _filesystem_path(path, recursive=True)
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
        target = _filesystem_path(_confined_child(install_root, name))
        backup = _filesystem_path(_confined_child(journal.parent, "backup", name))
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
    root = _resolve_path(root)
    transactions = _filesystem_path(_confined_child(root, ".update-work", "transactions"), recursive=True)
    for journal in sorted(transactions.glob("*/transaction.json")):
        state = _load_json(journal)
        recorded_root = state.get("install_root")
        if (
            state.get("schema") != 1
            or not isinstance(recorded_root, str)
            or _plain_path(Path(recorded_root)) != root
        ):
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
    staged_root, install_root = _resolve_path(staged_root), _resolve_path(install_root)
    hashes = {}
    entries = []
    for name in MANAGED_PATHS:
        source = _filesystem_path(_confined_child(staged_root, name))
        target = _filesystem_path(_confined_child(install_root, name))
        if not source.exists():
            raise UpdateError(f"Downloaded release is missing managed path {name}")
        hashes[name] = _tree_hash(source)
        entries.append({"name": name, "had_old": target.exists(), "state": "pending"})
    transaction = _confined_child(install_root, ".update-work", "transactions", uuid.uuid4().hex)
    _filesystem_path(transaction / "backup").mkdir(parents=True)
    journal = transaction / "transaction.json"
    state = {"schema": 1, "install_root": str(install_root), "status": "installing", "entries": entries}
    _journal_write(journal, state)
    try:
        for entry in entries:
            name = entry["name"]
            target = _filesystem_path(_confined_child(install_root, name))
            backup = _filesystem_path(_confined_child(transaction, "backup", name))
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
            source = _filesystem_path(_confined_child(staged_root, name))
            os.replace(source, target)
            entry["state"] = "installed"
            _journal_write(journal, state)
            LOG.info("phase=installed path=%s", name)
        for name, expected in hashes.items():
            if _tree_hash(install_root / name) != expected:
                raise UpdateError(f"安装后文件校验失败：{name}")
        state["status"] = "completed"
        _journal_write(journal, state)
        LOG.info("phase=completed backup=%s", transaction / "backup")
    except (Exception, KeyboardInterrupt) as exc:
        LOG.exception("phase=install_failed")
        try:
            _rollback(install_root, journal, state)
        except Exception as rollback_error:
            reason = "安装已中断" if isinstance(exc, KeyboardInterrupt) else f"安装失败：{exc}"
            raise UpdateError(f"{reason}；{rollback_error}") from exc
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise UpdateError(f"安装失败，已恢复旧版本：{exc}") from exc
    # Retain this successful backup; older successful versions can now go.
    for old_journal in _filesystem_path(transaction.parent, recursive=True).glob("*/transaction.json"):
        if old_journal.parent.name == transaction.name:
            continue
        try:
            if _load_json(old_journal).get("status") == "completed":
                old_root = _confined_child(transaction.parent, old_journal.parent.name)
                shutil.rmtree(_filesystem_path(old_root, recursive=True))
        except Exception:
            LOG.warning("phase=old_backup_cleanup_deferred path=%s", old_journal.parent)


def _download_and_verify(
    asset: ReleaseAsset,
    destination: Path,
    checksums: dict[str, str],
    *,
    opener: Callable[..., Any] | None,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    progress: Callable[[int], None] | None = None,
) -> Path:
    LOG.info("phase=download asset=%s", asset.name)
    actual = download_asset(asset, destination, opener=opener, max_bytes=max_bytes, progress=progress)
    if actual.lower() != expected_checksum(checksums, asset):
        _filesystem_path(destination).unlink(missing_ok=True)
        raise AssetVerificationError(f"SHA-256 verification failed for {asset.name}")
    return destination


def _mirror_partial_path(root: Path, channel: UpdateChannel, release: MirrorRelease) -> Path:
    partial = _filesystem_path(_confined_child(
        root, ".update-work", "mirror-downloads", channel, f"{release.tag}-{release.sha256}.zip.part",
    ))
    partial.parent.mkdir(parents=True, exist_ok=True)
    for old_partial in partial.parent.glob("*.zip.part"):
        if old_partial != partial:
            try:
                old_partial.unlink(missing_ok=True)
            except OSError:
                LOG.warning("phase=mirror_partial_cleanup_deferred path=%s", old_partial)
    return partial


def _download_from_mirror(
    client: MirrorClient, release: MirrorRelease, destination: Path, partial: Path,
) -> Path:
    def waiting(ahead: int | None) -> None:
        if ahead is None:
            _write_console_line("VPS 下载队列已满，正在等待入队。", "The VPS queue is full. Waiting to join.")
        else:
            _write_console_line(f"下载排队中，前面还有 {ahead} 位。按 Ctrl+C 取消。",
                                f"Waiting to download. {ahead} ahead. Press Ctrl+C to cancel.")

    LOG.info("phase=download asset=%s", release.filename)
    for attempt in range(MIRROR_DOWNLOAD_ATTEMPTS):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > release.size:
            partial.unlink()
            offset = 0
        if offset == release.size:
            break
        downloaded = offset
        try:
            with client.stream(release, offset=offset, on_wait=waiting, timeout=60) as (response, start):
                downloaded = start
                displayed = start * 100 // release.size
                if start:
                    _write_console_line(f"正在从 VPS 续传 {release.tag}（{displayed}%）……",
                                        f"Resuming {release.tag} from the VPS at {displayed}%...")
                else:
                    _write_console_line(f"正在从 VPS 下载 {release.tag}……",
                                        f"Downloading {release.tag} from the VPS...")
                with partial.open("ab" if start else "wb") as handle:
                    while downloaded < release.size:
                        chunk = response.read(min(1024 * 1024, release.size - downloaded))
                        if not chunk:
                            raise ConnectionError(f"VPS download ended at {downloaded}/{release.size} bytes")
                        downloaded += handle.write(chunk)
                        percent = downloaded * 100 // release.size
                        if percent >= displayed + 10 or percent == 100:
                            displayed = percent
                            _write_console_line(f"已下载 {percent}%", f"Downloaded {percent}%")
        except NETWORK_ERRORS as exc:
            LOG.warning("phase=mirror_download_interrupted tag=%s attempt=%s received=%s size=%s reason=%s",
                        release.tag, attempt + 1, downloaded, release.size, type(exc).__name__)
            if attempt + 1 == MIRROR_DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(0.5 * (attempt + 1))
        else:
            break

    with partial.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != release.sha256:
        partial.unlink()
        raise AssetVerificationError(f"SHA-256 verification failed for {release.filename}")
    filesystem_destination = _filesystem_path(destination)
    filesystem_destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial, filesystem_destination)
    return destination


def _report_mirror_fallback(error: Exception) -> None:
    LOG.warning("phase=mirror_fallback reason=%s", str(error) if isinstance(error, MirrorError) else type(error).__name__)
    _write_console_line("VPS 更新服务暂时不可用，正在切换到 GitHub。",
                        "The VPS update service is unavailable. Switching to GitHub.")


def _configured_mirror_url() -> str:
    override = os.environ.get(MIRROR_URL_ENV, "").strip()
    bundle = getattr(sys, "_MEIPASS", None)
    if override or bundle is None:
        return override
    try:
        with (Path(bundle) / MIRROR_ENDPOINT_RESOURCE).open("rb") as resource:
            encoded = resource.read(4097)
        if len(encoded) > 4096:
            raise ValueError("Invalid endpoint resource size")
        return base64.b64decode(encoded, validate=True).decode("utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        raise MirrorError("更新器内置的 VPS 地址无法读取") from None


def _configured_mirror_ca() -> Path | None:
    override = os.environ.get(MIRROR_CA_FILE_ENV, "").strip()
    if override:
        return Path(override)
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is None:
        return None
    ca_file = Path(bundle) / MIRROR_CA_RESOURCE
    return ca_file if ca_file.is_file() else None


@contextmanager
def update_lock(root: Path):
    """An OS-owned lock survives stale files but is released on process exit."""
    import msvcrt
    lock_path = _filesystem_path(_confined_child(root, ".update-work", "update.lock"))
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
    channel: UpdateChannel = "stable",
    opener: Callable[..., Any] | None = None,
) -> bool:
    root = _resolve_path(root)
    with update_lock(root):
        _cleanup_idle_helpers(root)
        transactions = _filesystem_path(_confined_child(root, ".update-work", "transactions"), recursive=True)
        pending = any(_load_json(p).get("status") in {"installing", "rolling_back", "rollback_failed"}
                      for p in transactions.glob("*/transaction.json"))
        if pending:
            terminate_installed_processes(root)
        if recover_pending_updates(root):
            raise UpdateError("已恢复上次中断的更新，请重新运行更新器检查新版")
        return _perform_update_locked(root, channel=channel, opener=opener)


def _perform_update_locked(
    root: Path, *, channel: UpdateChannel, opener: Callable[..., Any] | None,
) -> bool:
    current_tag, profile = load_installed_release(root)
    LOG.info("phase=check_version installed=%s profile=%s channel=%s", current_tag, profile, channel)
    mirror, mirrored_release, release = None, None, None
    try:
        mirror_url = _configured_mirror_url()
        if mirror_url:
            mirror = MirrorClient(mirror_url, opener=opener, ca_file=_configured_mirror_ca())
            mirrored_release = mirror.latest(channel)
    except MirrorError as exc:
        _report_mirror_fallback(exc)
    if mirrored_release is None:
        release = fetch_latest_release(channel=channel, opener=opener)
    selected_tag = mirrored_release.tag if mirrored_release is not None else release.tag
    current_version = release_version_key(current_tag)
    latest_version = release_version_key(selected_tag)
    if latest_version is None or current_version is None:
        raise UpdateError("Release version is invalid")
    same_base_channel_switch = (
        channel == "experimental"
        and current_version.stable
        and not latest_version.stable
        and latest_version[:3] == current_version[:3]
    )
    if latest_version <= current_version and not same_base_channel_switch:
        LOG.info("phase=already_current latest=%s", selected_tag)
        return False

    work_root = _confined_child(root, ".update-work", "downloads", uuid.uuid4().hex)
    main_archive = None
    partial = None
    if mirrored_release is not None:
        assert mirror is not None
        partial = _mirror_partial_path(root, channel, mirrored_release)
        try:
            main_archive = _download_from_mirror(
                mirror, mirrored_release, work_root / mirrored_release.filename, partial,
            )
        except (MirrorError, AssetVerificationError, *NETWORK_ERRORS) as exc:
            _report_mirror_fallback(exc)
            release = fetch_latest_release(channel=channel, opener=opener, pinned_tag=selected_tag)
    if main_archive is None:
        assert release is not None
        main_asset = select_asset(release, f"-win-x64-{profile}.zip")
        main_archive = _download_and_verify(main_asset, work_root / main_asset.name, release.checksums, opener=opener)
    staged_root = extract_release_archive(
        main_archive,
        work_root / "staging",
        expected_top_level=main_archive.stem,
    )
    validate_staged_release(staged_root, tag=selected_tag, profile=profile)
    LOG.info("phase=staged tag=%s", selected_tag)
    terminate_installed_processes(root)
    install_staged_release(staged_root, root)
    if partial is not None:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            LOG.warning("phase=mirror_partial_cleanup_deferred path=%s", partial)
    shutil.rmtree(_filesystem_path(work_root, recursive=True), ignore_errors=True)
    return True


def self_check() -> None:
    if parse_version("v1.2.3") != (1, 2, 3):
        raise UpdateError("Version parser self-check failed")
    if "更新.exe" not in MANAGED_PATHS or any(
        p in MANAGED_PATHS
        for p in ("gui-settings.ini", "gui-settings-layout-v2.ini", "logs", "user-data")
    ):
        raise UpdateError("Portable-data preservation self-check failed")


def _launch_helper(root: Path, *, channel: UpdateChannel = "stable") -> None:
    # Start a standalone copy before downloading or modifying anything. Only
    # the helper acquires the install lock, so there is no lock handoff gap.
    directory = _confined_child(root, ".update-work", "helpers", uuid.uuid4().hex)
    _filesystem_path(directory).mkdir(parents=True)
    helper = directory / "AuraUpdateHelper.exe"
    shutil.copy2(_filesystem_path(Path(sys.executable)), _filesystem_path(helper))
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(directory)
    from packages.aura_core.observability.logging.coordinator import (
        close_session, make_resume_descriptor, session_environment, start_session,
    )
    from packages.aura_core.observability.logging.stream_capture import install_capture, uninstall_capture
    LOG.info("phase=helper_staged channel=%s path=%s", channel, helper)
    descriptor = make_resume_descriptor()
    env.update(session_environment(descriptor))
    # Transfer file ownership before spawning. The original executable must
    # exit so the staged helper can replace it; it cannot remain a collector.
    uninstall_capture()
    close_session()
    try:
        subprocess.Popen([str(helper), "--apply-root", str(root), "--channel", channel], cwd=root, env=env,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), close_fds=True)
    except Exception:
        start_session(descriptor["log_root"], descriptor=descriptor, cleanup=False, role="updater")
        install_capture(native_stderr=True, capture_stdout=False)
        raise


def _cleanup_idle_helpers(root: Path) -> None:
    helpers = _confined_child(root, ".update-work", "helpers")
    active = {_normalize_windows_path(path) for _, path in _iter_windows_process_paths()}
    active.add(_normalize_windows_path(sys.executable))
    for directory in _filesystem_path(helpers, recursive=True).glob("*"):
        try:
            directory = _filesystem_path(_confined_child(helpers, directory.name), recursive=True)
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


def _setup_log(root: Path, *, resume: bool = False) -> Path:
    from packages.aura_core.config.loader import get_config_value
    from packages.aura_core.observability.logging.coordinator import get_current_log_path, start_session
    from packages.aura_core.observability.logging.log_session import resolve_log_root
    from packages.aura_core.observability.logging.stream_capture import install_capture
    log_root = resolve_log_root(root, os.environ.get("AURA_LOG_ROOT") or get_config_value(
        "logging.log_dir", "logs", base_path=str(root),
    ))
    start_session(log_root, cleanup=not resume,
                  descriptor=None if resume else {}, role="updater")
    install_capture(native_stderr=True, capture_stdout=False)
    LOG.setLevel(logging.INFO)
    return Path(get_current_log_path())


def _write_console_line(message: str, ascii_fallback: str) -> None:
    LOG.info("[UpdaterStatus] %s", message)
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


def choose_update_channel() -> UpdateChannel | None:
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.withdraw()
    window.title("Aura 更新")
    window.resizable(False, False)
    channel = tk.StringVar(master=window, value="stable")
    selection: UpdateChannel | None = None

    def confirm(_event: object = None) -> None:
        nonlocal selection
        selection = "experimental" if channel.get() == "experimental" else "stable"
        window.destroy()

    content = ttk.Frame(window, padding=20)
    content.grid(sticky="nsew")
    ttk.Label(content, text="选择更新通道").grid(sticky="w", pady=(0, 12))
    ttk.Radiobutton(content, text="正式版（推荐）", variable=channel, value="stable").grid(sticky="w")
    ttk.Radiobutton(content, text="测试版（-E.编号）", variable=channel, value="experimental").grid(
        sticky="w", pady=(8, 0),
    )
    ttk.Label(content, text="测试版可提前体验新功能，可能不稳定。").grid(sticky="w", pady=(6, 16))

    buttons = ttk.Frame(content)
    buttons.grid(sticky="e")
    cancel_button = ttk.Button(buttons, text="取消", command=window.destroy)
    cancel_button.grid(row=0, column=0, padx=(0, 8))
    cancel_button.bind("<Return>", lambda _event: window.destroy())
    continue_button = ttk.Button(buttons, text="继续", command=confirm)
    continue_button.grid(row=0, column=1)
    continue_button.bind("<Return>", confirm)
    window.bind("<Escape>", lambda _event: window.destroy())

    window.update_idletasks()
    x = max(0, (window.winfo_screenwidth() - window.winfo_reqwidth()) // 2)
    y = max(0, (window.winfo_screenheight() - window.winfo_reqheight()) // 2)
    window.geometry(f"+{x}+{y}")
    window.deiconify()
    continue_button.focus_set()
    window.mainloop()
    return selection


def main(
    argv: list[str] | None = None,
    *, channel_picker: Callable[[], UpdateChannel | None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Aura 独立更新器")
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--apply-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--channel", choices=CHANNEL_LABELS, help="指定更新通道并跳过选择窗口")
    args = parser.parse_args(argv)
    if args.self_check:
        check_logging = bool(str(os.environ.get("AURA_LOG_ROOT") or "").strip())
        try:
            if check_logging:
                from packages.aura_core.observability.logging.coordinator import flush
                log_path = _setup_log(application_root())
            self_check()
            if check_logging:
                marker = f"[Updater] phase=self_check pid={os.getpid()}"
                LOG.info(marker)
                if not flush() or marker not in log_path.read_text(encoding="utf-8"):
                    raise UpdateError("Unified updater log self-check failed")
            _write_console_line("更新器自检通过。", "Aura updater self-check passed.")
            return 0
        except Exception as exc:
            if check_logging:
                LOG.exception("phase=self_check_failed")
            _report_failure(exc, wait_for_enter=False)
            return 1
        finally:
            if check_logging:
                from packages.aura_core.observability.logging.coordinator import close_session
                from packages.aura_core.observability.logging.stream_capture import uninstall_capture
                uninstall_capture()
                close_session()

    root = _resolve_path(args.apply_root) if args.apply_root else application_root()
    log_path = None
    try:
        channel = args.channel or "stable"
        if args.channel is None and args.apply_root is None and channel_picker is not None:
            channel = channel_picker()
            if channel is None:
                return 0
        log_path = _setup_log(root, resume=bool(args.apply_root))
        if getattr(sys, "frozen", False) and args.apply_root is None:
            _launch_helper(root, channel=channel)
            return 0
        if args.apply_root:
            _wait_for_updater_exit(root)
        _write_console_line(f"正在检查最新{CHANNEL_LABELS[channel]}……", f"Checking the {channel} channel...")
        updated = perform_update(root, channel=channel)
        if updated:
            _write_console_line("更新完成。", "Update completed.")
        else:
            _write_console_line("所选通道没有比当前版本更新的版本。", "No newer release on the selected channel.")
        if args.apply_root:
            _wait_for_enter_after_failure()
        return 0
    except KeyboardInterrupt:
        _write_console_line("已取消更新。", "Update cancelled.")
        return 130
    except Exception as exc:
        LOG.exception("phase=failed")
        if log_path:
            _write_console_line(f"更新日志：{log_path}", f"Update log: {log_path}")
        _report_failure(exc, wait_for_enter=True)
        return 1
    finally:
        from packages.aura_core.observability.logging.coordinator import close_session
        from packages.aura_core.observability.logging.stream_capture import uninstall_capture
        uninstall_capture()
        close_session()


if __name__ == "__main__":
    raise SystemExit(main(channel_picker=choose_update_channel))
