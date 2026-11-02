"""剪切板页：复制过的文字攒在这儿，点一下复制回去。

只记文字（图片归「截屏管理器」管）。存储和监听都在 `clipboard_history.ClipStore`，
这里只负责把它们列出来、点一下写回剪贴板。
"""

from __future__ import annotations

import datetime

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QPoint, QRect,
                            QSize, Qt, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QLabel, QListView, QMenu,
                               QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

import list_style
import theme
from clipboard_history import ClipStore

ROW_H = list_style.ROW_H


def _when(mtime: float) -> str:
    moment = datetime.datetime.fromtimestamp(mtime)
    if moment.date() == datetime.date.today():
        return moment.strftime("%H:%M")
    return moment.strftime("%m/%d")


class ClipModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[dict] = []

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.items)):
            return None
        record = self.items[index.row()]
        text = record.get("text") or ""
        if role == Qt.DisplayRole:
            return text.split("\n", 1)[0]
        if role == Qt.UserRole:
            return record
        if role == Qt.ToolTipRole:
            return text[:500]
        return None

    def set_records(self, records: list[dict]) -> None:
        self.beginResetModel()
        self.items = list(records)
        self.endResetModel()


class ClipDelegate(QStyledItemDelegate):
    """一行一条：左边是复制过的文字，右边是复制时间。"""

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(180, ROW_H)

    def paint(self, painter: QPainter, option, index) -> None:
        record = index.data(Qt.UserRole)
        if record is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        body = list_style.paint_row(painter, option.rect, option)

        text = (record.get("text") or "").split("\n", 1)[0].strip() or "(空)"
        text_left = body.left() + list_style.PAD
        right_edge = body.right() - list_style.PAD

        main_font = list_style.main_font(option.font)
        meta_font = list_style.meta_font(option.font)
        when = _when(record["mtime"])
        when_w = list_style.width(meta_font, when)

        avail = right_edge - when_w - list_style.META_GAP - text_left
        if avail >= 30:
            list_style.draw(painter, QRect(text_left, body.top(), avail, body.height()),
                            text, main_font, theme.color("text"))
        list_style.draw(painter, QRect(right_edge - when_w, body.top(),
                                       when_w, body.height()),
                        when, meta_font, theme.color("dim"),
                        align=Qt.AlignRight | Qt.AlignVCenter)
        painter.restore()



class ClipPage(QWidget):
    kind = "clip"
    title = "剪切板"
    primary_tip = ""
    can_primary = False
    message = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setObjectName("ClipPage")
        self.config = config
        self.store = ClipStore(config, self)
        self.store.changed.connect(self.refresh)
        self._build()
        self.refresh()
        self.refresh_theme()

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(0)

        self.model = ClipModel(self)
        self.view = QListView()
        self.view.setObjectName("ClipList")
        self.view.setModel(self.model)
        self.delegate = ClipDelegate(self)
        self.view.setItemDelegate(self.delegate)
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setFrameShape(QListView.NoFrame)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.clicked.connect(self._on_clicked)
        self.view.customContextMenuRequested.connect(self._on_menu)
        root.addWidget(self.view, 1)

        self.hint = QLabel("复制文字后，就会出现在这里", self)
        self.hint.setObjectName("ClipEmpty")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setAttribute(Qt.WA_TransparentForMouseEvents)

    # ------------------------------------------------------------------ 数据
    def refresh(self) -> None:
        self.model.set_records(self.store.items())
        self.hint.setVisible(self.model.rowCount() == 0)
        self._place_hint()

    def _place_hint(self) -> None:
        if self.hint.isVisible():
            self.hint.setGeometry(self.rect())

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._place_hint()
        super().resizeEvent(event)

    # ------------------------------------------------------------------ 动作
    def _on_clicked(self, index: QModelIndex) -> None:
        record = index.data(Qt.UserRole)
        if record is None:
            return
        self.store.copy_text(record["text"])
        self._flash("已复制到剪贴板")

    def _on_menu(self, pos: QPoint) -> None:
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        record = index.data(Qt.UserRole)
        if record is None:
            return
        menu = QMenu(self)
        menu.addAction("复制到剪贴板", lambda: self._on_clicked(index))
        menu.addAction("删除这条", lambda: self._delete(record))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _delete(self, record: dict) -> None:
        self.store.delete_text(record["text"])
        self._flash("已删除这条")

    def _flash(self, text: str) -> None:
        self.message.emit(text)

    # ------------------------------------------------------------------ 页接口
    def activate(self) -> None:
        self.refresh()

    def deactivate(self) -> None:
        pass

    def primary(self) -> None:
        self.view.setFocus()

    def menu(self, anchor=None) -> None:
        menu = QMenu(self)
        menu.addAction(f"清空剪切板历史（{self.model.rowCount()} 条）", self._clear)
        if anchor is not None:
            menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        else:
            menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _clear(self) -> None:
        self.store.clear()
        self._flash("已清空剪切板历史")

    def refresh_theme(self) -> None:
        self.hint.setStyleSheet(f"color: {theme.color('placeholder')}; font-size: 11px;")
        self.view.viewport().update()

    def handle_key(self, action: str) -> bool:
        if action == "delete":
            index = self.view.currentIndex()
            record = index.data(Qt.UserRole) if index.isValid() else None
            if record is not None:
                self._delete(record)
                return True
        return False
