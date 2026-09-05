"""Result panel for strict fixed-team recommendations."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


CHARACTER_STATUS_LABELS = {
    "complete": "角色完全满足",
    "basic": "角色基本满足",
}
WEAPON_STATUS_LABELS = {
    "full": "武器满足满配",
    "low": "武器满足低配",
    "unmet": "武器不满足",
}


class TeamRecommendationPanel(QWidget):
    runRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner_busy = False
        self._task_running = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        title = QLabel("配队推荐", self)
        title.setObjectName("workflowTitle")
        note = QLabel(
            "读取最新角色与仓库装备数据，"
            "只列出五名角色齐全且全部达到最低觉醒要求的固定配队。",
            self,
        )
        note.setWordWrap(True)
        note.setProperty("caption", True)
        layout.addWidget(title)
        layout.addWidget(note)

        self.status_label = QLabel("尚未运行", self)
        self.status_label.setObjectName("teamRecommendationStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("resultState", "waiting")
        layout.addWidget(self.status_label)

        self.result_tree = QTreeWidget(self)
        self.result_tree.setObjectName("teamRecommendationTree")
        self.result_tree.setColumnCount(4)
        self.result_tree.setHeaderLabels(("配队 / 成员", "用途", "角色", "武器"))
        self.result_tree.setRootIsDecorated(True)
        self.result_tree.setAlternatingRowColors(True)
        self.result_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.result_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.result_tree, 1)

        action_band = QFrame(self)
        action_band.setObjectName("smallTaskRunBand")
        action_layout = QHBoxLayout(action_band)
        action_layout.setContentsMargins(0, 9, 0, 0)
        action_layout.setSpacing(8)
        self.run_status = QLabel("待运行", action_band)
        self.run_status.setObjectName("smallTaskRunStatus")
        self.run_status.setProperty("status", "waiting")
        action_layout.addWidget(self.run_status)
        action_layout.addStretch(1)
        self.cancel_button = QPushButton("取消", action_band)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        action_layout.addWidget(self.cancel_button)
        self.run_button = QPushButton("开始匹配", action_band)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self.runRequested.emit)
        action_layout.addWidget(self.run_button)
        layout.addWidget(action_band)

    def begin_run(self) -> None:
        self._task_running = True
        self.result_tree.clear()
        self._set_result_status("正在读取用户数据并匹配固定配队……", "running")
        self._set_run_status("正在匹配……", "running")
        self._sync_controls()

    def apply_result(self, payload: Mapping[str, Any]) -> None:
        self._task_running = False
        self.result_tree.clear()
        status = str(payload.get("status") or "")
        if status == "blocked":
            self._set_result_status(
                str(payload.get("message") or "角色或仓库装备数据不完整，请先更新用户数据。"),
                "blocked",
            )
            self._set_run_status("需要更新用户数据", "error")
            self._sync_controls()
            return
        if status != "success":
            self.show_error(str(payload.get("message") or "配队推荐结果格式无效。"))
            return

        recommendations = payload.get("recommendations")
        if not isinstance(recommendations, list):
            self.show_error("配队推荐结果缺少 recommendations。")
            return
        presence_only = payload.get("weapon_recognition_mode") == "presence"
        for recommendation in recommendations:
            if isinstance(recommendation, Mapping):
                self._append_recommendation(recommendation, presence_only=presence_only)
        counts = payload.get("counts") if isinstance(payload.get("counts"), Mapping) else {}
        summary = str(payload.get("message") or "匹配完成。")
        if recommendations:
            unmet_label = "未确认满足" if presence_only else "不满足"
            summary += (
                f" 角色完全 {int(counts.get('character_complete') or 0)}，"
                f"基本 {int(counts.get('character_basic') or 0)}；"
                f"武器满配 {int(counts.get('weapon_full') or 0)}，"
                f"低配 {int(counts.get('weapon_low') or 0)}，"
                f"{unmet_label} {int(counts.get('weapon_unmet') or 0)}。"
            )
        inventory_note = str(payload.get("weapon_inventory_note") or "")
        if presence_only and not inventory_note:
            inventory_note = "装备数量未知，每种按 1 件评估。"
        if inventory_note:
            summary += "\n" + inventory_note
        self._set_result_status(summary, "success")
        self._set_run_status("匹配完成", "success")
        if self.result_tree.topLevelItemCount():
            self.result_tree.topLevelItem(0).setExpanded(True)
        self._sync_controls()

    def _append_recommendation(
        self, recommendation: Mapping[str, Any], *, presence_only: bool = False
    ) -> None:
        character_status = str(recommendation.get("character_status") or "")
        weapon_status = str(recommendation.get("weapon_status") or "")
        parent = QTreeWidgetItem(
            [
                str(recommendation.get("title") or recommendation.get("team_id") or "--"),
                "、".join(str(value) for value in recommendation.get("categories") or []) or "--",
                CHARACTER_STATUS_LABELS.get(character_status, character_status or "--"),
                "武器未确认满足" if presence_only and weapon_status == "unmet"
                else WEAPON_STATUS_LABELS.get(weapon_status, weapon_status or "--"),
            ]
        )
        parent.setData(0, Qt.ItemDataRole.UserRole, str(recommendation.get("team_id") or ""))
        parent.setToolTip(0, str(recommendation.get("source_url") or ""))
        self.result_tree.addTopLevelItem(parent)

        for member in recommendation.get("members") or []:
            if not isinstance(member, Mapping):
                continue
            current = member.get("current_awakening")
            minimum = member.get("minimum_awakening")
            recommended = member.get("recommended_awakening")
            recommended_text = "--" if recommended is None else str(recommended)
            full_weapon = str(member.get("full_weapon_id") or "未提供")
            low_weapon = str(member.get("low_weapon_id") or "未提供")
            assigned = str(member.get("assigned_weapon_id") or "未分配")
            child = QTreeWidgetItem(
                [
                    f"{int(member.get('slot') or 0)}. {member.get('character_id') or '--'}",
                    "",
                    f"当前 {current} / 最低 {minimum} / 推荐 {recommended_text}",
                    f"满配 {full_weapon} · 低配 {low_weapon} · 当前 {assigned}",
                ]
            )
            child.setToolTip(2, child.text(2))
            child.setToolTip(3, child.text(3))
            parent.addChild(child)

    def show_error(self, message: str) -> None:
        self._task_running = False
        self.result_tree.clear()
        text = str(message or "未知错误")
        self._set_result_status(f"匹配失败：{text}", "error")
        self._set_run_status("匹配失败", "error")
        self._sync_controls()

    def set_runner_busy(self, busy: bool) -> None:
        self._runner_busy = bool(busy)
        self._sync_controls()

    def _sync_controls(self) -> None:
        self.run_button.setEnabled(not self._runner_busy and not self._task_running)
        self.cancel_button.setEnabled(self._runner_busy and self._task_running)

    def _set_result_status(self, text: str, state: str) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("resultState", state)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _set_run_status(self, text: str, state: str) -> None:
        self.run_status.setText(text)
        self.run_status.setProperty("status", state)
        self.run_status.style().unpolish(self.run_status)
        self.run_status.style().polish(self.run_status)


__all__ = ["TeamRecommendationPanel"]
