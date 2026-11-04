"""窗口管理器：把正在运行的窗口贴到左半 / 右半，或者最大化、最小化、关闭、后台运行。

列表里**每个窗口一行**（同一程序开三个窗口就是三行，能分别分屏）。
- 点图标或标题 = 点任务栏：把窗口叫到前台
- 右边五格按钮：`_` 最小化 · `<`/`>` 贴半屏 · `□` 最大化 · `✕` 关闭
- 右键给窗口设常驻状态：置顶 / 后台运行（设了后台运行，点 X 只收起来不关）
- 窗口有增减、改标题时自动刷新（1.5 秒轮询一次 EnumWindows，开销毫秒级）

图标按 exe 缓存 —— 枚举 + 取图标比看起来贵，不能每次 paint 都来一遍。
"""

from __future__ import annotations

import ctypes
import os

from PySide6.QtCore import (QAbstractListModel, QFileInfo, QModelIndex, QPoint,
                            QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileIconProvider,
                               QListView, QMenu, QMessageBox, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

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
_user32 = ctypes.windll.user32


def live_title(hwnd: int) -> str:
    """现读窗口标题：悬停要的是「此刻」的全称，不是列表刷新时的快照。"""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


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
            # 只给窗口全称；每次悬停现读，标题变了不用等列表刷新
            return live_title(record["hwnd"]) or record["title"]
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

        # 标题和按钮条之间留一小块给常驻标记（顶 / 后），一眼能看出设过什么
        flags = snap.flags_of(record["hwnd"])
        tags = "".join(label for flag, label in
                       ((snap.FLAG_TOPMOST, "顶"), (snap.FLAG_BACKGROUND, "后"))
                       if flag in flags)
        tag_font = list_style.meta_font(option.font)
        tag_w = list_style.width(tag_font, tags) + 8 if tags else 0

        avail = max(10, int(bar.left() - list_style.GAP - tag_w - text_left))
        if avail >= 20:
            hidden = snap.state_of(record["hwnd"]) == "hidden"
            list_style.draw(painter, QRect(text_left, body.top(), avail, body.height()),
                            index.data(Qt.DisplayRole) or "",
                            list_style.main_font(option.font),
                            theme.color("dim" if hidden else "text"))

        if tags:
            list_style.draw(painter,
                            QRect(bar.left() - tag_w, body.top(), tag_w, body.height()),
                            tags, tag_font, theme.color("dim"),
                            align=Qt.AlignRight | Qt.AlignVCenter)

        _draw_bar(painter, bar, snap.state_of(record["hwnd"]))
        painter.restore()



class SnapList(QListView):
    """右侧四个按钮才动手，点标题只选中；按住行上下拖可以调顺序。

    拖拽没走 Qt 的 InternalMove —— 那套底层走 mimeData/dropMimeData，重排时机
    不好控，这里自己判位置、自己画插入线，行为可预期。
    """

    snapped = Signal(QModelIndex, str)          # "min" / "left" / "right" / "max"
    reordered = Signal(int, int)                # 源行, 插入位置
    activated = Signal(QModelIndex)             # 点图标/标题 → 把窗口叫到前台

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
        row = self._press_row
        dragged = self._drag_row >= 0
        if dragged:
            if self._drop_row >= 0 and self._drop_row != self._drag_row:
                self.reordered.emit(self._drag_row, self._drop_row)
            self._clear_drag()
        self._press_pos = None
        self._press_row = -1
        super().mouseReleaseEvent(event)
        # 没拖动、按下和松开还在同一行 → 当成「点这一条」，把窗口叫到前台
        if not dragged and row >= 0 and event.button() == Qt.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid() and index.row() == row:
                self.activated.emit(index)

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


class AllWindowsRow(QWidget):
    """列表顶上固定的一行：点它的五个按钮 = 对列表里**所有**窗口做同一件事。

    不放进列表的 model 里 —— 那样要区分哨兵行、跳过拖拽/右键，全是特判；
    直接做成列表上方的一个固定行，「默认置顶」也名副其实（滚动不会把它带走）。
    """

    action = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover = False
        self.setMouseTracking(True)
        self.setFixedHeight(list_style.ROW_H + 2)
        self.setToolTip("对列表里的所有窗口执行")

    # ------------------------------------------------------------ 事件
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            action = SnapList._button_at(self.rect(), event.position().toPoint())
            if action:
                self.action.emit(action)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        on_bar = SnapList._button_at(self.rect(), event.position().toPoint())
        self.setCursor(Qt.PointingHandCursor if on_bar else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        row = self.rect()
        body = list_style.paint_row(p, row, None, hover=self._hover)

        # 图标：两个叠起来的小窗口，表示「所有」
        box = QRect(body.left() + list_style.LEAD,
                    body.center().y() - ICON_SIZE // 2, ICON_SIZE, ICON_SIZE)
        p.setPen(QPen(QColor(theme.color("text2")), 1.3))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(box.left(), box.top() + 1, 9, 8), 2, 2)
        p.drawRoundedRect(QRectF(box.left() + 5, box.top() + 6, 9, 8), 2, 2)

        text_left = box.right() + list_style.GAP
        bar = SnapDelegate.bar_rect(row)
        avail = int(bar.left()) - list_style.GAP - text_left
        if avail >= 20:
            list_style.draw(p, QRect(text_left, body.top(), avail, body.height()),
                            "全部窗口", list_style.main_font(self.font()),
                            theme.color("text"))
        _draw_bar(p, bar, None)
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
        self.view.activated.connect(self._on_activate)
        self.view.reordered.connect(self._move_row)
        self.view.customContextMenuRequested.connect(self._on_menu)

        self.all_row = AllWindowsRow(self)
        root.addWidget(self.all_row)
        root.addSpacing(2)
        root.addWidget(self.view, 1)
        self.all_row.action.connect(self._apply_to_all)

        # 有窗口开了/关了/改标题就自动刷新；1.5 秒轮询一次 EnumWindows，开销在毫秒级
        self._last_sig: tuple = ()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1500)
        self._poll_timer.timeout.connect(self._poll)

    # ------------------------------------------------------------------ 数据
    def refresh(self) -> None:
        """重新枚举，但**保留现有行序**：老窗口原地不动，新窗口追加到末尾。

        顺序一旦跟着 Z 序跳，用户刚拖好的位置就白搭了。
        """
        snap.prune_hidden()
        snap.prune_flags()
        current = closetotray.list_top_windows(exclude_pid=os.getpid(),
                                              include=snap.hidden_windows())
        by_hwnd = {win["hwnd"]: win for win in current}
        kept = [by_hwnd[rec["hwnd"]] for rec in self.model.items
                if rec["hwnd"] in by_hwnd]
        seen = {rec["hwnd"] for rec in kept}
        fresh = [win for win in current if win["hwnd"] not in seen]
        self.model.set_records(kept + fresh)
        self._apply_saved_flags()          # 置顶/后台这类设置，刷新后照样生效
        self._last_sig = tuple((w["hwnd"], w["title"]) for w in current)
        self.view.viewport().update()

    def _poll(self) -> None:
        """页面上停着的时候，盯一下窗口有没有增减、改标题、被谁关掉。"""
        revived = snap.revive_background()
        if revived:
            self._flash(f"{len(revived)} 个「后台运行」的窗口刚被关掉，已经拉回来了")
        if self._signature() != self._last_sig:
            self.refresh()

    def _signature(self) -> tuple:
        wins = closetotray.list_top_windows(exclude_pid=os.getpid(),
                                            include=snap.hidden_windows())
        return tuple((w["hwnd"], w["title"]) for w in wins)

    def _move_row(self, src: int, dst: int) -> None:
        self.model.move_row(src, dst)
        self.view.viewport().update()

    def _on_activate(self, index: QModelIndex) -> None:
        """点图标或标题 = 点任务栏图标：把那窗口叫到前台。"""
        record = index.data(Qt.UserRole)
        if record is None:
            return
        result = snap.activate(record["hwnd"])
        if result == "gone":
            self._flash("这个窗口已经关了")
            self.refresh()
            return
        self.view.viewport().update()      # 状态可能变了（隐藏→显示、最小化→还原）

    def _on_action(self, index: QModelIndex, action: str) -> None:
        record = index.data(Qt.UserRole)
        if record is None:
            return
        hwnd = record["hwnd"]
        name = record["title"][:14]

        if action in ("left", "right"):
            side = "左" if action == "left" else "右"
            result = snap.snap(hwnd, action)
            if result == "gone":
                self._flash("这个窗口已经关了")
                self.refresh()
            elif result in ("left", "right"):
                self._flash(f"「{name}」已贴到{side}半屏")
            elif result == "same":
                self._flash(f"「{name}」已经在{side}半屏了")
            else:
                self._flash("没贴上（窗口可能不让动）")
        elif action == "max":
            result = snap.maximize(hwnd)
            if result == "gone":
                self._flash("这个窗口已经关了")
                self.refresh()
            elif result == "maximized":
                self._flash(f"「{name}」已最大化")
            elif result == "same":
                self._flash(f"「{name}」已经是最大化")
            else:
                self._flash("最大化失败（窗口可能不让改）")
        elif action == "close":
            result = snap.close_or_hide(hwnd)
            if result == "hidden":
                self._flash(f"「{name}」已转到后台（后台运行模式，点 X 不关窗）")
                self.view.viewport().update()
                return
            if result == "closed":
                self._flash(f"已关闭「{name}」")
                QTimer.singleShot(320, self.refresh)  # 等它真退出了再从列表消失
                return
            self._flash("这个窗口已经关了")
            self.refresh()
            return
        else:                              # min
            result = snap.minimize(hwnd)
            if result == "gone":
                self._flash("这个窗口已经关了")
                self.refresh()
            elif result == "minimized":
                self._flash(f"「{name}」已最小化")
            else:
                self._flash(f"「{name}」已经是最小化")
        self.view.viewport().update()      # 只重画高亮，不重排

    def _on_menu(self, pos) -> None:
        # 菜单开着的时候别刷新：model 一 reset，菜单项绑的 QModelIndex 就失效了
        self._poll_timer.stop()
        try:
            menu = self._build_menu(self.view.indexAt(pos))
            menu.exec(self.view.viewport().mapToGlobal(pos))
        finally:
            if self.isVisible():
                self._poll_timer.start()

    def _build_menu(self, index: QModelIndex) -> QMenu:
        record = index.data(Qt.UserRole) if index.isValid() else None
        menu = QMenu(self)
        if record is not None:
            hwnd = record["hwnd"]
            # 「收起/叫回」那两项去掉了：点这一行就是叫回，✕ 就是收起，菜单里不再重复一遍

            # 下面两项是「这个窗口的常驻设置」，勾了就记住，刷新/重开都还在
            top = menu.addAction("窗口置顶")
            top.setCheckable(True)
            top.setChecked(snap.is_topmost(hwnd))
            top.triggered.connect(
                lambda on, h=hwnd: self._toggle_flag(h, snap.FLAG_TOPMOST, on))

            bg = menu.addAction("后台运行（点 X 不关闭）")
            bg.setCheckable(True)
            bg.setChecked(snap.has_flag(hwnd, snap.FLAG_BACKGROUND))
            bg.triggered.connect(
                lambda on, h=hwnd: self._toggle_flag(h, snap.FLAG_BACKGROUND, on))
            menu.addSeparator()
        menu.addAction("刷新列表", self.refresh)
        menu.addSeparator()
        restore_all = menu.addAction(f"全部还原（{snap.snapped_count()}）")
        restore_all.setEnabled(snap.snapped_count() > 0)
        restore_all.triggered.connect(self._restore_all)
        return menu

    # ------------------------------------------------------------------ 常驻标记
    def _toggle_flag(self, hwnd: int, name: str, on: bool) -> None:
        if name == snap.FLAG_TOPMOST:
            result = snap.set_topmost(hwnd, on)
            if result == "gone":
                self._flash("这个窗口已经关了")
                self.refresh()
                return
            if result == "failed":
                self._flash("置顶失败（窗口可能不让改）")
                return
            self._flash("已置顶这个窗口" if on else "已取消置顶")
        else:
            snap.set_flag(hwnd, name, on)
            self._flash("已设为后台运行：点 X 只收起、不关闭"
                        if on else "已取消后台运行：点 X 正常关闭")
        self._save_flags()
        self.view.viewport().update()

    @staticmethod
    def _flags_key(record: dict) -> str:
        """按「程序 + 标题」记住设置 —— hwnd 每次开机都变，靠不住。"""
        return f"{record.get('exe_path', '')}|{record.get('title', '')[:60]}"

    def _save_flags(self) -> None:
        data = {}
        for record in self.model.items:
            flags = snap.flags_of(record["hwnd"])
            if flags:
                data[self._flags_key(record)] = sorted(flags)
        self.config.setValue("snap_flags", data)

    def _apply_saved_flags(self) -> None:
        """列表刷新后，把上次记住的置顶/后台设置重新套到对应窗口上。"""
        saved = self.config.value("snap_flags", {})
        if not isinstance(saved, dict) or not saved:
            return
        for record in self.model.items:
            want = saved.get(self._flags_key(record))
            if not want:
                continue
            if snap.FLAG_TOPMOST in want and not snap.is_topmost(record["hwnd"]):
                snap.set_topmost(record["hwnd"], True)
            if snap.FLAG_BACKGROUND in want:
                snap.set_flag(record["hwnd"], snap.FLAG_BACKGROUND, True)

    def _restore_all(self) -> None:
        count = snap.restore_all()
        self.refresh()
        self._flash(f"已还原 {count} 个窗口" if count else "没有贴过的窗口")

    def _flash(self, text: str) -> None:
        self.message.emit(text)

    # ------------------------------------------------------------------ 页接口
    def activate(self) -> None:
        self.refresh()
        self._poll_timer.start()           # 只有停在这一页时才轮询

    def deactivate(self) -> None:
        self._poll_timer.stop()

    def primary(self) -> None:
        self.refresh()

    def menu(self, anchor=None) -> None:
        menu = self._build_page_menu()
        if anchor is not None:
            menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        else:
            menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _build_page_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("刷新列表", self.refresh)
        menu.addAction(f"全部还原（{snap.snapped_count()}）", self._restore_all)
        menu.addSeparator()

        # 五个按钮各来一个「全部」版本：对列表里每一个窗口做同一件事
        every = menu.addMenu("所有窗口")
        every.addAction("全部最小化", lambda: self._apply_to_all("min"))
        every.addSeparator()
        every.addAction("全部贴左半屏", lambda: self._apply_to_all("left"))
        every.addAction("全部贴右半屏", lambda: self._apply_to_all("right"))
        every.addSeparator()
        every.addAction("全部最大化", lambda: self._apply_to_all("max"))
        every.addSeparator()
        every.addAction("全部关闭…", lambda: self._apply_to_all("close"))
        return menu

    def _apply_to_all(self, action: str) -> None:
        """对列表里的每个窗口做同一件事（和点它自己那一格等价）。"""
        hwnds = [record["hwnd"] for record in self.model.items]
        if not hwnds:
            self._flash("列表里还没有窗口")
            return

        if action == "close":
            answer = QMessageBox.question(
                self, "全部关闭",
                f"关闭列表里的 {len(hwnds)} 个窗口？\n"
                "每个窗口会走自己的保存 / 确认流程。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return

        words = {"min": "最小化", "max": "最大化",
                 "left": "贴到左半屏", "right": "贴到右半屏", "close": "发出关闭"}
        done = 0
        for hwnd in hwnds:
            if action == "min":
                result = snap.minimize(hwnd)
            elif action == "max":
                result = snap.maximize(hwnd)
            elif action in ("left", "right"):
                result = snap.snap(hwnd, action)
            else:
                result = snap.close_or_hide(hwnd)
            if result not in ("gone", "failed"):
                done += 1

        if action == "close":
            self._flash(f"已向 {done} 个窗口{words[action]}")
            QTimer.singleShot(400, self.refresh)
        else:
            self._flash(f"已对 {done} 个窗口执行{words[action]}")
            self.view.viewport().update()

    def refresh_theme(self) -> None:
        self.view.viewport().update()
        self.all_row.update()
        self.update()

    def handle_key(self, action: str) -> bool:
        return False
