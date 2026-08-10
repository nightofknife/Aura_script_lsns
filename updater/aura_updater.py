from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
from typing import Any, Callable, Iterable
import urllib.parse
import urllib.request
import zipfile


LATEST_RELEASE_API = "https://api.github.com/repos/nightofknife/Aura_script_lsns/releases/latest"
FAILURE_MESSAGE = "更新失败，请前往 GitHub Releases 手动下载最新版本。"
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
SHA256_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
MAX_CHECKSUM_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200_000
MAX_EXTRACTED_BYTES = 16 * 1024 * 1024 * 1024

MANAGED_PATHS = (
    "runtime",
    "plans",
    "models",
    "config.yaml",
    "AuraResonanceGui.exe",
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
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _open_url(
    request: urllib.request.Request,
    *,
    opener: Callable[..., Any] | None,
    timeout_sec: float,
):
    open_url = opener or urllib.request.urlopen
    return open_url(request, timeout=max(float(timeout_sec), 0.1))


def fetch_latest_release(
    *,
    opener: Callable[..., Any] | None = None,
    timeout_sec: float = 15.0,
) -> LatestRelease:
    request = _request(LATEST_RELEASE_API, accept="application/vnd.github+json")
    with _open_url(request, opener=opener, timeout_sec=timeout_sec) as response:
        raw = response.read(MAX_CHECKSUM_BYTES + 1)
    if len(raw) > MAX_CHECKSUM_BYTES:
        raise UpdateError("GitHub release metadata is too large")
    payload = json.loads(raw.decode("utf-8"))
    if bool(payload.get("draft")) or bool(payload.get("prerelease")):
        raise UpdateError("Latest release is not a formal release")

    tag = str(payload.get("tag_name") or "").strip()
    if parse_version(tag) is None:
        raise UpdateError("Latest release tag is invalid")

    assets: list[ReleaseAsset] = []
    for item in payload.get("assets") or []:
        name = str(item.get("name") or "").strip()
        url = str(item.get("browser_download_url") or "").strip()
        parsed_url = urllib.parse.urlparse(url)
        if (
            not name
            or name != Path(name).name
            or parsed_url.scheme.lower() != "https"
            or parsed_url.hostname not in {"github.com", "www.github.com"}
        ):
            continue
        assets.append(ReleaseAsset(name=name, url=url))
    if not assets:
        raise UpdateError("Latest release has no downloadable assets")
    return LatestRelease(tag=tag, assets=tuple(assets))


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
    if parse_version(tag) is None or profile not in {"cpu", "gpu"}:
        raise UpdateError("Installed BUILD-INFO.json is invalid")
    return tag, profile


def validate_staged_release(root: Path, *, tag: str, profile: str) -> None:
    info = _load_json(root / "BUILD-INFO.json")
    if (
        parse_version(info.get("release_label")) != parse_version(tag)
        or str(info.get("profile") or "").strip().lower() != profile
    ):
        raise UpdateError("Downloaded release does not match the selected tag and profile")
    for relative in REQUIRED_RELEASE_PATHS:
        if not root.joinpath(*PurePosixPath(relative).parts).exists():
            raise UpdateError(f"Downloaded release is missing {relative}")


def validate_staged_overlay(root: Path, *, tag: str) -> Path:
    nvidia_root = root / "runtime" / "_internal" / "nvidia"
    info = _load_json(nvidia_root / "AURA-OVERLAY-INFO.json")
    if (
        parse_version(info.get("release_label")) != parse_version(tag)
        or str(info.get("profile") or "").strip().lower() != "overlay"
        or str(info.get("target_profile") or "").strip().lower() != "gpu"
    ):
        raise UpdateError("Downloaded NVIDIA overlay does not match the selected release")
    return nvidia_root


def merge_staged_overlay(release_root: Path, overlay_root: Path, *, tag: str) -> None:
    source = validate_staged_overlay(overlay_root, tag=tag)
    destination = release_root / "runtime" / "_internal" / "nvidia"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _normalize_windows_path(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(Path(path).resolve())))


def _confined_child(root: Path, *parts: str) -> Path:
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*parts).resolve()
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


def _terminate_windows_pid(pid: int) -> None:
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

    process = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, int(pid))
    if not process:
        raise UpdateError("Could not open an Aura process for termination")
    try:
        if not kernel32.TerminateProcess(process, 1):
            raise UpdateError("Could not terminate an Aura process")
        kernel32.WaitForSingleObject(process, 10_000)
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
    terminate = terminator or _terminate_windows_pid
    terminated: list[int] = []
    for pid, image_path in provide():
        if int(pid) == os.getpid():
            continue
        if _normalize_windows_path(image_path) in targets:
            terminate(int(pid))
            terminated.append(int(pid))
    return terminated


