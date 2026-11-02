"""区域截图：全屏遮罩 + 拖框选择 + 操作工具条（复制 / 固定 / 下载 / 取消）。

流程刻意是「先抓图、再显示遮罩」：抓到的是一张静态图，遮罩、选区框、
工具条都画在它上面，所以永远不会把自己截进去。

坐标说明：选区一律用**逻辑像素**（和 Qt 控件坐标一致）；真正裁图时再乘
``devicePixelRatio`` 取原图里的物理像素，这样 150% 缩放也不会糊。
"""

from __future__ import annotations

import datetime
import os
import tempfile

from PySide6.QtCore import (QPoint, QRect, QRectF, QSize, Qt, QThread, QTimer,
                            Signal)
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QGuiApplication,
                           QIcon, QImage, QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QMenu,
                               QToolButton, QWidget)

ACCENT = "#4C8DFF"
GLOW = QColor(110, 170, 255)          # 固定窗的蓝色柔光（对齐参考图 #7AB1FF）
DIM_ALPHA = 118
MIN_SIZE = 4                      # 小于这个尺寸当成误点，直接退出
DEFAULT_SHOT_HOTKEY = "Ctrl+Shift+A"

# 无父窗口的 QWidget 由 Python 托管，不留引用会被立刻回收（窗口一闪就没）
_pinned: list["PinnedWindow"] = []
_overlay: "ShotOverlay | None" = None


# --------------------------------------------------------------------------- 抓图
def grab_virtual_desktop() -> tuple[QImage, float, QRect]:
    """把所有屏幕拼成一张图。返回 ``(图像, 缩放比, 逻辑区域)``。

    图像的像素是**物理像素**（devicePixelRatio = 1），要显示成逻辑尺寸时
    自己除 dpr。
    """
    screens = QGuiApplication.screens()
    geo = QRect()
    for screen in screens:
        geo = geo.united(screen.geometry())
    if geo.isEmpty():
        geo = QRect(0, 0, 1920, 1080)

    dpr = max((screen.devicePixelRatio() for screen in screens), default=1.0)
    image = QImage(int(round(geo.width() * dpr)), int(round(geo.height() * dpr)),
                   QImage.Format_RGB32)
    image.fill(QColor(0, 0, 0))

    painter = QPainter(image)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    for screen in screens:
        shot = screen.grabWindow(0)
        if shot.isNull():
            continue
        g = screen.geometry()
        target = QRectF((g.x() - geo.x()) * dpr, (g.y() - geo.y()) * dpr,
                        g.width() * dpr, g.height() * dpr)
        painter.drawPixmap(target, shot, QRectF(shot.rect()))
    painter.end()
    return image, dpr, geo


