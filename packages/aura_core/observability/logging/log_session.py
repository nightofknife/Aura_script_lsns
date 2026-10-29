"""Process-owned log sessions and conservative, startup-only retention.

Only files registered by this module are eligible for deletion. Legacy logs,
unknown files, database history and linked directories are deliberately retained.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any

class _SessionDiagnostics:
    """Startup messages wait for the application's actual file handler."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: list[tuple] = []
        self._sink: logging.Logger | None = None

    def _emit(self, level: int, message: str, *args) -> None:
        with self._lock:
            sink = self._sink
            ready = sink is not None and any(
                handler.name == "task_file" and (getattr(handler, "stream", None) is not None
                    or getattr(handler, "state", None) is not None)
                for handler in sink.handlers
            )
            if not ready:
                self._pending.append((level, message, args))
                self._pending = self._pending[-256:]
                return
        sink.log(level, "[LogSession] " + message, *args)

    def warning(self, message: str, *args) -> None:
        self._emit(logging.WARNING, message, *args)

    def info(self, message: str, *args) -> None:
        self._emit(logging.INFO, message, *args)

    def flush_to(self, sink: logging.Logger) -> None:
        with self._lock:
            self._sink = sink
            pending, self._pending = self._pending, []
        for level, message, args in pending:
            sink.log(level, "[LogSession] " + message, *args)


_LOGGER = _SessionDiagnostics()


def flush_session_diagnostics(sink: logging.Logger) -> None:
    _LOGGER.flush_to(sink)


_RETENTION_SECONDS = 72 * 60 * 60
_SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_LOG_PATTERN = re.compile(
    r"(?:aura_session_\d{8}-\d{6}|aura_gui_\d+_\d{8}-\d{6})"
    r"(?:_[A-Za-z0-9_-]+)?\.log(?:\.\d+)?\Z"
)
_IMAGE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,120}\.png\Z")
_MEMBER_PATTERN = re.compile(r"([A-Za-z0-9_-]+)\.(\d+)\.([a-f0-9]{8})\.(json|lease)\Z")
_PROCESS_LOCK = threading.RLock()
_SESSION: LogSession | None = None


