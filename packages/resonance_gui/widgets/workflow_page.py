"""Three-column workflow dashboard for the Resonance PC GUI."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import QEasingCurve, QEvent, QMimeData, QPoint, QPropertyAnimation, QRect, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config_repository import ResonanceConfigRepository
from ..logic import PASSENGER_STAGE_LABELS, TRADE_STAGE_LABELS


WORKFLOW_TASKS: tuple[tuple[str, str], ...] = (
    ("startup", "进入主界面"),
    ("commerce", "跑商"),
    ("battle", "自动战斗"),
    ("close", "关闭游戏"),
)


class _TaskRow(QFrame):
    selected = Signal(str)

    def __init__(self, task_id: str, title: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.task_id = task_id
        self.setObjectName("workflowTaskRow")
        self.setProperty("selected", False)
        self.setMinimumHeight(52)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._drag_start = QPoint()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        self.enabled_check = QCheckBox(self)
        self.number_label = QLabel("", self)
        self.number_label.setObjectName("taskNumber")
        self.number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.number_label.setFixedSize(26, 26)
        self.name_label = QLabel(title, self)
        self.name_label.setObjectName("taskName")
        self.status_label = QLabel("○", self)
        self.status_label.setObjectName("taskStatus")
        self.drag_handle = QLabel("⋮⋮", self)
        self.drag_handle.setObjectName("taskDragHandle")
        self.drag_handle.setToolTip("拖动调整顺序")
        self.drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
        self.drag_handle.installEventFilter(self)
        layout.addWidget(self.enabled_check)
        layout.addWidget(self.number_label)
        layout.addWidget(self.name_label, 1)
        layout.addWidget(self.status_label)
        layout.addWidget(self.drag_handle)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.selected.emit(self.task_id)
        super().mousePressEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.drag_handle and event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self.selected.emit(self.task_id)
                self._drag_start = event.position().toPoint()
                return True
        if watched is self.drag_handle and event.type() == QEvent.Type.MouseMove:
            if event.buttons() & Qt.MouseButton.LeftButton:
                if (event.position().toPoint() - self._drag_start).manhattanLength() >= 8:
                    self._start_drag()
                return True
        return super().eventFilter(watched, event)

    def _start_drag(self) -> None:
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData("application/x-aura-workflow-task", self.task_id.encode("utf-8"))
        drag.setMimeData(mime)
        pixmap = self.grab()
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() - 16, pixmap.height() // 2))
        self.drag_handle.setCursor(Qt.CursorShape.ClosedHandCursor)
        drag.exec(Qt.DropAction.MoveAction)
        self.drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)


class _TaskRowsHost(QWidget):
    taskDropped = Signal(str, int)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._drop_indicator = QFrame(self)
        self._drop_indicator.setObjectName("taskDropIndicator")
        self._drop_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._drop_indicator.hide()
        self._indicator_animation = QPropertyAnimation(self._drop_indicator, b"geometry", self)
        self._indicator_animation.setDuration(110)
        self._indicator_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat("application/x-aura-workflow-task"):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat("application/x-aura-workflow-task"):
            target = self._drop_target(event.position().y())
            self._show_drop_indicator(target)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drop_indicator.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasFormat("application/x-aura-workflow-task"):
            return
        target = self._drop_target(event.position().y())
        task_id = bytes(event.mimeData().data("application/x-aura-workflow-task")).decode("utf-8")
        self._drop_indicator.hide()
        self.taskDropped.emit(task_id, target)
        event.acceptProposedAction()

    def _rows(self) -> list[_TaskRow]:
        return sorted(self.findChildren(_TaskRow), key=lambda row: row.geometry().top())

    def _drop_target(self, y: float) -> int:
        rows = self._rows()
        for index, row in enumerate(rows):
            if y < row.geometry().center().y():
                return index
        return len(rows)

    def _show_drop_indicator(self, target: int) -> None:
        rows = self._rows()
        if not rows:
            return
        if target <= 0:
            y = rows[0].geometry().top() - 2
        elif target >= len(rows):
            y = rows[-1].geometry().bottom() + 3
        else:
            y = (rows[target - 1].geometry().bottom() + rows[target].geometry().top()) // 2
        target_geometry = QRect(4, int(y), max(self.width() - 8, 1), 3)
        if not self._drop_indicator.isVisible():
            self._drop_indicator.setGeometry(target_geometry)
            self._drop_indicator.show()
            self._drop_indicator.raise_()
            return
        self._indicator_animation.stop()
        self._indicator_animation.setStartValue(self._drop_indicator.geometry())
        self._indicator_animation.setEndValue(target_geometry)
        self._indicator_animation.start()
        self._drop_indicator.raise_()


class WorkflowPage(QWidget):
    """Compose, configure and observe the first-version fixed task set."""

    runRequested = Signal()
    stopRequested = Signal()
    openTradeRequested = Signal()
    openPassengerRequested = Signal()
    openBattleRequested = Signal()
    previewTradeRequested = Signal()
    settingsRequested = Signal()

    def __init__(self, settings: ResonanceConfigRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._busy = False
        self._loading_state = False
        self._task_status: dict[str, QLabel] = {}
        self._task_checks: dict[str, QCheckBox] = {}
        self._task_rows: dict[str, _TaskRow] = {}
        self._task_order = [task_id for task_id, _title in WORKFLOW_TASKS]
        self._selected_task = "startup"
        self._commerce_order = ["trade", "passenger"]
        self._commerce_rows: dict[str, QFrame] = {}
        self._commerce_checks: dict[str, QCheckBox] = {}
        self._tree_items: dict[str, QTreeWidgetItem] = {}
        self._progress_items: dict[tuple[str, str], QTreeWidgetItem] = {}
        self._build_ui()
        self._load_state()
        self._select_row(0)

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        self.left_panel = self._build_task_panel()
        self.center_panel = self._build_config_panel()
        self.right_panel = self._build_run_panel()
        root.addWidget(self.left_panel, 20)
        root.addWidget(self.center_panel, 45)
        root.addWidget(self.right_panel, 35)

    def _build_task_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("workflowPanel")
        panel.setMinimumWidth(230)
        panel.setMaximumWidth(330)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 16, 14, 12)
        layout.setSpacing(10)
        title = QLabel("任务顺序", panel)
        title.setObjectName("workflowTitle")
        layout.addWidget(title)

        self.task_rows_host = _TaskRowsHost(panel)
        self.task_rows_host.taskDropped.connect(self._drop_task)
        self.task_rows_layout = QVBoxLayout(self.task_rows_host)
        self.task_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.task_rows_layout.setSpacing(6)
        layout.addWidget(self.task_rows_host)
        for task_id, title_text in WORKFLOW_TASKS:
            self._append_task(task_id, title_text)
        self.task_rows_layout.addStretch(1)

        footer = QFrame(panel)
        footer.setObjectName("workflowFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(4, 8, 4, 0)
        footer_layout.addStretch(1)
        self.settings_button = QPushButton("设置", footer)
        self.settings_button.setObjectName("quietButton")
        self.settings_button.clicked.connect(self.settingsRequested.emit)
        footer_layout.addWidget(self.settings_button)
        layout.addWidget(footer)
        return panel

    def _append_task(self, task_id: str, title: str) -> None:
        row = _TaskRow(task_id, title, self.task_rows_host)
        check = row.enabled_check
        check.setChecked(True)
        check.toggled.connect(self._save_state)
        row.selected.connect(self._select_task)
        self.task_rows_layout.addWidget(row)
        self._task_rows[task_id] = row
        self._task_checks[task_id] = check
        self._task_status[task_id] = row.status_label

    def _build_config_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("workflowPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 14)
        self.center_stack = QStackedWidget(panel)
        self.config_stack = QStackedWidget(panel)
        self.config_stack.addWidget(self._build_startup_config())
        self.config_stack.addWidget(self._build_commerce_config())
        self.config_stack.addWidget(self._build_battle_config())
        self.config_stack.addWidget(self._build_close_config())
        self.center_stack.addWidget(self.config_stack)
        (
            self.trade_editor_page,
            self.trade_editor_layout,
            self.trade_editor_header,
        ) = self._build_embedded_editor_page("完整货运参数")
        self.trade_preview_button = QPushButton("方案试算", self.trade_editor_page)
        self.trade_preview_button.setObjectName("primaryButton")
        self.trade_preview_button.setToolTip("使用当前参数计算方案，不操作游戏")
        self.trade_preview_button.clicked.connect(self._request_trade_preview)
        self.trade_editor_header.addWidget(self.trade_preview_button)
        (
            self.passenger_editor_page,
            self.passenger_editor_layout,
            self.passenger_editor_header,
        ) = self._build_embedded_editor_page("完整客运参数")
        self.trade_preview_page = self._build_trade_preview_page()
        self.center_stack.addWidget(self.trade_editor_page)
        self.center_stack.addWidget(self.passenger_editor_page)
        self.center_stack.addWidget(self.trade_preview_page)
        layout.addWidget(self.center_stack)
        return panel

    def _build_embedded_editor_page(
        self, title: str
    ) -> tuple[QWidget, QVBoxLayout, QHBoxLayout]:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        back = QPushButton("← 返回跑商设置", page)
        back.setObjectName("quietButton")
        back.clicked.connect(self.show_commerce_summary)
        heading = QLabel(title, page)
        heading.setObjectName("workflowTitle")
        header.addWidget(back)
        header.addWidget(heading)
        header.addStretch(1)
        layout.addLayout(header)
        return page, layout, header

    def _build_trade_preview_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        back = QPushButton("← 返回修改参数", page)
        back.setObjectName("quietButton")
        back.clicked.connect(self.show_trade_editor)
        heading = QLabel("货运方案试算", page)
        heading.setObjectName("workflowTitle")
        rerun = QPushButton("重新计算", page)
        rerun.clicked.connect(self._request_trade_preview)
        header.addWidget(back)
        header.addWidget(heading)
        header.addStretch(1)
        header.addWidget(rerun)
        layout.addLayout(header)
        self.trade_preview_layout = layout
        self.trade_preview_rerun_button = rerun
        return page

    def attach_parameter_editors(
        self, trade_panel: QWidget, passenger_panel: QWidget, trade_result_panel: QWidget
    ) -> None:
        for panel, target_layout in (
            (trade_panel, self.trade_editor_layout),
            (passenger_panel, self.passenger_editor_layout),
            (trade_result_panel, self.trade_preview_layout),
        ):
            panel.setMinimumWidth(0)
            panel.setMaximumWidth(16777215)
            target_layout.addWidget(panel, 1)

    def show_trade_editor(self) -> None:
        self.center_stack.setCurrentWidget(self.trade_editor_page)

    def show_passenger_editor(self) -> None:
        self.center_stack.setCurrentWidget(self.passenger_editor_page)

    def show_trade_preview(self) -> None:
        self.center_stack.setCurrentWidget(self.trade_preview_page)

    def _request_trade_preview(self) -> None:
        if self._busy:
            return
        self.show_trade_preview()
        self.previewTradeRequested.emit()

    def show_commerce_summary(self) -> None:
        self.center_stack.setCurrentWidget(self.config_stack)
        self.config_stack.setCurrentIndex(1)

    def _page_heading(self, title: str, description: str, parent: QWidget) -> QVBoxLayout:
        box = QVBoxLayout()
        heading = QLabel(title, parent)
        heading.setObjectName("workflowTitle")
        note = QLabel(description, parent)
        note.setWordWrap(True)
        note.setProperty("caption", True)
        box.addWidget(heading)
        box.addWidget(note)
        return box

    def _build_startup_config(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addLayout(self._page_heading("进入主界面", "启动客户端并等待雷索纳斯主界面就绪。", page))
        form = QFormLayout()
        self.startup_launch = QCheckBox("游戏未运行时自动启动", page)
        self.startup_launch.setChecked(True)
        self.startup_rounds = QSpinBox(page)
        self.startup_rounds.setRange(1, 3600)
        self.startup_rounds.setValue(300)
        self.startup_window_timeout = QSpinBox(page)
        self.startup_window_timeout.setRange(1, 600)
        self.startup_window_timeout.setValue(90)
        self.startup_window_timeout.setSuffix(" 秒")
        form.addRow("启动行为", self.startup_launch)
        form.addRow("窗口等待上限", self.startup_window_timeout)
        form.addRow("主界面识别轮次", self.startup_rounds)
        layout.addLayout(form)
        open_settings = QPushButton("打开游戏与启动设置", page)
        open_settings.clicked.connect(self.settingsRequested.emit)
        layout.addWidget(open_settings)
        layout.addStretch(1)
        return page

    def _build_commerce_config(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addLayout(self._page_heading("跑商", "货运与客运分别保存参数，并按下列顺序执行。", page))

        order_box = QFrame(page)
        order_box.setObjectName("linenInset")
        order_layout = QVBoxLayout(order_box)
        order_title = QHBoxLayout()
        order_title.addWidget(QLabel("跑商执行顺序", order_box))
        order_title.addStretch(1)
        swap = QPushButton("交换顺序", order_box)
        swap.clicked.connect(self._swap_commerce_order)
        order_title.addWidget(swap)
        order_layout.addLayout(order_title)
        self.commerce_rows_host = QWidget(order_box)
        self.commerce_rows_layout = QVBoxLayout(self.commerce_rows_host)
        self.commerce_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.commerce_rows_layout.setSpacing(6)
        for kind, title in (("trade", "货运"), ("passenger", "客运")):
            row = QFrame(self.commerce_rows_host)
            row.setObjectName("commerceStepRow")
            row.setMinimumHeight(42)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 5, 8, 5)
            check = QCheckBox("启用", row)
            check.setChecked(True)
            check.toggled.connect(self._save_state)
            number = QLabel("", row)
            number.setObjectName("commerceStepNumber")
            name = QLabel(title, row)
            name.setObjectName("taskName")
            up = QPushButton("↑", row)
            down = QPushButton("↓", row)
            up.setFixedWidth(34)
            down.setFixedWidth(34)
            up.clicked.connect(lambda checked=False, value=kind: self._move_commerce(value, -1))
            down.clicked.connect(lambda checked=False, value=kind: self._move_commerce(value, 1))
            row_layout.addWidget(check)
            row_layout.addWidget(number)
            row_layout.addWidget(name, 1)
            row_layout.addWidget(up)
            row_layout.addWidget(down)
            row.number_label = number
            row.up_button = up
            row.down_button = down
            self._commerce_rows[kind] = row
            self._commerce_checks[kind] = check
            self.commerce_rows_layout.addWidget(row)
        order_layout.addWidget(self.commerce_rows_host)
        self._rebuild_commerce_rows()
        layout.addWidget(order_box)

        self.commerce_tabs = QTabWidget(page)
        self.commerce_tabs.addTab(self._build_trade_summary(), "货运设置")
        self.commerce_tabs.addTab(self._build_passenger_summary(), "客运设置")
        layout.addWidget(self.commerce_tabs, 1)
        return page

    def _build_trade_summary(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.trade_fatigue = QSpinBox(page)
        self.trade_fatigue.setRange(0, 100000)
        self.trade_cargo = QSpinBox(page)
        self.trade_cargo.setRange(1, 100000)
        self.trade_medicine = QCheckBox("允许使用疲劳药", page)
        self.trade_investment = QCheckBox("自动进行蜃息岛投资", page)
        form.addRow("疲劳预算", self.trade_fatigue)
        form.addRow("货舱容量", self.trade_cargo)
        form.addRow("疲劳恢复", self.trade_medicine)
        form.addRow("蜃息岛投资", self.trade_investment)
        layout.addLayout(form)
        button = QPushButton("打开完整货运参数", page)
        button.clicked.connect(self.openTradeRequested.emit)
        layout.addWidget(button)
        layout.addStretch(1)
        return page

    def _build_passenger_summary(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.passenger_rounds = QSpinBox(page)
        self.passenger_rounds.setRange(1, 99)
        self.passenger_trade = QCheckBox("途中执行买卖货", page)
        self.passenger_reposition = QCheckBox("自动前往线路起点", page)
        form.addRow("往返次数", self.passenger_rounds)
        form.addRow("客运倒货", self.passenger_trade)
        form.addRow("起点处理", self.passenger_reposition)
        layout.addLayout(form)
        button = QPushButton("打开完整客运参数", page)
        button.clicked.connect(self.openPassengerRequested.emit)
        layout.addWidget(button)
        layout.addStretch(1)
        return page

    def _build_battle_config(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addLayout(self._page_heading("自动战斗", "按照现有作战任务单依次执行。", page))
        self.battle_summary = QLabel("尚未添加作战任务", page)
        self.battle_summary.setObjectName("linenInsetLabel")
        self.battle_summary.setWordWrap(True)
        layout.addWidget(self.battle_summary)
        button = QPushButton("编辑作战任务单", page)
        button.clicked.connect(self.openBattleRequested.emit)
        layout.addWidget(button)
        layout.addStretch(1)
        return page

    def _build_close_config(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addLayout(self._page_heading("关闭游戏", "正常关闭游戏窗口，必要时结束已验证的游戏进程。", page))
        form = QFormLayout()
        self.close_timeout = QSpinBox(page)
        self.close_timeout.setRange(0, 120)
        self.close_timeout.setValue(10)
        self.close_timeout.setSuffix(" 秒")
        self.close_force = QCheckBox("超时后结束进程", page)
        self.close_force.setChecked(True)
        form.addRow("等待退出时间", self.close_timeout)
        form.addRow("关闭策略", self.close_force)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _build_run_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("workflowPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 16, 14, 14)
        header = QHBoxLayout()
        title = QLabel("运行状态", panel)
        title.setObjectName("workflowTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.run_button = QPushButton("运行流程", panel)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self._toggle_run)
        header.addWidget(self.run_button)
        layout.addLayout(header)
        self.progress_label = QLabel("0 / 0 · 等待开始", panel)
        self.progress_label.setObjectName("workflowProgress")
        layout.addWidget(self.progress_label)
        self.run_tree = QTreeWidget(panel)
        self.run_tree.setObjectName("workflowRunTree")
        self.run_tree.setHeaderLabels(["任务与阶段", "状态"])
        self.run_tree.header().setStretchLastSection(False)
        self.run_tree.header().resizeSection(0, 260)
        layout.addWidget(self.run_tree, 3)
        log_title = QLabel("详细日志", panel)
        log_title.setObjectName("sectionTitle")
        layout.addWidget(log_title)
        self.log_view = QTextBrowser(panel)
        self.log_view.setObjectName("workflowLog")
        self.log_view.setPlaceholderText("流程事件和失败原因会显示在这里。")
        layout.addWidget(self.log_view, 2)
        return panel

    def _select_row(self, row: int) -> None:
        if 0 <= row < len(self._task_order):
            self._select_task(self._task_order[row])
        self._sync_move_buttons()

    def _select_task(self, task_id: str) -> None:
        if task_id not in self._task_rows:
            return
        self._selected_task = task_id
        self.center_stack.setCurrentWidget(self.config_stack)
        for row_id, row in self._task_rows.items():
            row.set_selected(row_id == task_id)
        self.config_stack.setCurrentIndex(
            {"startup": 0, "commerce": 1, "battle": 2, "close": 3}[task_id]
        )
        self._sync_move_buttons()

    def _move_current(self, delta: int) -> None:
        row = self._task_order.index(self._selected_task)
        target = row + delta
        if not 0 <= target < len(self._task_order) or self._busy:
            return
        task_id = self._task_order.pop(row)
        self._task_order.insert(target, task_id)
        self._rebuild_task_rows()
        self._save_state()

    def _drop_task(self, task_id: str, target: int) -> None:
        if self._busy or task_id not in self._task_order:
            return
        source = self._task_order.index(task_id)
        item = self._task_order.pop(source)
        if target > source:
            target -= 1
        self._task_order.insert(max(0, min(target, len(self._task_order))), item)
        self._selected_task = task_id
        self._rebuild_task_rows()
        self._select_task(task_id)
        self._save_state()

    def _renumber_tasks(self) -> None:
        for index, task_id in enumerate(self._task_order, 1):
            self._task_rows[task_id].number_label.setText(str(index))

    def _rebuild_task_rows(self) -> None:
        for task_id in self._task_order:
            self.task_rows_layout.removeWidget(self._task_rows[task_id])
        for task_id in self._task_order:
            self.task_rows_layout.insertWidget(
                self.task_rows_layout.count() - 1, self._task_rows[task_id]
            )
        self._renumber_tasks()
        self._sync_move_buttons()

    def _swap_commerce_order(self) -> None:
        if self._busy:
            return
        self._commerce_order.reverse()
        self._rebuild_commerce_rows()
        self._save_state()

    def _move_commerce(self, kind: str, delta: int) -> None:
        if self._busy:
            return
        source = self._commerce_order.index(kind)
        target = source + delta
        if not 0 <= target < len(self._commerce_order):
            return
        self._commerce_order[source], self._commerce_order[target] = (
            self._commerce_order[target], self._commerce_order[source]
        )
        self._rebuild_commerce_rows()
        self._save_state()

    def _rebuild_commerce_rows(self) -> None:
        for kind in self._commerce_order:
            self.commerce_rows_layout.removeWidget(self._commerce_rows[kind])
        for index, kind in enumerate(self._commerce_order):
            row = self._commerce_rows[kind]
            self.commerce_rows_layout.addWidget(row)
            row.number_label.setText(str(index + 1))
            row.up_button.setEnabled(not self._busy and index > 0)
            row.down_button.setEnabled(not self._busy and index < len(self._commerce_order) - 1)

    def _toggle_run(self) -> None:
        if self._busy:
            self.stopRequested.emit()
        else:
            self.runRequested.emit()

    def workflow_steps(self) -> list[str]:
        result: list[str] = []
        for task_id in self._task_order:
            if self._task_checks[task_id].isChecked():
                result.append(task_id)
        return result

    def commerce_steps(self) -> list[str]:
        return [kind for kind in self._commerce_order if self._commerce_checks[kind].isChecked()]

    def startup_inputs(self) -> dict[str, Any]:
        return {
            "executable_path": str(self._settings.value("game/executable_path", "") or "") or None,
            "launch_if_not_running": self.startup_launch.isChecked(),
            "window_timeout_sec": self.startup_window_timeout.value(),
            "max_settle_rounds": self.startup_rounds.value(),
            "round_interval_sec": 1.0,
        }

    def close_inputs(self) -> dict[str, Any]:
        return {
            "graceful_timeout_sec": self.close_timeout.value(),
            "force_after_timeout": self.close_force.isChecked(),
        }

    def apply_compact_inputs(self, trade: Mapping[str, Any], passenger: Mapping[str, Any]) -> None:
        self.trade_fatigue.setValue(int(trade.get("fatigue_budget", 700)))
        self.trade_cargo.setValue(int(trade.get("cargo_capacity", 750)))
        self.trade_medicine.setChecked(bool(trade.get("use_fatigue_medicine", False)))
        self.trade_investment.setChecked(bool(trade.get("auto_cape_island_investment", True)))
        self.passenger_rounds.setValue(int(passenger.get("round_trips", 1)))
        self.passenger_trade.setChecked(bool(passenger.get("trade_during_trip", False)))
        self.passenger_reposition.setChecked(bool(passenger.get("reposition_to_route", True)))

    def merge_trade_inputs(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        merged = dict(inputs)
        merged.update(
            fatigue_budget=self.trade_fatigue.value(),
            cargo_capacity=self.trade_cargo.value(),
            use_fatigue_medicine=self.trade_medicine.isChecked(),
            auto_cape_island_investment=self.trade_investment.isChecked(),
        )
        return merged

    def merge_passenger_inputs(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        merged = dict(inputs)
        merged.update(
            round_trips=self.passenger_rounds.value(),
            trade_during_trip=self.passenger_trade.isChecked(),
            reposition_to_route=self.passenger_reposition.isChecked(),
        )
        return merged

    def set_battle_count(self, count: int) -> None:
        self.battle_summary.setText(f"当前任务单包含 {count} 个作战任务。" if count else "尚未添加作战任务。")

    def begin_workflow(self, steps: list[str], commerce_steps: list[str]) -> None:
        self._busy = True
        self.run_button.setText("停止")
        self.run_button.setObjectName("dangerButton")
        self.run_button.style().unpolish(self.run_button)
        self.run_button.style().polish(self.run_button)
        self._set_editing_enabled(False)
        self.log_view.clear()
        self.run_tree.clear()
        self._tree_items.clear()
        self._progress_items.clear()
        labels = dict(WORKFLOW_TASKS)
        for index, step in enumerate(steps, 1):
            item = QTreeWidgetItem([f"{index}  {labels[step]}", "等待"])
            item.setData(0, Qt.ItemDataRole.UserRole, step)
            self.run_tree.addTopLevelItem(item)
            self._tree_items[step] = item
            if step == "commerce":
                for kind in commerce_steps:
                    child = QTreeWidgetItem(["货运" if kind == "trade" else "客运", "等待"])
                    child.setData(0, Qt.ItemDataRole.UserRole, kind)
                    item.addChild(child)
                    self._tree_items[kind] = child
                item.setExpanded(True)
        for task_id in self._task_status:
            self._set_left_status(task_id, "waiting" if task_id in steps else "skipped")
        self.progress_label.setText(f"0 / {len(steps)} · 准备执行")
        self.append_log("流程已启动，参数快照已锁定。")

    def mark_step(self, step: str, state: str, detail: str = "") -> None:
        item = self._tree_items.get(step)
        state_text = {
            "waiting": "等待", "running": "执行中", "success": "完成",
            "failed": "失败", "skipped": "跳过", "cancelled": "已停止",
        }.get(state, state)
        if item is not None:
            item.setText(1, state_text)
            item.setToolTip(0, detail)
        parent = item.parent() if item is not None else None
        top_step = str(parent.data(0, Qt.ItemDataRole.UserRole)) if parent is not None else step
        if top_step in self._task_status:
            self._set_left_status(top_step, state)
        if detail:
            self.append_log(f"{state_text} · {detail}")
        top_steps = [self.run_tree.topLevelItem(i) for i in range(self.run_tree.topLevelItemCount())]
        done = sum(1 for row in top_steps if row.text(1) in {"完成", "跳过"})
        current = detail or state_text
        self.progress_label.setText(f"{done} / {len(top_steps)} · {current}")

    def step_is_waiting(self, step: str) -> bool:
        item = self._tree_items.get(step)
        return item is not None and item.text(1) == "等待"

    def apply_progress_event(self, kind: str, event: Mapping[str, Any]) -> None:
        if not self._busy or kind not in {"trade", "passenger"}:
            return
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return
        stage = str(payload.get("stage") or "task")
        state = str(payload.get("state") or "running").lower()
        labels = TRADE_STAGE_LABELS if kind == "trade" else PASSENGER_STAGE_LABELS
        label = labels.get(stage, stage)
        parent = self._tree_items.get(kind)
        if parent is None:
            return
        key = (kind, stage)
        item = self._progress_items.get(key)
        if item is None:
            item = QTreeWidgetItem([label, "等待"])
            parent.addChild(item)
            self._progress_items[key] = item
            parent.setExpanded(True)
        view_state = "success" if state in {"completed", "success"} else (
            "failed" if state in {"blocked", "failed", "error"} else "running"
        )
        item.setText(1, {"success": "完成", "failed": "失败", "running": "执行中"}[view_state])
        detail_parts = [str(payload.get(key) or "") for key in ("current_city", "from_city", "to_city", "source_city", "destination_city")]
        detail = " → ".join(dict.fromkeys(value for value in detail_parts if value))
        self.progress_label.setText(f"执行中 · {label}" + (f" · {detail}" if detail else ""))
        self.append_log(f"{('完成' if view_state == 'success' else '执行中')} · {label}" + (f" · {detail}" if detail else ""))

    def finish_workflow(self, *, success: bool, message: str) -> None:
        self._busy = False
        self.run_button.setText("运行流程")
        self.run_button.setObjectName("primaryButton")
        self.run_button.style().unpolish(self.run_button)
        self.run_button.style().polish(self.run_button)
        self._set_editing_enabled(True)
        self.progress_label.setText(("已完成 · " if success else "已停止 · ") + message)
        self.append_log(message)

    def append_log(self, message: str) -> None:
        self.log_view.append(str(message))

    def is_running(self) -> bool:
        return self._busy

    def _set_left_status(self, task_id: str, state: str) -> None:
        symbols = {"waiting": "○", "running": "◉", "success": "✓", "failed": "!", "skipped": "–", "cancelled": "×"}
        label = self._task_status[task_id]
        label.setText(symbols.get(state, "○"))
        label.setProperty("runState", state)
        label.style().unpolish(label)
        label.style().polish(label)

    def _set_editing_enabled(self, enabled: bool) -> None:
        self.task_rows_host.setEnabled(enabled)
        self.center_panel.setEnabled(enabled)
        self._sync_move_buttons()
        self._rebuild_commerce_rows()

    def _sync_move_buttons(self) -> None:
        return

    def _load_state(self) -> None:
        self._loading_state = True
        raw_order = str(self._settings.value("workflow/task_order", "startup,commerce,battle,close") or "")
        order = [value for value in raw_order.split(",") if value in dict(WORKFLOW_TASKS)]
        if set(order) == set(dict(WORKFLOW_TASKS)):
            self._task_order = order
        enabled_raw = str(self._settings.value("workflow/enabled", "startup,commerce,battle,close") or "")
        enabled = set(enabled_raw.split(","))
        for task_id, check in self._task_checks.items():
            check.setChecked(task_id in enabled)
        commerce_raw = str(self._settings.value("workflow/commerce_order", "trade,passenger") or "")
        commerce_order = [value for value in commerce_raw.split(",") if value in {"trade", "passenger"}]
        if set(commerce_order) == {"trade", "passenger"}:
            self._commerce_order = commerce_order
        commerce_enabled_raw = str(
            self._settings.value("workflow/commerce_enabled", "trade,passenger") or ""
        )
        commerce_enabled = set(commerce_enabled_raw.split(","))
        for kind, check in self._commerce_checks.items():
            check.setChecked(kind in commerce_enabled)
        self._rebuild_task_rows()
        self._rebuild_commerce_rows()
        self._loading_state = False

    def _save_state(self, *_args: object) -> None:
        if self._loading_state:
            return
        order = list(self._task_order)
        enabled = [task_id for task_id in order if self._task_checks[task_id].isChecked()]
        commerce = list(self._commerce_order)
        commerce_enabled = [kind for kind in commerce if self._commerce_checks[kind].isChecked()]
        self._settings.set_value("workflow/task_order", ",".join(order))
        self._settings.set_value("workflow/enabled", ",".join(enabled))
        self._settings.set_value("workflow/commerce_order", ",".join(commerce))
        self._settings.set_value("workflow/commerce_enabled", ",".join(commerce_enabled))
