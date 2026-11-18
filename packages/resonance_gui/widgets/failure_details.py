"""On-demand diagnostics for user-facing failure summaries."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMenu, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)

from packages.aura_core.observability.failure_evidence import evidence_from
from packages.aura_core.observability.logging.log_session import get_log_session, resolve_log_root, safe_managed_path

from ..logic import extract_run_id, normalize_run_payload, pretty_json
from ..paths import resolve_application_root
from ..update_checker import current_version_label


class FailureDetailsButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__("排查信息…", parent)
        self._summary = ""
        self._payload: dict[str, Any] | None = None
        self.clicked.connect(self._show_details)
        self.hide()

    def set_report(self, summary: str = "", payload: Mapping[str, Any] | None = None) -> None:
        self._summary = summary
        self._payload = normalize_run_payload(payload) if payload is not None else None
        if self._payload is not None and "final_result" in self._payload:
            self._payload.pop("final_result_json", None)
        self.setVisible(self._payload is not None)

    @staticmethod
    def _screenshots(payload: dict[str, Any], log_root: Path) -> list[Path]:
        run = payload.get("run")
        detail = run.get("detail") if isinstance(run, dict) else payload.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        sources = (payload, detail, payload.get("final_result"), detail.get("final_result"))
        screenshots = []
        for source in sources:
            for ref in evidence_from(source):
                name = ref.get("screenshot")
                if not isinstance(name, str) or Path(name).parts[:1] != ("errors",):
                    continue
                try:
                    path = safe_managed_path(log_root, name)
                    if path.suffix.lower() == ".png" and path.is_file() and path not in screenshots:
                        screenshots.append(path)
                except (ValueError, OSError):
                    continue
        return screenshots

    def _show_details(self) -> None:
        if self._payload is None:
            return
        dialog = QDialog(self.window())
        dialog.setWindowTitle("排查信息")
        dialog.resize(720, 480)
        layout = QVBoxLayout(dialog)
        summary = QLabel(self._summary, dialog)
        summary.setTextFormat(Qt.TextFormat.PlainText)
        summary.setWordWrap(True)
        layout.addWidget(summary)
        details = QPlainTextEdit(dialog)
        details.setReadOnly(True)
        details.setPlainText(pretty_json({
            "version": current_version_label(),
            "run_id": extract_run_id(self._payload),
            "result": self._payload,
        }))
        layout.addWidget(details, 1)
        actions = QHBoxLayout()
        copy = QPushButton("复制排查信息", dialog)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(
            f"{self._summary}\n\n{details.toPlainText()}"
        ))
        actions.addWidget(copy)
        session = get_log_session()
        log_root = session.root if session else resolve_log_root(resolve_application_root())
        screenshots = self._screenshots(self._payload, log_root)
        if screenshots:
            button = QPushButton("查看截图", dialog)
            if len(screenshots) == 1:
                button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(screenshots[0]))))
            else:
                menu = QMenu(button)
                for index, path in enumerate(screenshots, start=1):
                    action = menu.addAction(f"失败截图 {index}")
                    action.triggered.connect(lambda _checked=False, image=path: QDesktopServices.openUrl(
                        QUrl.fromLocalFile(str(image))
                    ))
                button.setMenu(menu)
            actions.addWidget(button)
        if log_root.is_dir():
            logs = QPushButton("打开日志目录", dialog)
            logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_root))))
            actions.addWidget(logs)
        actions.addStretch()
        close = QPushButton("关闭", dialog)
        close.clicked.connect(dialog.reject)
        actions.addWidget(close)
        layout.addLayout(actions)
        dialog.exec()
