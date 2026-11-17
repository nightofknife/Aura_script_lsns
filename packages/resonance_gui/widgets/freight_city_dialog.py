"""Staged city selections for the freight route editor."""

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget,
)

from ..config_repository import PC_TRADE_CITY_OPTIONS


class FreightCityDialog(QDialog):
    def __init__(
        self, parent: QWidget, selected: set[str], *,
        eligible: set[str] | None = None, automatic: bool = False,
    ) -> None:
        super().__init__(parent)
        self._eligible = eligible
        self.setWindowTitle("选择参与规划城市" if eligible is None else "设置终点")
        self.resize(460, 420)
        layout = QVBoxLayout(self)
        self.automatic = QCheckBox("由程序自动选择终点", self)
        self.automatic.setChecked(automatic)
        layout.addWidget(self.automatic)
        self.automatic.setVisible(eligible is not None)
        self.selection_note = QLabel(self)
        layout.addWidget(self.selection_note)

        grid = QGridLayout()
        self.city_buttons: dict[str, QPushButton] = {}
        for index, (city_id, name) in enumerate(PC_TRADE_CITY_OPTIONS):
            button = QPushButton(name, self)
            button.setCheckable(True)
            button.setChecked(city_id in selected)
            button.setAutoDefault(False)
            button.setProperty("cityOption", True)
            button.toggled.connect(self._update_selection)
            self.city_buttons[city_id] = button
            grid.addWidget(button, index // 3, index % 3)
        layout.addLayout(grid)

        if eligible is None:
            actions = QHBoxLayout()
            for caption, checked in (("全选", True), ("清空", False)):
                button = QPushButton(caption, self)
                button.setAutoDefault(False)
                button.clicked.connect(lambda _, value=checked: self._set_all(value))
                actions.addWidget(button)
            actions.addStretch()
            layout.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("应用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.automatic.toggled.connect(self._update_selection)
        self._update_selection()

    def selected_city_ids(self) -> set[str]:
        return {city_id for city_id, button in self.city_buttons.items() if button.isChecked()}

    def _set_all(self, checked: bool) -> None:
        for button in self.city_buttons.values():
            button.setChecked(checked)

    def _update_selection(self) -> None:
        count = len(self.selected_city_ids())
        if self._eligible is None:
            self.selection_note.setText(f"已选 {count} / {len(self.city_buttons)} 城 · 至少选择两城")
            return
        automatic = self.automatic.isChecked()
        self.selection_note.setText("终点由程序自动选择" if automatic else f"已选 {count} 个终点，可多选")
        for city_id, button in self.city_buttons.items():
            eligible = city_id in self._eligible
            button.setEnabled(not automatic and (eligible or button.isChecked()))
            button.setToolTip("" if eligible else "该城市未加入参与规划城市。")
            if not eligible and button.isChecked():
                button.setToolTip("该终点已不在参与规划城市内，请取消或重新选择城市。")
