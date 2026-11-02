"""日历页：一个只读的月历。

有便签提醒的日子打一个小点，点某天会在状态栏列出那天的便签。只读 ——
要加提醒还是去便签页，省得这里再暴露一套写入路径。

没用 QCalendarWidget：它内部有固定的 sizeHint，会把窗口最小宽度顶大（那个坑
踩过一次），而且样式要一层层挖。自绘一张表反而短，行高还能按可用高度自适应。
"""

from __future__ import annotations

import calendar as pycal
import datetime

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMenu, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

import notes as nt
import theme

HEADER_H = 24
WEEK_H = 16
WEEKDAYS = "一二三四五六日"


class MiniCalendar(QWidget):
    picked = Signal(object)          # datetime.date

    def __init__(self, parent=None):
        super().__init__(parent)
        today = datetime.date.today()
        self._year, self._month = today.year, today.month
        self._selected = today
        self._marks: dict[datetime.date, int] = {}
        self._hover: datetime.date | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(110)
        self.setMinimumWidth(0)

    # ------------------------------------------------------------------ 数据
    def set_marks(self, marks: dict) -> None:
        self._marks = dict(marks)
        self.update()

    def selected(self) -> datetime.date:
        return self._selected

    def current_month(self) -> tuple[int, int]:
        return self._year, self._month

    def marks(self) -> dict:
        return self._marks

    def month_label(self) -> str:
        return f"{self._year} 年 {self._month} 月"

    def set_month(self, year: int, month: int) -> None:
        while month < 1:
            year, month = year - 1, month + 12
        while month > 12:
            year, month = year + 1, month - 12
        self._year, self._month = year, month
        self.update()

    def shift_month(self, delta: int) -> None:
        self.set_month(self._year, self._month + delta)

    def select(self, day: datetime.date) -> None:
        self._selected = day
        self.set_month(day.year, day.month)
        self.picked.emit(day)
        self.update()

    def go_today(self) -> None:
        self.select(datetime.date.today())

    # ------------------------------------------------------------------ 几何
    def _rows(self) -> list[list[int]]:
        return pycal.monthcalendar(self._year, self._month)

    def _geometry_parts(self):
        rows = self._rows()
        count = max(1, len(rows))
        avail = max(1.0, self.height() - WEEK_H)
        # 行高封顶 30：面板拉得再高也别把日期摊成一片散沙；多出来的空间留底部。
        # 底下挂了「当天便签」时会挤一点，所以下限放到 13。
        row_h = min(30.0, max(13.0, avail / count))
        top = float(WEEK_H)
        col_w = self.width() / 7.0
        return rows, top, row_h, col_w

    def _day_at(self, x: float, y: float) -> datetime.date | None:
        rows, top, row_h, col_w = self._geometry_parts()
        if y < top or col_w <= 0:
            return None
        row = int((y - top) // row_h)
        col = int(x // col_w)
        if not (0 <= row < len(rows)) or not (0 <= col < 7):
            return None
        day = rows[row][col]
        if not day:
            return None
        try:
            return datetime.date(self._year, self._month, day)
        except ValueError:
            return None

    # ------------------------------------------------------------------ 事件
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        day = self._day_at(event.position().x(), event.position().y())
        if day != self._hover:
            self._hover = day
            self.setCursor(Qt.PointingHandCursor if day else Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            day = self._day_at(event.position().x(), event.position().y())
            if day is not None:
                self.select(day)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rows, top, row_h, col_w = self._geometry_parts()
        today = datetime.date.today()
        base = QFont(self.font())
        base.setPointSizeF(8.4)
        small = QFont(self.font())
        small.setPointSizeF(7.0)

        # 星期行
        p.setFont(small)
        p.setPen(QColor(theme.color("muted")))
        for col, name in enumerate(WEEKDAYS):
            p.drawText(QRectF(col * col_w, 0, col_w, WEEK_H),
                       Qt.AlignCenter, name)

        for row, week in enumerate(rows):
            for col, day in enumerate(week):
                if not day:
                    continue
                try:
                    moment = datetime.date(self._year, self._month, day)
                except ValueError:
                    continue
                cell = QRectF(col * col_w + 1, top + row * row_h, col_w - 2, row_h - 1)
                centre = cell.center()
                radius = min(11.0, max(7.0, min(cell.width(), cell.height()) * 0.5 - 2.5))

                if moment == self._selected:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(theme.color("sel")))
                    p.drawRoundedRect(cell.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
                elif moment == self._hover:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(theme.color("hover")))
                    p.drawRoundedRect(cell.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)

                if moment == today:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(theme.color("accent")))
                    p.drawEllipse(centre, radius, radius)

                p.setFont(base)
                if moment == today:
                    p.setPen(QColor("#FFFFFF"))
                elif moment == self._selected:
                    p.setPen(QColor(theme.color("text")))
                elif moment.weekday() >= 5:
                    p.setPen(QColor(theme.color("muted")))
                else:
                    p.setPen(QColor(theme.color("text2")))
                p.drawText(cell, Qt.AlignCenter, str(day))

                marks = self._marks.get(moment, 0)
                if marks:
                    p.setPen(Qt.NoPen)
                    dot = QColor(theme.color("accent"))
                    dot.setAlpha(235 if moment == today else 170)
                    p.setBrush(dot)
                    size = 3.0 if marks < 3 else 4.2
                    p.drawEllipse(
                        QPointF(centre.x(), cell.bottom() - size * 0.85), size, size)
        p.end()


class CalendarPage(QWidget):
    kind = "calendar"
    title = "日历"
    primary_tip = "回到今天"
    can_primary = True
    message = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setObjectName("CalendarPage")
        self.config = config
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(HEADER_H)
        lay = QHBoxLayout(header)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(0)

        self.btn_prev = self._nav_button("‹", "上个月", lambda: self._shift(-1))
        self.btn_next = self._nav_button("›", "下个月", lambda: self._shift(1))
        self.label = QLabel("")
        self.label.setObjectName("CalTitle")
        self.label.setAlignment(Qt.AlignCenter)
        self.count = QLabel("")
        self.count.setObjectName("CalCount")
        self.count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.count.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        lay.addWidget(self.btn_prev)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.count)
        lay.addWidget(self.btn_next)
        root.addWidget(header)

        self.cal = MiniCalendar()
        self.cal.picked.connect(self._on_picked)
        root.addWidget(self.cal, 1)

        # 当天便签挂在日历下面：条数多了就滚动，比挤在状态栏一行里强
        self.day_list = QListWidget()
        self.day_list.setObjectName("CalDayList")
        self.day_list.setFrameShape(QListWidget.NoFrame)
        self.day_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.day_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.day_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.day_list.setVisible(False)
        root.addWidget(self.day_list)

    def _nav_button(self, text: str, tip: str, slot) -> QToolButton:
        btn = QToolButton()
        btn.setObjectName("CalNav")
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setFixedSize(20, 20)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(slot)
        return btn

    # ------------------------------------------------------------------ 数据
    def refresh(self) -> None:
        notes = nt.load_notes(self.config.value("notes", []))
        marks: dict[datetime.date, int] = {}
        for note in notes:
            moment = note.remind_dt
            if moment is None:
                continue
            marks[moment.date()] = marks.get(moment.date(), 0) + 1
        self._notes = notes
        self.cal.set_marks(marks)
        self._fill_day_list(self.cal.selected())
        self._sync_label()

    def _fill_day_list(self, day: datetime.date) -> None:
        """把当天的便签铺到日历下方（最多露 3 行，多的滚动）。"""
        picked = []
        for note in self._notes:
            moment = note.remind_dt
            if moment is not None and moment.date() == day:
                picked.append((moment.strftime("%H:%M"), note.text))

        self.day_list.clear()
        for when, text in picked:
            item = QListWidgetItem(f"{when}  {text}")
            item.setToolTip(f"{day.strftime('%m-%d')} {when}\n{text}")
            item.setSizeHint(QSize(0, 19))
            self.day_list.addItem(item)

        if not picked:
            self.day_list.setVisible(False)
            return
        rows = min(3, len(picked))
        row_h = self.day_list.sizeHintForRow(0)
        if row_h <= 0:
            row_h = 19
        self.day_list.setVisible(True)
        self.day_list.setFixedHeight(row_h * rows + 2)

    def _sync_label(self) -> None:
        self.label.setText(self.cal.month_label())
        year, month = self.cal.current_month()
        total = sum(count for day, count in self.cal.marks().items()
                    if day.year == year and day.month == month)
        self.count.setText(f"{total} 条" if total else "")

    def _shift(self, delta: int) -> None:
        self.cal.shift_month(delta)
        self._sync_label()
        # 换月了，底下的「当天便签」跟着清掉，免得看着像这个月的
        self.day_list.clear()
        self.day_list.setVisible(False)

    def _on_picked(self, day: datetime.date) -> None:
        self.label.setText(self.cal.month_label())
        self._fill_day_list(day)
        count = self.day_list.count()
        if count:
            self._flash(f"{day.strftime('%m-%d')}：{count} 条提醒")
        else:
            self._flash(f"{day.strftime('%m-%d')}：这天没有提醒")

    def _flash(self, text: str) -> None:
        self.message.emit(text)

    # ------------------------------------------------------------------ 页接口
    def activate(self) -> None:
        self.refresh()

    def deactivate(self) -> None:
        pass

    def primary(self) -> None:
        self.cal.go_today()
        self._sync_label()

    def menu(self, anchor=None) -> None:
        menu = QMenu(self)
        menu.addAction("回到今天", self.primary)
        menu.addAction("上个月", lambda: self._shift(-1))
        menu.addAction("下个月", lambda: self._shift(1))
        menu.addSeparator()
        menu.addAction("刷新（便签改过之后）", self.refresh)
        if anchor is not None:
            menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        else:
            menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def refresh_theme(self) -> None:
        self.update()
        self.cal.update()

    def handle_key(self, action: str) -> bool:
        return False
