"""计算器页：一行输入、一个大号结果、下面是历史。

思路沿用以前挂在地址栏上的那套（``calc.py`` 的 AST 白名单求值 + 60 条历史），
只是从「文件浏览区铺一张假列表」搬成了独立的一页。

历史每条两行：上面是表达式（灰），下面是加粗的结果；右侧可以挂一句备注。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QApplication,
                               QLineEdit, QListView, QMenu, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

import calc
import list_style
import theme

ROW_H = list_style.ROW_H


class CalcModel(QAbstractListModel):
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
            return f"{record['expr']} = {record['result']}"
        if role == Qt.UserRole:
            return record
        if role == Qt.ToolTipRole:
            return f"{record['expr']}\n= {record['result']}"
        return None

    def set_records(self, records: list[dict]) -> None:
        self.beginResetModel()
        self.items = list(records)
        self.endResetModel()


class CalcDelegate(QStyledItemDelegate):
    """一行一条：左边算式（灰）、右边结果（粗体），备注挤最右边。"""

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(160, ROW_H)

    def paint(self, painter: QPainter, option, index) -> None:
        record = index.data(Qt.UserRole)
        if record is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        body = list_style.paint_row(painter, option.rect, option)

        text_left = body.left() + list_style.PAD
        right_edge = body.right() - list_style.PAD

        main_font = list_style.main_font(option.font)
        result_font = QFont(main_font)
        result_font.setBold(True)

        left_text = record["expr"]

        result = "=  " + calc.format_result(record["result"])
        result_w = min(list_style.width(result_font, result), right_edge - text_left - 40)
        if result_w <= 0:
            painter.restore()
            return
        expr_avail = right_edge - result_w - list_style.META_GAP - text_left
        if expr_avail >= 40:                     # 算式和结果能同排放下
            list_style.draw(painter, QRect(text_left, body.top(), expr_avail, body.height()),
                            left_text, main_font, theme.color("dim"),
                            mode=Qt.ElideMiddle)
        list_style.draw(painter, QRect(right_edge - result_w, body.top(),
                                       result_w, body.height()),
                        result, result_font, theme.color("text"),
                        align=Qt.AlignRight | Qt.AlignVCenter)
        painter.restore()



class CalcPage(QWidget):
    kind = "calc"
    title = "计算器"
    primary_tip = "开始新的算式"
    can_primary = True
    message = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setObjectName("CalcPage")
        self.config = config
        self.records = calc.load_history(config)
        self._build()
        self._refresh()

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(0)

        self.input = QLineEdit()
        self.input.setObjectName("CalcInput")
        self.input.setPlaceholderText("输入算式，回车计算")
        self.input.setFixedHeight(28)
        self.input.returnPressed.connect(self._compute)
        root.addWidget(self.input)

        self.model = CalcModel(self)
        self.view = QListView()
        self.view.setObjectName("CalcList")
        self.view.setModel(self.model)
        self.delegate = CalcDelegate(self)
        self.view.setItemDelegate(self.delegate)
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setFrameShape(QListView.NoFrame)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(self._recall)
        root.addWidget(self.view, 1)

    # ------------------------------------------------------------------ 计算
    def _compute(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        result, error = calc.eval_expression(text)
        if result is None:
            self.input.setStyleSheet(f"color: {theme.color('accent')};")
            self.input.setToolTip(error or "算不出来")
            self._flash(error or "算不出来")
            return
        self.input.setStyleSheet("")
        self.input.setToolTip("")
        self.records = calc.push_history(self.records, text, result)
        calc.save_history(self.config, self.records)
        self.input.clear()
        self._refresh()
        self._flash(f"{text} = {calc.format_result(result)}")

    def primary(self) -> None:
        self.input.clear()
        self.input.setFocus()

    def _flash(self, text: str) -> None:
        """结果顺手说一声（状态栏），不弹窗。"""
        self.message.emit(text)

    def _refresh(self) -> None:
        self.model.set_records(self.records)
        if self.records:
            self.view.scrollToTop()
        self.view.viewport().update()

    # ------------------------------------------------------------------ 历史
    def _row_record(self, index=None) -> dict | None:
        index = index or self.view.currentIndex()
        if not index.isValid():
            return None
        return index.data(Qt.UserRole)

    def _recall(self, index) -> None:
        record = index.data(Qt.UserRole)
        if record is None:
            return
        self.input.setText(record["expr"])
        self.input.setFocus()
        self.input.selectAll()

    def _context_menu(self, pos) -> None:
        index = self.view.indexAt(pos)
        if index.isValid():
            self.view.setCurrentIndex(index)
        record = self._row_record(index if index.isValid() else None)
        menu = QMenu(self)
        if record is not None:
            menu.addAction("填回输入框", lambda r=record: self._recall_text(r["expr"]))
            menu.addSeparator()
            menu.addAction("复制结果",
                           lambda r=record: QApplication.clipboard().setText(r["result"]))
            menu.addAction("复制表达式",
                           lambda r=record: QApplication.clipboard().setText(r["expr"]))
            menu.addSeparator()
            menu.addAction("删除这条记录", lambda r=record: self._delete(r))
            menu.addAction("清空全部记录", self.clear_all)
        else:
            menu.addAction("恢复输入框", self.primary)
            menu.addAction("清空全部记录", self.clear_all)
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _recall_text(self, text: str) -> None:
        self.input.setText(text)
        self.input.setFocus()
        self.input.selectAll()

    def _delete(self, record: dict) -> None:
        self.records = [r for r in self.records
                        if not (r["expr"] == record["expr"]
                                and r["result"] == record["result"])]
        calc.save_history(self.config, self.records)
        self._refresh()
        self._flash("已删除这条记录" if self.records else "已清空算式记录")

    def clear_all(self) -> None:
        if not self.records:
            return
        self.records = []
        calc.save_history(self.config, self.records)
        self._refresh()
        self._flash("已清空算式记录")

    def menu(self, anchor=None) -> None:
        menu = QMenu(self)
        menu.addAction("恢复输入框", self.primary)
        menu.addSeparator()
        menu.addAction("复制最近结果", self._copy_latest)
        menu.addAction("清空全部记录", self.clear_all)
        if anchor is not None:
            menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        else:
            menu.exec(self.input.mapToGlobal(self.input.rect().bottomLeft()))

    def _copy_latest(self) -> None:
        if not self.records:
            self._flash("还没有算式记录")
            return
        QApplication.clipboard().setText(self.records[0]["result"])
        self._flash("已复制最近一条结果")

    # ------------------------------------------------------------------ 页接口
    def activate(self) -> None:
        self.input.setFocus()

    def deactivate(self) -> None:
        calc.save_history(self.config, self.records)

    def refresh_theme(self) -> None:
        self.input.setStyleSheet("")
        self.view.viewport().update()

    def handle_key(self, action: str) -> bool:
        if action == "delete":
            record = self._row_record()
            if record is not None:
                self._delete(record)
                return True
        return False
