"""所有列表共用的行样式。

为什么单拎一个文件：文件列表和七个页签的列表原来各画各的 —— 行高 26/30/42/46/52/62、
左右内缩 4/6/7、字号 7.2~9.6 全不一样，切页的时候观感是散的。这里把「一行长什么样」
收成一处，各页只负责往里摆自己的内容（图标、标题、时间、按钮）。

改动这里的常量，文件列表和所有页签列表会一起变。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import QStyle

import theme

ROW_H = 30             # 标准行高（文字列表都用这个）
TALL_ROW_H = 48        # 带缩略图的行（截屏管理器）
INSET_X = 7            # 行卡片的左右内缩
INSET_Y = 2            # 行卡片的上下内缩
RADIUS = 6             # 行卡片圆角
PAD = 9                # 文字距卡片边缘
LEAD = 6               # 行首（图标 / 色条）距卡片左边
GAP = 8                # 图标与文字之间的距离
META_GAP = 12          # 正文与右侧灰字（时间等）之间留的呼吸

MAIN_PT = theme.LIST_FONT_PT      # 9.0 —— 列表正文
META_PT = theme.META_FONT_PT      # 7.8 —— 右侧灰色说明（时间、尺寸）


def body_rect(row: QRect) -> QRect:
    """整行里真正放内容的那块（去掉上下左右的留白）。"""
    return row.adjusted(INSET_X, INSET_Y, -INSET_X, -INSET_Y)


def paint_row(painter, row: QRect, option, fill: QColor | None = None) -> QRect:
    """画行的底色：常驻底色 → 选中 → 悬停。返回内容区。"""
    body = body_rect(row)
    if fill is not None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(body, RADIUS, RADIUS)
    if option.state & QStyle.State_Selected:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.color("sel")))
        painter.drawRoundedRect(body, RADIUS, RADIUS)
    elif option.state & QStyle.State_MouseOver:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.color("hover")))
        painter.drawRoundedRect(body, RADIUS, RADIUS)
    return body


def main_font(base: QFont) -> QFont:
    font = QFont(base)
    font.setPointSizeF(MAIN_PT)
    return font


def meta_font(base: QFont) -> QFont:
    font = QFont(base)
    font.setPointSizeF(META_PT)
    return font


def width(font: QFont, text: str) -> int:
    return QFontMetrics(font).horizontalAdvance(text)


def draw(painter, rect: QRect, text: str, font: QFont, color: str,
         align=Qt.AlignLeft | Qt.AlignVCenter,
         mode=Qt.ElideRight) -> int:
    """在 rect 里画一行省略过的字，返回实际画出来的宽度。"""
    painter.setFont(font)
    painter.setPen(QColor(color))
    metrics = QFontMetrics(font)
    shown = metrics.elidedText(text, mode, max(0, rect.width()))
    painter.drawText(rect, align, shown)
    return metrics.horizontalAdvance(shown)