def _remove_managed_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def install_staged_release(staged_root: Path, install_root: Path) -> None:
    staged_root = staged_root.resolve()
    install_root = install_root.resolve()
    for relative in MANAGED_PATHS:
        parts = PurePosixPath(relative).parts
        source = staged_root.joinpath(*parts)
        target = install_root.joinpath(*parts)
        if not source.exists():
            raise UpdateError(f"Downloaded release is missing managed path {relative}")
        _remove_managed_target(target)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _download_and_verify(
    asset: ReleaseAsset,
    destination: Path,
    checksums: dict[str, str],
    *,
    opener: Callable[..., Any] | None,
) -> Path:
    actual = download_asset(asset, destination, opener=opener)
    if actual.lower() != expected_checksum(checksums, asset):
        destination.unlink(missing_ok=True)
        raise UpdateError(f"SHA-256 verification failed for {asset.name}")
    return destination


def perform_update(
    root: Path,
    *,
    opener: Callable[..., Any] | None = None,
) -> bool:
    root = root.resolve()
    current_tag, profile = load_installed_release(root)
    release = fetch_latest_release(opener=opener)
    current_version = parse_version(current_tag)
    latest_version = parse_version(release.tag)
    if latest_version is None or current_version is None:
        raise UpdateError("Release version is invalid")
    if latest_version <= current_version:
        return False

    download_root = _confined_child(root, "updates", release.tag)
    work_root = _confined_child(root, ".update-work")
    checksums_asset = select_asset(release, "SHA256SUMS.txt", exact=True)
    checksums_path = download_root / checksums_asset.name
    download_asset(
        checksums_asset,
        checksums_path,
        opener=opener,
        max_bytes=MAX_CHECKSUM_BYTES,
    )
    checksums = parse_checksums(checksums_path.read_bytes())

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
        work_root / "staging" / "main",
        expected_top_level=expected_main_root,
    )
    validate_staged_release(staged_root, tag=release.tag, profile=profile)

    installed_overlay = root / "runtime" / "_internal" / "nvidia" / "AURA-OVERLAY-INFO.json"
    if profile == "gpu" and installed_overlay.is_file():
        overlay_asset = select_asset(release, "-nvidia-cu13-overlay.zip")
        overlay_archive = _download_and_verify(
            overlay_asset,
            download_root / overlay_asset.name,
            checksums,
            opener=opener,
        )
        overlay_root = extract_release_archive(
            overlay_archive,
            work_root / "staging" / "overlay",
            expected_top_level=expected_main_root,
        )
        merge_staged_overlay(staged_root, overlay_root, tag=release.tag)

    terminate_installed_processes(root)
    install_staged_release(staged_root, root)
    shutil.rmtree(work_root, ignore_errors=True)
    shutil.rmtree(download_root, ignore_errors=True)
    shutil.rmtree(_confined_child(root, "updates"), ignore_errors=True)
    return True


def self_check() -> None:
    if parse_version("v1.2.3") != (1, 2, 3):
        raise UpdateError("Version parser self-check failed")
    if "更新.exe" in MANAGED_PATHS or "gui-settings.ini" in MANAGED_PATHS or "logs" in MANAGED_PATHS:
        raise UpdateError("Portable-data preservation self-check failed")


def _write_console_line(message: str, ascii_fallback: str) -> None:
    try:
        sys.stdout.write(message + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(ascii_fallback + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aura 独立更新器")
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_check:
        try:
            self_check()
        except Exception:
            _write_console_line(
                FAILURE_MESSAGE,
                "Update failed. Please download the latest release from GitHub Releases.",
            )
            return 1
        _write_console_line("更新器自检通过。", "Aura updater self-check passed.")
        return 0

    try:
        _write_console_line("正在检查最新正式版……", "Checking the latest formal release...")
        updated = perform_update(application_root())
        if updated:
            _write_console_line("更新完成。", "Update completed.")
        else:
            _write_console_line("当前已是最新正式版。", "The installed release is already current.")
        return 0
    except Exception:
        _write_console_line(
            FAILURE_MESSAGE,
            "Update failed. Please download the latest release from GitHub Releases.",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
