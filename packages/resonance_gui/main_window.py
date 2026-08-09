"""Main window for the Resonance desktop GUI."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
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
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .bridge import RunnerBridge
from .config_repository import GuiPreferences, ResonanceConfigRepository
from .logic import (
    PC_GAME_NAME,
    extract_final_result,
    extract_run_id,
    extract_status,
    parse_inputs_json,
    pretty_json,
    render_result_text,
    trade_result_summary,
)
from .style import APP_STYLE
from .task_specs import CATEGORIES, TASKS_BY_ID, WORKBENCH_TASKS, TaskSpec
from .widgets import BattlePage, CommercePage
from .widgets.run_detail import RunDetailView


class ResonanceMainWindow(QMainWindow):
    requestInitialize = Signal()
    requestRefreshTasks = Signal()
    requestRefreshHistory = Signal()
    requestRefreshTarget = Signal()
    requestRunNow = Signal(str, object, object, float)
    requestRunPcTrade = Signal(object, float)
    requestPreviewPcTrade = Signal(object, float)
    requestRunPcPassenger = Signal(object, float)
    requestRunPcBattle = Signal(object, float)
    requestValidatePcBattle = Signal(object, float)
    requestEnqueueTask = Signal(str, object, object, float)
    requestClearQueue = Signal()
    requestCancelCurrent = Signal()
    requestBridgeClose = Signal()

    def __init__(
        self,
        *,
        bridge: RunnerBridge | None = None,
        settings: ResonanceConfigRepository | None = None,
        initialize_on_startup: bool = True,
    ) -> None:
        super().__init__()
        self._settings = settings or ResonanceConfigRepository()
        self._preferences = self._settings.load_preferences()
        self._bridge = bridge or RunnerBridge()
        self._bridge_thread = QThread(self)
        self._task_items: dict[str, QTreeWidgetItem] = {}
        self._history_rows: list[dict[str, Any]] = []
        self._busy = False
        self._active_game_name = ""
        self._active_kind = ""
        self._commerce_active = False
        self._commerce_stopping = False
        self._commerce_current_kind = ""
        self._commerce_pending: list[str] = []
        self._commerce_inputs: dict[str, dict[str, Any]] = {}
        self._current_task: TaskSpec = TASKS_BY_ID.get(self._preferences.last_task_id) or WORKBENCH_TASKS[0]

        self.setWindowTitle("Aura 雷索纳斯控制台")
        self.setMinimumSize(1040, 680)
        self.resize(1280, 820)
        self._build_ui()
        self._wire_bridge()
        self._select_task(self._current_task.task_id)
        if initialize_on_startup:
            QTimer.singleShot(0, self.requestInitialize.emit)

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("appRoot")
        root.setStyleSheet(APP_STYLE)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_navigation())

        self.page_stack = QStackedWidget(root)
        self.commerce_page = CommercePage(self._settings, self.page_stack)
        self.trade_page = self.commerce_page.trade_page
        self.passenger_page = self.commerce_page.passenger_page
        self.battle_page = BattlePage(self._settings, self.page_stack)
        self.workbench_page = self._build_workbench_page()
        self.history_page = self._build_history_page()
        self.settings_page = self._build_settings_page()
        for page in (
            self.commerce_page,
            self.battle_page,
            self.workbench_page,
            self.history_page,
            self.settings_page,
        ):
            self.page_stack.addWidget(page)
        layout.addWidget(self.page_stack, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("雷索纳斯 GUI 就绪")
        self._switch_page(0)

        self.trade_page.startRequested.connect(self._run_pc_trade)
        self.trade_page.previewRequested.connect(self._preview_pc_trade)
        self.trade_page.cancelRequested.connect(self.requestCancelCurrent.emit)
        self.trade_page.refreshTargetRequested.connect(self.requestRefreshTarget.emit)
        self.passenger_page.startRequested.connect(self._run_pc_passenger)
        self.passenger_page.cancelRequested.connect(self.requestCancelCurrent.emit)
        self.passenger_page.refreshTargetRequested.connect(self.requestRefreshTarget.emit)
        self.commerce_page.overview_page.startRequested.connect(self._start_commerce_sequence)
        self.commerce_page.overview_page.stopRequested.connect(self._stop_commerce_sequence)
        self.battle_page.startRequested.connect(self._run_pc_battle)
        self.battle_page.validateRequested.connect(self._validate_pc_battle)
        self.battle_page.cancelRequested.connect(self.requestCancelCurrent.emit)
        self.battle_page.refreshTargetRequested.connect(self.requestRefreshTarget.emit)

    def _build_navigation(self) -> QWidget:
        nav = QFrame(self)
        nav.setObjectName("navigation")
        nav.setFixedWidth(168)
        layout = QVBoxLayout(nav)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(6)
        title = QLabel("AURA", nav)
        title.setObjectName("brandTitle")
        caption = QLabel("雷索纳斯控制台", nav)
        caption.setObjectName("brandCaption")
        layout.addWidget(title)
        layout.addWidget(caption)
        layout.addSpacing(20)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, text in enumerate(("跑商", "战斗", "任务工具", "历史", "设置")):
            button = QPushButton(text, nav)
            button.setCheckable(True)
            button.setProperty("nav", True)
            button.clicked.connect(lambda checked=False, page=index: self._navigate_to_page(page))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            layout.addWidget(button)
        layout.addStretch(1)
        runtime = QLabel("PC runtime\nWGC + SendInput", nav)
        runtime.setObjectName("brandCaption")
        layout.addWidget(runtime)
        return nav

    def _navigate_to_page(self, index: int) -> None:
        if index == 0:
            self.commerce_page.show_overview()
        self._switch_page(index)

    def _switch_page(self, index: int) -> None:
        self.page_stack.setCurrentIndex(index)
        if 0 <= index < len(self.nav_buttons):
            self.nav_buttons[index].setChecked(True)
        if index == 3:
            self.requestRefreshHistory.emit()

    def _build_workbench_page(self) -> QWidget:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 12)
        title = QLabel("任务工具", root)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        splitter = QSplitter(Qt.Orientation.Horizontal, root)
        layout.addWidget(splitter, 1)

        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 8, 8, 0)
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
        right_layout.setContentsMargins(8, 8, 0, 0)
        self.task_title = QLabel(right)
        self.task_title.setObjectName("sectionTitle")
        self.task_description = QLabel(right)
        self.task_description.setWordWrap(True)
        self.task_description.setProperty("caption", True)
        right_layout.addWidget(self.task_title)
        right_layout.addWidget(self.task_description)
        self.inputs_editor = QPlainTextEdit(right)
        self.inputs_editor.setTabChangesFocus(False)
        self.inputs_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        right_layout.addWidget(self.inputs_editor, 3)

        button_row = QHBoxLayout()
        self.run_button = QPushButton("运行", right)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self._run_selected_now)
        self.enqueue_button = QPushButton("加入队列", right)
        self.enqueue_button.clicked.connect(self._enqueue_selected)
        self.clear_queue_button = QPushButton("清空队列", right)
        self.clear_queue_button.clicked.connect(self.requestClearQueue.emit)
        self.cancel_button = QPushButton("取消当前", right)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(self.requestCancelCurrent.emit)
        for button in (self.run_button, self.enqueue_button, self.clear_queue_button, self.cancel_button):
            button_row.addWidget(button)
        button_row.addStretch(1)
        right_layout.addLayout(button_row)
        self.queue_label = QLabel("队列：空", right)
        self.queue_label.setProperty("caption", True)
        right_layout.addWidget(self.queue_label)
        self.run_detail = RunDetailView(right)
        right_layout.addWidget(self.run_detail, 2)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([270, 760])
        self.task_tree.currentItemChanged.connect(self._handle_task_selection_changed)
        return root

    def _build_history_page(self) -> QWidget:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 12)
        top = QHBoxLayout()
        title = QLabel("运行历史", root)
        title.setObjectName("pageTitle")
        top.addWidget(title)
        top.addStretch(1)
        self.history_filter = QComboBox(root)
        self.history_filter.addItem("全部类型", "all")
        self.history_filter.addItem("跑商", "trade")
        self.history_filter.addItem("客运", "passenger")
        self.history_filter.addItem("战斗", "battle")
        self.history_filter.currentIndexChanged.connect(self._render_history)
        top.addWidget(self.history_filter)
        refresh = QPushButton("刷新历史", root)
        refresh.clicked.connect(self.requestRefreshHistory.emit)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.history_table = QTableWidget(0, 7, root)
        self.history_table.setHorizontalHeaderLabels(
            ["CID", "状态", "类型 / 摘要", "结果", "开始时间", "时长", "任务"]
        )
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.cellDoubleClicked.connect(self._open_history_row)
        layout.addWidget(self.history_table, 1)
        hint = QLabel("双击记录可在对应功能页打开只读结果", root)
        hint.setProperty("caption", True)
        layout.addWidget(hint)
        return root

    def _build_settings_page(self) -> QWidget:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 12)
        title = QLabel("设置", root)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
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
        self.requestRefreshTarget.connect(self._bridge.refresh_target)
        self.requestRunNow.connect(self._bridge.run_task_now)
        self.requestRunPcTrade.connect(self._bridge.run_pc_trade)
        self.requestPreviewPcTrade.connect(self._bridge.preview_pc_trade)
        self.requestRunPcPassenger.connect(self._bridge.run_pc_passenger)
        self.requestRunPcBattle.connect(self._bridge.run_pc_battle)
        self.requestValidatePcBattle.connect(self._bridge.validate_pc_battle)
        self.requestEnqueueTask.connect(self._bridge.enqueue_task)
        self.requestClearQueue.connect(self._bridge.clear_queue)
        self.requestCancelCurrent.connect(self._bridge.cancel_current)
        self.requestBridgeClose.connect(self._bridge.close)

        self._bridge.tasksLoaded.connect(self._on_tasks_loaded)
        self._bridge.historyLoaded.connect(self._on_history_loaded)
        self._bridge.queueChanged.connect(self._on_queue_changed)
        self._bridge.taskStarted.connect(self._on_task_started)
        self._bridge.taskDispatched.connect(self._on_task_dispatched)
        self._bridge.runUpdated.connect(self._on_run_updated)
        self._bridge.tradeProgress.connect(self.trade_page.apply_progress)
        self._bridge.passengerProgress.connect(self.passenger_page.apply_progress)
        self._bridge.targetStatusChanged.connect(self.trade_page.set_target_status)
        self._bridge.targetStatusChanged.connect(self.passenger_page.set_target_status)
        self._bridge.targetStatusChanged.connect(self.battle_page.set_target_status)
        self._bridge.cancelRequested.connect(self.trade_page.cancel_requested)
        self._bridge.cancelRequested.connect(self.passenger_page.cancel_requested)
        self._bridge.cancelRequested.connect(self.battle_page.cancel_requested)
        self._bridge.cancelRequested.connect(self._on_commerce_cancel_requested)
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

    def _run_pc_trade(self, inputs: object, _unused_timeout: float) -> None:
        self.requestRunPcTrade.emit(inputs, float(self.timeout_spin.value()))

    def _preview_pc_trade(self, inputs: object, _unused_timeout: float) -> None:
        self.requestPreviewPcTrade.emit(inputs, float(self.timeout_spin.value()))

    def _run_pc_passenger(self, inputs: object, _unused_timeout: float) -> None:
        self.requestRunPcPassenger.emit(inputs, float(self.timeout_spin.value()))

    def _start_commerce_sequence(self, run_trade: bool, run_passenger: bool) -> None:
        if self._busy or self._commerce_active or not (run_trade or run_passenger):
            return

        snapshots: dict[str, dict[str, Any]] = {}
        if run_trade:
            try:
                snapshots["trade"] = self.trade_page.collect_inputs()
            except ValueError as exc:
                self.commerce_page.show_trade()
                QMessageBox.warning(self, "货运参数错误", str(exc))
                return
        if run_passenger:
            try:
                snapshots["passenger"] = self.passenger_page.collect_inputs()
            except ValueError as exc:
                self.commerce_page.show_passenger()
                QMessageBox.warning(self, "客运参数错误", str(exc))
                return

        if "trade" in snapshots:
            self._settings.save_trade_inputs(snapshots["trade"])
        if "passenger" in snapshots:
            self._settings.save_passenger_inputs(snapshots["passenger"])

        self._commerce_inputs = snapshots
        self._commerce_pending = [
            kind for kind in ("trade", "passenger") if kind in snapshots
        ]
        self._commerce_active = True
        self._commerce_stopping = False
        self._commerce_current_kind = ""
        self.trade_page.set_busy(True)
        self.passenger_page.set_busy(True)
        self.run_button.setEnabled(False)
        self.enqueue_button.setEnabled(False)
        self.commerce_page.overview_page.set_running(True)
        self._dispatch_next_commerce_task()

    def _dispatch_next_commerce_task(self) -> None:
        if not self._commerce_active or self._commerce_stopping:
            return
        if not self._commerce_pending:
            self._finish_commerce_sequence()
            return

        kind = self._commerce_pending.pop(0)
        self._commerce_current_kind = kind
        inputs = dict(self._commerce_inputs[kind])
        timeout = float(self.timeout_spin.value())
        if kind == "trade":
            self.requestRunPcTrade.emit(inputs, timeout)
        else:
            self.requestRunPcPassenger.emit(inputs, timeout)

    def _stop_commerce_sequence(self) -> None:
        if not self._commerce_active:
            return
        self._commerce_pending.clear()
        self._commerce_stopping = True
        self.commerce_page.overview_page.set_stopping()
        if self._commerce_current_kind:
            self.requestCancelCurrent.emit()
        elif not self._busy:
            self._finish_commerce_sequence()

    def _abort_commerce_sequence(self, *, cancel_current: bool) -> None:
        if not self._commerce_active:
            return
        self._commerce_pending.clear()
        self._commerce_stopping = True
        self.commerce_page.overview_page.set_stopping()
        if cancel_current and self._commerce_current_kind:
            self.requestCancelCurrent.emit()
        if not self._busy and not self._commerce_current_kind:
            self._finish_commerce_sequence()

    def _finish_commerce_sequence(self) -> None:
        self._commerce_active = False
        self._commerce_stopping = False
        self._commerce_current_kind = ""
        self._commerce_pending.clear()
        self._commerce_inputs.clear()
        self.trade_page.set_busy(self._busy)
        self.passenger_page.set_busy(self._busy)
        self.run_button.setEnabled(not self._busy)
        self.enqueue_button.setEnabled(True)
        self.commerce_page.overview_page.set_running(False)
        self.commerce_page.overview_page.set_external_busy(self._busy)

    def _on_commerce_cancel_requested(self, _payload: dict[str, Any]) -> None:
        if self._commerce_active:
            self._abort_commerce_sequence(cancel_current=False)

    def _run_pc_battle(self, inputs: object, _unused_timeout: float) -> None:
        self.requestRunPcBattle.emit(inputs, float(self.timeout_spin.value()))

    def _validate_pc_battle(self, inputs: object, _unused_timeout: float) -> None:
        self.requestValidatePcBattle.emit(inputs, float(self.timeout_spin.value()))

    def _run_selected_now(self) -> None:
        inputs = self._collect_inputs()
        if inputs is not None:
            task = self._current_task
            self.requestRunNow.emit(task.task_ref, inputs, task.title, float(self.timeout_spin.value()))

    def _enqueue_selected(self) -> None:
        inputs = self._collect_inputs()
        if inputs is not None:
            task = self._current_task
            self.requestEnqueueTask.emit(task.task_ref, inputs, task.title, float(self.timeout_spin.value()))

    def _on_tasks_loaded(self, rows: list[dict[str, Any]]) -> None:
        refs = {str(row.get("task_ref") or "") for row in rows}
        missing = [task.task_ref for task in WORKBENCH_TASKS if task.task_ref not in refs]
        self.statusBar().showMessage(
            f"任务列表已加载，工作台缺失 {len(missing)} 个引用" if missing else f"任务列表已加载：{len(rows)} 个"
        )

    def _on_history_loaded(self, rows: list[dict[str, Any]]) -> None:
        self._history_rows = list(rows[: int(self._preferences.history_limit)])
        self._render_history()

    def _render_history(self, *_args: object) -> None:
        selected_kind = (
            str(self.history_filter.currentData() or "all")
            if hasattr(self, "history_filter")
            else "all"
        )
        visible_rows = [
            row
            for row in self._history_rows
            if selected_kind == "all" or self._history_kind(row) == selected_kind
        ]
        self._visible_history_rows = visible_rows
        self.history_table.setRowCount(0)
        for row in visible_rows:
            kind = self._history_kind(row)
            index = self.history_table.rowCount()
            self.history_table.insertRow(index)
            duration_ms = row.get("duration_ms")
            if kind == "battle":
                summary_text = "战斗任务单"
                result_text = "查看执行详情"
                type_label = "战斗"
            elif kind == "passenger":
                result = extract_final_result(row)
                summary_text = "海角城 ↔ 岚心城"
                result_text = str(result.get("total_revenue") or "--")
                type_label = "客运"
            else:
                summary = trade_result_summary(row)
                city_path = summary.get("city_path") or []
                summary_text = " -> ".join(str(city) for city in city_path) or "跑商任务"
                result_text = str(summary.get("expected_profit") or "--")
                type_label = "跑商"
            values = [
                extract_run_id(row),
                extract_status(row),
                f"{type_label} · {summary_text}",
                result_text,
                str(row.get("started_at") or row.get("created_at") or ""),
                self._format_duration(duration_ms),
                str(row.get("task_name") or row.get("task_ref") or ""),
            ]
            for col, value in enumerate(values):
                self.history_table.setItem(index, col, QTableWidgetItem(value))
    def _open_history_row(self, row: int, column: int) -> None:
        del column
        visible_rows = getattr(self, "_visible_history_rows", self._history_rows)
        if 0 <= row < len(visible_rows):
            payload = visible_rows[row]
            history_kind = self._history_kind(payload)
            if history_kind == "battle":
                self.battle_page.show_history_result(payload)
                self._switch_page(1)
            elif history_kind == "passenger":
                self.passenger_page.show_history_result(payload)
                self.commerce_page.show_passenger()
                self._switch_page(0)
            else:
                self.trade_page.show_history_result(payload)
                self.commerce_page.show_trade()
                self._switch_page(0)

    @staticmethod
    def _history_kind(row: dict[str, Any]) -> str:
        task_identity = " ".join(
            str(row.get(key) or "")
            for key in ("task_name", "task_ref", "task_id")
        ).lower()
        if "passenger" in task_identity:
            return "passenger"
        return "battle" if "battle" in task_identity else "trade"

    def _on_queue_changed(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            self.queue_label.setText("队列：空")
            return
        labels = ", ".join(str(row.get("label") or row.get("task_ref")) for row in rows[:5])
        suffix = "" if len(rows) <= 5 else f" 等 {len(rows)} 项"
        self.queue_label.setText(f"队列：{labels}{suffix}")

    def _on_task_started(self, payload: dict[str, Any]) -> None:
        self._active_game_name = str(payload.get("game_name") or "")
        self._active_kind = str(payload.get("kind") or "")
        self.statusBar().showMessage(f"派发中：{payload.get('label')}")
        if payload.get("game_name") != PC_GAME_NAME:
            self.run_detail.show_text(pretty_json(payload))

    def _on_task_dispatched(self, payload: dict[str, Any]) -> None:
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        if item.get("game_name") == PC_GAME_NAME:
            kind = str(item.get("kind") or "")
            if kind == "trade_preview":
                self.trade_page.begin_preview(payload)
                self.commerce_page.show_trade()
                self._switch_page(0)
            elif kind == "trade_run":
                self.trade_page.begin_run(payload)
                if not self._commerce_active:
                    self.commerce_page.show_trade()
                    self._switch_page(0)
            elif kind == "passenger_run":
                self.passenger_page.begin_run(payload)
                if not self._commerce_active:
                    self.commerce_page.show_passenger()
                    self._switch_page(0)
            elif kind == "battle_preview":
                self.battle_page.begin_validation(payload)
                self._switch_page(1)
            elif kind == "battle_run":
                self.battle_page.begin_run(payload)
                self._switch_page(1)
            else:
                self.run_detail.show_text(pretty_json(payload))
        else:
            self.run_detail.show_text(pretty_json(payload))

    def _on_run_updated(self, payload: dict[str, Any]) -> None:
        if self._active_game_name == PC_GAME_NAME and self._active_kind.startswith("trade_"):
            self.trade_page.update_run(payload)
        elif self._active_game_name == PC_GAME_NAME and self._active_kind.startswith("passenger_"):
            self.passenger_page.update_run(payload)
        elif self._active_game_name == PC_GAME_NAME and self._active_kind.startswith("battle_"):
            self.battle_page.update_run(payload)

    def _on_task_finished(self, payload: dict[str, Any]) -> None:
        self.statusBar().showMessage("任务执行结束")
        finished_kind = ""
        if self._active_game_name == PC_GAME_NAME:
            item = payload.get("gui_item") if isinstance(payload.get("gui_item"), dict) else {}
            kind = str(item.get("kind") or self._active_kind)
            finished_kind = kind
            if kind == "trade_preview":
                self.trade_page.finish_preview(payload)
            elif kind == "trade_run":
                self.trade_page.finish_run(payload)
            elif kind == "passenger_run":
                self.passenger_page.finish_run(payload)
            elif kind == "battle_preview":
                self.battle_page.finish_validation(payload)
            elif kind == "battle_run":
                self.battle_page.finish_run(payload)
        if self._commerce_active and finished_kind == f"{self._commerce_current_kind}_run":
            status = extract_status(payload)
            result = extract_final_result(payload)
            result_status = str(result.get("status") or "").strip().lower()
            succeeded = (
                status == "success"
                and result.get("success") is not False
                and result_status not in {"blocked", "error", "failed", "timeout", "cancelled"}
            )
            self._commerce_current_kind = ""
            if not succeeded:
                self._abort_commerce_sequence(cancel_current=False)
        self.run_detail.show_text(render_result_text(payload))

    def _on_task_failed(self, payload: dict[str, Any]) -> None:
        self.statusBar().showMessage(f"任务异常：{payload.get('error', '')}")
        stage = str(payload.get("stage") or "")
        if self._commerce_active:
            self._abort_commerce_sequence(
                cancel_current=stage != "cancel_task" and bool(self._commerce_current_kind)
            )
        if stage in {"run_pc_battle", "validate_pc_battle"}:
            self.battle_page.show_failure(payload)
        elif stage == "run_pc_passenger":
            self.passenger_page.show_failure(payload)
        elif stage in {"run_pc_trade", "preview_pc_trade"}:
            self.trade_page.show_failure(payload)
        elif (
            self._active_game_name == PC_GAME_NAME
            and self._active_kind.startswith("battle_")
        ):
            self.battle_page.show_failure(payload)
        elif (
            self._active_game_name == PC_GAME_NAME
            and self._active_kind.startswith("passenger_")
        ):
            self.passenger_page.show_failure(payload)
        elif (
            self._active_game_name == PC_GAME_NAME
            and self._active_kind.startswith("trade_")
        ):
            self.trade_page.show_failure(payload)
        self.run_detail.show_text(pretty_json(payload))

    def _on_busy_changed(self, busy: bool) -> None:
        self._busy = bool(busy)
        if not busy:
            self._active_game_name = ""
            self._active_kind = ""
        self.trade_page.set_busy(busy or self._commerce_active)
        self.passenger_page.set_busy(busy or self._commerce_active)
        self.battle_page.set_busy(busy)
        self.commerce_page.overview_page.set_external_busy(busy)
        self.run_button.setEnabled(not busy and not self._commerce_active)
        self.enqueue_button.setEnabled(not self._commerce_active)
        self.cancel_button.setEnabled(busy)
        if self._commerce_active and not busy:
            if self._commerce_stopping:
                self._finish_commerce_sequence()
            elif self._commerce_pending:
                self._dispatch_next_commerce_task()
            else:
                self._finish_commerce_sequence()

    def _on_log_message(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def _save_preferences(self) -> None:
        self._preferences = GuiPreferences(
            timeout_sec=float(self.timeout_spin.value()) if hasattr(self, "timeout_spin") else self._preferences.timeout_sec,
            history_limit=self._preferences.history_limit,
            last_task_id=self._current_task.task_id,
        )
        self._settings.save_preferences(self._preferences)

    @staticmethod
    def _format_duration(value: Any) -> str:
        try:
            seconds = int(float(value) / 1000)
        except (TypeError, ValueError):
            return "--"
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy:
            box = QMessageBox(self)
            box.setWindowTitle("任务仍在运行")
            box.setText("PC 自动化任务仍在运行。")
            stay = box.addButton("继续运行界面", QMessageBox.ButtonRole.RejectRole)
            cancel_and_exit = box.addButton("取消任务并退出", QMessageBox.ButtonRole.DestructiveRole)
            box.exec()
            if box.clickedButton() is stay:
                event.ignore()
                return
            if box.clickedButton() is cancel_and_exit:
                self.requestCancelCurrent.emit()
            else:
                event.ignore()
                return
        self._save_preferences()
        self.requestBridgeClose.emit()
        self._bridge_thread.quit()
        self._bridge_thread.wait(5000)
        super().closeEvent(event)


def create_main_window() -> ResonanceMainWindow:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication must exist before creating ResonanceMainWindow.")
    return ResonanceMainWindow()
