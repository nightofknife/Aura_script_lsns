"""Container page that groups freight and passenger trading workspaces."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config_repository import ResonanceConfigRepository
from .commerce_overview_page import CommerceOverviewPage
from .passenger_page import PassengerPage
from .trade_page import TradePage


class CommercePage(QWidget):
    """Top-level trading workspace with freight and passenger sections."""

    OVERVIEW_INDEX = 0
    FREIGHT_INDEX = 1
    PASSENGER_INDEX = 2

    def __init__(self, settings: ResonanceConfigRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui(settings)

    def _build_ui(self, settings: ResonanceConfigRepository) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame(self)
        header.setObjectName("commerceHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 10, 18, 10)
        header_layout.setSpacing(16)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("跑商", header)
        title.setObjectName("commerceTitle")
        caption = QLabel("统一管理货运与客运任务", header)
        caption.setObjectName("commerceCaption")
        title_box.addWidget(title)
        title_box.addWidget(caption)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)

        self.section_group = QButtonGroup(self)
        self.section_group.setExclusive(True)
        self.section_buttons: list[QPushButton] = []
        for index, text in enumerate(("总览", "货运", "客运")):
            button = QPushButton(text, header)
            button.setCheckable(True)
            button.setProperty("commerceNav", True)
            button.clicked.connect(lambda checked=False, section=index: self.show_section(section))
            self.section_group.addButton(button, index)
            self.section_buttons.append(button)
            header_layout.addWidget(button)
        layout.addWidget(header)

        self.section_stack = QStackedWidget(self)
        self.overview_page = CommerceOverviewPage(self.section_stack)
        self.trade_page = TradePage(settings, self.section_stack)
        self.passenger_page = PassengerPage(settings, self.section_stack)
        self.section_stack.addWidget(self.overview_page)
        self.section_stack.addWidget(self.trade_page)
        self.section_stack.addWidget(self.passenger_page)
        layout.addWidget(self.section_stack, 1)
        self.show_overview()

    def show_section(self, index: int) -> None:
        if not 0 <= index < self.section_stack.count():
            return
        self.section_stack.setCurrentIndex(index)
        self.section_buttons[index].setChecked(True)

    def show_trade(self) -> None:
        self.show_section(self.FREIGHT_INDEX)

    def show_passenger(self) -> None:
        self.show_section(self.PASSENGER_INDEX)

    def show_overview(self) -> None:
        self.show_section(self.OVERVIEW_INDEX)