# --------------------------------------------------------------------------- 图标
def _tool_icon(kind: str, color: str = "#E8EAED", size: int = 18) -> QIcon:
    """画工具条上的小图标（不用外部图片资源）。"""
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color), 1.5)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    if kind == "copy":
        p.drawRoundedRect(QRectF(6.2, 2.8, 8.6, 10.6), 1.8, 1.8)
        p.drawRoundedRect(QRectF(3.2, 5.4, 8.6, 10.6), 1.8, 1.8)
    elif kind == "pin":
        p.drawEllipse(QRectF(6.4, 1.8, 5.2, 5.2))          # 图钉帽
        p.drawLine(QRectF(9.0, 6.6, 0, 2.4).topLeft(),
                   QRectF(9.0, 6.6, 0, 2.4).bottomLeft())   # 针杆
        p.drawRoundedRect(QRectF(3.4, 9.6, 11.2, 6.6), 1.6, 1.6)   # 被钉住的图
    elif kind == "download":
        p.drawLine(QPoint(9, 2), QPoint(9, 11))            # 箭头
        p.drawPolyline([QPoint(5, 8), QPoint(9, 12), QPoint(13, 8)])
        p.drawPolyline([QPoint(3, 12), QPoint(3, 15), QPoint(15, 15), QPoint(15, 12)])
    else:                                                   # cancel
        p.drawLine(QPoint(4, 4), QPoint(14, 14))
        p.drawLine(QPoint(14, 4), QPoint(4, 14))
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- 工具条
class ShotToolbar(QWidget):
    """选区下方那排按钮。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ShotBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget#ShotBar { background: rgba(28, 30, 34, 238); border-radius: 10px; }
            QToolButton { color: #E8EAED; background: transparent; border: none;
                          padding: 5px 9px 6px 9px; font-size: 9pt; }
            QToolButton:hover { background: rgba(255, 255, 255, 30); border-radius: 7px; }
            QToolButton:pressed { background: rgba(255, 255, 255, 48); border-radius: 7px; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        self.btn_copy = self._button("复制", "copy", "复制到剪贴板（也可以直接双击图片）")
        self.btn_pin = self._button("固定", "pin", "固定到屏幕（Ctrl+T）")
        self.btn_save = self._button("下载", "download", "另存为图片（Ctrl+S）")
        self.btn_cancel = self._button("取消", "cancel", "取消截图（Esc）")
        for btn in (self.btn_copy, self.btn_pin, self.btn_save):
            layout.addWidget(btn)
        layout.addWidget(self._spacer())
        layout.addWidget(self.btn_cancel)

    @staticmethod
    def _spacer() -> QWidget:
        line = QWidget()
        line.setFixedWidth(1)
        line.setFixedHeight(28)
        line.setStyleSheet("background: rgba(255,255,255,40); margin: 4px 4px;")
        return line

    def _button(self, text: str, icon: str, tip: str) -> QToolButton:
        btn = QToolButton()
        btn.setText(text)
        btn.setIcon(_tool_icon(icon))
        btn.setIconSize(QSize(18, 18))
        btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        btn.setToolTip(tip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        return btn

    def place_near(self, selection: QRect, screen: QRect) -> None:
        """摆在选区下方；下方放不下就翻到上方；左右超出屏幕就往里收。"""
        self.adjustSize()
        w, h = self.width(), self.height()
        gap = 8

        x = selection.center().x() - w // 2
        y = selection.bottom() + gap
        if y + h > screen.bottom() - 4:                 # 下方不够 → 上方
            y = selection.top() - gap - h
        if y < screen.top() + 4:                        # 上方也不够 → 塞回选区底部内侧
            y = max(screen.top() + 4, selection.bottom() - h - gap)

        x = max(screen.left() + 4, min(x, screen.right() - w - 4))
        self.move(x, y)
        self.show()
        self.raise_()


# --------------------------------------------------------------------------- 遮罩
class ShotOverlay(QWidget):
    """全屏遮罩：拖框选区 → 可移动 / 可拉伸 → 选完弹工具条。"""

    closed = Signal()

    HANDLE = 7            # 手柄小方块边长
    HIT = 7               # 命中容差（像素）
    CURSORS = {
        "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor,
        "e": Qt.SizeHorCursor, "w": Qt.SizeHorCursor,
        "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
        "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
        "move": Qt.SizeAllCursor,
    }

    def __init__(self, image: QImage, dpr: float, geo: QRect,
                 default_dir: str = "", on_message=None, parent=None):
        super().__init__(parent)
        self._image = image
        self._dpr = dpr or 1.0
        self._dir = default_dir or os.path.expanduser("~")
        self._on_message = on_message

        self._origin: QPoint | None = None
        self._cursor: QPoint | None = None
        self._rect = QRect()
        self._state = "idle"          # idle / dragging / selected
        self._op: str | None = None   # new / move / resize
        self._edge = ""
        self._rect0 = QRect()
        self._press0: QPoint | None = None
        self._closed = False
        self._remembered = False      # 这一张有没有存进「最近截图」

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setGeometry(geo)

        self.bar = ShotToolbar(self)
        self.bar.hide()
        self.bar.btn_copy.clicked.connect(self.copy_to_clipboard)
        self.bar.btn_pin.clicked.connect(self.pin_to_screen)
        self.bar.btn_save.clicked.connect(self.save_as)
        self.bar.btn_cancel.clicked.connect(self.cancel)

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        p.drawImage(self.rect(), self._image)            # 冻结的桌面
        p.fillRect(self.rect(), QColor(0, 0, 0, DIM_ALPHA))

        sel = self._rect
        if sel.isValid() and sel.width() >= 1 and sel.height() >= 1:
            src = QRect(int(sel.x() * self._dpr), int(sel.y() * self._dpr),
                        int(sel.width() * self._dpr), int(sel.height() * self._dpr))
            p.drawImage(sel, self._image, src)           # 选区内恢复原亮度

            pen = QPen(QColor(ACCENT), 1.2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(sel.adjusted(0, 0, -1, -1))
            self._paint_handles(p, sel)
            self._paint_size(p, sel)

        if self._state == "dragging" and self._cursor is not None:
            pen = QPen(QColor(255, 255, 255, 60), 1)
            p.setPen(pen)
            p.drawLine(self.rect().left(), self._cursor.y(),
                       self.rect().right(), self._cursor.y())
            p.drawLine(self._cursor.x(), self.rect().top(),
                       self._cursor.x(), self.rect().bottom())
        elif self._state == "idle":
            self._paint_hint(p)

        self._paint_coords(p)
        p.end()

    def _handle_points(self, sel: QRect) -> dict:
        return {
            "nw": sel.topLeft(), "n": QPoint(sel.center().x(), sel.top()),
            "ne": sel.topRight(), "e": QPoint(sel.right(), sel.center().y()),
            "se": sel.bottomRight(), "s": QPoint(sel.center().x(), sel.bottom()),
            "sw": sel.bottomLeft(), "w": QPoint(sel.left(), sel.center().y()),
        }

    def _paint_handles(self, p: QPainter, sel: QRect) -> None:
        size = self.HANDLE
        p.setPen(QPen(QColor(ACCENT), 1.2))
        p.setBrush(QColor("#FFFFFF"))
        for point in self._handle_points(sel).values():
            p.drawRect(QRect(point.x() - size // 2, point.y() - size // 2, size, size))
        p.setBrush(Qt.NoBrush)

    def _paint_size(self, p: QPainter, sel: QRect) -> None:
        text = f"{sel.width()} × {sel.height()}"
        font = QFont(self.font())
        font.setPointSizeF(8.5)
        p.setFont(font)
        fm = QFontMetrics(font)
        w = fm.horizontalAdvance(text) + 14
        h = fm.height() + 6
        x = sel.left()
        y = sel.top() - h - 6
        if y < self.rect().top() + 2:
            y = sel.top() + 6                  # 贴着屏幕顶就塞进选区里
        x = max(self.rect().left() + 2, min(x, self.rect().right() - w - 2))
        box = QRect(x, y, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(28, 30, 34, 220))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(QColor("#E8EAED"))
        p.drawText(box, Qt.AlignCenter, text)

    def _paint_hint(self, p: QPainter) -> None:
        text = "拖动鼠标框选区域    ·    Esc 取消"
        font = QFont(self.font())
        font.setPointSizeF(10)
        p.setFont(font)
        fm = QFontMetrics(font)
        w = fm.horizontalAdvance(text) + 28
        h = fm.height() + 14
        box = QRect(self.width() // 2 - w // 2, self.height() // 2 - h // 2, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(28, 30, 34, 210))
        p.drawRoundedRect(box, 7, 7)
        p.setPen(QColor("#E8EAED"))
        p.drawText(box, Qt.AlignCenter, text)

    def _coord_rect(self) -> QRect:
        """鼠标坐标那个小标签的位置（跟着光标走，贴边就翻到另一侧）。"""
        if self._cursor is None:
            return QRect()
        point = self.mapToGlobal(self._cursor)
        text = f"{point.x()}, {point.y()}"
        fm = QFontMetrics(self._coord_font())
        w = fm.horizontalAdvance(text) + 14
        h = fm.height() + 6
        x = self._cursor.x() + 18
        y = self._cursor.y() + 18
        if x + w > self.rect().right() - 4:
            x = self._cursor.x() - 18 - w
        if y + h > self.rect().bottom() - 4:
            y = self._cursor.y() - 18 - h
        return QRect(x, y, w, h)

    def _coord_font(self) -> QFont:
        font = QFont(self.font())
        font.setPointSizeF(8.5)
        return font

    def _paint_coords(self, p: QPainter) -> None:
        box = self._coord_rect()
        if box.isNull() or box.isEmpty():
            return
        global_pos = self.mapToGlobal(self._cursor)
        text = f"{global_pos.x()}, {global_pos.y()}"
        p.setFont(self._coord_font())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(28, 30, 34, 215))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(QColor("#E8EAED"))
        p.drawText(box, Qt.AlignCenter, text)

    # ------------------------------------------------------------ 鼠标
    def _hit(self, pos: QPoint) -> str | None:
        """判断鼠标落在选区的哪一部分：手柄 / 内部 / 外面。"""
        if self._state != "selected" or self._rect.width() < 1:
            return None
        sel = self._rect
        h = self.HIT
        if not (sel.left() - h <= pos.x() <= sel.right() + h
                and sel.top() - h <= pos.y() <= sel.bottom() + h):
            return None
        near_l = abs(pos.x() - sel.left()) <= h
        near_r = abs(pos.x() - sel.right()) <= h
        near_t = abs(pos.y() - sel.top()) <= h
        near_b = abs(pos.y() - sel.bottom()) <= h
        if near_l and near_t:
            return "nw"
        if near_r and near_t:
            return "ne"
        if near_l and near_b:
            return "sw"
        if near_r and near_b:
            return "se"
        if near_l:
            return "w"
        if near_r:
            return "e"
        if near_t:
            return "n"
        if near_b:
            return "s"
        return "move" if sel.contains(pos) else None

    def _clamp(self, rect: QRect) -> QRect:
        bounds = self.rect()
        w = max(MIN_SIZE, rect.width())
        h = max(MIN_SIZE, rect.height())
        x = max(bounds.left(), min(rect.left(), bounds.right() - w + 1))
        y = max(bounds.top(), min(rect.top(), bounds.bottom() - h + 1))
        return QRect(x, y, w, h)

    def _resize_to(self, base: QRect, press: QPoint, now: QPoint, edge: str) -> QRect:
        delta = now - press
        l, t, r, b = base.left(), base.top(), base.right(), base.bottom()
        if "w" in edge:
            l = min(l + delta.x(), r - MIN_SIZE)
        if "e" in edge:
            r = max(r + delta.x(), l + MIN_SIZE)
        if "n" in edge:
            t = min(t + delta.y(), b - MIN_SIZE)
        if "s" in edge:
            b = max(b + delta.y(), t + MIN_SIZE)
        return self._clamp(QRect(QPoint(l, t), QPoint(r, b)).normalized())

    def _set_cursor_for(self, pos: QPoint) -> None:
        hit = self._hit(pos)
        self.setCursor(self.CURSORS.get(hit, Qt.CrossCursor))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            self.cancel()
            return
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        self._cursor = pos
        hit = self._hit(pos)

        if hit == "move":                       # 拖动整个选区
            self._op = "move"
            self._rect0 = QRect(self._rect)
            self._press0 = pos
            self.bar.hide()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if hit:                                 # 拉边 / 拉角
            self._op = "resize"
            self._edge = hit
            self._rect0 = QRect(self._rect)
            self._press0 = pos
            self.bar.hide()
            return

        self._state = "dragging"                # 重新框一个
        self._op = "new"
        self._origin = pos
        self._rect = QRect(pos, pos)
        self.bar.hide()
        self.setCursor(Qt.CrossCursor)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        old_box = self._coord_rect()
        self._cursor = pos

        if self._op == "move" and self._press0 is not None:
            self._rect = self._clamp(self._rect0.translated(pos - self._press0))
            self.update()
            return
        if self._op == "resize" and self._press0 is not None:
            self._rect = self._resize_to(self._rect0, self._press0, pos, self._edge)
            self.update()
            return
        if self._op == "new" and self._origin is not None:
            self._rect = QRect(self._origin, pos).normalized()
            self.update()
            return

        # 只是移动鼠标：换光标形状 + 刷新坐标标签
        self._set_cursor_for(pos)
        self.update(old_box.united(self._coord_rect()))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        self._cursor = pos

        if self._op in ("move", "resize"):
            self._op = None
            self._rect = self._clamp(self._rect.normalized())
            self._state = "selected"
            self.bar.place_near(self._rect, self.rect())
            self._set_cursor_for(pos)
            self.update()
            return

        if self._op != "new":
            return
        self._op = None
        self._rect = self._rect.normalized()
        if self._rect.width() < MIN_SIZE or self._rect.height() < MIN_SIZE:
            self.cancel()                       # 几乎没拖，当误触
            return
        self._state = "selected"
        self.bar.place_near(self._rect, self.rect())
        self._set_cursor_for(pos)
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._rect.contains(event.position().toPoint()):
            self.copy_to_clipboard()            # 双击图片 = 复制

    # ------------------------------------------------------------ 键盘
    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        if key == Qt.Key_Escape:
            self.cancel()
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self.copy_to_clipboard()
        elif mods & Qt.ControlModifier and key == Qt.Key_T:
            self.pin_to_screen()
        elif mods & Qt.ControlModifier and key == Qt.Key_S:
            self.save_as()
        elif mods & Qt.ControlModifier and key == Qt.Key_C:
            self.copy_to_clipboard()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._cursor = self.mapFromGlobal(QCursor.pos())
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.ActiveWindowFocusReason)

    # ------------------------------------------------------------ 动作
    def _cropped(self) -> QImage | None:
        if self._rect.width() < 1 or self._rect.height() < 1:
            return None
        d = self._dpr
        src = QRect(int(self._rect.x() * d), int(self._rect.y() * d),
                    max(1, int(self._rect.width() * d)),
                    max(1, int(self._rect.height() * d)))
        image = self._image.copy(src)
        image.setDevicePixelRatio(d)            # 显示时才按物理尺寸走
        if not self._remembered:                # 复制/贴图/另存都算「截过一次」
            self._remembered = True
            remember_shot(image)
        return image

    def _tell(self, text: str) -> None:
        if self._on_message is not None:
            self._on_message(text)

    def copy_to_clipboard(self) -> None:
        image = self._cropped()
        if image is None:
            return
        QApplication.clipboard().setPixmap(QPixmap.fromImage(image))
        self._tell(f"截图已复制（{image.width()} × {image.height()}）")
        self._finish()

    def pin_to_screen(self) -> None:
        image = self._cropped()
        if image is None:
            return
        QApplication.clipboard().setPixmap(QPixmap.fromImage(image))
        open_pinned(image, self.mapToGlobal(self._rect.topLeft())
                    - QPoint(PinnedWindow.PAD, PinnedWindow.PAD),
                    self._dir, self._on_message)
        self._tell("已固定到屏幕（拖动可移动，右键有菜单，Esc 关闭）")
        self._finish()

    def save_as(self) -> None:
        image = self._cropped()
        if image is None:
            return
        self.hide()                             # 别让遮罩盖住文件对话框
        path = save_image_dialog(image, self._dir, self)
        if path:
            self._tell(f"截图已保存：{path}")
            self._finish()
        else:
            self.show()                         # 用户取消了，回到选区状态
            self.setFocus()

    def cancel(self) -> None:
        self._finish()

    def _finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.bar.hide()
        self.closed.emit()
        self.close()


# --------------------------------------------------------------------------- 固定窗
_OCR_TASKS: set = set()      # 顶住引用，线程跑完自己移除


class _OcrTask(QThread):
    """后台跑 OCR。系统引擎一次二三十毫秒，但没必要让贴图等它。"""

    done = Signal(object, str)

    def __init__(self, image):
        super().__init__()
        self._image = image.copy()

    def run(self):
        try:
            import ocr
            words, error = ocr.analyze(self._image)
        except Exception as exc:
            words, error = [], f"识别失败：{exc}"
        self.done.emit(words, error)


class PinnedWindow(QWidget):
    """钉在屏幕上的截图小窗。

    - 滚轮缩放，四周一圈蓝色柔光
    - **鼠标移到文字上会变成 I 形光标**，可以像文档一样拖选文字（OCR 出来后
      才知道字在哪，所以第一次悬停时才识别）
    - 拖空白处移动窗口，双击文字选中整行
    """

    PAD = 15                 # 柔光留白
    RADIUS = 6
    MIN_SCALE = 0.1
    MAX_SCALE = 8.0

    def __init__(self, image: QImage, default_dir: str = "", on_message=None):
        super().__init__(None)
        self._image = image
        self._dir = default_dir
        self._on_message = on_message
        self._drag_offset: QPoint | None = None
        self._scale = 1.0
        self._badge = False

        self._words: list[dict] = []
        self._ocr_state = "idle"        # idle / busy / done / failed
        self._anchor: int | None = None
        self._current: int | None = None
        self._selecting = False

        dpr = image.devicePixelRatio() or 1.0
        self._base = QSize(int(round(image.width() / dpr)),
                           int(round(image.height() / dpr)))

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.OpenHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        self._badge_timer = QTimer(self)
        self._badge_timer.setSingleShot(True)
        self._badge_timer.setInterval(900)
        self._badge_timer.timeout.connect(self._hide_badge)
        self._apply_geometry()
        self._start_ocr()

    # ------------------------------------------------------------ 尺寸
    def _display_size(self) -> QSize:
        return QSize(max(1, round(self._base.width() * self._scale)),
                     max(1, round(self._base.height() * self._scale)))

    def _apply_geometry(self) -> None:
        size = self._display_size()
        self.resize(size.width() + self.PAD * 2, size.height() + self.PAD * 2)
        self.update()

    def fit_width(self, width: int) -> None:
        """把显示宽度对齐到给定值，高度按原图比例走。

        极端竖图（按宽度铺开会高过屏幕）才改成按屏幕高度收，否则底部跑出屏幕
        就没法拖了。
        """
        base_w = max(1, self._base.width())
        base_h = max(1, self._base.height())
        scale = width / base_w
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            limit = screen.availableGeometry().height() * 0.95
            if base_h * scale > limit:
                scale = limit / base_h
        self._scale = max(self.MIN_SCALE, min(self.MAX_SCALE, scale))
        self._apply_geometry()

    def align_photo(self, top_left: QPoint) -> None:
        """让「图片左上角」落在全局坐标 top_left（窗口左上角还要减掉柔光留白）。"""
        self.move(top_left - QPoint(self.PAD, self.PAD))

    def _hide_badge(self) -> None:
        self._badge = False
        self.update()

    def _body(self) -> QRect:
        return QRect(self.PAD, self.PAD, *self._display_size().toTuple())

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        body = self._body()

        # 蓝色柔光：贴边那圈最浓，往外 15px 线性淡掉
        p.setPen(Qt.NoPen)
        for i in range(self.PAD, 0, -1):
            alpha = int(round(30 * (1 - (i - 1) / self.PAD)))
            if alpha <= 0:
                continue
            p.setBrush(QColor(GLOW.red(), GLOW.green(), GLOW.blue(), alpha))
            p.drawRoundedRect(body.adjusted(-i, -i, i, i),
                              3 + i * 0.45, 3 + i * 0.45)

        p.setBrush(Qt.NoBrush)
        p.drawImage(QRectF(body), self._image,
                    QRectF(0, 0, self._image.width(), self._image.height()))

        self._paint_selection(p)

        if self._badge:
            self._paint_badge(p, body)
        p.end()

    def _paint_selection(self, p: QPainter) -> None:
        for word in self._selected_words():
            rect = self.word_rect(word).adjusted(-1, -1, 1, 1)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(64, 132, 255, 92))
            p.drawRoundedRect(rect, 2, 2)

    def _paint_badge(self, p: QPainter, body: QRect) -> None:
        text = f"{int(round(self._scale * 100))}%"
        font = QFont(self.font())
        font.setPointSizeF(9)
        font.setBold(True)
        p.setFont(font)
        fm = QFontMetrics(font)
        w = fm.horizontalAdvance(text) + 14
        h = fm.height() + 6
        box = QRect(body.right() - w - 8, body.bottom() - h - 8, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 22, 26, 200))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(QColor("#E8EAED"))
        p.drawText(box, Qt.AlignCenter, text)

    # ------------------------------------------------------------ OCR
    def _start_ocr(self) -> None:
        """贴上来就在后台识别，鼠标移过去时已经是 I 形光标。"""
        if self._ocr_state != "idle":
            return
        self._ocr_state = "busy"
        task = _OcrTask(self._image)
        task.done.connect(self._on_ocr_done)
        task.finished.connect(lambda t=task: _OCR_TASKS.discard(t))
        _OCR_TASKS.add(task)
        self._ocr_task = task
        task.start()

    def _on_ocr_done(self, words, error) -> None:
        self._words = list(words or [])
        self._ocr_state = "done" if self._words else "failed"
        if error and self._on_message is not None:
            self._on_message(error)
        if self._words and self.underMouse():
            self.setCursor(Qt.IBeamCursor)
        self.update()

    def _ensure_ocr(self) -> None:
        self._start_ocr()

    # ------------------------------------------------------------ 文字几何
    def word_rect(self, word: dict) -> QRectF:
        size = self._display_size()
        return QRectF(self.PAD + word["x"] * size.width(),
                      self.PAD + word["y"] * size.height(),
                      max(1.0, word["w"] * size.width()),
                      max(1.0, word["h"] * size.height()))

    def _word_at(self, pos: QPoint) -> int | None:
        """返回落在这个点上的词序号；上下稍微放宽一点，好点中。"""
        size = self._display_size()
        if not size.width() or not size.height():
            return None
        nx = (pos.x() - self.PAD) / size.width()
        ny = (pos.y() - self.PAD) / size.height()
        for index, word in enumerate(self._words):
            if (word["x"] - 0.004 <= nx <= word["x"] + word["w"] + 0.004
                    and word["y"] - word["h"] * 0.22 <= ny
                    <= word["y"] + word["h"] * 1.22):
                return index
        return None

    def _selected_words(self) -> list[dict]:
        if self._anchor is None or self._current is None:
            return []
        lo, hi = sorted((self._anchor, self._current))
        return self._words[lo:hi + 1]

    def selected_text(self) -> str:
        words = self._selected_words()
        if not words:
            return ""
        by_line: dict[int, list[dict]] = {}
        for word in words:
            by_line.setdefault(word["line"], []).append(word)
        import ocr
        return "\n".join(ocr.join_words(by_line[i]) for i in sorted(by_line))

    def _all_text(self) -> str:
        if not self._words:
            return ""
        import ocr
        by_line: dict[int, list[dict]] = {}
        for word in self._words:
            by_line.setdefault(word["line"], []).append(word)
        return "\n".join(ocr.join_words(by_line[i]) for i in sorted(by_line))

    # ------------------------------------------------------------ 缩放
    def wheelEvent(self, event) -> None:  # noqa: N802
        dy = event.angleDelta().y()
        if not dy:
            return
        # 一次事件可能合并好几格，按格数算才跟手
        notches = max(-4.0, min(4.0, dy / 120.0))
        self._zoom(self._scale * (1.12 ** notches), event.position().toPoint())

    def _zoom(self, target: float, anchor: QPoint | None = None) -> None:
        target = max(self.MIN_SCALE, min(self.MAX_SCALE, target))
        if abs(target - self._scale) < 1e-4:
            return
        old = self._display_size()
        if anchor is None:
            anchor = QPoint(self.PAD + old.width() // 2, self.PAD + old.height() // 2)
        rx = (anchor.x() - self.PAD) / max(1, old.width())
        ry = (anchor.y() - self.PAD) / max(1, old.height())

        # 先把锚点的全局位置记下来，再改尺寸——改完再取就已经是新坐标了
        keep = self.mapToGlobal(anchor)
        self._scale = target
        new = self._display_size()
        self._apply_geometry()
        self.move(keep - QPoint(round(rx * new.width()) + self.PAD,
                                round(ry * new.height()) + self.PAD))

        self._badge = True
        self._badge_timer.start()
        self.update()

    def _reset_zoom(self) -> None:
        self._zoom(1.0)

    # ------------------------------------------------------------ 交互
    def enterEvent(self, event) -> None:  # noqa: N802
        self._ensure_ocr()
        if event is not None:
            super().enterEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            self._ensure_ocr()
            self._menu(event.globalPosition().toPoint())
            return
        if event.button() != Qt.LeftButton:
            return

        pos = event.position().toPoint()
        if event.modifiers() & Qt.ControlModifier:
            # 满屏都是字的时候按住 Ctrl 拖，专门用来挪窗口
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if self._word_at(pos) is not None:
            # 点在文字上：开始选词，不动窗口
            self._selecting = True
            self._anchor = self._word_at(pos)
            self._current = self._anchor
            self.setCursor(Qt.IBeamCursor)
            self.update()
            return

        # 点在图上空白处：清掉选择，改成拖窗口
        self._anchor = self._current = None
        self._selecting = False
        self._drag_offset = event.globalPosition().toPoint() - self.pos()
        self.setCursor(Qt.ClosedHandCursor)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._selecting:
            index = self._word_at(pos)
            if index is not None and index != self._current:
                self._current = index
                self.update()
            return
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            return

        self._ensure_ocr()
        # 悬停在文字上就变 I 形光标，这正是用户要的"可以选文本"的手感
        self.setCursor(Qt.IBeamCursor if self._word_at(pos) is not None
                       else Qt.OpenHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        self._selecting = False
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            self.setCursor(Qt.IBeamCursor if self._word_at(pos) is not None
                           else Qt.OpenHandCursor)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        index = self._word_at(event.position().toPoint())
        if index is None:
            return
        line = self._words[index]["line"]                 # 双击选中整行
        same = [i for i, w in enumerate(self._words) if w["line"] == line]
        self._anchor, self._current = same[0], same[-1]
        self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.ControlModifier)
        if key == Qt.Key_Escape:
            self.close()
        elif ctrl and key == Qt.Key_C:
            self._copy_text_or_image()
        elif ctrl and key in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom(self._scale * 1.12)
        elif ctrl and key == Qt.Key_Minus:
            self._zoom(self._scale / 1.12)
        elif ctrl and key == Qt.Key_0:
            self._reset_zoom()
        elif ctrl and key == Qt.Key_A:
            if self._words:
                self._anchor, self._current = 0, len(self._words) - 1
                self.update()
        elif ctrl and key == Qt.Key_S:
            self._save()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self in _pinned:
            _pinned.remove(self)
        super().closeEvent(event)

    # ------------------------------------------------------------ 菜单 / 动作
    def _menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        has_selection = bool(self._selected_words())
        has_text = bool(self._words)

        act = menu.addAction("复制文字（选中）      Ctrl+C", self._copy_text_or_image)
        act.setEnabled(has_selection)
        act = menu.addAction("复制全部文字", self._copy_all_text)
        act.setEnabled(has_text)
        act = menu.addAction("全选文字              Ctrl+A", self._select_all)
        act.setEnabled(has_text)
        if self._ocr_state == "failed":
            tip = menu.addAction("（这张图没认出文字）")
            tip.setEnabled(False)
        menu.addSeparator()
        menu.addAction("复制图片", self._copy)
        menu.addAction("另存为…      Ctrl+S", self._save)
        menu.addSeparator()
        menu.addAction("放大          滚轮上", lambda: self._zoom(self._scale * 1.25))
        menu.addAction("缩小          滚轮下", lambda: self._zoom(self._scale / 1.25))
        menu.addAction("原始大小（100%）", self._reset_zoom)
        menu.addSeparator()
        menu.addAction("关闭              Esc", self.close)
        menu.exec(global_pos)

    def _select_all(self) -> None:
        if self._words:
            self._anchor, self._current = 0, len(self._words) - 1
            self.update()

    def _copy_text_or_image(self) -> None:
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)
            if self._on_message is not None:
                self._on_message(f"已复制 {len(text)} 个字")
            return
        self._copy()

    def _copy_all_text(self) -> None:
        text = self._all_text()
        if text:
            QApplication.clipboard().setText(text)
            if self._on_message is not None:
                self._on_message(f"已复制全部文字（{len(text)} 字）")

    def _copy(self) -> None:
        QApplication.clipboard().setPixmap(QPixmap.fromImage(self._image))

    def _save(self) -> None:
        path = save_image_dialog(self._image, self._dir, self)
        if path and self._on_message is not None:
            self._on_message(f"截图已保存：{path}")


# --------------------------------------------------------------------------- 入口
def save_image_dialog(image: QImage, default_dir: str, parent=None) -> str | None:
    """弹「另存为」，返回实际保存的路径（取消返回 None）。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = default_dir if os.path.isdir(default_dir) else os.path.expanduser("~")
    default = os.path.join(base, f"截图_{stamp}.png")
    path, _ = QFileDialog.getSaveFileName(
        parent, "保存截图", default, "PNG 图片 (*.png);;JPEG 图片 (*.jpg)")
    if not path:
        return None
    if not os.path.splitext(path)[1]:
        path += ".png"
    return path if image.save(path) else None


