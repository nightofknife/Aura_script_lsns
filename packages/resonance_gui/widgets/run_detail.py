"""Run detail viewer."""

from __future__ import annotations

from PySide6.QtWidgets import QTextBrowser


class RunDetailView(QTextBrowser):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setPlaceholderText("任务结果会显示在这里。")

    def show_text(self, text: str) -> None:
        self.setPlainText(str(text or ""))
