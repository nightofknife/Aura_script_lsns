"""Responsive Qt bridge between the Resonance GUI and Aura game runner."""

from __future__ import annotations

import itertools
import os
import threading
import time
import uuid
import weakref
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal, Slot

from packages.aura_game import SubprocessGameRunner
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.observability.logging.coordinator import get_session_descriptor, subscribe

from .config_repository import TRADE_PREVIEW_INPUT_KEYS
from .logic import (
    AFFIX_REFRESH_PROGRESS_EVENT,
    AFFIX_REFRESH_PROGRESS_SCHEMA,
    PC_AFFIX_REFRESH_TASK_REF,
    PC_AFFIX_REFRESH_READ_TASK_REF,
    ETERNAL_SCUFFLE_PROGRESS_EVENT,
    DEEP_DIVE_LOOP_PROGRESS_EVENT,
    PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF,
    ETERNAL_SCUFFLE_PROGRESS_SCHEMA,
    PC_ETERNAL_SCUFFLE_TASK_REF,
    PC_BATTLE_TASK_REF,
    PC_COMBINED_COMMERCE_TASK_REF,
    PC_GAME_NAME,
    PC_PASSENGER_TASK_REF,
    PC_TRADE_PREVIEW_TASK_REF,
    PC_TRADE_TASK_REF,
    TERMINAL_STATUSES,
    PASSENGER_PROGRESS_EVENT,
    PASSENGER_PROGRESS_SCHEMA,
    TRADE_PROGRESS_EVENT,
    TRADE_PROGRESS_SCHEMA,
    extract_run_id,
    extract_status,
    normalize_run_payload,
    normalize_trade_task_inputs,
    recovery_snapshot_task_input,
)

RunnerFactory = Callable[[], Any]


DEEP_DIVE_PLANNED_RUN_PROGRESS_EVENT = "task.resonance_pc_deep_dive_planned_run_progress"


