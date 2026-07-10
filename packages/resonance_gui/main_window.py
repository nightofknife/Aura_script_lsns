"""Main window for the Resonance desktop GUI."""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .bridge import RunnerBridge
from .config_repository import GuiPreferences, ResonanceConfigRepository
from .logic import extract_run_id, extract_status, parse_inputs_json, pretty_json, render_result_text
from .task_specs import CATEGORIES, TASKS_BY_ID, WORKBENCH_TASKS, TaskSpec
from .widgets.run_detail import RunDetailView


class ResonanceMainWindow(QMainWindow):
    requestInitialize = Signal()
    requestRefreshTasks = Signal()
    requestRefreshHistory = Signal()
    requestRunNow = Signal(str, object, object, float)
    requestEnqueueTask = Signal(str, object, object, float)
    requestClearQueue = Signal()
    requestCancelCurrent = Signal()
    requestBridgeClose = Signal()

    def __init__(self, *, bridge: RunnerBridge | None = None, settings: ResonanceConfigRepository | None = None) -> None:
        super().__init__()
        self._settings = settings or ResonanceConfigRepository()
        self._preferences = self._settings.load_preferences()
        self._bridge = bridge or RunnerBridge()
        self._bridge_thread = QThread(self)
        self._task_items: dict[str, QTreeWidgetItem] = {}
        self._current_task: TaskSpec = TASKS_BY_ID.get(self._preferences.last_task_id) or WORKBENCH_TASKS[0]

        self.setWindowTitle("Aura 雷索纳斯控制台")
        self.resize(1180, 760)
        self._build_ui()
        self._wire_bridge()
        self._select_task(self._current_task.task_id)
        QTimer.singleShot(0, self.requestInitialize.emit)

    def _build_ui(self) -> None:
        tabs = QTabWidget(self)
        tabs.addTab(self._build_workbench_tab(), "工作台")
        tabs.addTab(self._build_history_tab(), "历史")
        tabs.addTab(self._build_settings_tab(), "设置")
        self.setCentralWidget(tabs)
        self.statusBar().showMessage("雷索纳斯 GUI 就绪")

    def _build_workbench_tab(self) -> QWidget:
        root = QWidget(self)
        layout = QHBoxLayout(root)
        splitter = QSplitter(Qt.Orientation.Horizontal, root)
        layout.addWidget(splitter)

        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        self.task_tree = QTreeWidget(left)
        self.task_tree.setHeaderHidden(True)
        self.task_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._populate_task_tree()
        left_layout.addWidget(self.task_tree)

        refresh_button = QPushButton("刷新任务", left)
        refresh_button.clicked.connect(self.requestRefreshTasks.emit)
        left_layout.addWidget(refresh_button)

        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)

        self.task_title = QLabel(right)
        self.task_title.setObjectName("taskTitle")
        self.task_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right_layout.addWidget(self.task_title)

        self.task_description = QLabel(right)
        self.task_description.setWordWrap(True)
        self.task_description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right_layout.addWidget(self.task_description)

        self.inputs_editor = QPlainTextEdit(right)
        self.inputs_editor.setTabChangesFocus(False)
        self.inputs_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        right_layout.addWidget(self.inputs_editor, 3)

        button_row = QHBoxLayout()
        self.run_button = QPushButton("运行", right)
        self.run_button.clicked.connect(self._run_selected_now)
        self.enqueue_button = QPushButton("加入队列", right)
        self.enqueue_button.clicked.connect(self._enqueue_selected)
        self.clear_queue_button = QPushButton("清空队列", right)
        self.clear_queue_button.clicked.connect(self.requestClearQueue.emit)
        self.cancel_button = QPushButton("取消当前", right)
        self.cancel_button.clicked.connect(self.requestCancelCurrent.emit)
        for button in (self.run_button, self.enqueue_button, self.clear_queue_button, self.cancel_button):
            button_row.addWidget(button)
        button_row.addStretch(1)
        right_layout.addLayout(button_row)

        self.queue_label = QLabel("队列：空", right)
        right_layout.addWidget(self.queue_label)

        separator = QFrame(right)
        separator.setFrameShape(QFrame.Shape.HLine)
        right_layout.addWidget(separator)

        self.run_detail = RunDetailView(right)
        right_layout.addWidget(self.run_detail, 2)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        self.task_tree.currentItemChanged.connect(self._handle_task_selection_changed)
        return root

    def _build_history_tab(self) -> QWidget:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        top = QHBoxLayout()
        refresh = QPushButton("刷新历史", root)
        refresh.clicked.connect(self.requestRefreshHistory.emit)
        top.addWidget(refresh)
        top.addStretch(1)
        layout.addLayout(top)

        self.history_table = QTableWidget(0, 5, root)
        self.history_table.setHorizontalHeaderLabels(["Run", "状态", "任务", "开始时间", "结束时间"])
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.history_table)
        return root

    def _build_settings_tab(self) -> QWidget:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        form = QFormLayout()
        self.timeout_spin = QDoubleSpinBox(root)
        self.timeout_spin.setRange(0.0, 7200.0)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSpecialValueText("无限等待")
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setValue(float(self._preferences.timeout_sec))
        form.addRow("任务等待超时", self.timeout_spin)
        layout.addLayout(form)
        layout.addStretch(1)
        self.timeout_spin.valueChanged.connect(self._save_preferences)
        return root

    def _populate_task_tree(self) -> None:
        self.task_tree.clear()
        self._task_items.clear()
        by_category: dict[str, list[TaskSpec]] = {category: [] for category in CATEGORIES}
        for task in WORKBENCH_TASKS:
            by_category.setdefault(task.category, []).append(task)
        for category, tasks in by_category.items():
            category_item = QTreeWidgetItem([category])
            category_item.setFlags(category_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.task_tree.addTopLevelItem(category_item)
            for task in tasks:
                task_item = QTreeWidgetItem([task.title])
                task_item.setData(0, Qt.ItemDataRole.UserRole, task.task_id)
                category_item.addChild(task_item)
                self._task_items[task.task_id] = task_item
            category_item.setExpanded(True)

    def _wire_bridge(self) -> None:
        self._bridge.moveToThread(self._bridge_thread)
        self.requestInitialize.connect(self._bridge.initialize)
        self.requestRefreshTasks.connect(self._bridge.refresh_tasks)
        self.requestRefreshHistory.connect(self._bridge.refresh_history)
        self.requestRunNow.connect(self._bridge.run_task_now)
        self.requestEnqueueTask.connect(self._bridge.enqueue_task)
        self.requestClearQueue.connect(self._bridge.clear_queue)
        self.requestCancelCurrent.connect(self._bridge.cancel_current)
        self.requestBridgeClose.connect(self._bridge.close)

        self._bridge.tasksLoaded.connect(self._on_tasks_loaded)
        self._bridge.historyLoaded.connect(self._on_history_loaded)
        self._bridge.queueChanged.connect(self._on_queue_changed)
        self._bridge.taskStarted.connect(self._on_task_started)
        self._bridge.taskFinished.connect(self._on_task_finished)
        self._bridge.taskFailed.connect(self._on_task_failed)
        self._bridge.busyChanged.connect(self._on_busy_changed)
        self._bridge.logMessage.connect(self._on_log_message)
        self._bridge_thread.start()

    def _handle_task_selection_changed(self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None) -> None:
        del previous
        if current is None:
            return
        task_id = current.data(0, Qt.ItemDataRole.UserRole)
        if not task_id:
            return
        self._current_task = TASKS_BY_ID[str(task_id)]
        self._render_current_task()
        self._save_preferences()

    def _select_task(self, task_id: str) -> None:
        item = self._task_items.get(task_id) or next(iter(self._task_items.values()))
        self.task_tree.setCurrentItem(item)
        self._current_task = TASKS_BY_ID[str(item.data(0, Qt.ItemDataRole.UserRole))]
        self._render_current_task()

    def _render_current_task(self) -> None:
        task = self._current_task
        self.task_title.setText(f"{task.category} / {task.title}")
        self.task_description.setText(f"{task.description}\n{task.task_ref}")
        self.inputs_editor.setPlainText(pretty_json(task.default_inputs))

    def _collect_inputs(self) -> dict[str, Any] | None:
        try:
            return parse_inputs_json(self.inputs_editor.toPlainText())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "参数错误", str(exc))
            return None

    def _run_selected_now(self) -> None:
        inputs = self._collect_inputs()
        if inputs is None:
            return
        task = self._current_task
        self.requestRunNow.emit(task.task_ref, inputs, task.title, float(self.timeout_spin.value()))

    def _enqueue_selected(self) -> None:
        inputs = self._collect_inputs()
        if inputs is None:
            return
        task = self._current_task
        self.requestEnqueueTask.emit(task.task_ref, inputs, task.title, float(self.timeout_spin.value()))

    def _on_tasks_loaded(self, rows: list[dict[str, Any]]) -> None:
        refs = {str(row.get("task_ref") or "") for row in rows}
        missing = [task.task_ref for task in WORKBENCH_TASKS if task.task_ref not in refs]
        if missing:
            self.statusBar().showMessage(f"任务列表已加载，工作台缺失 {len(missing)} 个引用")
            return
        self.statusBar().showMessage(f"任务列表已加载：{len(rows)} 个")

    def _on_history_loaded(self, rows: list[dict[str, Any]]) -> None:
        self.history_table.setRowCount(0)
        for row in rows[: int(self._preferences.history_limit)]:
            index = self.history_table.rowCount()
            self.history_table.insertRow(index)
            values = [
                extract_run_id(row),
                extract_status(row),
                str(row.get("task_name") or row.get("task_ref") or ""),
                str(row.get("started_at") or row.get("created_at") or ""),
                str(row.get("ended_at") or row.get("updated_at") or ""),
            ]
            for col, value in enumerate(values):
                self.history_table.setItem(index, col, QTableWidgetItem(value))

    def _on_queue_changed(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            self.queue_label.setText("队列：空")
            return
        labels = ", ".join(str(row.get("label") or row.get("task_ref")) for row in rows[:5])
        suffix = "" if len(rows) <= 5 else f" 等 {len(rows)} 项"
        self.queue_label.setText(f"队列：{labels}{suffix}")

    def _on_task_started(self, payload: dict[str, Any]) -> None:
        self.statusBar().showMessage(f"运行中：{payload.get('label')}")
        self.run_detail.show_text(pretty_json(payload))

    def _on_task_finished(self, payload: dict[str, Any]) -> None:
        self.statusBar().showMessage("任务完成")
        self.run_detail.show_text(render_result_text(payload))

    def _on_task_failed(self, payload: dict[str, Any]) -> None:
        self.statusBar().showMessage("任务失败")
        self.run_detail.show_text(pretty_json(payload))

    def _on_busy_changed(self, busy: bool) -> None:
        self.run_button.setEnabled(not busy)
        self.enqueue_button.setEnabled(True)

    def _on_log_message(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def _save_preferences(self) -> None:
        self._preferences = GuiPreferences(
            timeout_sec=float(self.timeout_spin.value()) if hasattr(self, "timeout_spin") else self._preferences.timeout_sec,
            history_limit=self._preferences.history_limit,
            last_task_id=self._current_task.task_id,
        )
        self._settings.save_preferences(self._preferences)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_preferences()
        self.requestBridgeClose.emit()
        self._bridge_thread.quit()
        self._bridge_thread.wait(3000)
        super().closeEvent(event)


def create_main_window() -> ResonanceMainWindow:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication must exist before creating ResonanceMainWindow.")
    return ResonanceMainWindow()
