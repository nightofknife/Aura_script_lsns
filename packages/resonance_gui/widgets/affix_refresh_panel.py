"""Configure bounded affix refreshes and preserve matches for manual replacement."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..affix_catalog import decimal_value, eligible_affixes, normalize_affix_inputs
from ..config_repository import ResonanceConfigRepository


STAGES = {
    "preflight": "检查窗口和配置", "observe": "读取当前词缀", "read": "读取当前界面",
    "inspect": "检查当前候选", "evaluate": "判断词缀", "refresh": "刷新词缀",
    "refresh_confirm": "确认刷新", "wait_refresh": "等待读取刷新结果", "match": "判断词缀",
    "completed": "已结束",
}


class AffixRefreshPanel(QWidget):
    readRequested = Signal()
    runRequested = Signal(object)
    cancelRequested = Signal()

    def __init__(self, settings: ResonanceConfigRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._saved = settings.load_affix_refresh_inputs()
        self._catalog: list[dict[str, Any]] = []
        self._state: dict[str, Any] = {}
        self._runner_busy = self._task_running = self._stopping = False
        self._mode = self._last_stage = ""
        self._auto_read_needed = True
        self._auto_read_queued = False
        self._ocr_log_path: Path | None = None
        self._log_cid = ""
        self._log_offset = 0
        self._log_identity: tuple[int, int] | None = None
        self._log_pending = b""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        layout = QVBoxLayout(content)
        title = QLabel("刷新词缀", self)
        title.setObjectName("workflowTitle")
        layout.addWidget(title)
        note = QLabel("请先打开游戏中的词缀替换窗口。刷到目标后自动停止，替换由你手动完成。", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.read_button = QPushButton("重新读取界面", self)
        self.read_button.setToolTip("读取胶卷余额和当前候选，不选择旧词缀、不消耗胶卷。")
        self.read_button.clicked.connect(self._request_read)
        layout.addWidget(self.read_button)
        self.balance_label = QLabel("进入本页后自动读取胶卷余额和当前候选", self)
        self.balance_label.setWordWrap(True)
        self.balance_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.balance_label)
        form = QFormLayout()
        self.target_combo = QComboBox(self)
        self.target_combo.setEditable(True)
        self.target_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.target_combo.setMinimumContentsLength(16)
        self.target_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.target_combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.target_combo.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.target_combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        form.addRow("目标词缀（可搜索）", self.target_combo)
        self.comparison_combo = QComboBox(self)
        for label, value in (("任意数值", "any"), ("至少达到指定值", "gte"),
                             ("等于指定值", "eq"), ("满值", "max")):
            self.comparison_combo.addItem(label, value)
        self.comparison_combo.setCurrentIndex(self.comparison_combo.findData(self._saved["comparison"]))
        form.addRow("数值要求", self.comparison_combo)
        self.threshold_edit = QLineEdit(str(self._saved["threshold"]), self)
        self.threshold_edit.setPlaceholderText("十进制数值，例如 0.5")
        form.addRow("要求数值", self.threshold_edit)
        self.range_label = QLabel("选择词缀后显示目录数值范围和档位", self)
        self.range_label.setWordWrap(True)
        form.addRow("目录数值范围", self.range_label)
        self.film_spin = QSpinBox(self)
        self.film_spin.setRange(0, 9999)
        self.film_spin.setValue(self._saved["max_film_cost"])
        self.film_spin.setToolTip("0 表示仅检查已有候选，不消耗新胶卷。")
        form.addRow("胶卷消耗上限", self.film_spin)
        layout.addLayout(form)
        scope_note = QLabel("目录包含全部词缀，请选择该装备可获得的词缀。胶卷上限为 0 时仅检查已有候选。自动替换暂未开放。", self)
        scope_note.setWordWrap(True)
        layout.addWidget(scope_note)
        self.config_summary_label = QLabel("", self)
        self.status_label = QLabel("设好目标和胶卷上限后即可开始，任务会先检查游戏界面。", self)
        self.summary_label = QLabel("", self)
        for label in (self.config_summary_label, self.status_label, self.summary_label):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
        log_actions = QHBoxLayout()
        self.log_toggle = QPushButton("查看 OCR 日志", self)
        self.log_toggle.setCheckable(True)
        self.log_toggle.toggled.connect(self._toggle_logs)
        log_actions.addWidget(self.log_toggle)
        self.open_log_button = QPushButton("打开日志文件", self)
        self.open_log_button.clicked.connect(self._open_log)
        self.open_log_button.setEnabled(False)
        log_actions.addWidget(self.open_log_button)
        log_actions.addStretch(1)
        layout.addLayout(log_actions)
        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(150)
        self.log_view.setPlaceholderText("本次 OCR 原文、置信度和解析结果；完整记录保留在日志文件中。")
        self.log_view.setVisible(False)
        layout.addWidget(self.log_view)
        layout.addStretch(1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.cancel_button = QPushButton("停止", self)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(self._request_stop)
        self.run_button = QPushButton("开始刷新", self)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self._request_run)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.run_button)
        root.addLayout(actions)
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(500)
        self._log_timer.timeout.connect(self._read_ocr_log)
        self.target_combo.currentIndexChanged.connect(self._configuration_changed)
        self.target_combo.editTextChanged.connect(self._configuration_changed)
        self.comparison_combo.currentIndexChanged.connect(self._configuration_changed)
        self.threshold_edit.textChanged.connect(self._configuration_changed)
        self.film_spin.valueChanged.connect(self._configuration_changed)
        self._load_catalog()
        self._configuration_changed()

    def _load_catalog(self) -> None:
        try:
            self._catalog = eligible_affixes()
            if not self._catalog:
                raise ValueError("词缀目录为空，请更新资源。")
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self.status_label.setText(str(exc))
            return
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItem("请选择目标词缀", None)
        for row in self._catalog:
            label = str(row.get("description") or row.get("text") or row["id"]).replace("%s", "数值").replace("%%", "%")
            self.target_combo.addItem(label, row["id"])
        self.target_combo.setCurrentIndex(max(0, self.target_combo.findData(self._saved.get("target_affix_id"))))
        self.target_combo.blockSignals(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Hidden construction only loads local data; dispatch after showing.
        if not event.spontaneous() and not self._task_running:
            self._auto_read_needed = True
            self._queue_auto_read()

    def _queue_auto_read(self) -> None:
        if (not self._auto_read_needed or self._auto_read_queued
                or self._runner_busy or self._task_running or not self.isVisible()):
            return
        self._auto_read_queued = True
        QTimer.singleShot(0, self._auto_read)

    def _auto_read(self) -> None:
        self._auto_read_queued = False
        if (not self._auto_read_needed or self._runner_busy
                or self._task_running or not self.isVisible()):
            return
        self._request_read()

    def defer_auto_read(self) -> None:
        self._auto_read_needed = True

    def _configuration_changed(self, *_args: Any) -> None:
        target = self._target()
        self.range_label.setText(
            f"{target.get('min_value')} ～ {target.get('max_value')} {target.get('unit', '')}；档位间隔 {target.get('step')}"
            if target else "选择词缀后显示目录数值范围和档位")
        try:
            stable = self._stable_inputs()
            self._settings.save_affix_refresh_inputs(stable)
            self._saved = stable
        except ValueError:
            pass
        self._sync_controls()

    def _target(self) -> dict[str, Any] | None:
        if self.target_combo.currentText() != self.target_combo.itemText(self.target_combo.currentIndex()):
            return None
        target_id = self.target_combo.currentData()
        return next((row for row in self._catalog if row.get("id") == target_id), None)

    def _stable_inputs(self) -> dict[str, Any]:
        target = self._target()
        return normalize_affix_inputs({"target_affix_id": target.get("id") if target else self._saved.get("target_affix_id"),
                                       "comparison": self.comparison_combo.currentData(),
                                       "threshold": self.threshold_edit.text().strip(),
                                       "max_film_cost": self.film_spin.value(), "auto_replace": False})

    def collect_inputs(self) -> dict[str, Any]:
        target = self._target()
        if not target:
            raise ValueError("请从词缀目录中选择目标。")
        inputs = self._stable_inputs()
        if inputs["comparison"] in {"gte", "eq"}:
            minimum = decimal_value(str(target["min_value"]))
            maximum = decimal_value(str(target["max_value"]))
            step = decimal_value(str(target["step"]))
            value = decimal_value(inputs["threshold"])
            if not minimum <= value <= maximum:
                raise ValueError("要求数值超出目录中的词缀范围。")
            if inputs["comparison"] == "eq" and step > 0 and (value - minimum) % step:
                raise ValueError("要求数值不在词缀的数值档位中。")
        return inputs

    def _request_read(self) -> None:
        if not self._runner_busy and not self._task_running:
            self._auto_read_needed = False
            self.readRequested.emit()

    def _request_run(self) -> None:
        if self._runner_busy or self._task_running:
            return
        try:
            inputs = self.collect_inputs()
            self._settings.save_affix_refresh_inputs(inputs)
        except (ValueError, KeyError) as exc:
            self.status_label.setText(str(exc))
            return
        self.runRequested.emit(inputs)

    def _request_stop(self) -> None:
        self.mark_stopping()
        self.cancelRequested.emit()

    def mark_stopping(self, *_args: Any) -> None:
        if self._task_running:
            self._stopping = True
            self._render()
            self._sync_controls()

    def cancel_failed(self, message: str) -> None:
        if self._task_running:
            self._stopping = False
            self._state["message"] = f"停止请求未送达，可再次点击停止：{message}"
            self._render()
            self._sync_controls()

    def begin(self, mode: str, inputs: Mapping[str, Any] | None = None) -> None:
        self._mode = mode
        self._task_running = True
        self._stopping = False
        self._last_stage = "read" if mode == "read" else "preflight"
        self._state = {"status": "running", "stage": self._last_stage, "auto_replace": False}
        if mode == "run":
            self._state.update(film_spent=0, film_limit=(inputs or {}).get("max_film_cost", 0))
        self._log_timer.stop()
        self.log_view.clear()
        self._log_cid = ""
        self._log_offset = 0
        self._log_pending = b""
        self._log_identity = None
        self._ocr_log_path = None
        self.open_log_button.setEnabled(False)
        self._render()
        self._sync_controls()

    def _toggle_logs(self, expanded: bool) -> None:
        self.log_view.setVisible(expanded)
        self.log_toggle.setText("收起 OCR 日志" if expanded else "查看 OCR 日志")
        if expanded:
            self._read_ocr_log()

    def _open_log(self) -> None:
        if self._ocr_log_path and self._ocr_log_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._ocr_log_path)))

    def _read_ocr_log(self) -> None:
        if not self._ocr_log_path or not self._log_cid:
            return
        try:
            stat = self._ocr_log_path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self._log_identity != identity or stat.st_size < self._log_offset:
                self._log_offset = 0
                self._log_pending = b""
            self._log_identity = identity
            with self._ocr_log_path.open("rb") as stream:
                stream.seek(self._log_offset)
                chunk = stream.read(256 * 1024)
                self._log_offset = stream.tell()
            parts = (self._log_pending + chunk).split(b"\n")
            self._log_pending = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", errors="replace").rstrip("\r")
                if "[AffixRefreshOCR]" in line and (
                        f"[cid:{self._log_cid}]" in line or f"cid={self._log_cid} " in line):
                    self.log_view.appendPlainText(line)
            self.open_log_button.setEnabled(True)
            if self._log_offset < stat.st_size:
                # Drain older chunks without blocking the GUI or rereading the
                # whole file; also finish draining after a terminal result.
                QTimer.singleShot(0, self._read_ocr_log)
        except OSError:
            self.open_log_button.setEnabled(False)

    def apply_progress(self, event: Mapping[str, Any]) -> None:
        if not self._task_running:
            return
        payload = event.get("payload", event)
        if isinstance(payload, Mapping):
            self._merge(payload)
            self._render()

    def _merge(self, payload: Mapping[str, Any]) -> None:
        self._state.update(payload)
        stage = str(payload.get("stage") or "")
        if stage and stage not in {"completed", "failed", "cancelled", "stopped"}:
            self._last_stage = stage
        path, cid = payload.get("ocr_log_path"), str(payload.get("cid") or self._log_cid)
        if isinstance(path, str) and path and cid:
            resolved = Path(path).resolve()
            if resolved != self._ocr_log_path or cid != self._log_cid:
                self._ocr_log_path, self._log_cid = resolved, cid
                self._log_offset, self._log_pending, self._log_identity = 0, b"", None
            self.open_log_button.setEnabled(resolved.is_file())
            self.open_log_button.setToolTip(str(resolved))
            if self._task_running and not self._log_timer.isActive():
                self._log_timer.start()

    def apply_result(self, payload: Mapping[str, Any]) -> None:
        terminal = dict(payload)
        previous_spent, final_spent = self._state.get("film_spent"), terminal.get("film_spent")
        if type(previous_spent) is int and type(final_spent) is int:
            terminal["film_spent"] = max(previous_spent, final_spent)
        if terminal.get("framework_interrupted"):
            reserved = self._state.get("film_reserved")
            if type(reserved) is int:
                final_reserved = terminal.get("film_reserved")
                terminal["film_reserved"] = max(reserved, final_reserved if type(final_reserved) is int else 0)
            terminal["result_uncertain"] = bool(self._state.get("result_uncertain") or terminal.get("result_uncertain")
                                                or terminal.get("film_reserved"))
        self._merge(terminal)
        self._task_running = self._stopping = False
        self._log_timer.stop()
        self._read_ocr_log()
        if self._mode == "read":
            snapshot = payload.get("snapshot")
            if payload.get("success") is True and isinstance(snapshot, Mapping) and snapshot.get("valid") is True:
                self._state["film_remaining"] = snapshot.get("film_remaining")
                self._state["current_candidate"] = snapshot.get("candidate")
        self._render()
        self._sync_controls()

    def show_error(self, message: str, *, cancelled: bool = False) -> None:
        self.apply_result({"success": False, "status": "cancelled" if cancelled else "failed", "reason": message,
                           "message": message, "framework_interrupted": True})

    def _render(self) -> None:
        state = self._state
        status = str(state.get("status") or "running")
        if self._stopping:
            label = "正在停止，等待当前操作结果"
        elif self._task_running:
            label = "正在读取界面" if self._mode == "read" else "正在刷新"
        elif self._mode == "read" and state.get("success") is True:
            label = "读取完成，命中目标后将停止并保留候选"
        elif state.get("matched") is True and status == "completed" and state.get("reason") == "target_found":
            label = "已命中目标，请手动替换"
        elif state.get("reason") in {"budget_exhausted", "budget_reached", "film_limit_reached"}:
            label = "预算用尽，未命中目标"
        elif status in {"cancelled", "stopped"}:
            label = "已停止"
        else:
            label = "执行失败，已停止"
        stage = STAGES.get(self._last_stage, self._last_stage)
        message = state.get("message") or state.get("error") or state.get("reason")
        self.status_label.setText(label + (f" · {stage}" if stage else "") + (f"\n{message}" if message else ""))
        balance = state.get("film_remaining")
        self.balance_label.setText(f"剩余胶卷：{'未确认' if balance is None else balance}")
        lines = []
        if "film_spent" in state:
            lines.append(f"已消耗胶卷：{state['film_spent']} / {state.get('film_limit', '?')}")
        if state.get("film_reserved"):
            lines.append(f"已提交、待确认消耗：{state['film_reserved']}")
        if state.get("result_uncertain"):
            lines.append("操作结果尚未确认，请查看保留的游戏画面。")
        candidate = state.get("current_candidate")
        lines.append(f"当前候选：{candidate.get('text', '')}" if isinstance(candidate, Mapping) else "当前候选：暂无或未读取")
        if state.get("matched") and status == "completed":
            lines.append("候选已保留在游戏界面，请手动选择旧词缀并确认替换。")
        self.summary_label.setText("\n".join(lines))

    def set_runner_busy(self, busy: bool) -> None:
        self._runner_busy = bool(busy)
        self._sync_controls()
        if not busy:
            self._queue_auto_read()

    def _sync_controls(self, *_args: Any) -> None:
        editable = not self._runner_busy and not self._task_running
        self.read_button.setEnabled(editable)
        for widget in (self.target_combo, self.comparison_combo, self.film_spin):
            widget.setEnabled(editable)
        self.threshold_edit.setEnabled(editable and self.comparison_combo.currentData() in {"gte", "eq"})
        try:
            inputs = self.collect_inputs()
            error = ""
            target = self._target() or {}
            threshold = inputs["threshold"]
            number = f" {threshold}{target.get('unit', '')}" if threshold else ""
            self.config_summary_label.setText(f"{self.target_combo.currentText()} · {self.comparison_combo.currentText()}{number} · 最多消耗 {inputs['max_film_cost']} 个胶卷 · 命中后停止")
        except (ValueError, KeyError, TypeError) as exc:
            error = str(exc)
            self.config_summary_label.setText(error)
        self.run_button.setEnabled(editable and not error)
        self.run_button.setToolTip(error or "启动后先检查窗口和胶卷余额，再自动刷新；命中后停止。")
        self.cancel_button.setEnabled(self._task_running and not self._stopping)