def _linked(path: Path) -> bool:
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(attributes.st_mode) or bool(
        getattr(attributes, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_path(root: Path, path: Path) -> bool:
    """Check lexical containment and every existing path component."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return not any(_linked(item) for item in (path, *path.parents))


def resolve_log_root(base_path: str | Path, configured: str | Path | None = None) -> Path:
    path = Path(configured) if configured else Path("logs")
    if not path.is_absolute():
        path = Path(base_path) / path
    path = Path(os.path.abspath(path))
    if not _safe_path(path, path):
        raise ValueError(f"Log directory must not pass through a link: {path}")
    return path


def safe_managed_path(root: Path, relative: str) -> Path:
    """Return an unlinked managed log/evidence path, including absent targets.

    Call again after creating parent directories and before opening the target.
    This intentionally does not resolve links away before checking them.
    """
    root = Path(os.path.abspath(root))
    fragment = Path(relative)
    if fragment.is_absolute() or fragment.drive or ".." in fragment.parts:
        raise ValueError("Managed log paths must be relative and contained")
    parts = fragment.parts
    managed_log = len(parts) == 1 and _LOG_PATTERN.fullmatch(parts[0])
    managed_image = (
        len(parts) == 3
        and parts[0] == "errors"
        and _SESSION_PATTERN.fullmatch(parts[1])
        and _IMAGE_PATTERN.fullmatch(parts[2])
    )
    target = root / fragment
    if not (managed_log or managed_image) or not _safe_path(root, target):
        raise ValueError("Unknown or linked managed log path")
    return target


class _FileLock:
    """A lifetime advisory lock: Windows byte lock or POSIX flock."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        try:
            if not _safe_path(self.path.parent, self.path):
                return False
            self.handle = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                self.handle.write(b"\0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            return False

    def close(self) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def acquire_bounded(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self.acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.025)


class LogSession:
    def __init__(self, root: Path, session_id: str):
        self.root = root
        self.session_id = session_id
        self.pid = os.getpid()
        self._closed = False
        self._lock = threading.RLock()
        self._logs: set[str] = set()
        self._lease: _FileLock | None = None
        self._directory = root / ".sessions" / session_id
        token = f"{session_id}.{self.pid}.{uuid.uuid4().hex[:8]}"
        self._metadata = self._directory / f"{token}.json"
        self._lease_path = self._directory / f"{token}.lease"

    @property
    def errors_dir(self) -> Path:
        return self.root / "errors" / self.session_id

    @property
    def available(self) -> bool:
        return not self._closed and self._lease is not None

    def _open(self) -> None:
        if not _safe_path(self.root, self._directory):
            raise ValueError("Unsafe log session directory")
        self._directory.mkdir(parents=True, exist_ok=True)
        lease = _FileLock(self._lease_path)
        if not lease.acquire():
            raise OSError(f"Cannot hold log session lease: {self._lease_path}")
        self._lease = lease
        self._write_metadata()

    def _write_metadata(self) -> None:
        if not _safe_path(self.root, self._metadata):
            raise ValueError("Unsafe log metadata path")
        payload = {
            "format": 1,
            "session_id": self.session_id,
            "pid": self.pid,
            "logs": sorted(self._logs),
        }
        temporary = self._metadata.with_suffix(".json.tmp")
        if not _safe_path(self.root, temporary):
            raise ValueError("Unsafe temporary metadata path")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self._metadata)

    def register_log(self, path: str | Path) -> bool:
        """Associate a session log with this process; never register arbitrary files."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = Path(os.path.abspath(candidate))
        if (
            candidate.parent != self.root
            or not _LOG_PATTERN.fullmatch(candidate.name)
            or ".log." in candidate.name
            or not _safe_path(self.root, candidate)
        ):
            _LOGGER.warning("Log retention did not register unknown path: %s", candidate)
            return False
        with self._lock:
            if self._closed or self._lease is None:
                return False
            coordination = _FileLock(self.root / ".sessions" / ".coord.lock")
            held = coordination.acquire_bounded()
            if not held:
                _LOGGER.warning("Skipping session log registration: coordination is busy")
                return False
            try:
                self._logs.add(candidate.name)
                self._write_metadata()
            except (OSError, ValueError) as exc:
                _LOGGER.warning("Could not register session log %s: %s", candidate, exc)
                return False
            finally:
                if held:
                    coordination.close()
        return True

    def close(self) -> None:
        """Release only this process's lease; a worker keeps its own lease."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._lease is not None:
                self._lease.close()
                self._lease = None
            try:
                if _safe_path(self.root, self._lease_path):
                    self._lease_path.unlink(missing_ok=True)
            except OSError as exc:
                _LOGGER.warning("Could not remove ended log lease: %s", exc)


def _session_files(root: Path, directory: Path) -> list[Path]:
    """Return the complete managed group, or fail closed on unknown metadata."""
    session_id = directory.name
    files: set[Path] = set()
    metadata = list(directory.iterdir())
    if not metadata:
        raise ValueError("Empty session metadata")
    for member in metadata:
        if not _safe_path(root, member) or not member.is_file():
            raise ValueError("Unsafe or unknown session member")
        if member.name == "writer.lock":
            files.add(member)
            continue
        match = _MEMBER_PATTERN.fullmatch(member.name)
        if match is None or match.group(1) != session_id:
            raise ValueError("Unknown session owner filename")
        if member.suffix == ".lease":
            if not member.with_suffix(".json").is_file():
                raise ValueError("Lease has no ownership metadata")
            continue
        if member.suffix != ".json":
            raise ValueError("Incomplete or unknown session metadata")
        data = json.loads(member.read_text(encoding="utf-8"))
        if (
            data.get("format") != 1
            or data.get("session_id") != session_id
            or data.get("pid") != int(match.group(2))
            or not isinstance(data.get("logs"), list)
        ):
            raise ValueError("Unsupported session metadata")
        files.add(member)
        for name in data["logs"]:
            if not isinstance(name, str) or not _LOG_PATTERN.fullmatch(name) or ".log." in name:
                raise ValueError("Unknown registered log")
            log_path = root / name
            if not _safe_path(root, log_path):
                raise ValueError("Linked log file")
            if log_path.exists():
                if not log_path.is_file():
                    raise ValueError("Log is not a file")
                files.add(log_path)
            for rotated in root.glob(f"{name}.*"):
                if not _LOG_PATTERN.fullmatch(rotated.name) or not _safe_path(root, rotated):
                    raise ValueError("Unknown rotation file")
                if not rotated.is_file():
                    raise ValueError("Rotation is not a file")
                files.add(rotated)
    evidence = root / "errors" / session_id
    if not _safe_path(root, evidence):
        raise ValueError("Linked evidence directory")
    if evidence.exists():
        for item in evidence.iterdir():
            if not _IMAGE_PATTERN.fullmatch(item.name) or not _safe_path(root, item) or not item.is_file():
                raise ValueError("Unknown evidence file")
            files.add(item)
    if not files:
        raise ValueError("No complete session metadata")
    return sorted(files)


def _cleanup(root: Path, cutoff: float, current_session: str) -> None:
    registry = root / ".sessions"
    probes: list[_FileLock] = []
    cleaned: list[Path] = []
    try:
        directories = []
        for item in registry.iterdir():
            if item.name == ".coord.lock":
                continue
            if not _SESSION_PATTERN.fullmatch(item.name) or not _safe_path(root, item) or not item.is_dir():
                _LOGGER.warning("Skipping startup log cleanup: unknown session metadata")
                return
            directories.append(item)
        # Hold all inactive leases until cleanup completes. Any other live process
        # or unrecognised lease aborts the entire startup attempt.
        for directory in directories:
            for lease_path in directory.glob("*.lease"):
                if not _safe_path(root, lease_path) or not lease_path.is_file():
                    return
                probe = _FileLock(lease_path)
                if not probe.acquire():
                    _LOGGER.info("Skipping startup log cleanup: another process holds a log lease")
                    return
                probes.append(probe)
        groups = []
        for directory in directories:
            if directory.name == current_session:
                continue
            try:
                files = _session_files(root, directory)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                _LOGGER.warning("Skipping startup log cleanup: cannot establish session ownership (%s)", exc)
                return
            if max(path.stat().st_mtime for path in files) < cutoff:
                groups.append((directory, files))
        for directory, files in groups:
            # All session joins/registrations use the same coordination lock.
            current_files = _session_files(root, directory)
            if current_files != files or max(path.stat().st_mtime for path in current_files) >= cutoff:
                continue
            data_files = [path for path in files if path.parent != directory]
            complete = True
            for path in data_files:
                if not _safe_path(root, path):
                    complete = False
                    continue
                try:
                    path.unlink()
                except OSError as exc:
                    complete = False
                    _LOGGER.warning("Could not remove expired log file %s: %s", path, exc)
            if not complete:
                continue
            for path in (path for path in files if path.parent == directory):
                try:
                    path.unlink()
                except OSError as exc:
                    complete = False
                    _LOGGER.warning("Could not remove expired session metadata %s: %s", path, exc)
            evidence = root / "errors" / directory.name
            if evidence.exists() and _safe_path(root, evidence):
                try:
                    evidence.rmdir()
                except OSError:
                    pass
            if complete:
                cleaned.append(directory)
            # Empty metadata directories are removed after lease handles close.
    except (OSError, ValueError) as exc:
        _LOGGER.warning("Startup log cleanup skipped or incomplete: %s", exc)
    finally:
        for probe in probes:
            probe.close()
        # Remove only stale lease files from sessions with no remaining metadata.
        for directory in cleaned:
            try:
                members = list(directory.iterdir())
                if members and all(member.suffix == ".lease" for member in members):
                    # Never remove an unprobed or unknown lease.
                    known_leases = {probe.path for probe in probes}
                    for member in members:
                        if member not in known_leases:
                            continue
                        if _safe_path(root, member):
                            member.unlink()
                directory.rmdir()
            except OSError:
                pass


def start_log_session(
    log_root: str | Path,
    cleanup: bool = True,
    session_id: str | None = None,
) -> LogSession:
    """Start/join once per process; only an owning GUI/CLI requests cleanup."""
    global _SESSION
    with _PROCESS_LOCK:
        if _SESSION is not None and _SESSION.pid == os.getpid() and not _SESSION._closed:
            return _SESSION
        inherited = os.environ.get("AURA_LOG_SESSION_ID")
        chosen_id = session_id or inherited or (
            time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        )
        if not _SESSION_PATTERN.fullmatch(chosen_id):
            raise ValueError("Invalid log session identifier")
        root = resolve_log_root(Path.cwd(), log_root)
        registry = root / ".sessions"
        session = LogSession(root, chosen_id)
        coordination = None
        held = False
        try:
            if not _safe_path(root, registry):
                raise ValueError("Unsafe log session registry")
            registry.mkdir(parents=True, exist_ok=True)
            coordination = _FileLock(registry / ".coord.lock")
            held = coordination.acquire()
            if cleanup and held:
                _cleanup(root, time.time() - _RETENTION_SECONDS, chosen_id)
            elif cleanup:
                _LOGGER.info("Skipping startup log cleanup: session coordination is busy")
            if not held:
                # Registration may wait briefly, but cleanup is never retried.
                held = coordination.acquire_bounded()
            if held:
                session._open()
            else:
                # Keep the root/ID for ordinary logs; no ownership means no PNG
                # writes and no retention of those unregistered log files.
                _LOGGER.warning("Log session ownership unavailable: coordination is busy")
        except Exception as exc:
            try:
                session.close()
            except OSError:
                pass
            # Ordinary logs remain usable even if session metadata is not.
            session = LogSession(root, chosen_id)
            _LOGGER.warning("Ownership/retention disabled; ordinary logging continues: %s", exc)
        finally:
            if held:
                coordination.close()
        _SESSION = session
        os.environ["AURA_LOG_SESSION_ID"] = session.session_id
        os.environ["AURA_LOG_ROOT"] = str(session.root)
        atexit.register(session.close)
        # Release the lease only after log handlers have stopped writing.
        atexit.register(logging.shutdown)
        return session


def get_log_session() -> LogSession | None:
    """Passive lookup; never starts retention or creates a new session."""
    if _SESSION is not None and _SESSION.pid == os.getpid() and not _SESSION._closed:
        return _SESSION
    return None
