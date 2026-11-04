"""Searchable offline tutorial with navigation-only links to real settings."""

from __future__ import annotations

from html import escape, unescape
import re
from urllib.parse import quote, unquote

from PySide6.QtCore import QPointF, QSignalBlocker, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..tutorial_content import TUTORIAL_CHAPTERS


def _tutorial_help_icon() -> QIcon:
    """Draw a balanced help glyph at native and high-DPI toolbar sizes."""
    icon = QIcon()
    for mode, color in (
        (QIcon.Mode.Normal, "#68775d"),
        (QIcon.Mode.Active, "#45583c"),
        (QIcon.Mode.Disabled, "#a39f92"),
    ):
        for scale in (1, 2, 3):
            pixmap = QPixmap(24 * scale, 24 * scale)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(scale, scale)
            pen = QPen(QColor(color), 1.65)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawEllipse(QPointF(12, 12), 9.25, 9.25)
            question = QPainterPath(QPointF(9.2, 9))
            question.cubicTo(9.5, 6.1, 14.9, 6.1, 14.9, 9.2)
            question.cubicTo(14.9, 11.3, 12, 11.3, 12, 13.4)
            painter.drawPath(question)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(12, 16.8), 0.95, 0.95)
            painter.end()
            pixmap.setDevicePixelRatio(scale)
            icon.addPixmap(pixmap, mode)
    return icon


