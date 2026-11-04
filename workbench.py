"""下半区的工作台：一排图标页签 + 七个页面。

为什么用图标不用文字：面板最窄能拖到 260px，七个两字页签放不下，文字会被
挤成省略号。图标每个 29px，七个共 203px，任何宽度都安全；名字靠悬停提示补。

页面统一接口（见 ``Page``）：``activate`` / ``deactivate`` / ``primary`` /
``menu`` / ``refresh_theme``。容器只管切页和分发，不认识具体页面。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QSizePolicy, QStackedWidget,
                               QToolButton, QToolTip, QVBoxLayout, QWidget)

import theme

HEADER_H = 30          # 页签条高度：图标 18px 上下各留 6px，不再贴边
TAB_W = 29             # 单个页签的最小占位宽（面板拖到最窄时用这个）
TAB_MAX_W = 40         # 宽一点时每格最多摊到这么宽，再宽就留白，别把图标拉散
TAB_ICON = 18
TAB_PAD = 8            # 页签条左右外边距：别让第一个图标贴住面板边缘
SLOT_PAD = 2           # 单个格子内缩：决定选中底色离图标多远
_ICON_BASE = 16.0      # 图标坐标是按 16px 画的，放大时整体缩放


# ---------------------------------------------------------------- 图标
_ICONS: dict[tuple[str, str], QPixmap] = {}


def tab_icon(kind: str, color: str) -> QPixmap:
    """18px 线稿图标，按颜色缓存（选中是 accent，未选中是 dim）。"""
    key = (kind, color)
    cached = _ICONS.get(key)
    if cached is not None:
        return cached

    dpr = 2
    pm = QPixmap(TAB_ICON * dpr, TAB_ICON * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    # 下面的坐标都是按 16px 画布写的，这里统一缩放到 TAB_ICON
    p.scale(TAB_ICON / _ICON_BASE, TAB_ICON / _ICON_BASE)
    pen = QPen(QColor(color), 1.3)
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    s = _ICON_BASE

    if kind == "notes":                       # 便签：折角纸 + 两道横线
        p.drawPath(_note_path(s))
        p.drawLine(QPointF(5.0, 8.0), QPointF(11.0, 8.0))
        p.drawLine(QPointF(5.0, 10.8), QPointF(9.0, 10.8))
    elif kind == "calc":                      # 计算器：屏幕 + 六个点
        p.drawRoundedRect(QRectF(2.9, 1.6, 10.2, 12.8), 2.0, 2.0)
        p.drawLine(QPointF(4.6, 4.4), QPointF(11.4, 4.4))
        for row in range(2):
            for col in range(3):
                p.drawPoint(QPointF(5.6 + col * 2.4, 8.2 + row * 2.2))
    elif kind == "pad":                       # 记事本：竖线 + 三道横线
        p.drawRoundedRect(QRectF(2.9, 1.6, 10.2, 12.8), 2.0, 2.0)
        p.drawLine(QPointF(5.6, 1.9), QPointF(5.6, 14.0))
        for y in (4.6, 7.4, 10.2):
            p.drawLine(QPointF(7.4, y), QPointF(11.6, y))
    elif kind == "calendar":                  # 日历：顶栏 + 四格
        p.drawRoundedRect(QRectF(2.6, 3.2, 10.8, 11.2), 2.0, 2.0)
        p.drawLine(QPointF(2.9, 6.2), QPointF(13.1, 6.2))
        p.drawLine(QPointF(5.4, 1.5), QPointF(5.4, 3.4))
        p.drawLine(QPointF(10.6, 1.5), QPointF(10.6, 3.4))
        for dy in (8.2, 10.8):
            for dx in (5.4, 8.4):
                p.drawPoint(QPointF(dx, dy))
    elif kind == "shot":                      # 截屏：取景框
        for cx, cy, dx, dy in ((2.6, 2.6, 1, 1), (13.0, 2.6, -1, 1),
                               (2.6, 13.2, 1, -1), (13.0, 13.2, -1, -1)):
            p.drawLine(QPointF(cx, cy), QPointF(cx + 3.4 * dx, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + 3.4 * dy))
        p.drawEllipse(QRectF(6.3, 6.3, 3.0, 3.0))
    elif kind == "clip":                      # 剪切板：板子 + 顶部夹子
        p.drawRoundedRect(QRectF(3.4, 3.0, 9.2, 10.8), 1.6, 1.6)
        p.drawRoundedRect(QRectF(6.0, 1.8, 4.0, 2.8), 1.0, 1.0)
        p.drawLine(QPointF(5.6, 7.8), QPointF(10.4, 7.8))
        p.drawLine(QPointF(5.6, 10.4), QPointF(9.2, 10.4))
    elif kind == "snap":                      # 分屏：两块并排
        p.drawRect(QRectF(2.6, 3.4, 4.4, 9.2))
        p.drawRect(QRectF(9.0, 3.4, 4.4, 9.2))
    p.end()

    _ICONS[key] = pm
    return pm


def _note_path(size: float):
    """便签纸轮廓：右上角折一下。"""
    path = QPainterPath()
    m = 2.7
    right = size - m - 0.2
    bottom = size - 2.0
    fold = 4.2
    path.moveTo(m, m)
    path.lineTo(right - fold, m)
    path.lineTo(right, m + fold)
    path.lineTo(right, bottom)
    path.lineTo(m, bottom)
    path.closeSubpath()
    path.moveTo(right - fold, m)
    path.lineTo(right - fold, m + fold)
    path.lineTo(right, m + fold)
    return path


# ---------------------------------------------------------------- 页签条
class TabStrip(QWidget):
    """一排图标页签。悬停时**立刻**弹出板块名（Qt 自带的 tooltip 有 700ms 延迟，太钝）。"""

    changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._kinds: list[str] = []
        self._tips: list[str] = []
        self._index = 0
        self._hover = -1
        self.setMouseTracking(True)
        self.setFixedHeight(HEADER_H)
        self.setMinimumWidth(0)
        # Ignored：别让页签的自然宽度把窗口最小宽度顶大（那个坑踩过一次）
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def set_tabs(self, tabs: list[tuple[str, str]]) -> None:
        """tabs: [(图标名, 提示语)]"""
        self._kinds = [kind for kind, _tip in tabs]
        self._tips = [tip for _kind, tip in tabs]
        self._index = 0
        self.setMinimumWidth(TAB_PAD * 2 + len(self._kinds) * TAB_W)
        self.update()

    def count(self) -> int:
        return len(self._kinds)

    def current(self) -> int:
        return self._index

    def set_current(self, index: int, notify: bool = True) -> None:
        if not (0 <= index < len(self._kinds)) or index == self._index:
            return
        self._index = index
        QToolTip.hideText()
        self.update()
        if notify:
            self.changed.emit(index)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(TAB_PAD * 2 + len(self._kinds) * TAB_W, HEADER_H)

    def _cell(self) -> float:
        """每格实际宽度：按当前宽度均分，夹在 TAB_W ~ TAB_MAX_W 之间。

        固定 29px 时宽面板右边会空一大块，看着像没画完；均分之后整排是齐的。
        """
        count = max(1, len(self._kinds))
        span = (self.width() - TAB_PAD * 2) / count
        return max(TAB_W, min(TAB_MAX_W, span))

    def _slot(self, index: int) -> QRectF:
        cell = self._cell()
        return QRectF(TAB_PAD + index * cell + SLOT_PAD, 4,
                      cell - SLOT_PAD * 2, HEADER_H - 8)

    def _at(self, x: float) -> int:
        index = int((x - TAB_PAD) // self._cell())
        return index if 0 <= index < len(self._kinds) else -1

    # ------------------------------------------------------------------ 悬停
    def _show_tip(self, index: int) -> None:
        tip = self._tips[index] if 0 <= index < len(self._tips) else ""
        if not tip:
            QToolTip.hideText()
            return
        box = self._slot(index)
        anchor = self.mapToGlobal(QPoint(int(box.center().x()), self.height() + 2))
        QToolTip.showText(anchor, tip, self)      # 立即显示，不走 Qt 的延迟逻辑

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            index = self._at(event.position().x())
            if index >= 0:
                if index == self._index:
                    self.changed.emit(index)   # 再点一次当前页签 = 让这一页刷新一下
                else:
                    self.set_current(index)
            QToolTip.hideText()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._at(event.position().x())
        if index != self._hover:
            self._hover = index
            self.update()
            if index >= 0:
                self._show_tip(index)
            else:
                QToolTip.hideText()
        self.setCursor(Qt.PointingHandCursor if index >= 0 else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        for index, kind in enumerate(self._kinds):
            box = self._slot(index)
            active = index == self._index
            if active:
                # 选中：淡色底 + 一圈更浅的描边，比单靠底色更好认
                p.setPen(QPen(QColor(theme.color("sel_border")), 1))
                p.setBrush(QColor(theme.color("sel")))
                p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
            elif index == self._hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(theme.color("btn_hover")))
                p.drawRoundedRect(box, 7, 7)

            color = theme.color("accent") if active else (
                theme.color("icon") if index == self._hover else theme.color("dim"))
            pixmap = tab_icon(kind, color)
            p.drawPixmap(
                QPoint(int(box.center().x()) - TAB_ICON // 2,
                       int(box.center().y()) - TAB_ICON // 2), pixmap)
        p.end()



# ---------------------------------------------------------------- 容器
class Workbench(QWidget):
    message = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setObjectName("Workbench")
        self.config = config
        self.pages: list = []
        self._build()

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("WorkbenchHeader")
        header.setFixedHeight(HEADER_H)
        lay = QHBoxLayout(header)
        lay.setContentsMargins(2, 0, 6, 0)
        lay.setSpacing(4)

        self.tabs = TabStrip(self)
        lay.addWidget(self.tabs, 1)
        self.btn_more = self._small_button("⋯", "更多", self._more)
        lay.addWidget(self.btn_more)
        root.addWidget(header)

        self.stack = QStackedWidget()
        self.stack.setObjectName("WorkbenchStack")
        root.addWidget(self.stack, 1)

        for page in self._make_pages():
            if hasattr(page, "message"):
                page.message.connect(self.message)
            self.pages.append(page)
            self.stack.addWidget(page)

        self.tabs.set_tabs([(page.kind, page.title) for page in self.pages])
        self.tabs.changed.connect(self._switch)
        self._switch(0)

    def _small_button(self, text: str, tip: str, slot) -> QToolButton:
        btn = QToolButton(self)
        btn.setObjectName("WorkbenchBtn")
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setFixedSize(24, 24)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(slot)
        return btn

    def _make_pages(self) -> list:
        from page_calc import CalcPage
        from page_calendar import CalendarPage
        from page_notepad import NotepadPage
        from page_clip import ClipPage
        from page_shots import ShotsPage
        from page_snap import SnapPage
        from notes_panel import NotesPage          # 便签页就是原来的面板
        return [
            NotesPage(self.config, self),        # 待办事项
            CalendarPage(self.config, self),     # 日历
            NotepadPage(self.config, self),      # 记事本
            CalcPage(self.config, self),         # 计算器
            ClipPage(self.config, self),         # 剪切板
            ShotsPage(self.config, self),        # 截屏管理器
            SnapPage(self.config, self),         # 窗口管理器
        ]

    # ------------------------------------------------------------------ 切页
    def _switch(self, index: int) -> None:
        # 先让旧页面收摊（窗口管理器靠这个停掉轮询定时器），再激活新的
        previous = getattr(self, "_active_index", -1)
        if 0 <= previous < len(self.pages) and previous != index:
            self.pages[previous].deactivate()
        page = self.pages[index]
        self.stack.setCurrentIndex(index)
        self.tabs.set_current(index, notify=False)
        self.btn_more.setToolTip(f"{page.title} · 更多")
        page.activate()
        self._active_index = index

    def _more(self) -> None:
        self.pages[self.tabs.current()].menu(self.btn_more)

    # ------------------------------------------------------------------ 对外
    def current_page(self):
        return self.pages[self.tabs.current()]

    def show_page(self, key: str) -> None:
        """按图标名切页（菜单/快捷键用）。"""
        for index, page in enumerate(self.pages):
            if page.kind == key:
                self._switch(index)
                return

    def notes_page(self):
        for page in self.pages:
            if page.kind == "notes":
                return page
        return None

    def contains(self, widget) -> bool:
        return widget is not None and (widget is self or self.isAncestorOf(widget))

    def handle_key(self, action: str) -> bool:
        """把 F2 / Delete 这类按键交给当前页；不处理就返回 False。"""
        page = self.current_page()
        handler = getattr(page, "handle_key", None)
        return bool(handler and handler(action))

    def refresh_theme(self) -> None:
        self.tabs.update()
        for page in self.pages:
            page.refresh_theme()
        self.update()
