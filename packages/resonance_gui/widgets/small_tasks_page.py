"""Three-column home for tasks that do not belong to the ordered workflow."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config_repository import ResonanceConfigRepository
from .player_data_panel import PlayerDataPanel
from .team_recommendation_panel import TeamRecommendationPanel


USER_DATA_TASK_ID = "player_data_refresh"
TEAM_RECOMMENDATION_TASK_ID = "team_recommendation"
CATEGORY_TASKS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("user_data", "用户数据", ((USER_DATA_TASK_ID, "刷新用户数据"),)),
    ("team_tools", "配队工具", ((TEAM_RECOMMENDATION_TASK_ID, "配队推荐"),)),
)


class _SmallTaskColumn(QFrame):
    def __init__(self, title: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("smallTaskPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        heading = QLabel(title, self)
        heading.setObjectName("smallTaskColumnTitle")
        heading.setContentsMargins(16, 14, 16, 12)
        layout.addWidget(heading)

        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(10, 10, 10, 10)
        self.body_layout.setSpacing(8)
        layout.addWidget(self.body, 1)


class SmallTasksPage(QWidget):
    """Select, configure and run non-workflow tasks."""

    runPlayerDataRequested = Signal(object)
    runTeamRecommendationRequested = Signal()
    cancelRequested = Signal()
    cacheRequested = Signal()

    def __init__(
        self,
        settings: ResonanceConfigRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._runner_busy = False
        self._active_task_id = ""
        self._category_tasks = {
            category_id: tasks for category_id, _label, tasks in CATEGORY_TASKS
        }
        self._task_pages: dict[str, QWidget] = {}

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        self.category_panel = _SmallTaskColumn("任务分类", self)
        self.category_panel.setMinimumWidth(230)
        self.category_panel.setMaximumWidth(330)
        self.category_list = QListWidget(self.category_panel.body)
        self.category_list.setObjectName("smallTaskCategoryList")
        for category_id, label, _tasks in CATEGORY_TASKS:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, category_id)
            item.setSizeHint(QSize(0, 48))
            self.category_list.addItem(item)
        self.category_panel.body_layout.addWidget(self.category_list)

        self.task_panel = _SmallTaskColumn("任务列表", self)
        self.task_panel.setMinimumWidth(320)
        self.task_list = QListWidget(self.task_panel.body)
        self.task_list.setObjectName("smallTaskList")
        self.task_panel.body_layout.addWidget(self.task_list)

        self.detail_panel = _SmallTaskColumn("任务详情", self)
        self.detail_panel.setMinimumWidth(470)
        self.detail_stack = QStackedWidget(self.detail_panel.body)
        self.detail_stack.setObjectName("smallTaskDetailStack")
        self.detail_panel.body_layout.addWidget(self.detail_stack, 1)

        self.player_task_page = QWidget(self.detail_stack)
        player_layout = QVBoxLayout(self.player_task_page)
        player_layout.setContentsMargins(0, 0, 0, 0)
        player_layout.setSpacing(8)
        self.player_data_panel = PlayerDataPanel(
            settings,
            self.player_task_page,
            title_text="刷新用户数据",
        )
        self.player_data_panel.cacheRequested.connect(self.cacheRequested.emit)
        player_layout.addWidget(self.player_data_panel, 1)
        self._build_player_action_band(player_layout)
        self.detail_stack.addWidget(self.player_task_page)
        self._task_pages[USER_DATA_TASK_ID] = self.player_task_page

        self.team_recommendation_panel = TeamRecommendationPanel(self.detail_stack)
        self.team_recommendation_panel.runRequested.connect(
            self.runTeamRecommendationRequested.emit
        )
        self.team_recommendation_panel.cancelRequested.connect(self.cancelRequested.emit)
        self.detail_stack.addWidget(self.team_recommendation_panel)
        self._task_pages[TEAM_RECOMMENDATION_TASK_ID] = self.team_recommendation_panel

        root.addWidget(self.category_panel, 22)
        root.addWidget(self.task_panel, 30)
        root.addWidget(self.detail_panel, 48)

        self.category_list.currentItemChanged.connect(self._category_changed)
        self.task_list.currentItemChanged.connect(self._task_changed)
        self.category_list.setCurrentRow(0)

    @property
    def current_task_id(self) -> str:
        item = self.task_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def _build_player_action_band(self, parent_layout: QVBoxLayout) -> None:
        run_band = QFrame(self.player_task_page)
        run_band.setObjectName("smallTaskRunBand")
        run_layout = QHBoxLayout(run_band)
        run_layout.setContentsMargins(0, 9, 0, 0)
        run_layout.setSpacing(8)
        self.run_status = QLabel("待运行", run_band)
        self.run_status.setObjectName("smallTaskRunStatus")
        self.run_status.setProperty("status", "waiting")
        run_layout.addWidget(self.run_status)
        run_layout.addStretch(1)
        self.cancel_button = QPushButton("取消", run_band)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        run_layout.addWidget(self.cancel_button)
        self.run_button = QPushButton("立即运行", run_band)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self._request_player_data_run)
        run_layout.addWidget(self.run_button)
        parent_layout.addWidget(run_band)

    def _category_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        self.task_list.clear()
        if current is None:
            return
        category_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        for task_id, label in self._category_tasks.get(category_id, ()):
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            item.setSizeHint(QSize(0, 56))
            self.task_list.addItem(item)
        if self.task_list.count():
            self.task_list.setCurrentRow(0)

    def _task_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        task_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        page = self._task_pages.get(task_id)
        if page is not None:
            self.detail_stack.setCurrentWidget(page)

    def _request_player_data_run(self) -> None:
        if self._runner_busy:
            return
        try:
            inputs = self.player_data_panel.collect_inputs()
        except ValueError as exc:
            self.show_player_data_error(str(exc))
            return
        self.runPlayerDataRequested.emit(inputs)

    def begin_player_data_run(self) -> None:
        self._active_task_id = USER_DATA_TASK_ID
        self._set_player_status("正在刷新用户数据……", "running")
        self._sync_controls()

    def apply_refresh_result(
        self,
        player_data: Mapping[str, Any],
        *,
        mark_complete: bool = True,
    ) -> None:
        self.player_data_panel.apply_refresh_result(player_data)
        if mark_complete:
            self._active_task_id = ""
            self._set_player_status("刷新完成", "success")
        self._sync_controls()

    def set_snapshot(self, player_data: Mapping[str, Any]) -> None:
        self.player_data_panel.set_snapshot(player_data)

    def show_player_data_error(self, message: str) -> None:
        self._active_task_id = ""
        self.player_data_panel.show_error(message)
        self._set_player_status(f"刷新失败：{str(message or '未知错误')}", "error")
        self._sync_controls()

    def begin_team_recommendation_run(self) -> None:
        self._active_task_id = TEAM_RECOMMENDATION_TASK_ID
        self.team_recommendation_panel.begin_run()
        self._sync_controls()

    def apply_team_recommendation_result(self, payload: Mapping[str, Any]) -> None:
        self._active_task_id = ""
        self.team_recommendation_panel.apply_result(payload)
        self._sync_controls()

    def show_team_recommendation_error(self, message: str) -> None:
        self._active_task_id = ""
        self.team_recommendation_panel.show_error(message)
        self._sync_controls()

    def set_runner_busy(self, busy: bool) -> None:
        self._runner_busy = bool(busy)
        self.player_data_panel.set_runner_busy(busy)
        self.team_recommendation_panel.set_runner_busy(busy)
        self._sync_controls()

    def _sync_controls(self) -> None:
        selection_enabled = not self._runner_busy and not self._active_task_id
        self.category_list.setEnabled(selection_enabled)
        self.task_list.setEnabled(selection_enabled)
        player_running = self._active_task_id == USER_DATA_TASK_ID
        self.run_button.setEnabled(selection_enabled)
        self.cancel_button.setEnabled(self._runner_busy and player_running)

    def _set_player_status(self, text: str, status: str) -> None:
        self.run_status.setText(text)
        self.run_status.setProperty("status", status)
        self.run_status.style().unpolish(self.run_status)
        self.run_status.style().polish(self.run_status)


__all__ = [
    "CATEGORY_TASKS",
    "SmallTasksPage",
    "TEAM_RECOMMENDATION_TASK_ID",
    "USER_DATA_TASK_ID",
]
