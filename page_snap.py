"""分屏页：把正在运行的窗口像 Win7 那样贴到屏幕的左半 / 右半。

列表里每行一个程序（同一程序开多个窗口会归并成一行，操作标题最长的那个）。
右边两个小方块就是开关：◧ 贴左半、◨ 贴右半，**再点一次还原**；
点行的其它位置默认贴左半。窗口枚举和过滤直接用 closetotray 里那套。

图标按 exe 缓存 —— 枚举 + 取图标比看起来贵，不能每次 paint 都来一遍。
"""

from __future__ import annotations

import os

from PySide6.QtCore import (QAbstractListModel, QFileInfo, QModelIndex, QPoint,
                            QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileIconProvider,
                               QListView, QMenu, QStyle, QStyledItemDelegate,
                               QVBoxLayout, QWidget)

import closetotray
import list_style
import snap
import theme

ROW_H = list_style.ROW_H
ICON_SIZE = 16
CELL_W = 19
CELL_H = 20
# 图标用矢量画，不用字符 —— 小字号下 □ ✕ 这类字形容易缺字或糊成一团
BUTTONS = ("min", "left", "right", "max", "close")
BAR_W = CELL_W * len(BUTTONS)          # 五格连成一条，共 95px

_exe_icons: dict[str, QPixmap] = {}
_provider = QFileIconProvider()


def _exe_icon(exe_path: str) -> QPixmap:
    cached = _exe_icons.get(exe_path)
    if cached is not None:
        return cached
    icon = QIcon()
    if exe_path and os.path.isfile(exe_path):
        icon = _provider.icon(QFileInfo(exe_path))
    pixmap = icon.pixmap(ICON_SIZE, ICON_SIZE)
    if pixmap.isNull():               # 取不到图标的画个通用窗口形
        pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
        pixmap.fill(Qt.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(theme.color("muted")), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(2.5, 2.5, 13, 13))
        p.drawLine(QRectF(2.5, 6, 13, 0).topLeft(), QRectF(15.5, 6, 0, 0).topLeft())
        p.end()
    _exe_icons[exe_path] = pixmap
    return pixmap