class TutorialHelpButton(QToolButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tutorialHelpButton")
        self.setFixedSize(34, 34)
        self.setIcon(_tutorial_help_icon())
        self.setIconSize(QSize(22, 22))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCheckable(True)
        self.setAccessibleName("使用教程")
        self.setToolTip("使用教程 · 功能 Wiki（F1）\n查参数作用、联动规则与操作步骤")
        self.setShortcut("F1")


class TutorialPage(QWidget):
    destinationRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_row = -1
        self._history: list[tuple[int, int]] = []
        self._history_position = -1
        self._render_revision = 0
        self._items: dict[int, QTreeWidgetItem] = {}
        self._groups: dict[str, QTreeWidgetItem] = {}
        self._search_text = tuple(
            " ".join((chapter.title, chapter.category, *chapter.keywords,
                      unescape(re.sub(r"<[^>]+>", " ", chapter.body)))).casefold()
            for chapter in TUTORIAL_CHAPTERS
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        heading = QLabel("使用教程 · 功能 Wiki", self)
        heading.setObjectName("workflowTitle")
        root.addWidget(heading)
        hint = QLabel("按功能查参数作用、联动规则与实例；第一次使用可先看「先跑通一次」。全部内容可离线查看。", self)
        hint.setWordWrap(True)
        hint.setProperty("caption", True)
        root.addWidget(hint)

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索功能或参数：疲劳预算、单本收益阈值、便当、刷新轮数…")
        self.search.setAccessibleName("搜索使用教程")
        self.search.setClearButtonEnabled(True)
        root.addWidget(self.search)

        body = QHBoxLayout()
        body.setSpacing(14)
        self.chapters = QTreeWidget(self)
        self.chapters.setObjectName("tutorialChapters")
        self.chapters.setAccessibleName("功能 Wiki 分类与条目")
        self.chapters.setHeaderHidden(True)
        self.chapters.setMinimumWidth(180)
        self.chapters.setMaximumWidth(260)
        self.chapters.setIndentation(14)
        self.chapters.setExpandsOnDoubleClick(True)
        self.chapters.setWordWrap(True)
        self.chapters.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for index, chapter in enumerate(TUTORIAL_CHAPTERS):
            if chapter.category not in self._groups:
                group = QTreeWidgetItem(self.chapters, [chapter.category])
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                font = group.font(0)
                font.setBold(True)
                group.setFont(0, font)
                self._groups[chapter.category] = group
            item = QTreeWidgetItem(self._groups[chapter.category], [chapter.title])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            item.setToolTip(0, chapter.title)
            self._items[index] = item
        self._groups[TUTORIAL_CHAPTERS[0].category].setExpanded(True)
        body.addWidget(self.chapters)

        article = QVBoxLayout()
        article.setSpacing(8)
        toolbar = QHBoxLayout()
        self.back_button = QToolButton(self)
        self.back_button.setText("←")
        self.back_button.setAccessibleName("返回上一篇浏览记录")
        self.back_button.setToolTip("返回上一篇浏览记录")
        self.forward_button = QToolButton(self)
        self.forward_button.setText("→")
        self.forward_button.setAccessibleName("前进到下一篇浏览记录")
        self.forward_button.setToolTip("前进到下一篇浏览记录")
        toolbar.addWidget(self.back_button)
        toolbar.addWidget(self.forward_button)
        self.breadcrumb = QLabel(self)
        self.breadcrumb.setProperty("caption", True)
        self.breadcrumb.setWordWrap(True)
        toolbar.addWidget(self.breadcrumb, 1)
        self.outline = QComboBox(self)
        self.outline.setAccessibleName("本页目录")
        self.outline.setMinimumWidth(110)
        self.outline.setMaximumWidth(210)
        toolbar.addWidget(self.outline)
        article.addLayout(toolbar)
        self.reader = QTextBrowser(self)
        self.reader.setObjectName("tutorialReader")
        self.reader.setAccessibleName("教程正文")
        self.reader.setOpenLinks(False)
        self.reader.setOpenExternalLinks(False)
        self.reader.document().setDefaultStyleSheet(
            "body { color: #38342e; font-family: 'Microsoft YaHei UI', 'Segoe UI'; "
            "font-size: 14px; } "
            "h1 { font-size: 20px; margin-bottom: 16px; } "
            "h2 { font-size: 16px; margin-top: 20px; margin-bottom: 8px; } "
            "h3 { font-size: 14px; margin-top: 16px; } "
            "p, li { line-height: 150%; } li { margin-bottom: 10px; } "
            "table { border-collapse: collapse; margin-top: 10px; margin-bottom: 14px; } "
            "th { background-color: #e8eddf; text-align: left; } "
            "td, th { border: 1px solid #d4c6ad; padding: 8px; } "
            "a { color: #526b43; text-decoration: underline; }"
        )
        self.reader.document().setDocumentMargin(20)
        article.addWidget(self.reader, 1)
        body.addLayout(article, 1)
        root.addLayout(body, 1)

        footer = QHBoxLayout()
        self.previous_button = QPushButton("上一节", self)
        self.next_button = QPushButton("下一节", self)
        self.position = QLabel(self)
        self.position.setProperty("caption", True)
        self.action_button = QPushButton(self)
        self.action_button.setObjectName("primaryButton")
        self.action_button.setToolTip("只打开对应页面，不启动任务或修改参数。")
        footer.addWidget(self.previous_button)
        footer.addWidget(self.position)
        footer.addWidget(self.next_button)
        footer.addStretch(1)
        footer.addWidget(self.action_button)
        root.addLayout(footer)

        self.chapters.currentItemChanged.connect(self._chapter_changed)
        self.search.textChanged.connect(self._filter_chapters)
        self.previous_button.clicked.connect(lambda: self._move(-1))
        self.next_button.clicked.connect(lambda: self._move(1))
        self.action_button.clicked.connect(self._open_destination)
        self.back_button.clicked.connect(lambda: self._visit_history(-1))
        self.forward_button.clicked.connect(lambda: self._visit_history(1))
        self.outline.activated.connect(self._scroll_to_section)
        self.reader.anchorClicked.connect(self._open_link)
        self._select_row(0)

    def _visible_rows(self) -> list[int]:
        return [row for row, item in self._items.items() if not item.isHidden()]

    def _apply_filter(self, query: str) -> list[int]:
        terms = query.casefold().split()
        with QSignalBlocker(self.chapters):
            for row, text in enumerate(self._search_text):
                self._items[row].setHidden(not all(term in text for term in terms))
            for group in self._groups.values():
                group.setHidden(all(group.child(i).isHidden() for i in range(group.childCount())))
                if terms and not group.isHidden():
                    group.setExpanded(True)
        return self._visible_rows()

    def _filter_chapters(self, query: str) -> None:
        visible = self._apply_filter(query)
        row = self._current_row if self._current_row in visible else (visible[0] if visible else -1)
        self._select_row(row)

    def _chapter_changed(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        if current is not None:
            row = current.data(0, Qt.ItemDataRole.UserRole)
            if row is not None:
                self._select_row(int(row))

    def _save_history_position(self) -> None:
        if 0 <= self._history_position < len(self._history):
            if self._history[self._history_position][0] == self._current_row:
                self._history[self._history_position] = (
                    self._current_row, self.reader.verticalScrollBar().value(),
                )

    def _select_row(self, row: int, *, remember: bool = True, restore_scroll: int | None = None) -> None:
        if remember:
            self._save_history_position()
        if row in self._items and self._items[row].isHidden():
            with QSignalBlocker(self.search):
                self.search.clear()
            self._apply_filter("")
        self._current_row = row
        with QSignalBlocker(self.chapters):
            if row in self._items:
                item = self._items[row]
                item.parent().setExpanded(True)
                self.chapters.setCurrentItem(item)
                self.chapters.scrollToItem(item)
            else:
                self.chapters.setCurrentItem(None)
        if remember and row in self._items:
            if self._history_position < 0 or self._history[self._history_position][0] != row:
                self._history = self._history[:self._history_position + 1] + [(row, 0)]
                self._history_position = len(self._history) - 1
        self._show_chapter(row, restore_scroll=restore_scroll)

    def _show_chapter(self, row: int, *, restore_scroll: int | None = None) -> None:
        self._render_revision += 1
        visible = self._visible_rows()
        valid = row in visible
        self.action_button.setEnabled(valid)
        self.previous_button.setEnabled(valid and visible.index(row) > 0)
        self.next_button.setEnabled(valid and visible.index(row) < len(visible) - 1)
        self.back_button.setEnabled(self._history_position > 0)
        self.forward_button.setEnabled(self._history_position < len(self._history) - 1)
        self.outline.clear()
        self.outline.addItem("本页目录", "")
        if not valid:
            self.position.setText("0 / 0")
            self.action_button.setText("打开对应页面")
            self.breadcrumb.setText("没有匹配条目")
            self.outline.setEnabled(False)
            self.reader.setExtraSelections([])
            self.reader.setHtml("<h1>没有找到相关章节</h1><p>请换一个关键词，或清空搜索查看全部教程。</p>")
            return
        chapter = TUTORIAL_CHAPTERS[row]
        self.position.setText(f"{visible.index(row) + 1} / {len(visible)}")
        self.action_button.setText(chapter.action_label)
        self.breadcrumb.setText(f"{chapter.category} / {chapter.title}")

        def add_section(match: re.Match[str]) -> str:
            anchor = f"wiki-section-{self.outline.count()}"
            title = unescape(re.sub(r"<[^>]+>", "", match.group(2)))
            self.outline.addItem(title, anchor)
            return f'<h{match.group(1)}><a name="{anchor}"></a>{match.group(2)}</h{match.group(1)}>'

        body = re.sub(r"<h([23])(?:\s[^>]*)?>(.*?)</h\1>", add_section, chapter.body, flags=re.I | re.S)
        self.outline.setEnabled(self.outline.count() > 1)
        related = [
            other for other in TUTORIAL_CHAPTERS
            if other.category == chapter.category and other.title != chapter.title
        ]
        if related:
            links = " · ".join(
                f'<a href="wiki:{quote(other.title, safe="")}">{escape(other.title)}</a>'
                for other in related[:3]
            )
            body += f"<h2>同类条目</h2><p>{links}</p>"
        self.reader.setHtml(f"<html><body><h1>{escape(chapter.title)}</h1>{body}</body></html>")
        self.reader.verticalScrollBar().setValue(0)
        self._highlight_search(scroll_to_match=restore_scroll is None)
        if restore_scroll is not None:
            revision = self._render_revision
            self.reader.verticalScrollBar().setValue(restore_scroll)
            # Restore again once the text layout has adjusted the scroll range.
            QTimer.singleShot(0, lambda: self._restore_scroll_position(revision, restore_scroll))

    def _restore_scroll_position(self, revision: int, position: int) -> None:
        if self._render_revision == revision:
            self.reader.verticalScrollBar().setValue(position)

    def _highlight_search(self, *, scroll_to_match: bool = True) -> None:
        selections = []
        first_match = None
        for term in self.search.text().split():
            cursor = QTextCursor(self.reader.document())
            while len(selections) < 300:
                cursor = self.reader.document().find(term, cursor)
                if cursor.isNull():
                    break
                selection = QTextEdit.ExtraSelection()
                selection.cursor = cursor
                selection.format.setBackground(QColor("#f7e5ae"))
                selection.format.setForeground(QColor("#38342e"))
                selections.append(selection)
                if first_match is None:
                    first_match = QTextCursor(cursor)
        self.reader.setExtraSelections(selections)
        if first_match is not None and scroll_to_match:
            first_match.clearSelection()
            self.reader.setTextCursor(first_match)
            self.reader.ensureCursorVisible()

    def _scroll_to_section(self, index: int) -> None:
        anchor = self.outline.itemData(index)
        if anchor:
            self.reader.scrollToAnchor(str(anchor))

    def _open_link(self, url: QUrl) -> None:
        if url.scheme() != "wiki":
            return
        title = unquote(url.path())
        for row, chapter in enumerate(TUTORIAL_CHAPTERS):
            if chapter.title == title:
                self._select_row(row)
                return

    def _visit_history(self, offset: int) -> None:
        position = self._history_position + offset
        if 0 <= position < len(self._history):
            self._save_history_position()
            self._history_position = position
            row, scroll = self._history[position]
            self._select_row(row, remember=False, restore_scroll=scroll)

    def _move(self, offset: int) -> None:
        visible = self._visible_rows()
        current = self._current_row
        if current in visible:
            position = visible.index(current) + offset
            if 0 <= position < len(visible):
                self._select_row(visible[position])

    def _open_destination(self) -> None:
        row = self._current_row
        if row in self._visible_rows():
            self.destinationRequested.emit(TUTORIAL_CHAPTERS[row].destination)
