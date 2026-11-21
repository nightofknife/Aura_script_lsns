"""Persist visually confirmed Calculation Recording percentages in the Aura user data folder."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from packages.aura_core.observability.logging.log_session import _FileLock


PROGRESS_FILE = "calculation-recording-progress.json"
PROGRESS_VALUES = frozenset({0, 20, 40, 60, 80, 100})
_SCHEMA_VERSION = 1


class RecordingProgressError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def progress_path(persistent_data: PersistentDataService) -> Path:
    return persistent_data.root / PROGRESS_FILE


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("进度档案存在重复键")
    return result


@contextmanager
def _write_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _FileLock(path.with_name(f".{path.name}.write.lock"))
    if not lock.acquire_bounded():
        raise RecordingProgressError("calculation_recording_progress_busy", "无法锁定虚演刻录本地进度文件。")
    try:
        yield
    finally:
        lock.close()


class RecordingProgress:
    """An account-scoped view of recorded percentages, read afresh on each write."""

    def __init__(self, path: Path, account: str) -> None:
        if not isinstance(account, str) or not account or account != account.strip():
            raise ValueError("虚演刻录本地档案标识不能为空或带边缘空格。")
        self.path = path
        self.account = account

    @classmethod
    def load(cls, path: Path | PersistentDataService, account: str) -> RecordingProgress:
        target = progress_path(path) if isinstance(path, PersistentDataService) else Path(path)
        progress = cls(target, account)
        progress._read()
        return progress

    def get(self, number: int) -> int | None:
        return self._read()["accounts"].get(self.account, {}).get(str(number))

    def save(self, number: int, progress: int) -> None:
        if type(number) is not int or number <= 0:
            raise ValueError("虚演刻录关卡编号必须是正整数。")
        if type(progress) is not int or progress not in PROGRESS_VALUES:
            raise ValueError("虚演刻录完成度必须是 0、20、40、60、80 或 100。")

        try:
            with _write_lock(self.path):
                document = self._read()
                stages = document["accounts"].setdefault(self.account, {})
                key = str(number)
                if stages.get(key) != progress:
                    stages[key] = progress
                    self._replace(document)
        except (OSError, ValueError) as exc:
            raise RecordingProgressError(
                "calculation_recording_progress_write_failed", "写入虚演刻录本地进度失败。"
            ) from exc

    def _read(self) -> dict:
        try:
            with self.path.open(encoding="utf-8") as handle:
                document = json.load(handle, object_pairs_hook=_unique_object)
        except FileNotFoundError:
            return {"schema_version": _SCHEMA_VERSION, "accounts": {}}
        except (OSError, ValueError) as exc:
            raise RecordingProgressError(
                "calculation_recording_progress_read_failed", "无法读取虚演刻录本地进度文件。"
            ) from exc

        if (
            not isinstance(document, dict)
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != _SCHEMA_VERSION
            or not isinstance(document.get("accounts"), dict)
        ):
            raise RecordingProgressError(
                "calculation_recording_progress_invalid", "虚演刻录本地进度文件格式无效。"
            )
        for account, stages in document["accounts"].items():
            if (
                not account or account != account.strip()
                or not isinstance(stages, dict)
                or any(
                    not number.isascii() or not number.isdecimal() or number.startswith("0")
                    or type(value) is not int or value not in PROGRESS_VALUES
                    for number, value in stages.items()
                )
            ):
                raise RecordingProgressError(
                    "calculation_recording_progress_invalid", "虚演刻录本地进度包含无效关卡记录。"
                )
        return document

    def _replace(self, document: dict) -> None:
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", prefix=f".{self.path.name}.",
                suffix=".tmp", dir=self.path.parent, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