# --------------------------------------------------------------------------- 最近截图
SHOT_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "SideExplorer", "shots")
SHOT_KEEP = 500                   # 磁盘上留 500 张（「截屏」页最多也列这么多）
SHOT_MAX_SIDE = 2000              # 4K 截屏缩到这个尺寸再存，一张几百 KB

_shot_listeners: list = []        # 截完图通知「截屏」页刷新


def add_shot_listener(callback) -> None:
    """注册「有新截图」回调（页面的 refresh）。弱引用，页面没了自然失效。"""
    import weakref
    ref = (weakref.WeakMethod(callback)
           if getattr(callback, "__self__", None) is not None else callback)
    _shot_listeners.append(ref)


def _notify_new_shot(path: str) -> None:
    import weakref
    for ref in list(_shot_listeners):
        if isinstance(ref, weakref.WeakMethod):
            callback = ref()
            if callback is None:                 # 页面已经销毁
                try:
                    _shot_listeners.remove(ref)
                except ValueError:
                    pass
                continue
        else:
            callback = ref
        try:
            callback()
        except RuntimeError:                     # Qt 对象已析构
            pass


def remember_shot(image: QImage) -> str | None:
    """存一份 PNG 给「截屏」页回看；顺手把太旧的清掉，不会越攒越多。"""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
    except OSError:
        return None

    shot = image
    if max(image.width(), image.height()) > SHOT_MAX_SIDE:
        shot = image.scaled(SHOT_MAX_SIDE, SHOT_MAX_SIDE, Qt.KeepAspectRatio,
                            Qt.SmoothTransformation)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    # 文件名带上原图尺寸，读列表时不用把图整个解码
    name = f"shot_{stamp}_{image.width()}x{image.height()}.png"
    path = os.path.join(SHOT_DIR, name)
    if not shot.save(path, "PNG"):
        return None
    _trim_shots()
    _notify_new_shot(path)
    return path