class RunnerBridge(QObject):
    tasksLoaded = Signal(list)
    historyLoaded = Signal(list)
    taskStarted = Signal(dict)
    taskDispatched = Signal(dict)
    runUpdated = Signal(dict)
    taskFinished = Signal(dict)
    taskFailed = Signal(dict)
    tradeProgress = Signal(dict)
    passengerProgress = Signal(dict)
    eternalScuffleProgress = Signal(dict)
    affixRefreshProgress = Signal(dict)
    deepDiveLoopProgress = Signal(dict)
    deepDivePlannedRunProgress = Signal(dict)
    targetStatusChanged = Signal(dict)
    cancelRequested = Signal(dict)
    busyChanged = Signal(bool)
    logMessage = Signal(str)
    closeCompleted = Signal()
    closeFailed = Signal(str)

    def __init__(self, runner_factory: RunnerFactory | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        if runner_factory is None:
            base_path = str(os.environ.get("AURA_BASE_PATH") or "").strip()
            runner_factory = lambda: SubprocessGameRunner(
                env_overrides={"AURA_BASE_PATH": base_path} if base_path else None,
            )
        self._runner_factory = runner_factory
        self._runner: Any | None = None
        self._use_input_bridge = False
        self._runner_input_bridge = False
        self._shutdown_requested = threading.Event()
        self._closed = False
        self._busy = False
        self._current_cid = ""
        self._scuffle_sequence = -1
        self._affix_sequence = -1
        self._current_item: dict[str, Any] | None = None
        self._started_monotonic = 0.0
        self._cancel_sent = False
        self._timeout_cancel = False
        self._poll_error_count = 0
        self._ticket_counter = itertools.count(1)
        self._poll_timer: QTimer | None = None
        self._log_display_id = uuid.uuid4().hex
        self._log_unsubscribe: Callable[[], None] | None = None
        self._subscribe_log_messages()

    @property
    def current_cid(self) -> str:
        return self._current_cid

    @property
    def busy(self) -> bool:
        return self._busy

    def _log_message(self, message: str) -> None:
        self._subscribe_log_messages()
        logger.info("[GuiStatus] %s", message, extra={
            "gui_display_id": self._log_display_id,
            "gui_display_message": message,
        })
        if self._log_unsubscribe is None and not self._closed:
            # Isolated/offline widgets may intentionally have no log session.
            self.logMessage.emit(message)

    def _subscribe_log_messages(self) -> None:
        if self._log_unsubscribe is not None or self._closed or get_session_descriptor() is None:
            return
        weak_self = weakref.ref(self)
        display_id = self._log_display_id

        def committed(record) -> None:
            if getattr(record, "gui_display_id", None) != display_id:
                return
            bridge = weak_self()
            if bridge is not None and not bridge._closed:
                bridge.logMessage.emit(str(record.gui_display_message))

        self._log_unsubscribe = subscribe(committed)

    def _unsubscribe_log_messages(self) -> None:
        unsubscribe, self._log_unsubscribe = self._log_unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def _runner_instance(self) -> Any:
        if self._shutdown_requested.is_set():
            raise RuntimeError("GUI runner is shutting down")
        if self._runner is None:
            self._runner = self._runner_factory()
            if isinstance(self._runner, SubprocessGameRunner):
                self._runner.env_overrides["AURA_GUI_INPUT_BRIDGE"] = (
                    "1" if self._use_input_bridge else "0"
                )
            self._runner_input_bridge = self._use_input_bridge
        if self._shutdown_requested.is_set():
            if isinstance(self._runner, SubprocessGameRunner):
                self._runner.request_shutdown()
            raise RuntimeError("GUI runner is shutting down")
        return self._runner

    @Slot(bool)
    def set_input_bridge_enabled(self, enabled: bool) -> None:
        """Save the desired mode; an active task keeps its existing worker."""
        self._use_input_bridge = bool(enabled)

    def _task_runner_instance(self) -> Any:
        # Rebuild only between tasks so no gesture or held button changes backend.
        if (
            isinstance(self._runner, SubprocessGameRunner)
            and self._runner_input_bridge != self._use_input_bridge
        ):
            self._runner.close()
            self._runner = None
        return self._runner_instance()

    def request_shutdown(self, *, force: bool = False) -> None:
        """Thread-safe signal only; Qt timers, queues and IPC stay on the bridge thread."""
        self._shutdown_requested.set()
        runner = self._runner
        if isinstance(runner, SubprocessGameRunner):
            runner.request_shutdown(force=force)

    @Slot()
    def initialize(self) -> None:
        if self._shutdown_requested.is_set():
            return
        self._ensure_poll_timer()
        self._log_message("正在加载雷索纳斯任务列表。")
        self.refresh_tasks()
        self.refresh_history()
        self.refresh_target()

    @Slot()
    def refresh_tasks(self) -> None:
        try:
            rows = list(self._runner_instance().list_tasks(PC_GAME_NAME))
        except Exception as exc:  # noqa: BLE001
            self.taskFailed.emit({"stage": "list_tasks", "error": str(exc)})
            return
        self.tasksLoaded.emit(rows)
        self._log_message(f"已加载 {len(rows)} 个雷索纳斯任务。")

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
    def run_pc_task(self, task_ref: str, inputs: object, label: object = None, timeout_sec: float = 0.0) -> None:
        """Run one generic Resonance PC task without enabling bridge queue chaining."""
        if self._busy:
            self.taskFailed.emit({"stage": "run_pc_task", "error": "已有任务正在运行。"})
            return
        item = self._make_item(PC_GAME_NAME, task_ref, inputs, label, timeout_sec)
        item["kind"] = "workflow_task"
        self._run_item(item)

    @Slot(object, float)
    def run_pc_trade(self, inputs: object, timeout_sec: float = 0.0) -> None:
        if self._busy:
            self.taskFailed.emit({"stage": "run_pc_trade", "error": "已有任务正在运行。"})
            return
        run_inputs = dict(inputs or {}) if isinstance(inputs, dict) else {}
        run_inputs.pop("start_city_id", None)
        item = self._make_item(PC_GAME_NAME, PC_TRADE_TASK_REF, run_inputs, "PC 自动跑商", timeout_sec)
        item["kind"] = "trade_run"
        self._run_item(item)

    @Slot(object, float)
    def run_pc_passenger(self, inputs: object, timeout_sec: float = 0.0) -> None:
        if self._busy:
            self.taskFailed.emit({"stage": "run_pc_passenger", "error": "已有任务正在运行。"})
            return
        run_inputs = dict(inputs or {}) if isinstance(inputs, dict) else {}
        item = self._make_item(
            PC_GAME_NAME,
            PC_PASSENGER_TASK_REF,
            run_inputs,
            "PC 独立客运",
            timeout_sec,
        )
        item["kind"] = "passenger_run"
        self._run_item(item)

    @Slot(object, float)
    def run_pc_combined_commerce(self, inputs: object, timeout_sec: float = 0.0) -> None:
        if self._busy:
            self.taskFailed.emit(
                {"stage": "run_pc_combined_commerce", "error": "已有任务正在运行。"}
            )
            return
        run_inputs = dict(inputs or {}) if isinstance(inputs, dict) else {}
        if isinstance(run_inputs.get("trade_inputs"), dict):
            trade_inputs = dict(run_inputs["trade_inputs"])
            trade_inputs.pop("start_city_id", None)
            run_inputs["trade_inputs"] = trade_inputs
        item = self._make_item(
            PC_GAME_NAME,
            PC_COMBINED_COMMERCE_TASK_REF,
            run_inputs,
            "PC 客货运组合",
            timeout_sec,
        )
        item["kind"] = "combined_commerce_run"
        self._run_item(item)

    @Slot(object, float)
    def preview_pc_trade(self, inputs: object, timeout_sec: float = 0.0) -> None:
        if self._busy:
            self.taskFailed.emit({"stage": "preview_pc_trade", "error": "已有任务正在运行。"})
            return
        preview_inputs = dict(inputs or {}) if isinstance(inputs, dict) else {}
        preview_inputs = {
            key: value for key, value in preview_inputs.items() if key in TRADE_PREVIEW_INPUT_KEYS
        }
        item = self._make_item(
            PC_GAME_NAME,
            PC_TRADE_PREVIEW_TASK_REF,
            preview_inputs,
            "计算跑商方案",
            timeout_sec,
        )
        item["kind"] = "trade_preview"
        self._run_item(item)

    @Slot(object, float)
    def run_pc_battle(self, inputs: object, timeout_sec: float = 0.0) -> None:
        if self._busy:
            self.taskFailed.emit({"stage": "run_pc_battle", "error": "已有任务正在运行。"})
            return
        run_inputs = dict(inputs or {}) if isinstance(inputs, dict) else {}
        item = self._make_item(
            PC_GAME_NAME,
            PC_BATTLE_TASK_REF,
            run_inputs,
            "PC 自动战斗",
            timeout_sec,
        )
        item["kind"] = "battle_run"
        self._run_item(item)


    @Slot()
    def cancel_current(self) -> None:
        if not self._current_cid:
            self._log_message("当前没有可取消的任务。")
            return
        if self._cancel_sent:
            return
        self._request_cancel(reason="user")

    @Slot()
    def poll_current(self) -> None:
        if self._shutdown_requested.is_set():
            return
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
            # Losing visibility is never proof that the worker exited, including
            # before a user cancellation. Preserve its CID and the busy lock;
            # repeated failures request cancellation but still need a real
            # terminal snapshot with execution_pending=False before draining.
            request_cancel = self._poll_error_count >= 3 and not self._cancel_sent
            self.taskFailed.emit(
                {
                    "stage": "poll_run",
                    "cid": self._current_cid,
                    "error": str(exc),
                    "recoverable": True,
                    "attempt": self._poll_error_count,
                }
            )
            if request_cancel:
                self._log_message(f"任务状态连续读取失败，正在请求停止并等待执行端确认：{exc}")
                self._request_cancel(reason="poll_error")
            return

        self._poll_error_count = 0
        if run:
            self.runUpdated.emit(run)
            status = extract_status(run)
            if status in TERMINAL_STATUSES:
                if run.get("execution_pending", False):
                    return
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
        self.request_shutdown()
        self._stop_polling()
        if self._closed:
            self.closeCompleted.emit()
            return
        runner = self._runner
        try:
            if runner is not None and hasattr(runner, "close"):
                runner.close()
        except Exception as exc:
            logger.exception("[GuiShutdown] phase=bridge_close_failed")
            self.closeFailed.emit(str(exc))
            return
        self._runner = None
        self._closed = True
        self._unsubscribe_log_messages()
        logger.info("[GuiShutdown] phase=bridge_closed")
        self.closeCompleted.emit()

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

    def _run_item(self, item: dict[str, Any]) -> None:
        if self._shutdown_requested.is_set():
            return
        self._set_busy(True)
        self._current_cid = ""
        self._current_item = item
        self._scuffle_sequence = -1
        self._affix_sequence = -1
        self._cancel_sent = False
        self._timeout_cancel = False
        self._poll_error_count = 0
        self._started_monotonic = time.monotonic()
        self.taskStarted.emit(dict(item))
        self._log_message(f"开始执行：{item['label']}")

        try:
            dispatch_inputs = dict(item["inputs"])
            if item["game_name"] == PC_GAME_NAME:
                if item["task_ref"] in {PC_TRADE_TASK_REF, PC_TRADE_PREVIEW_TASK_REF}:
                    dispatch_inputs = normalize_trade_task_inputs(dispatch_inputs)
                elif item["task_ref"] == PC_COMBINED_COMMERCE_TASK_REF:
                    trade = dispatch_inputs.get("trade_inputs")
                    if isinstance(trade, dict):
                        dispatch_inputs["trade_inputs"] = normalize_trade_task_inputs(trade)
            if (
                item["game_name"] == PC_GAME_NAME
                and item["task_ref"] in {PC_TRADE_TASK_REF, PC_COMBINED_COMMERCE_TASK_REF}
                and dispatch_inputs.get("recovery_snapshot") is not None
            ):
                dispatch_inputs["recovery_snapshot"] = recovery_snapshot_task_input(
                    dispatch_inputs["recovery_snapshot"]
                )
            logger.info("[TaskDispatch] phase=request game=%s task=%s input_fields=%s",
                        item["game_name"], item["task_ref"], sorted(item["inputs"]))
            raw_dispatch = self._task_runner_instance().run_task(
                game_name=item["game_name"],
                task_ref=item["task_ref"],
                inputs=dispatch_inputs,
                wait=False,
                timeout_sec=0.0,
            )
            if not isinstance(raw_dispatch, dict):
                raise RuntimeError("任务派发协议异常：后台返回值不是对象。")
            dispatch = normalize_run_payload(raw_dispatch)
            status = str(dispatch.get("status") or "").lower()
            if status not in {"success", "queued"} or dispatch.get("success") is False:
                message = dispatch.get("message") or dispatch.get("error") or "后台未返回成功状态。"
                raise RuntimeError(f"任务派发失败：{message}")
            self._current_cid = extract_run_id(dispatch)
            if not self._current_cid:
                raise RuntimeError("任务派发协议异常：后台返回成功，但缺少 CID。")
            logger.info("[TaskDispatch] phase=accepted game=%s task=%s cid=%s",
                        item["game_name"], item["task_ref"], self._current_cid)
            payload = {"item": dict(item), "dispatch": dispatch, "cid": self._current_cid}
            self.taskDispatched.emit(payload)
            self._start_polling()
        except Exception as exc:  # noqa: BLE001
            logger.error("[TaskDispatch] phase=rejected game=%s task=%s reason=%s",
                         item["game_name"], item["task_ref"], exc)
            self.taskFailed.emit(
                {
                    "stage": "run_task",
                    "task_ref": item["task_ref"],
                    "kind": item.get("kind"),
                    "error": str(exc),
                }
            )
            self._log_message(f"执行失败：{item['label']} - {exc}")
            self._reset_current()
            self._set_busy(False)

    def _consume_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            name = str(event.get("name") or "")
            if name == AFFIX_REFRESH_PROGRESS_EVENT:
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                sequence = payload.get("sequence")
                if (not self._current_cid
                        or (self._current_item or {}).get("task_ref") not in {PC_AFFIX_REFRESH_TASK_REF, PC_AFFIX_REFRESH_READ_TASK_REF}
                        or payload.get("schema") != AFFIX_REFRESH_PROGRESS_SCHEMA
                        or payload.get("cid") != self._current_cid
                        or type(sequence) is not int or sequence <= self._affix_sequence):
                    continue
                self._affix_sequence = sequence
                self.affixRefreshProgress.emit(dict(event))
                continue
            if name == DEEP_DIVE_PLANNED_RUN_PROGRESS_EVENT:
                payload = event.get("payload")
                if (isinstance(payload, dict) and self._current_cid
                        and (self._current_item or {}).get("task_ref") == PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF
                        and payload.get("schema") == "resonance_pc.deep_dive_planned_run.v1"
                        and payload.get("cid") == self._current_cid):
                    previous = getattr(self, "_deep_dive_planned_progress_cursor", ("", 0))
                    sequence = payload.get("sequence")
                    if type(sequence) is int and (previous[0] != self._current_cid or sequence > previous[1]):
                        self._deep_dive_planned_progress_cursor = (self._current_cid, sequence)
                        self.deepDivePlannedRunProgress.emit(dict(event))
                continue
            if name == DEEP_DIVE_LOOP_PROGRESS_EVENT:
                payload=event.get('payload')
                if (isinstance(payload,dict) and self._current_cid
                        and (self._current_item or {}).get('task_ref') == PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF
                        and payload.get('schema') == 'resonance_pc.deep_dive_loop.v1'
                        and payload.get('cid') == self._current_cid):
                    previous=getattr(self,'_deep_dive_progress_cursor',('',0))
                    sequence=payload.get('sequence')
                    if type(sequence) is int and (previous[0]!=self._current_cid or sequence>previous[1]):
                        self._deep_dive_progress_cursor=(self._current_cid,sequence)
                        self.deepDiveLoopProgress.emit(dict(event))
                continue
            if name == ETERNAL_SCUFFLE_PROGRESS_EVENT:
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                sequence = payload.get("sequence")
                if (not self._current_cid
                        or (self._current_item or {}).get("task_ref") != PC_ETERNAL_SCUFFLE_TASK_REF
                        or payload.get("schema") != ETERNAL_SCUFFLE_PROGRESS_SCHEMA
                        or payload.get("cid") != self._current_cid
                        or type(sequence) is not int or sequence <= self._scuffle_sequence):
                    continue
                self._scuffle_sequence = sequence
                self.eternalScuffleProgress.emit(dict(event))
                continue
            if name not in {TRADE_PROGRESS_EVENT, PASSENGER_PROGRESS_EVENT}:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            schema = str(payload.get("schema") or "")
            expected_schema = (
                TRADE_PROGRESS_SCHEMA
                if name == TRADE_PROGRESS_EVENT
                else PASSENGER_PROGRESS_SCHEMA
            )
            if schema != expected_schema:
                continue
            if str(payload.get("cid") or "") != self._current_cid:
                continue
            if name == TRADE_PROGRESS_EVENT:
                self.tradeProgress.emit(dict(event))
            else:
                self.passengerProgress.emit(dict(event))

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
        self._log_message(
            f"已因等待超时请求取消任务 {self._current_cid}。"
            if reason == "timeout"
            else f"正在停止任务 {self._current_cid}，等待旧任务退出后才能再次运行。"
        )

    def _finish_current(self, run: dict[str, Any]) -> None:
        if self._shutdown_requested.is_set():
            self._reset_current()
            return
        item = dict(self._current_item or {})
        payload = dict(run)
        payload.setdefault("cid", self._current_cid)
        payload["gui_timeout_cancelled"] = self._timeout_cancel
        payload["gui_item"] = item
        self.taskFinished.emit(payload)
        self._log_message(f"执行结束：{item.get('label', '')}")
        self._reset_current()
        self.refresh_history()
        self.refresh_target()
        self._set_busy(False)

    def _reset_current(self) -> None:
        self._stop_polling()
        self._scuffle_sequence = -1
        self._affix_sequence = -1
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
