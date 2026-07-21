"""Responsive Qt bridge between the Resonance GUI and Aura game runner."""

from __future__ import annotations

import itertools
import time
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal, Slot

from packages.aura_game import SubprocessGameRunner

from .logic import (
    GAME_NAME,
    PC_GAME_NAME,
    PC_TRADE_PREVIEW_TASK_REF,
    PC_TRADE_TASK_REF,
    TERMINAL_STATUSES,
    TRADE_PROGRESS_EVENT,
    TRADE_PROGRESS_SCHEMA,
    extract_run_id,
    extract_status,
    normalize_run_payload,
)

RunnerFactory = Callable[[], Any]


class RunnerBridge(QObject):
    tasksLoaded = Signal(list)
    historyLoaded = Signal(list)
    queueChanged = Signal(list)
    taskQueued = Signal(dict)
    taskStarted = Signal(dict)
    taskDispatched = Signal(dict)
    runUpdated = Signal(dict)
    taskFinished = Signal(dict)
    taskFailed = Signal(dict)
    tradeProgress = Signal(dict)
    targetStatusChanged = Signal(dict)
    cancelRequested = Signal(dict)
    busyChanged = Signal(bool)
    logMessage = Signal(str)

    def __init__(self, runner_factory: RunnerFactory | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runner_factory = runner_factory or (lambda: SubprocessGameRunner())
        self._runner: Any | None = None
        self._queue: list[dict[str, Any]] = []
        self._busy = False
        self._current_cid = ""
        self._current_item: dict[str, Any] | None = None
        self._started_monotonic = 0.0
        self._cancel_sent = False
        self._timeout_cancel = False
        self._poll_error_count = 0
        self._ticket_counter = itertools.count(1)
        self._poll_timer: QTimer | None = None

    @property
    def current_cid(self) -> str:
        return self._current_cid

    @property
    def busy(self) -> bool:
        return self._busy

    def _runner_instance(self) -> Any:
        if self._runner is None:
            self._runner = self._runner_factory()
        return self._runner

    @Slot()
    def initialize(self) -> None:
        self._ensure_poll_timer()
        self.logMessage.emit("正在加载雷索纳斯任务列表。")
        self.refresh_tasks()
        self.refresh_history()
        self.refresh_target()

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
            rows = list(self._runner_instance().list_runs(limit=50, game_name=PC_GAME_NAME))
        except Exception as exc:  # noqa: BLE001
            self.taskFailed.emit({"stage": "list_runs", "error": str(exc)})
            return
        self.historyLoaded.emit([normalize_run_payload(row) for row in rows])

    @Slot()
    def refresh_target(self) -> None:
        try:
            payload = self._runner_instance().target_status(game_name=PC_GAME_NAME)
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "game_name": PC_GAME_NAME, "error": str(exc)}
        self.targetStatusChanged.emit(dict(payload or {}))

    @Slot(str, object, object, float)
    def enqueue_task(self, task_ref: str, inputs: object, label: object = None, timeout_sec: float = 0.0) -> None:
        self._queue.append(self._make_item(GAME_NAME, task_ref, inputs, label, timeout_sec))
        self.taskQueued.emit(dict(self._queue[-1]))
        self._emit_queue()
        if not self._busy:
            self._run_next()

    @Slot(str, object, object, float)
    def run_task_now(self, task_ref: str, inputs: object, label: object = None, timeout_sec: float = 0.0) -> None:
        self._queue.insert(0, self._make_item(GAME_NAME, task_ref, inputs, label, timeout_sec))
        self._emit_queue()
        if not self._busy:
            self._run_next()

    @Slot(object, float)
    def run_pc_trade(self, inputs: object, timeout_sec: float = 0.0) -> None:
        if self._busy:
            self.taskFailed.emit({"stage": "run_pc_trade", "error": "已有任务正在运行。"})
            return
        run_inputs = dict(inputs or {}) if isinstance(inputs, dict) else {}
        run_inputs.pop("start_city_id", None)
        item = self._make_item(PC_GAME_NAME, PC_TRADE_TASK_REF, run_inputs, "PC 自动跑商", timeout_sec)
        item["kind"] = "trade_run"
        self._queue.insert(0, item)
        self._emit_queue()
        self._run_next()

    @Slot(object, float)
    def preview_pc_trade(self, inputs: object, timeout_sec: float = 0.0) -> None:
        if self._busy:
            self.taskFailed.emit({"stage": "preview_pc_trade", "error": "已有任务正在运行。"})
            return
        preview_inputs = dict(inputs or {}) if isinstance(inputs, dict) else {}
        for key in (
            "use_fatigue_medicine",
            "allowed_fatigue_medicines",
            "fatigue_medicine_max_uses",
        ):
            preview_inputs.pop(key, None)
        item = self._make_item(
            PC_GAME_NAME,
            PC_TRADE_PREVIEW_TASK_REF,
            preview_inputs,
            "计算跑商方案",
            timeout_sec,
        )
        item["kind"] = "trade_preview"
        self._queue.insert(0, item)
        self._emit_queue()
        self._run_next()

    @Slot()
    def clear_queue(self) -> None:
        self._queue.clear()
        self._emit_queue()

    @Slot()
    def cancel_current(self) -> None:
        if not self._current_cid:
            self.logMessage.emit("当前没有可取消的任务。")
            return
        if self._cancel_sent:
            return
        self._request_cancel(reason="user")

    @Slot()
    def poll_current(self) -> None:
        if not self._current_cid or not self._current_item:
            self._stop_polling()
            return
        runner = self._runner_instance()
        try:
            events = list(runner.poll_events(limit=200, timeout_sec=0.0))
            self._consume_events(events)
            run = normalize_run_payload(runner.get_run(self._current_cid))
        except Exception as exc:  # noqa: BLE001
            self._poll_error_count += 1
            terminal_failure = self._poll_error_count >= 3
            self.taskFailed.emit(
                {
                    "stage": "poll_run",
                    "cid": self._current_cid,
                    "error": str(exc),
                    "recoverable": not terminal_failure,
                    "attempt": self._poll_error_count,
                }
            )
            if terminal_failure:
                self.logMessage.emit(f"任务状态连续读取失败：{exc}")
                self._reset_current()
                self._run_next()
            return

        self._poll_error_count = 0
        if run:
            self.runUpdated.emit(run)
            status = extract_status(run)
            if status in TERMINAL_STATUSES:
                self._finish_current(run)
                return

        timeout_sec = float(self._current_item.get("timeout_sec") or 0.0)
        if (
            timeout_sec > 0
            and not self._cancel_sent
            and time.monotonic() - self._started_monotonic >= timeout_sec
        ):
            self._timeout_cancel = True
            self._request_cancel(reason="timeout")

    @Slot()
    def close(self) -> None:
        self._stop_polling()
        runner = self._runner
        self._runner = None
        if runner is not None and hasattr(runner, "close"):
            runner.close()

    def _make_item(
        self,
        game_name: str,
        task_ref: str,
        inputs: object,
        label: object,
        timeout_sec: float,
    ) -> dict[str, Any]:
        return {
            "ticket": next(self._ticket_counter),
            "game_name": str(game_name),
            "task_ref": str(task_ref),
            "label": str(label or task_ref),
            "inputs": dict(inputs or {}) if isinstance(inputs, dict) else {},
            "timeout_sec": float(timeout_sec if timeout_sec is not None else 0.0),
        }

    def _run_next(self) -> None:
        if not self._queue:
            self._set_busy(False)
            return

        self._set_busy(True)
        item = self._queue.pop(0)
        self._emit_queue()
        self._current_cid = ""
        self._current_item = item
        self._cancel_sent = False
        self._timeout_cancel = False
        self._poll_error_count = 0
        self._started_monotonic = time.monotonic()
        self.taskStarted.emit(dict(item))
        self.logMessage.emit(f"开始执行：{item['label']}")

        try:
            dispatch = normalize_run_payload(
                self._runner_instance().run_task(
                    game_name=item["game_name"],
                    task_ref=item["task_ref"],
                    inputs=item["inputs"],
                    wait=False,
                    timeout_sec=0.0,
                )
            )
            self._current_cid = extract_run_id(dispatch)
            if not self._current_cid:
                raise RuntimeError("任务派发结果缺少 CID。")
            payload = {"item": dict(item), "dispatch": dispatch, "cid": self._current_cid}
            self.taskDispatched.emit(payload)
            self._start_polling()
        except Exception as exc:  # noqa: BLE001
            self.taskFailed.emit(
                {
                    "stage": "run_task",
                    "task_ref": item["task_ref"],
                    "kind": item.get("kind"),
                    "error": str(exc),
                }
            )
            self.logMessage.emit(f"执行失败：{item['label']} - {exc}")
            self._reset_current()
            self._run_next()

    def _consume_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            if str(event.get("name") or "") != TRADE_PROGRESS_EVENT:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("schema") or "") != TRADE_PROGRESS_SCHEMA:
                continue
            if str(payload.get("cid") or "") != self._current_cid:
                continue
            self.tradeProgress.emit(dict(event))

    def _request_cancel(self, *, reason: str) -> None:
        self._cancel_sent = True
        try:
            result = self._runner_instance().cancel_task(self._current_cid)
        except Exception as exc:  # noqa: BLE001
            self._cancel_sent = False
            self.taskFailed.emit({"stage": "cancel_task", "cid": self._current_cid, "error": str(exc)})
            return
        payload = {
            "cid": self._current_cid,
            "reason": reason,
            "timeout": reason == "timeout",
            "result": result if isinstance(result, dict) else {"result": result},
        }
        self.cancelRequested.emit(payload)
        self.logMessage.emit(
            f"已因等待超时请求取消任务 {self._current_cid}。"
            if reason == "timeout"
            else f"已请求取消任务 {self._current_cid}。"
        )

    def _finish_current(self, run: dict[str, Any]) -> None:
        item = dict(self._current_item or {})
        payload = dict(run)
        payload.setdefault("cid", self._current_cid)
        payload["gui_timeout_cancelled"] = self._timeout_cancel
        payload["gui_item"] = item
        self.taskFinished.emit(payload)
        self.logMessage.emit(f"执行结束：{item.get('label', '')}")
        self._reset_current()
        self.refresh_history()
        self.refresh_target()
        self._run_next()

    def _reset_current(self) -> None:
        self._stop_polling()
        self._current_cid = ""
        self._current_item = None
        self._cancel_sent = False
        self._timeout_cancel = False
        self._poll_error_count = 0

    def _ensure_poll_timer(self) -> QTimer | None:
        if self._poll_timer is not None:
            return self._poll_timer
        if QCoreApplication.instance() is None:
            return None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(500)
        self._poll_timer.timeout.connect(self.poll_current)
        return self._poll_timer

    def _start_polling(self) -> None:
        timer = self._ensure_poll_timer()
        if timer is not None and not timer.isActive():
            timer.start()

    def _stop_polling(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()

    def _set_busy(self, value: bool) -> None:
        if self._busy == value:
            return
        self._busy = value
        self.busyChanged.emit(value)

    def _emit_queue(self) -> None:
        self.queueChanged.emit([dict(item) for item in self._queue])