def _draw_glyph(p: QPainter, box: QRectF, kind: str, color: QColor) -> None:
    """一格里的图形：最小化 _ / 左 < / 右 > / 最大化 □ / 关闭 ✕。"""
    cx, cy = box.center().x(), box.center().y()
    pen = QPen(color, 1.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if kind == "min":
        p.drawLine(QPointF(cx - 4, cy + 3.5), QPointF(cx + 4, cy + 3.5))
    elif kind == "left":
        p.drawPolyline([QPointF(cx + 2.6, cy - 4.2), QPointF(cx - 2.8, cy),
                        QPointF(cx + 2.6, cy + 4.2)])
    elif kind == "right":
        p.drawPolyline([QPointF(cx - 2.6, cy - 4.2), QPointF(cx + 2.8, cy),
                        QPointF(cx - 2.6, cy + 4.2)])
    elif kind == "max":
        p.drawRect(QRectF(cx - 3.8, cy - 3.2, 7.6, 6.4))
    elif kind == "close":
        p.drawLine(QPointF(cx - 3.4, cy - 3.4), QPointF(cx + 3.4, cy + 3.4))
        p.drawLine(QPointF(cx + 3.4, cy - 3.4), QPointF(cx - 3.4, cy + 3.4))


def _draw_bar(p: QPainter, bar: QRectF, state: str | None) -> None:
    """画一整条按钮：外壳一个圆角，内部五格（_ < > □ ✕），状态格填色。

    连成一条而不是五块独立按钮：分开要 5×(宽+间距) 才放得下，
    连体只要 95，而且看着是一组操作，有整体感。
    """
    clip = QPainterPath()
    clip.addRoundedRect(bar, 5, 5)

    p.save()
    p.setClipPath(clip)
    p.fillRect(bar, QColor(theme.color("bg")))

    # 格与格之间的细分隔线
    p.setPen(QPen(QColor(theme.color("line3")), 1))
    for i in range(1, len(BUTTONS)):
        x = bar.left() + i * CELL_W
        p.drawLine(QPointF(x, bar.top() + 4), QPointF(x, bar.bottom() - 4))

    # 当前状态那一格填色（盖掉分隔线，看着像被"按下去"）
    for i, key in enumerate(BUTTONS):
        if state == key:
            p.fillRect(QRectF(bar.left() + i * CELL_W, bar.top(), CELL_W, CELL_H),
                       QColor(theme.color("accent")))
    p.restore()

    p.setPen(QPen(QColor(theme.color("line3")), 1))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(bar.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)

    for i, key in enumerate(BUTTONS):
        active = state == key
        color = QColor("#FFFFFF") if active else QColor(theme.color("text2"))
        _draw_glyph(p, QRectF(bar.left() + i * CELL_W, bar.top(), CELL_W, CELL_H),
                    key, color)


class WindowModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[dict] = []

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.items)):
            return None
        record = self.items[index.row()]
        if role == Qt.DisplayRole:
            return record["title"]
        if role == Qt.UserRole:
            return record
        if role == Qt.ToolTipRole:
            return (f"{record['title']}\n{record['exe']}\n"
                    f"按钮：_ 最小化 · < > 贴左/右半屏 · □ 最大化 · ✕ 关闭\n"
                    f"右键可「后台运行 / 叫回前台」；再点一次同一个按钮就是还原；拖动可调整顺序")
        return None

    def set_records(self, records: list[dict]) -> None:
        self.beginResetModel()
        self.items = list(records)
        self.endResetModel()

    # ------------------------------------------------------------ 拖动排序
    def flags(self, index):
        base = super().flags(index)
        return base | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled

    def move_row(self, src: int, dst: int) -> bool:
        """把第 src 行拖到 dst 这个插入位置（0..rowCount）。"""
        if not (0 <= src < len(self.items)):
            return False
        target = dst if dst < src else dst - 1
        target = max(0, min(len(self.items) - 1, target))
        if target == src:
            return False
        if not self.beginMoveRows(QModelIndex(), src, src, QModelIndex(), dst):
            return False
        self.items.insert(target, self.items.pop(src))
        self.endMoveRows()
        return True


