"""Shared, transactional product selection for visible prison-stock reads."""
from __future__ import annotations

import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout

from ..trade_catalog import trade_meta_path


def prison_product_options() -> dict[str, str]:
    names = json.loads(trade_meta_path("products.json").read_text(encoding="utf-8"))
    catalog = json.loads(trade_meta_path("prison_warehouse_goods_templates_pc.json").read_text(encoding="utf-8"))
    return {identity: names[identity] for identity, spec in catalog["products"].items()
            if spec.get("supported") is True and identity in names}


class PrisonProductDialog(QDialog):
    def __init__(self, goods: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("监狱成品读取范围")
        self.resize(420, 520)
        layout = QVBoxLayout(self)
        help_label = QLabel("选中几个商品就只读取几个；不选表示读取当前可见的全部成品。不会滚动或取货。", self)
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索商品名称或 ID")
        layout.addWidget(self.search)
        self.list = QListWidget(self)
        for identity, name in prison_product_options().items():
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, identity)
            item.setToolTip(f"商品 ID：{identity}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if identity in goods or name in goods else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        layout.addWidget(self.list)
        clear = QPushButton("清空选择（读取全部可见）", self)
        clear.clicked.connect(self.clear_selection)
        layout.addWidget(clear)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self.filter_items)

    def filter_items(self, query: str) -> None:
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setHidden(query.casefold() not in f"{item.text()} {item.data(Qt.ItemDataRole.UserRole)}".casefold())

    def clear_selection(self) -> None:
        for index in range(self.list.count()):
            self.list.item(index).setCheckState(Qt.CheckState.Unchecked)

    def selected_goods(self) -> list[str]:
        return [self.list.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.list.count())
                if self.list.item(index).checkState() == Qt.CheckState.Checked]


def prison_selection_text(goods: list[str]) -> str:
    return f"监狱成品：已选 {len(goods)} 种…" if goods else "监狱成品：全部可见…"