def _trim_shots() -> None:
    try:
        names = [n for n in os.listdir(SHOT_DIR) if n.startswith("shot_")]
    except OSError:
        return
    if len(names) <= SHOT_KEEP:
        return
    entries = []
    for name in names:
        path = os.path.join(SHOT_DIR, name)
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            continue
    entries.sort(reverse=True)
    for _mtime, path in entries[SHOT_KEEP:]:
        try:
            os.remove(path)
        except OSError:
            pass


def recent_shots(limit: int = SHOT_KEEP) -> list[dict]:
    """最近几张截图，新的在前：``[{path, mtime, width, height}]``。"""
    try:
        names = [n for n in os.listdir(SHOT_DIR)
                 if n.startswith("shot_") and n.lower().endswith(".png")]
    except OSError:
        return []
    items = []
    for name in names:
        path = os.path.join(SHOT_DIR, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        width = height = 0
        stem = os.path.splitext(name)[0]
        try:
            piece = stem.rsplit("_", 1)[-1]
            width, height = (int(v) for v in piece.split("x"))
        except (ValueError, IndexError):
            pass
        items.append({"path": path, "mtime": mtime, "width": width, "height": height})
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items[:limit]


def delete_shot(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def clear_shots() -> None:
    try:
        names = [n for n in os.listdir(SHOT_DIR) if n.startswith("shot_")]
    except OSError:
        return
    for name in names:
        delete_shot(os.path.join(SHOT_DIR, name))


def stop_ocr_tasks(timeout_ms: int = 3000) -> None:
    """退出前等后台识别线程收尾，免得 Qt 报 "Destroyed while thread is running"。"""
    for task in list(_OCR_TASKS):
        task.wait(timeout_ms)


def open_pinned(image: QImage, at: QPoint | None = None,
                default_dir: str = "", on_message=None,
                width: int | None = None,
                photo_at: QPoint | None = None) -> "PinnedWindow":
    """把一张图贴到屏幕上。

    ``at`` 是窗口左上角；``photo_at`` 是**图片**左上角（会自己让出柔光留白），
    ``width`` 指定显示宽度（高度按比例）。截屏页重新贴图用的是后两个。
    """
    win = PinnedWindow(image, default_dir, on_message)
    if width:
        win.fit_width(width)
    if photo_at is not None:
        win.align_photo(photo_at)
    elif at is not None:
        win.move(at)
    win.show()
    _pinned.append(win)
    return win


def pinned_count() -> int:
    """现在有几张图贴在屏幕上。"""
    return len(_pinned)


def close_all_pinned() -> int:
    """把贴在屏幕上的截图全部关掉，返回关掉几张。"""
    windows = list(_pinned)
    _pinned.clear()                   # closeEvent 里也会摘，先清掉免得漏
    for win in windows:
        try:
            win.close()
        except RuntimeError:          # 已经被 Qt 回收
            pass
    return len(windows)


def has_active_shot() -> bool:
    return _overlay is not None


def start_region_shot(default_dir: str = "", on_message=None,
                      on_closed=None) -> ShotOverlay | None:
    """抓屏并弹出选区遮罩。已经在截了就只把旧的那层提到前面。"""
    global _overlay
    if _overlay is not None:
        _overlay.raise_()
        _overlay.activateWindow()
        return _overlay
    image, dpr, geo = grab_virtual_desktop()
    overlay = ShotOverlay(image, dpr, geo, default_dir=default_dir,
                          on_message=on_message)
    _overlay = overlay                       # 顶住，别被回收
    overlay.closed.connect(_release_overlay)
    if on_closed is not None:
        overlay.closed.connect(on_closed)
    overlay.show()
    return overlay


def _release_overlay() -> None:
    global _overlay
    _overlay = None