class SnapDelegate(QStyledItemDelegate):
    """一行一个窗口：图标 + 标题，右侧那条五格按钮（_ < > □ ✕）。"""

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(220, ROW_H)

    @staticmethod
    def bar_rect(row: QRect) -> QRectF:
        body = list_style.body_rect(row)
        return QRectF(body.right() - list_style.PAD - BAR_W,
                      body.center().y() - CELL_H / 2 + 0.5, BAR_W, CELL_H)

    def paint(self, painter: QPainter, option, index) -> None:
        record = index.data(Qt.UserRole)
        if record is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        body = list_style.paint_row(painter, option.rect, option)

        icon = _exe_icon(record.get("exe_path", ""))
        painter.drawPixmap(QPoint(body.left() + list_style.LEAD,
                                  body.center().y() - ICON_SIZE // 2), icon)

        text_left = body.left() + list_style.LEAD + ICON_SIZE + list_style.GAP
        bar = self.bar_rect(option.rect)
        avail = max(10, int(bar.left() - list_style.GAP - text_left))
        if avail >= 20:
            hidden = snap.state_of(record["hwnd"]) == "hidden"
            list_style.draw(painter, QRect(text_left, body.top(), avail, body.height()),
                            index.data(Qt.DisplayRole) or "",
                            list_style.main_font(option.font),
                            theme.color("dim" if hidden else "text"))

        _draw_bar(painter, bar, snap.state_of(record["hwnd"]))
        painter.restore()



class SnapList(QListView):
    """右侧四个按钮才动手，点标题只选中；按住行上下拖可以调顺序。

    拖拽没走 Qt 的 InternalMove —— 那套底层走 mimeData/dropMimeData，重排时机
    不好控，这里自己判位置、自己画插入线，行为可预期。
    """

    snapped = Signal(QModelIndex, str)          # "min" / "left" / "right" / "max"
    reordered = Signal(int, int)                # 源行, 插入位置

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos = None
        self._press_row = -1
        self._drag_row = -1
        self._drop_row = -1
        self.setDragDropMode(QListView.NoDragDrop)

    # ------------------------------------------------------------ 事件
    @staticmethod
    def _button_at(rect: QRect, pos: QPoint) -> str | None:
        bx = SnapDelegate.bar_rect(rect).left()
        if pos.x() < bx:
            return None
        slot = int((pos.x() - bx) // CELL_W)
        slot = max(0, min(len(BUTTONS) - 1, slot))
        return BUTTONS[slot]

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            index = self.indexAt(pos)
            if index.isValid():
                record = index.data(Qt.UserRole)
                action = (self._button_at(self.visualRect(index), pos)
                          if record is not None else None)
                if action:
                    self.setCurrentIndex(index)
                    self.snapped.emit(index, action)
                    return
                self._press_pos = pos              # 记下起点，可能是要拖
                self._press_row = index.row()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_pos is not None and self._press_row >= 0:
            moved = event.position().toPoint() - self._press_pos
            if moved.manhattanLength() >= 8:
                self._drag_row = self._press_row
                self._drop_row = self._row_at(event.position().y())
                self.setCursor(Qt.ClosedHandCursor)
                self.viewport().update()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_row >= 0:
            if self._drop_row >= 0 and self._drop_row != self._drag_row:
                self.reordered.emit(self._drag_row, self._drop_row)
            self._clear_drag()
        self._press_pos = None
        self._press_row = -1
        super().mouseReleaseEvent(event)

    def _clear_drag(self) -> None:
        self._drag_row = -1
        self._drop_row = -1
        self.unsetCursor()
        self.viewport().update()

    def _row_at(self, y: int) -> int:
        """y 落在哪两行之间 → 插入位置 0..rowCount。"""
        model = self.model()
        count = model.rowCount()
        if count == 0:
            return 0
        index = self.indexAt(QPoint(self.viewport().width() // 2, y))
        if not index.isValid():
            return count if y > 0 else 0
        rect = self.visualRect(index)
        return index.row() + (1 if y > rect.center().y() else 0)

    # ------------------------------------------------------------ 插入线
    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._drag_row < 0 or self._drop_row < 0:
            return
        model = self.model()
        count = model.rowCount()
        if count == 0:
            return
        if self._drop_row <= 0:
            y = self.visualRect(model.index(0, 0)).top()
        elif self._drop_row >= count:
            y = self.visualRect(model.index(count - 1, 0)).bottom()
        else:
            y = self.visualRect(model.index(self._drop_row, 0)).top()

        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(theme.color("accent")), 2))
        p.drawLine(QPoint(8, y), QPoint(self.viewport().width() - 8, y))
        p.end()


class SnapPage(QWidget):
    kind = "snap"
    title = "窗口管理器"
    primary_tip = ""
    can_primary = False
    message = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setObjectName("SnapPage")
        self.config = config
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(0)

        self.model = WindowModel(self)
        self.view = SnapList()
        self.view.setObjectName("SnapList")
        self.view.setModel(self.model)
        self.delegate = SnapDelegate(self)
        self.view.setItemDelegate(self.delegate)
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setFrameShape(QListView.NoFrame)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.snapped.connect(self._on_action)
        self.view.reordered.connect(self._move_row)
        self.view.customContextMenuRequested.connect(self._on_menu)
        root.addWidget(self.view, 1)

    # ------------------------------------------------------------------ 数据
    def refresh(self) -> None:
        """重新枚举，但**保留现有行序**：老窗口原地不动，新窗口追加到末尾。

        顺序一旦跟着 Z 序跳，用户刚拖好的位置就白搭了。
        """
        snap.prune_hidden()
        current = closetotray.list_top_windows(exclude_pid=os.getpid(),
                                              include=snap.hidden_windows())
        by_hwnd = {win["hwnd"]: win for win in current}
        kept = [by_hwnd[rec["hwnd"]] for rec in self.model.items
                if rec["hwnd"] in by_hwnd]
        seen = {rec["hwnd"] for rec in kept}
        fresh = [win for win in current if win["hwnd"] not in seen]
        self.model.set_records(kept + fresh)
        self.view.viewport().update()

    def _move_row(self, src: int, dst: int) -> None:
        self.model.move_row(src, dst)
        self.view.viewport().update()

    def _on_action(self, index: QModelIndex, action: str) -> None:
        record = index.data(Qt.UserRole)
        if record is None:
            return
        hwnd = record["hwnd"]
        name = record["title"][:14]

        if action in ("left", "right"):
            result = snap.snap(hwnd, action)
            if result in ("left", "right"):
                side = "左" if action == "left" else "右"
                self._flash(f"「{name}」已贴到{side}半屏（再点一次还原）")
            elif result == "restored":
                self._flash("已还原原位")
            elif result == "gone":
                self._flash("这个窗口已经关了")
                self.refresh()
            else:
                self._flash("没贴上（窗口可能不让动）")
        elif action == "max":
            result = snap.maximize(hwnd)
            if result == "maximized":
                self._flash(f"「{name}」已最大化（再点一次还原）")
            elif result == "restored":
                self._flash("已还原")
            else:
                self._flash("这个窗口已经关了")
                self.refresh()
        elif action == "close":
            snap.close_window(hwnd)
            self._flash(f"已关闭「{name}」")
            QTimer.singleShot(320, self.refresh)      # 等它真退出了再从列表消失
            return
        elif action == "hidden":
            result = snap.hide_window(hwnd)
            if result == "hidden":
                self._flash(f"「{name}」已收起来（还在列表里，点「大」或「左/右」能叫回来）")
            elif result == "shown":
                self._flash(f"「{name}」已叫回来")
            else:
                self._flash("这个窗口已经关了")
                self.refresh()
                return
            self.view.viewport().update()             # 列表不动，只更新高亮
            return
        else:                              # min
            result = snap.minimize(hwnd)
            if result == "minimized":
                self._flash(f"「{name}」已最小化（点「大」或「左/右」能叫回来）")
            elif result == "restored":
                self._flash("已还原")
            else:
                self._flash("这个窗口已经关了")
                self.refresh()
        self.view.viewport().update()      # 只重画高亮，不重排

    def _on_menu(self, pos) -> None:
        index = self.view.indexAt(pos)
        record = index.data(Qt.UserRole) if index.isValid() else None
        menu = QMenu(self)
        if record is not None:
            hidden = snap.state_of(record["hwnd"]) == "hidden"
            menu.addAction("叫回前台" if hidden else "后台运行",
                           lambda i=index: self._on_action(i, "hidden"))
            menu.addSeparator()
        menu.addAction("刷新列表", self.refresh)
        menu.addSeparator()
        restore_all = menu.addAction(f"全部还原（{snap.snapped_count()}）")
        restore_all.setEnabled(snap.snapped_count() > 0)
        restore_all.triggered.connect(self._restore_all)
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _restore_all(self) -> None:
        count = snap.restore_all()
        self.refresh()
        self._flash(f"已还原 {count} 个窗口" if count else "没有贴过的窗口")

    def _flash(self, text: str) -> None:
        self.message.emit(text)

    # ------------------------------------------------------------------ 页接口
    def activate(self) -> None:
        self.refresh()

    def deactivate(self) -> None:
        pass

    def primary(self) -> None:
        self.refresh()

    def menu(self, anchor=None) -> None:
        menu = QMenu(self)
        menu.addAction("刷新列表", self.refresh)
        menu.addAction(f"全部还原（{snap.snapped_count()}）", self._restore_all)
        if anchor is not None:
            menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        else:
            menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def refresh_theme(self) -> None:
        self.view.viewport().update()
        self.update()

    def handle_key(self, action: str) -> bool:
        return False
