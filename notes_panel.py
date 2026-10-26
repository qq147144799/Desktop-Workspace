"""
便签面板：列表 + 颜色 + 提醒 + CSV 导入导出。

每条便签一行，左侧色条点一下改颜色，右侧时间点一下设提醒，双击改正文。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from PySide6.QtCore import (QAbstractListModel, QDateTime, QEvent, QMimeData,
                            QModelIndex, QPoint, QRect, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QColor, QCursor, QDrag, QFont, QFontMetrics, QIcon,
                           QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QDateTimeEdit, QDialog,
                               QFileDialog, QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QLabel, QLineEdit, QListView,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox,
                               QPushButton, QStyle, QStyledItemDelegate,
                               QToolButton, QVBoxLayout, QWidget)

import notes as nt
import theme

NOTE_ROW_H = 30
REMIND_AREA_W = 68
NOTE_MIME = "application/x-sideexplorer-note"


def color_swatch_icon(bg: str, bar: str) -> QIcon:
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(bg))
    p.drawRoundedRect(0, 0, 16, 16, 4, 4)
    p.setBrush(QColor(bar))
    p.drawRoundedRect(2, 5, 3, 6, 1.5, 1.5)
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- 模型


class NoteModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[nt.Note] = []

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.items)):
            return None
        note = self.items[index.row()]
        if role == Qt.DisplayRole:
            return note.text
        if role == Qt.UserRole:
            return note
        if role == Qt.ToolTipRole:
            tip = note.text
            if note.remind:
                tip += f"\n提醒：{note.remind}"
            return tip
        return None

    def flags(self, index):
        base = super().flags(index)
        if index.isValid():
            return base | Qt.ItemIsDragEnabled | Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole) -> bool:  # noqa: N802
        if role != Qt.EditRole or not index.isValid():
            return False
        if not (0 <= index.row() < len(self.items)):
            return False
        text = str(value or "").strip()
        if not text:
            return False
        self.items[index.row()].text = text
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.UserRole])
        return True

    def move_row(self, src: int, dst: int) -> bool:
        """把 src 行移到 dst 位置（dst 是插入点，可取 rowCount）。"""
        items = self.items
        if not (0 <= src < len(items)):
            return False
        if not (0 <= dst <= len(items)):
            return False
        target = dst - 1 if dst > src else dst
        if target == src:
            return False
        if not self.beginMoveRows(QModelIndex(), src, src, QModelIndex(), dst):
            return False
        items.insert(target, items.pop(src))
        self.endMoveRows()
        return True

    def note_at(self, row: int) -> nt.Note | None:
        if 0 <= row < len(self.items):
            return self.items[row]
        return None

    def set_notes(self, items: list[nt.Note]) -> None:
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def add(self, note: nt.Note) -> int:
        row = len(self.items)
        self.beginInsertRows(QModelIndex(), row, row)
        self.items.append(note)
        self.endInsertRows()
        return row

    def extend(self, items: list[nt.Note]) -> None:
        if not items:
            return
        start = len(self.items)
        self.beginInsertRows(QModelIndex(), start, start + len(items) - 1)
        self.items.extend(items)
        self.endInsertRows()

    def remove(self, row: int) -> None:
        if 0 <= row < len(self.items):
            self.beginRemoveRows(QModelIndex(), row, row)
            self.items.pop(row)
            self.endRemoveRows()

    def touch(self) -> None:
        if not self.items:
            return
        top = self.index(0, 0)
        bottom = self.index(len(self.items) - 1, 0)
        self.dataChanged.emit(top, bottom, [Qt.DisplayRole, Qt.UserRole])


# --------------------------------------------------------------------------- 委托


class NoteDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(0, NOTE_ROW_H)

    # ------------------------------------------------------- 原地编辑
    def createEditor(self, parent, option, index):  # noqa: N802
        editor = QLineEdit(parent)
        editor.setObjectName("NoteEditor")
        editor.setFrame(False)
        return editor

    def setEditorData(self, editor, index) -> None:  # noqa: N802
        note = index.data(Qt.UserRole)
        editor.setText(note.text if note else "")

    def setModelData(self, editor, model, index) -> None:  # noqa: N802
        model.setData(index, editor.text(), Qt.EditRole)

    def updateEditorGeometry(self, editor, option, index) -> None:  # noqa: N802
        editor.setGeometry(option.rect.adjusted(16, 3, -10, -3))

    def paint(self, painter: QPainter, option, index) -> None:
        note = index.data(Qt.UserRole)
        if note is None:
            return
        now = datetime.now()
        bg, bar = nt.color_pair(note.color)
        rect = option.rect

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        body = rect.adjusted(7, 2, -7, -2)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(body, 6, 6)

        if option.state & QStyle.State_Selected:
            painter.setPen(QPen(QColor("#9DBEF5"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(body.adjusted(0, 0, -1, -1), 6, 6)
        elif option.state & QStyle.State_MouseOver:
            painter.setPen(QPen(QColor("#DFE4EC"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(body.adjusted(0, 0, -1, -1), 6, 6)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(bar))
        painter.drawRoundedRect(QRect(body.left() + 6, body.center().y() - 8, 3, 16), 1.5, 1.5)

        content_font = QFont(option.font)
        content_font.setPointSizeF(theme.LIST_FONT_PT)
        painter.setFont(content_font)
        fm = QFontMetrics(content_font)

        text_left = body.left() + 16
        text_width = max(0, body.right() - text_left - REMIND_AREA_W)

        overdue = note.remind_dt is not None and note.remind_dt <= now and not note.fired
        painter.setPen(QColor("#6B7280") if overdue else QColor("#1F2329"))
        painter.drawText(QRect(text_left, body.top(), text_width, body.height()),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         fm.elidedText(note.text, Qt.ElideRight, text_width))

        small_font = QFont(option.font)
        small_font.setPointSizeF(theme.META_FONT_PT)
        painter.setFont(small_font)
        time_rect = QRect(body.right() - REMIND_AREA_W - 2, body.top(),
                          REMIND_AREA_W, body.height())

        moment = note.remind_dt
        if moment is not None:
            painter.setPen(QColor("#D14343") if overdue else QColor("#2F6FEB"))
            painter.drawText(time_rect, Qt.AlignRight | Qt.AlignVCenter,
                             nt.human_time(moment, now))
        elif option.state & QStyle.State_MouseOver:
            painter.setPen(QColor("#B7BDC6"))
            painter.drawText(time_rect, Qt.AlignRight | Qt.AlignVCenter, "设提醒")
        painter.restore()


class NoteView(QListView):
    """便签列表：可以拖着调整先后顺序。"""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)

    def startDrag(self, actions) -> None:  # noqa: N802
        index = self.currentIndex()
        if not index.isValid():
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(NOTE_MIME, str(index.row()).encode())
        drag.setMimeData(mime)
        rect = self.visualRect(index)
        if rect.isValid():
            drag.setPixmap(self.viewport().grab(rect))
            drag.setHotSpot(QPoint(rect.width() // 2, rect.height() // 2))
        drag.exec(Qt.MoveAction)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(NOTE_MIME):
            event.setDropAction(Qt.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(NOTE_MIME):
            event.setDropAction(Qt.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasFormat(NOTE_MIME):
            event.ignore()
            return
        try:
            src = int(bytes(event.mimeData().data(NOTE_MIME)).decode())
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            return

        pos = event.position().toPoint()
        index = self.indexAt(pos)
        if index.isValid():
            rect = self.visualRect(index)
            dst = index.row() + (1 if pos.y() > rect.center().y() else 0)
        else:
            dst = self.model().rowCount()
        self.panel.move_note(src, dst)
        event.setDropAction(Qt.MoveAction)
        event.accept()


# --------------------------------------------------------------------------- 提醒对话框


class PanelPopup(QFrame):
    """嵌在面板内部的浮层：绝对定位、带阴影，不出窗口。"""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.setObjectName("PanelPopup")
        self.setFrameShape(QFrame.NoFrame)
        effect = QGraphicsDropShadowEffect(self)
        effect.setBlurRadius(18)
        effect.setOffset(0, 5)
        effect.setColor(QColor(0, 0, 0, 70))
        self.setGraphicsEffect(effect)
        self.hide()

    def show_at(self, anchor: QRect) -> None:
        self.adjustSize()
        area = self.panel.rect()
        width, height = self.width(), self.height()
        x = max(4, min(anchor.left(), max(4, area.width() - width - 4)))
        y = anchor.bottom() + 4
        if y + height > area.height() - 4:
            y = max(4, anchor.top() - height - 4)
        self.move(x, y)
        self.show()
        self.raise_()


class RemindPopup(PanelPopup):
    """选提醒时间，直接浮在便签行下面。"""

    def __init__(self, panel):
        super().__init__(panel)
        self.note: nt.Note | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        title = QLabel("提醒时间")
        title.setObjectName("PopupTitle")
        root.addWidget(title)

        self.edit = QDateTimeEdit()
        self.edit.setCalendarPopup(True)
        self.edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.edit.setFixedHeight(28)
        root.addWidget(self.edit)

        quick = QHBoxLayout()
        quick.setSpacing(6)
        for label, kind in (("1 小时后", "h1"), ("今晚 8 点", "tonight"), ("明早 9 点", "tomorrow")):
            btn = QPushButton(label)
            btn.setObjectName("PopupChip")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=kind: self._quick(k))
            quick.addWidget(btn)
        root.addLayout(quick)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.btn_clear = QPushButton("清除")
        self.btn_clear.setObjectName("PopupChip")
        self.btn_clear.clicked.connect(lambda: self._apply(clear=True))
        actions.addWidget(self.btn_clear)
        actions.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("PopupChip")
        cancel.clicked.connect(self.hide)
        actions.addWidget(cancel)
        okay = QPushButton("确定")
        okay.setDefault(True)
        okay.clicked.connect(lambda: self._apply())
        actions.addWidget(okay)
        root.addLayout(actions)

    def _quick(self, kind: str) -> None:
        now = datetime.now()
        if kind == "h1":
            value = now + timedelta(hours=1)
        elif kind == "tonight":
            value = now.replace(hour=20, minute=0, second=0, microsecond=0)
        else:
            value = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        self.edit.setDateTime(QDateTime(value))

    def open_for(self, note: nt.Note, anchor: QRect) -> None:
        self.note = note
        current = note.remind_dt or (datetime.now() + timedelta(minutes=30))
        self.edit.setDateTime(QDateTime(current))
        self.btn_clear.setEnabled(bool(note.remind))
        self.show_at(anchor)

    def _apply(self, clear: bool = False) -> None:
        note = self.note
        if note is None:
            self.hide()
            return
        note.remind = "" if clear else nt.format_time(self.edit.dateTime().toPython())
        note.fired = False
        self.hide()
        self.panel.after_remind_changed()


class NotifyCard(PanelPopup):
    """到点提醒，以卡片形式盖在便签列表上。"""

    def __init__(self, panel):
        super().__init__(panel)
        self.notes: list[nt.Note] = []
        self.setFixedWidth(max(200, panel.width() - 24))

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        title = QLabel("便签提醒")
        title.setObjectName("PopupTitle")
        root.addWidget(title)

        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setObjectName("PopupBodyStrong")
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.body)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addStretch(1)
        snooze = QPushButton("10 分钟后提醒")
        snooze.setObjectName("PopupChip")
        snooze.clicked.connect(self._snooze)
        actions.addWidget(snooze)
        okay = QPushButton("知道了")
        okay.setDefault(True)
        okay.clicked.connect(self._acknowledge)
        actions.addWidget(okay)
        root.addLayout(actions)

    def show_notes(self, notes: list[nt.Note]) -> None:
        self.notes = notes
        if len(notes) == 1:
            text = notes[0].text
            if notes[0].remind:
                text += f"\n\n提醒时间：{notes[0].remind}"
        else:
            text = f"有 {len(notes)} 条便签到点了\n\n" + "\n".join(f"· {n.text}" for n in notes)
        self.body.setText(text)
        area = self.panel.rect()
        self.adjustSize()
        x = max(4, (area.width() - self.width()) // 2)
        y = 8
        self.move(x, y)
        self.show()
        self.raise_()

    def _snooze(self) -> None:
        later = nt.format_time(datetime.now() + timedelta(minutes=10))
        for note in self.notes:
            note.remind = later
            note.fired = False
        self.notes = []
        self.hide()
        self.panel.after_remind_changed()

    def _acknowledge(self) -> None:
        """知道了 = 这次提醒处理完了，把提醒清掉，别让行尾还挂着时间。"""
        for note in self.notes:
            note.remind = ""
            note.fired = False
        self.notes = []
        self.hide()
        self.panel.after_remind_changed()


# --------------------------------------------------------------------------- 回收站对话框


class TrashDialog(QDialog):
    """便签回收站：恢复选中的、彻底删掉、或者清空。"""

    def __init__(self, entries: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("便签回收站")
        self.setMinimumSize(400, 320)
        self.entries = entries          # 直接操作调用方传进来的列表
        self.restored: list[nt.Note] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        self.hint = QLabel("")
        root.addWidget(self.hint)

        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.itemDoubleClicked.connect(lambda _i: self._restore())
        root.addWidget(self.list_widget, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.btn_restore = QPushButton("恢复选中")
        self.btn_restore.clicked.connect(self._restore)
        buttons.addWidget(self.btn_restore)
        self.btn_delete = QPushButton("彻底删除")
        self.btn_delete.clicked.connect(self._delete)
        buttons.addWidget(self.btn_delete)
        self.btn_clear = QPushButton("清空回收站")
        self.btn_clear.clicked.connect(self._clear)
        buttons.addWidget(self.btn_clear)
        buttons.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)

        self._refresh_list()

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for index, entry in enumerate(self.entries):
            note = entry["note"]
            when = entry.get("deleted_at") or ""
            label = f"{note.text}"
            if when:
                label += f"        （{when} 删除）"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, index)
            self.list_widget.addItem(item)
        count = len(self.entries)
        self.hint.setText(f"回收站里有 {count} 条便签，双击可恢复" if count else "回收站是空的")
        for btn in (self.btn_restore, self.btn_delete, self.btn_clear):
            btn.setEnabled(count > 0)
        if count and self.list_widget.currentRow() < 0:
            self.list_widget.setCurrentRow(0)

    def _selected_index(self) -> int:
        item = self.list_widget.currentItem()
        if item is None:
            return -1
        return int(item.data(Qt.UserRole))

    def _restore(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        entry = self.entries.pop(index)
        self.restored.append(entry["note"])
        self._refresh_list()
        self.list_widget.setCurrentRow(min(index, self.list_widget.count() - 1))

    def _delete(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        self.entries.pop(index)
        self._refresh_list()

    def _clear(self) -> None:
        if not self.entries:
            return
        reply = QMessageBox.question(self, "清空回收站",
                                     f"彻底删除这 {len(self.entries)} 条便签？此操作不可撤销。",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self.entries.clear()
        self._refresh_list()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        screen = self.screen()
        if screen is None:
            return
        area = screen.availableGeometry()
        if self.frameGeometry().left() < area.left():
            self.move(area.left() + 8, self.y())


# --------------------------------------------------------------------------- 面板


class NotesPanel(QWidget):
    message = Signal(str)      # 给状态栏用

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setObjectName("Notes")
        self.config = config
        self.model = NoteModel(self)
        self.model.set_notes(nt.load_notes(config.value("notes", [])))
        self.trash: list[dict] = nt.load_trash(config.value("trash_notes", []))
        self._build()
        self._refresh()
        self._update_count()
        self.remind_popup = RemindPopup(self)
        self.notify_card = NotifyCard(self)

        self._timer = QTimer(self)
        self._timer.setInterval(20000)
        self._timer.timeout.connect(self.check_reminders)
        self._timer.start()
        QTimer.singleShot(0, self._refresh)          # 布局完成后再摆空状态提示
        QTimer.singleShot(2500, self.check_reminders)

    # ------------------------------------------------------------------ 构建
    def _small_button(self, text: str, tip: str, slot) -> QToolButton:
        btn = QToolButton(self)
        btn.setObjectName("NotesBtn")
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setFixedSize(22, 22)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(slot)
        return btn

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("NotesHeader")
        header.setFixedHeight(26)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 0, 8, 0)
        hl.setSpacing(2)

        title = QLabel("便签")
        title.setObjectName("NotesTitle")
        hl.addWidget(title)
        self.count_label = QLabel("")
        self.count_label.setObjectName("NotesCount")
        hl.addSpacing(6)
        hl.addWidget(self.count_label)
        hl.addStretch(1)
        hl.addWidget(self._small_button("＋", "添加便签", self.focus_input))
        hl.addWidget(self._small_button("⋯", "更多", self.show_menu))
        root.addWidget(header)

        self.view = NoteView(self)
        self.view.setObjectName("NoteList")
        self.view.setModel(self.model)
        self.note_delegate = NoteDelegate(self)
        self.view.setItemDelegate(self.note_delegate)
        self.note_delegate.commitData.connect(self._on_edited)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.view.setEditTriggers(QAbstractItemView.DoubleClicked)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        # 双击由 editTriggers 直接进入原位编辑，不再额外连信号
        self.view.viewport().installEventFilter(self)
        root.addWidget(self.view, 1)

        self.empty_hint = QLabel("还没有便签，下面输入一行试试", self.view.viewport())
        self.empty_hint.setObjectName("NoteEmpty")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.input = QLineEdit()
        self.input.setObjectName("NoteInput")
        self.input.setPlaceholderText("记一条，回车添加")
        self.input.setFixedHeight(26)
        self.input.returnPressed.connect(self._on_input_submitted)
        root.addWidget(self.input)

    # ------------------------------------------------------------------ 基础
    def _save(self) -> None:
        self.config.setValue("notes", nt.dump_notes(self.model.items))
        self.config.setValue("trash_notes", nt.dump_trash(self.trash))
        self._update_count()

    def _trash_note(self, note: nt.Note) -> None:
        self.trash.insert(0, nt.make_trash_entry(note))
        del self.trash[nt.TRASH_LIMIT:]

    def _update_count(self) -> None:
        total = len(self.model.items)
        self.count_label.setText(str(total) if total else "")

    def _refresh(self) -> None:
        empty = self.model.rowCount() == 0
        self.empty_hint.setVisible(empty)
        if empty:
            self.empty_hint.setGeometry(self.view.viewport().rect())

    def refresh_theme(self) -> None:
        """换肤后重画一遍（委托里画的东西不吃样式表）。"""
        self.view.viewport().update()
        self.empty_hint.setStyleSheet(
            f"color: {theme.color('placeholder')}; font-size: 11px;")
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.model.rowCount() == 0:
            self.empty_hint.setGeometry(self.view.viewport().rect())

    def focus_input(self) -> None:
        self.input.setFocus()

    def _on_input_submitted(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        row = self.model.add(nt.Note(text=text))
        self._refresh()
        self._save()
        index = self.model.index(row, 0)
        self.view.setCurrentIndex(index)
        self.view.scrollToBottom()

    def has_focus(self) -> bool:
        from PySide6.QtWidgets import QApplication
        current = QApplication.focusWidget()
        if current is None:
            return False
        return current is self or self.isAncestorOf(current)

    # ------------------------------------------------------------------ 选中
    def _current_row(self) -> int:
        index = self.view.currentIndex()
        return index.row() if index.isValid() else -1

    def _context_menu(self, pos: QPoint) -> None:
        index = self.view.indexAt(pos)
        if index.isValid():
            self.view.setCurrentIndex(index)
        row = self._current_row()
        menu = QMenu(self)

        if row >= 0:
            menu.addAction("编辑内容", self._edit_current)
            up_act = menu.addAction("上移", lambda: self.move_note(row, row - 1))
            up_act.setEnabled(row > 0)
            down_act = menu.addAction("下移", lambda: self.move_note(row, row + 2))
            down_act.setEnabled(row < self.model.rowCount() - 1)
            color_menu = menu.addMenu("颜色")
            for key in nt.COLOR_ORDER:
                label, bg, bar = nt.COLORS[key]
                act = color_menu.addAction(color_swatch_icon(bg, bar), f"{label}色")
                act.triggered.connect(lambda _=False, k=key, r=row: self._set_color(r, k))
            menu.addAction("设置提醒…", lambda: self.pick_remind(row))
            if self.model.note_at(row) and self.model.note_at(row).remind:
                menu.addAction("清除提醒", lambda: self.clear_remind(row))
            menu.addSeparator()
            menu.addAction("删除这条", self.delete_selected)
            menu.addSeparator()

        menu.addAction("添加便签", self.focus_input)
        menu.addAction("导入 CSV…", self.import_csv)
        menu.addAction("导出 CSV…", self.export_csv)
        menu.addSeparator()
        menu.addAction(self._trash_label(), self.open_trash)
        menu.addAction("清空全部（移到回收站）", self.clear_all)
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _trash_label(self) -> str:
        if self.trash:
            return f"回收站（{len(self.trash)}）…"
        return "回收站…"

    def show_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("添加便签", self.focus_input)
        menu.addSeparator()
        menu.addAction("导入 CSV…", self.import_csv)
        menu.addAction("导出 CSV…", self.export_csv)
        menu.addSeparator()
        menu.addAction(self._trash_label(), self.open_trash)
        menu.addAction("清空全部（移到回收站）", self.clear_all)
        menu.exec(self.btn_menu_anchor())

    def btn_menu_anchor(self) -> QPoint:
        buttons = self.findChildren(QToolButton, "NotesBtn")
        if buttons:
            return buttons[-1].mapToGlobal(QPoint(buttons[-1].width() - 120,
                                                  buttons[-1].height() + 2))
        return QCursor.pos()

    # ------------------------------------------------------------------ 编辑
    def _edit_current(self, index=None) -> None:
        """双击 / 右键「编辑内容」都直接进入原位置编辑，不再弹输入框。"""
        row = index.row() if index is not None and index.isValid() else self._current_row()
        if row < 0:
            return
        model_index = self.model.index(row, 0)
        self.view.setCurrentIndex(model_index)
        self.view.edit(model_index)

    def rename_selected(self) -> None:
        self._edit_current()

    def delete_selected(self) -> None:
        row = self._current_row()
        if row < 0:
            return
        note = self.model.note_at(row)
        if note is not None:
            self._trash_note(note)
        self.model.remove(row)
        self._refresh()
        self._save()
        self.message.emit("已移到回收站，可从「⋯ → 回收站」恢复")

    def _set_color(self, row: int, color: str) -> None:
        note = self.model.note_at(row)
        if note is None:
            return
        note.color = color
        self.model.touch()
        self._save()

    def move_note(self, src: int, dst: int) -> None:
        """把第 src 条移动到 dst 插入点（拖拽和上移/下移都走这里）。"""
        if not self.model.move_row(src, dst):
            return
        target = dst - 1 if dst > src else dst
        target = max(0, min(target, self.model.rowCount() - 1))
        index = self.model.index(target, 0)
        self.view.setCurrentIndex(index)
        self.view.scrollTo(index)
        self._save()

    def pick_remind(self, row: int) -> None:
        note = self.model.note_at(row)
        if note is None:
            return
        rect = self.view.visualRect(self.model.index(row, 0))
        anchor = rect.translated(self.view.mapTo(self, QPoint(0, 0)))
        self.remind_popup.open_for(note, anchor)

    def clear_remind(self, row: int) -> None:
        note = self.model.note_at(row)
        if note is None:
            return
        note.remind = ""
        note.fired = False
        self.model.touch()
        self._save()

    # ------------------------------------------------------------------ 提醒
    def check_reminders(self) -> None:
        now = datetime.now()
        due = [n for n in self.model.items if n.is_due(now)]
        if not due:
            return
        for note in due:
            note.fired = True
        self.model.touch()
        self._save()
        self._show_reminder(due, now)

    def _show_reminder(self, due: list[nt.Note], now: datetime) -> None:
        self.notify_card.show_notes(due)

    def after_remind_changed(self) -> None:
        self.model.touch()
        self._save()

    def _on_edited(self, editor=None) -> None:
        QTimer.singleShot(0, self._save)

    # ------------------------------------------------------------------ CSV
    def export_csv(self) -> None:
        default = os.path.join(os.path.expanduser("~"), nt.default_export_name())
        path, _ = QFileDialog.getSaveFileName(self, "导出便签", default, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            count = nt.export_csv(nt.ensure_csv_ext(path), self.model.items)
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        self.message.emit(f"已导出 {count} 条便签到 {os.path.basename(path)}")

    def import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入便签", os.path.expanduser("~"),
                                              "CSV 文件 (*.csv);;所有文件 (*.*)")
        if not path:
            return
        try:
            incoming = nt.import_csv(path)
        except OSError as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        if not incoming:
            QMessageBox.information(self, "导入便签", "这个文件里没有读到便签。")
            return

        box = QMessageBox(self.window())
        box.setWindowTitle("导入便签")
        box.setIcon(QMessageBox.Question)
        box.setText(f"读到 {len(incoming)} 条便签，怎么处理？")
        add_button = box.addButton("追加到现有", QMessageBox.AcceptRole)
        replace_button = box.addButton("替换现有", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is add_button:
            self.model.extend(incoming)
            self.message.emit(f"已追加 {len(incoming)} 条便签")
        elif clicked is replace_button:
            self.model.set_notes(incoming)
            self.message.emit(f"已用 {len(incoming)} 条便签替换原列表")
        else:
            return
        self._refresh()
        self._save()

    def clear_all(self) -> None:
        if not self.model.items:
            return
        reply = QMessageBox.question(self.window(), "清空便签",
                                     f"把这 {len(self.model.items)} 条便签全部移到回收站？",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        for note in list(self.model.items):
            self._trash_note(note)
        self.model.set_notes([])
        self._refresh()
        self._save()
        self.message.emit("便签已移到回收站")

    def open_trash(self) -> None:
        dialog = TrashDialog(self.trash, self.window())
        dialog.exec()
        if dialog.restored:
            self.model.extend(dialog.restored)
            self._refresh()
            self.message.emit(f"已恢复 {len(dialog.restored)} 条便签")
        self._save()

    # ------------------------------------------------------------------ 事件
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.view.viewport() and event.type() == QEvent.MouseButtonPress:
            popup = getattr(self, "remind_popup", None)
            if popup is not None:
                popup.hide()
            if event.button() == Qt.LeftButton:
                pos = event.position().toPoint()
                index = self.view.indexAt(pos)
                if index.isValid():
                    rect = self.view.visualRect(index)
                    offset = pos.x() - rect.left()
                    if offset <= 14:                       # 左侧色条
                        self._pick_color_menu(index.row(), event.globalPosition().toPoint())
                        return True
                    if pos.x() >= rect.right() - REMIND_AREA_W:   # 右侧时间区
                        self.pick_remind(index.row())
                        return True
        return super().eventFilter(obj, event)

    def _pick_color_menu(self, row: int, global_pos: QPoint) -> None:
        menu = QMenu(self)
        for key in nt.COLOR_ORDER:
            label, bg, bar = nt.COLORS[key]
            act = menu.addAction(color_swatch_icon(bg, bar), f"{label}色")
            act.triggered.connect(lambda _=False, k=key, r=row: self._set_color(r, k))
        menu.exec(global_pos)
