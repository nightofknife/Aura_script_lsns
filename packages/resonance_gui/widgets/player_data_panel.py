"""Workflow configuration and snapshot view for Resonance PC player data."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config_repository import (
    PLAYER_DATA_INVENTORY_CATEGORY_ORDER,
    PLAYER_DATA_STAGE_ORDER,
    ResonanceConfigRepository,
)


STAGE_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("location", "当前位置", "当前所在城市"),
    ("profile", "用户信息", "UID、昵称、等级、货舱、澄明度及疲劳"),
    (
        "inventory",
        "仓库",
        "按所选分类扫描模板目录内已支持的内容；期限识别暂时关闭，耗时较长",
    ),
    ("characters", "角色", "模板识别已收录角色及点亮星级"),
)

INVENTORY_CATEGORY_LABELS = {
    "items": "道具",
    "materials": "材料",
}

_NO_CACHE_ERROR_MARKER = "No cached Resonance PC player data is available"


def _format_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "从未更新"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%m-%d %H:%M:%S")
    except ValueError:
        return raw


def _ratio_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "--"
    current = value.get("current")
    maximum = value.get("max")
    return f"{current} / {maximum}" if current is not None and maximum is not None else "--"


class PlayerDataPanel(QWidget):
    """Select refresh stages and inspect the latest merged player-data snapshot."""

    cacheRequested = Signal()

    def __init__(
        self,
        settings: ResonanceConfigRepository,
        parent: QWidget | None = None,
        *,
        title_text: str = "更新用户数据",
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._title_text = str(title_text)
        self._snapshot: dict[str, Any] = {}
        self._stage_checks: dict[str, QCheckBox] = {}
        self._stage_times: dict[str, QLabel] = {}
        self._inventory_category_checks: dict[str, QCheckBox] = {}
        self._build_ui()
        self._load_inputs()
        self._render_snapshot()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        heading = QLabel(self._title_text, self)
        heading.setObjectName("workflowTitle")
        note = QLabel(
            "任务开始时只校验主界面，不会启动游戏或自动恢复页面。勾选需要读取的数据即可。",
            self,
        )
        note.setWordWrap(True)
        note.setProperty("caption", True)
        layout.addWidget(heading)
        layout.addWidget(note)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_selection_tab(), "更新设置")
        self.tabs.addTab(self._build_snapshot_tab(), "数据快照")
        layout.addWidget(self.tabs, 1)

    def _build_selection_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 8)
        layout.setSpacing(10)

        shortcuts = QHBoxLayout()
        shortcuts.addWidget(QLabel("快速选择", page))
        shortcuts.addStretch(1)
        for text, stages in (
            ("全部", PLAYER_DATA_STAGE_ORDER),
            ("基础信息", ("location", "profile")),
            ("仅仓库", ("inventory",)),
            ("仅角色", ("characters",)),
        ):
            button = QPushButton(text, page)
            button.setObjectName("quietButton")
            button.clicked.connect(
                lambda checked=False, selected=tuple(stages): self._select_stages(selected)
            )
            shortcuts.addWidget(button)
        layout.addLayout(shortcuts)

        stage_grid = QGridLayout()
        stage_grid.setHorizontalSpacing(10)
        stage_grid.setVerticalSpacing(8)
        for index, (stage, title, description) in enumerate(STAGE_DEFINITIONS):
            row = QFrame(page)
            row.setObjectName("playerDataStageRow")
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(10, 8, 10, 8)
            row_layout.setSpacing(3)
            top = QHBoxLayout()
            check = QCheckBox(title, row)
            check.setToolTip(description)
            updated = QLabel("从未更新", row)
            updated.setObjectName("playerDataStageTime")
            top.addWidget(check)
            top.addStretch(1)
            top.addWidget(updated)
            detail = QLabel(description, row)
            detail.setWordWrap(True)
            detail.setProperty("caption", True)
            row_layout.addLayout(top)
            row_layout.addWidget(detail)
            if stage == "inventory":
                categories = QHBoxLayout()
                categories.setSpacing(12)
                categories.addWidget(QLabel("扫描分类", row))
                for category in PLAYER_DATA_INVENTORY_CATEGORY_ORDER:
                    category_check = QCheckBox(INVENTORY_CATEGORY_LABELS[category], row)
                    categories.addWidget(category_check)
                    self._inventory_category_checks[category] = category_check
                categories.addStretch(1)
                row_layout.addLayout(categories)
                self.inventory_expiry_notice = QLabel(
                    "期限识别暂时关闭；限时道具将按道具类型合并数量。",
                    row,
                )
                self.inventory_expiry_notice.setWordWrap(True)
                self.inventory_expiry_notice.setProperty("caption", True)
                self.inventory_expiry_notice.setProperty("status", "warning")
                row_layout.addWidget(self.inventory_expiry_notice)
            stage_grid.addWidget(row, index // 2, index % 2)
            self._stage_checks[stage] = check
            self._stage_times[stage] = updated
        layout.addLayout(stage_grid)

        self.selection_summary = QLabel(page)
        self.selection_summary.setObjectName("playerDataSelectionSummary")
        self.selection_summary.setWordWrap(True)
        layout.addWidget(self.selection_summary)
        layout.addStretch(1)

        for check in self._stage_checks.values():
            check.toggled.connect(self._refresh_selection_summary)
        for check in self._inventory_category_checks.values():
            check.toggled.connect(self._refresh_selection_summary)
        self._stage_checks["inventory"].toggled.connect(
            self._sync_inventory_category_controls
        )
        return page

    def _build_snapshot_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 8)
        layout.setSpacing(9)

        header = QHBoxLayout()
        self.snapshot_updated = QLabel("尚未读取缓存", page)
        self.snapshot_updated.setObjectName("playerDataSnapshotUpdated")
        header.addWidget(self.snapshot_updated)
        header.addStretch(1)
        self.cache_button = QPushButton("读取最新缓存", page)
        self.cache_button.setObjectName("quietButton")
        self.cache_button.clicked.connect(self._request_cache)
        header.addWidget(self.cache_button)
        layout.addLayout(header)

        self.identity_label = QLabel("账号：--", page)
        self.identity_label.setObjectName("playerDataIdentity")
        self.identity_label.setWordWrap(True)
        self.status_label = QLabel("状态：--", page)
        self.status_label.setWordWrap(True)
        self.currency_label = QLabel("货币：--", page)
        self.currency_label.setWordWrap(True)
        layout.addWidget(self.identity_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.currency_label)

        self.snapshot_details_tabs = QTabWidget(page)

        inventory_page = QWidget(self.snapshot_details_tabs)
        inventory_layout = QVBoxLayout(inventory_page)
        inventory_layout.setContentsMargins(6, 8, 6, 6)
        inventory_layout.setSpacing(8)
        inventory_header = QHBoxLayout()
        inventory_title = QLabel("仓库", inventory_page)
        inventory_title.setObjectName("sectionTitle")
        self.inventory_summary = QLabel("尚无数据", inventory_page)
        self.inventory_summary.setProperty("caption", True)
        self.inventory_category_combo = QComboBox(inventory_page)
        for category in PLAYER_DATA_INVENTORY_CATEGORY_ORDER:
            self.inventory_category_combo.addItem(
                INVENTORY_CATEGORY_LABELS[category], category
            )
        self.inventory_category_combo.currentIndexChanged.connect(
            lambda _index: self._render_inventory()
        )
        self.inventory_search = QLineEdit(inventory_page)
        self.inventory_search.setPlaceholderText("搜索仓库内容")
        self.inventory_search.setClearButtonEnabled(True)
        self.inventory_search.setMaximumWidth(180)
        self.inventory_search.textChanged.connect(self._filter_inventory)
        inventory_header.addWidget(inventory_title)
        inventory_header.addWidget(self.inventory_summary)
        inventory_header.addStretch(1)
        inventory_header.addWidget(self.inventory_category_combo)
        inventory_header.addWidget(self.inventory_search)
        inventory_layout.addLayout(inventory_header)

        self.inventory_table = QTableWidget(0, 3, inventory_page)
        self.inventory_table.setHorizontalHeaderLabels(("道具", "数量", "期限"))
        self.inventory_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.inventory_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.inventory_table.setAlternatingRowColors(True)
        self.inventory_table.verticalHeader().hide()
        header_view = self.inventory_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        inventory_layout.addWidget(self.inventory_table, 1)
        self.snapshot_details_tabs.addTab(inventory_page, "仓库")

        character_page = QWidget(self.snapshot_details_tabs)
        character_layout = QVBoxLayout(character_page)
        character_layout.setContentsMargins(6, 8, 6, 6)
        character_layout.setSpacing(8)
        character_header = QHBoxLayout()
        character_title = QLabel("角色", character_page)
        character_title.setObjectName("sectionTitle")
        self.character_summary = QLabel("尚无数据", character_page)
        self.character_summary.setProperty("caption", True)
        character_header.addWidget(character_title)
        character_header.addWidget(self.character_summary)
        character_header.addStretch(1)
        character_layout.addLayout(character_header)
        self.character_table = QTableWidget(0, 2, character_page)
        self.character_table.setHorizontalHeaderLabels(("角色", "星级"))
        self.character_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.character_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.character_table.setAlternatingRowColors(True)
        self.character_table.verticalHeader().hide()
        character_header_view = self.character_table.horizontalHeader()
        character_header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        character_header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        character_layout.addWidget(self.character_table, 1)
        self.snapshot_details_tabs.addTab(character_page, "角色")
        layout.addWidget(self.snapshot_details_tabs, 1)

        self.snapshot_message = QLabel("", page)
        self.snapshot_message.setWordWrap(True)
        self.snapshot_message.setProperty("caption", True)
        layout.addWidget(self.snapshot_message)
        return page

    def _load_inputs(self) -> None:
        saved = self._settings.load_player_data_inputs()
        stages = set(saved.get("stages") or [])
        for stage, check in self._stage_checks.items():
            check.setChecked(stage in stages)
        selected_categories = set(saved.get("inventory_categories") or ["items"])
        for category, check in self._inventory_category_checks.items():
            check.setChecked(category in selected_categories)
        self._sync_inventory_category_controls()
        self._refresh_selection_summary()

    def _select_stages(self, selected: tuple[str, ...]) -> None:
        selected_set = set(selected)
        for stage, check in self._stage_checks.items():
            check.setChecked(stage in selected_set)
        self._refresh_selection_summary()

    def _refresh_selection_summary(self, _checked: bool = False) -> None:
        selected = self.selected_data_stages()
        if not selected:
            self.selection_summary.setText("请至少选择一个读取阶段。")
            self.selection_summary.setProperty("status", "error")
        else:
            inventory_note = ""
            if "inventory" in selected:
                categories = self.selected_inventory_categories()
                if categories:
                    labels = "、".join(INVENTORY_CATEGORY_LABELS[item] for item in categories)
                    inventory_note = f"；仓库将扫描 {labels}，预计耗时较长"
                else:
                    self.selection_summary.setText("仓库阶段至少需要选择一个分类。")
                    self.selection_summary.setProperty("status", "error")
                    self.selection_summary.style().unpolish(self.selection_summary)
                    self.selection_summary.style().polish(self.selection_summary)
                    return
            self.selection_summary.setText(
                f"将读取 {len(selected)} 个阶段并自动合并保存{inventory_note}。"
            )
            self.selection_summary.setProperty("status", "success")
        self.selection_summary.style().unpolish(self.selection_summary)
        self.selection_summary.style().polish(self.selection_summary)

    def selected_data_stages(self) -> list[str]:
        return [stage for stage in PLAYER_DATA_STAGE_ORDER if self._stage_checks[stage].isChecked()]

    def selected_inventory_categories(self) -> list[str]:
        return [
            category
            for category in PLAYER_DATA_INVENTORY_CATEGORY_ORDER
            if self._inventory_category_checks[category].isChecked()
        ]

    def _sync_inventory_category_controls(self, _checked: bool = False) -> None:
        enabled = self._stage_checks["inventory"].isChecked()
        for check in self._inventory_category_checks.values():
            check.setEnabled(enabled)

    def collect_inputs(self) -> dict[str, Any]:
        stages = self.selected_data_stages()
        if not stages:
            raise ValueError("用户数据更新至少需要选择一个读取阶段。")
        categories = self.selected_inventory_categories()
        if "inventory" in stages and not categories:
            raise ValueError("仓库阶段至少需要选择道具或材料中的一项。")
        inputs = {
            "stages": stages,
            "inventory_categories": categories or ["items"],
        }
        self._settings.save_player_data_inputs(inputs)
        return inputs

    def _request_cache(self) -> None:
        self.snapshot_message.setText("正在读取本地缓存……")
        self.cacheRequested.emit()

    def set_runner_busy(self, busy: bool) -> None:
        self.cache_button.setEnabled(not busy)

    def show_error(self, message: str) -> None:
        raw_message = str(message or "").strip()
        if _NO_CACHE_ERROR_MARKER in raw_message:
            display_message = "没有可读缓存。"
        else:
            display_message = f"读取失败：{raw_message or '未知错误'}"
        self.snapshot_message.setText(display_message)
        self.snapshot_message.setProperty("status", "error")
        self.snapshot_message.style().unpolish(self.snapshot_message)
        self.snapshot_message.style().polish(self.snapshot_message)

    def set_snapshot(self, snapshot: Mapping[str, Any], *, message: str = "已读取最新缓存") -> None:
        self._snapshot = copy.deepcopy(dict(snapshot))
        self.snapshot_message.setText(message)
        self.snapshot_message.setProperty("status", "success")
        self.snapshot_message.style().unpolish(self.snapshot_message)
        self.snapshot_message.style().polish(self.snapshot_message)
        self._render_snapshot()
        self.tabs.setCurrentIndex(1)

    def apply_refresh_result(self, refreshed: Mapping[str, Any]) -> None:
        fresh = copy.deepcopy(dict(refreshed))
        merged = copy.deepcopy(self._snapshot)
        for key in ("location", "profile"):
            if key in fresh:
                merged[key] = fresh[key]
        fresh_currencies = fresh.get("currencies")
        if isinstance(fresh_currencies, Mapping):
            currencies = merged.setdefault("currencies", {})
            if not isinstance(currencies, dict):
                currencies = {}
                merged["currencies"] = currencies
            currencies.update(copy.deepcopy(dict(fresh_currencies)))
        if "inventory" in fresh:
            categories = self._inventory_categories(merged.get("inventory"))
            categories.update(self._inventory_categories(fresh.get("inventory")))
            merged["inventory"] = {
                "schema_version": 2,
                "categories": categories,
            }
        if "characters" in fresh:
            merged["characters"] = copy.deepcopy(fresh["characters"])
        fresh_status = fresh.get("status")
        if isinstance(fresh_status, Mapping):
            status = merged.setdefault("status", {})
            if not isinstance(status, dict):
                status = {}
                merged["status"] = status
            for key in ("cargo", "clarity", "fatigue"):
                if key in fresh_status:
                    status[key] = copy.deepcopy(fresh_status[key])

        fresh_metadata = fresh.get("metadata")
        if isinstance(fresh_metadata, Mapping):
            metadata = merged.setdefault("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
                merged["metadata"] = metadata
            section_times = metadata.setdefault("section_updated_at", {})
            if not isinstance(section_times, dict):
                section_times = {}
                metadata["section_updated_at"] = section_times
            fresh_times = fresh_metadata.get("section_updated_at")
            if isinstance(fresh_times, Mapping):
                section_times.update(copy.deepcopy(dict(fresh_times)))
            metadata["section_updated_at"] = {
                stage: section_times[stage]
                for stage in PLAYER_DATA_STAGE_ORDER
                if stage in section_times
            }
            refreshed_at = fresh_metadata.get("refreshed_at")
            if refreshed_at:
                metadata["updated_at"] = refreshed_at
            category_times = metadata.setdefault("inventory_category_updated_at", {})
            if not isinstance(category_times, dict):
                category_times = {}
                metadata["inventory_category_updated_at"] = category_times
            fresh_category_times = fresh_metadata.get("inventory_category_updated_at")
            if isinstance(fresh_category_times, Mapping):
                category_times.update(copy.deepcopy(dict(fresh_category_times)))
            metadata["persisted"] = bool(fresh_metadata.get("persisted"))

        persisted = bool(dict(fresh_metadata or {}).get("persisted"))
        self.set_snapshot(
            merged,
            message="本次结果已合并保存" if persisted else "本次结果仅显示，未写入缓存",
        )

    def _render_snapshot(self) -> None:
        snapshot = self._snapshot
        metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), Mapping) else {}
        updated_at = metadata.get("updated_at") or metadata.get("refreshed_at")
        self.snapshot_updated.setText(
            f"整份数据更新于 {_format_timestamp(updated_at)}" if updated_at else "尚未读取缓存"
        )
        section_times = metadata.get("section_updated_at")
        section_times = section_times if isinstance(section_times, Mapping) else {}
        for stage, label in self._stage_times.items():
            label.setText(_format_timestamp(section_times.get(stage)))

        profile = snapshot.get("profile") if isinstance(snapshot.get("profile"), Mapping) else {}
        location = snapshot.get("location") if isinstance(snapshot.get("location"), Mapping) else {}
        nickname = profile.get("nickname") or "--"
        uid = profile.get("uid") or "--"
        level = profile.get("level") if profile.get("level") is not None else "--"
        city = location.get("current_city") or "--"
        self.identity_label.setText(f"账号：{nickname} · UID {uid} · Lv.{level} · {city}")

        status = snapshot.get("status") if isinstance(snapshot.get("status"), Mapping) else {}
        self.status_label.setText(
            "状态："
            f"货舱 {_ratio_text(status.get('cargo'))}   "
            f"澄明度 {_ratio_text(status.get('clarity'))}   "
            f"疲劳 {_ratio_text(status.get('fatigue'))}"
        )
        currencies = (
            snapshot.get("currencies")
            if isinstance(snapshot.get("currencies"), Mapping)
            else {}
        )
        iron = currencies.get("iron_coins")
        birch = currencies.get("birch_stone")
        iron_text = f"{int(iron):,}" if isinstance(iron, (int, float)) else "--"
        birch_text = f"{int(birch):,}" if isinstance(birch, (int, float)) else "--"
        self.currency_label.setText(f"货币：铁盟币 {iron_text}   桦石 {birch_text}")

        self._render_inventory()
        self._render_characters()

    @staticmethod
    def _inventory_categories(payload: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(payload, Mapping):
            return {}
        raw_categories = payload.get("categories")
        if isinstance(raw_categories, Mapping):
            return {
                category: copy.deepcopy(dict(raw_categories[category]))
                for category in PLAYER_DATA_INVENTORY_CATEGORY_ORDER
                if isinstance(raw_categories.get(category), Mapping)
            }
        category = str(payload.get("category") or "").strip()
        if category in PLAYER_DATA_INVENTORY_CATEGORY_ORDER:
            return {category: copy.deepcopy(dict(payload))}
        if isinstance(payload.get("items"), list):
            return {"items": copy.deepcopy(dict(payload))}
        return {}

    def _render_inventory(self) -> None:
        if not hasattr(self, "inventory_table"):
            return
        inventory = self._snapshot.get("inventory")
        categories = self._inventory_categories(inventory)
        category = str(self.inventory_category_combo.currentData() or "items")
        category_payload = categories.get(category, {})
        result_key = "items" if category == "items" else "materials"
        entries = category_payload.get(result_key)
        items = list(entries) if isinstance(entries, list) else []
        expiry_recognition_disabled = (
            category == "items"
            and category_payload.get("expiry_recognition_enabled") is False
        )
        self.inventory_table.setHorizontalHeaderLabels(
            (INVENTORY_CATEGORY_LABELS[category], "数量", "期限")
        )
        self.inventory_table.setColumnHidden(
            2,
            category == "materials" or expiry_recognition_disabled,
        )
        self.inventory_table.setRowCount(len(items))
        for row, item in enumerate(items):
            entry = item if isinstance(item, Mapping) else {}
            expiry = entry.get("expiry") if isinstance(entry.get("expiry"), Mapping) else {}
            expiry_text = f"{expiry.get('value')} 天" if expiry.get("value") is not None else "--"
            values = (
                entry.get("name")
                or entry.get("item_id")
                or entry.get("material_id")
                or "--",
                entry.get("count"),
                expiry_text,
            )
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(str(value if value is not None else "--"))
                if column in {1, 2}:
                    table_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.inventory_table.setItem(row, column, table_item)
        stack_count = category_payload.get("matched_stack_count")
        pages = category_payload.get("pages_scanned")
        details = [f"{len(items)} 种记录"] if items else ["尚无数据"]
        if stack_count is not None:
            details.append(f"{stack_count} 个物品堆")
        if pages is not None:
            details.append(f"{pages} 页")
        if expiry_recognition_disabled:
            details.append("期限识别已暂停，限时道具按类型合并")
        metadata = (
            self._snapshot.get("metadata")
            if isinstance(self._snapshot.get("metadata"), Mapping)
            else {}
        )
        category_times = metadata.get("inventory_category_updated_at")
        if isinstance(category_times, Mapping) and category_times.get(category):
            details.append(f"更新于 {_format_timestamp(category_times.get(category))}")
        self.inventory_summary.setText(" · ".join(details))
        self._filter_inventory(self.inventory_search.text())

    def _filter_inventory(self, text: str) -> None:
        needle = str(text or "").strip().lower()
        for row in range(self.inventory_table.rowCount()):
            name_item = self.inventory_table.item(row, 0)
            name = name_item.text().lower() if name_item is not None else ""
            self.inventory_table.setRowHidden(row, bool(needle and needle not in name))

    def _render_characters(self) -> None:
        if not hasattr(self, "character_table"):
            return
        payload = self._snapshot.get("characters")
        payload = payload if isinstance(payload, Mapping) else {}
        raw_entries = payload.get("entries")
        entries = list(raw_entries) if isinstance(raw_entries, list) else []
        self.character_table.setRowCount(len(entries))
        for row, raw_entry in enumerate(entries):
            entry = raw_entry if isinstance(raw_entry, Mapping) else {}
            name = entry.get("name") or entry.get("character_id") or "--"
            raw_stars = entry.get("stars")
            try:
                stars = max(0, min(int(raw_stars), 5))
                star_text = f"{'★' * stars}{'☆' * (5 - stars)}（{stars}/5）"
            except (TypeError, ValueError):
                star_text = "--"
            name_item = QTableWidgetItem(str(name))
            star_item = QTableWidgetItem(star_text)
            star_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.character_table.setItem(row, 0, name_item)
            self.character_table.setItem(row, 1, star_item)

        pages = payload.get("pages_scanned")
        details = [f"{len(entries)} 个角色"] if entries else ["尚无数据"]
        if pages is not None:
            details.append(f"{pages} 页")
        metadata = (
            self._snapshot.get("metadata")
            if isinstance(self._snapshot.get("metadata"), Mapping)
            else {}
        )
        section_times = metadata.get("section_updated_at")
        if isinstance(section_times, Mapping) and section_times.get("characters"):
            details.append(f"更新于 {_format_timestamp(section_times.get('characters'))}")
        self.character_summary.setText(" · ".join(details))


__all__ = ["PlayerDataPanel", "STAGE_DEFINITIONS"]
