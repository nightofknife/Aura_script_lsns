"""Qt bridge between the Resonance GUI and Aura game runner."""

from __future__ import annotations

import itertools
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, Slot

from packages.aura_game import SubprocessGameRunner

from .logic import GAME_NAME, extract_run_id, normalize_run_payload

RunnerFactory = Callable[[], Any]


class RunnerBridge(QObject):
    tasksLoaded = Signal(list)
    historyLoaded = Signal(list)
    queueChanged = Signal(list)
    taskQueued = Signal(dict)
    taskStarted = Signal(dict)
    taskFinished = Signal(dict)
    taskFailed = Signal(dict)
    busyChanged = Signal(bool)
    logMessage = Signal(str)

    def __init__(self, runner_factory: RunnerFactory | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runner_factory = runner_factory or (lambda: SubprocessGameRunner())
        self._runner: Any | None = None
        self._queue: list[dict[str, Any]] = []
        self._busy = False
        self._current_cid = ""
        self._ticket_counter = itertools.count(1)

    def _runner_instance(self) -> Any:
        if self._runner is None:
            self._runner = self._runner_factory()
        return self._runner

    @Slot()
    def initialize(self) -> None:
        self.logMessage.emit("正在加载雷索纳斯任务列表。")
        self.refresh_tasks()
        self.refresh_history()

    @Slot()
    def refresh_tasks(self) -> None:
        try:
            rows = list(self._runner_instance().list_tasks(GAME_NAME))
        except Exception as exc:  # noqa: BLE001
            self.taskFailed.emit({"stage": "list_tasks", "error": str(exc)})
            return
        self.tasksLoaded.emit(rows)
        self.logMessage.emit(f"已加载 {len(rows)} 个雷索纳斯任务。")

    @Slot()
    def refresh_history(self) -> None:
        try:
            rows = list(self._runner_instance().list_runs(limit=50, game_name=GAME_NAME))
        except Exception as exc:  # noqa: BLE001
            self.taskFailed.emit({"stage": "list_runs", "error": str(exc)})
            return
        self.historyLoaded.emit([normalize_run_payload(row) for row in rows])

    @Slot(str, object, object, float)
    def enqueue_task(self, task_ref: str, inputs: object, label: object = None, timeout_sec: float = 600.0) -> None:
        item = {
            "ticket": next(self._ticket_counter),
            "task_ref": str(task_ref),
            "label": str(label or task_ref),
            "inputs": dict(inputs or {}) if isinstance(inputs, dict) else {},
            "timeout_sec": float(timeout_sec or 600.0),
        }
        self._queue.append(item)
        self.taskQueued.emit(dict(item))
        self._emit_queue()
        if not self._busy:
            self._run_next()

    @Slot(str, object, object, float)
    def run_task_now(self, task_ref: str, inputs: object, label: object = None, timeout_sec: float = 600.0) -> None:
        self._queue.insert(
            0,
            {
                "ticket": next(self._ticket_counter),
                "task_ref": str(task_ref),
                "label": str(label or task_ref),
                "inputs": dict(inputs or {}) if isinstance(inputs, dict) else {},
                "timeout_sec": float(timeout_sec or 600.0),
            },
        )
        self._emit_queue()
        if not self._busy:
            self._run_next()

    @Slot()
    def clear_queue(self) -> None:
        if self._busy:
            self._queue.clear()
        else:
            self._queue.clear()
        self._emit_queue()

    @Slot()
    def cancel_current(self) -> None:
        if not self._current_cid:
            self.logMessage.emit("当前没有可取消的任务。")
            return
        try:
            result = self._runner_instance().cancel_task(self._current_cid)
        except Exception as exc:  # noqa: BLE001
            self.taskFailed.emit({"stage": "cancel_task", "cid": self._current_cid, "error": str(exc)})
            return
        self.logMessage.emit(f"已请求取消任务 {self._current_cid}。")
        self.taskFinished.emit(normalize_run_payload(result if isinstance(result, dict) else {"result": result}))

    @Slot()
    def close(self) -> None:
        runner = self._runner
        self._runner = None
        if runner is not None and hasattr(runner, "close"):
            runner.close()

    def _run_next(self) -> None:
        if not self._queue:
            self._set_busy(False)
            return

        self._set_busy(True)
        item = self._queue.pop(0)
        self._emit_queue()
        self._current_cid = ""
        self.taskStarted.emit(dict(item))
        self.logMessage.emit(f"开始执行：{item['label']}")

        try:
            result = self._runner_instance().run_task(
                game_name=GAME_NAME,
                task_ref=item["task_ref"],
                inputs=item["inputs"],
                wait=True,
                timeout_sec=item["timeout_sec"],
            )
            payload = normalize_run_payload(result if isinstance(result, dict) else {"result": result})
            self._current_cid = extract_run_id(payload)
            self.taskFinished.emit(payload)
            self.logMessage.emit(f"执行完成：{item['label']}")
        except Exception as exc:  # noqa: BLE001
            self.taskFailed.emit({"stage": "run_task", "task_ref": item["task_ref"], "error": str(exc)})
            self.logMessage.emit(f"执行失败：{item['label']} - {exc}")
        finally:
            self._current_cid = ""
            self.refresh_history()
            self._run_next()

    def _set_busy(self, value: bool) -> None:
        if self._busy == value:
            return
        self._busy = value
        self.busyChanged.emit(value)

    def _emit_queue(self) -> None:
        self.queueChanged.emit([dict(item) for item in self._queue])
