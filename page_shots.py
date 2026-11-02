"""截屏页：截图历史列表（最多 500 条），点一条重新贴到屏幕上。

截图本体存在 ``%LOCALAPPDATA%\\SideExplorer\\shots``，这里只按 mtime 取最新的那批，
内存里只留可见区域的缩略图（最多缓存 80 张）—— 500 张 4K 原图全摊在内存里要几个 G。

用 QListView 而不是一堆 QWidget：条数多了 Widget 方案光创建就卡。
缩略图在 delegate 里按需加载，滚到哪儿加载到哪儿。

点击 = 贴到屏幕上（左上角对齐面板、宽度跟面板一致），右键有复制 / 另存 / 删除。
"""

from __future__ import annotations

import datetime
import os
import subprocess
import time

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QPoint, QRect,
                            QRectF, QSize, Qt, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QImage, QPainter,
                           QPainterPath, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog,
                               QHBoxLayout, QLabel, QListView, QMenu, QStyle,
                               QStyledItemDelegate, QToolButton, QVBoxLayout,
                               QWidget)

import fileops
import list_style
import screenshot
import theme

ROW_H = list_style.TALL_ROW_H   # 54：带缩略图，比文字行高一档
THUMB_W = 62
THUMB_CACHE = 80

_THUMBS: dict[tuple, QPixmap] = {}
_THUMB_ORDER: list[tuple] = []


def _thumb(path: str, size: QSize) -> QPixmap:
    """按需加载缩略图，先进先出淘汰，避免几百张图把内存吃光。"""
    key = (path, size.width(), size.height())
    cached = _THUMBS.get(key)
    if cached is not None:
        return cached
    pixmap = QPixmap(path)
    thumb = (pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
             if not pixmap.isNull() else QPixmap())
    _THUMBS[key] = thumb
    _THUMB_ORDER.append(key)
    while len(_THUMB_ORDER) > THUMB_CACHE:
        _THUMBS.pop(_THUMB_ORDER.pop(0), None)
    return thumb


def _when(mtime: float) -> str:
    moment = datetime.datetime.fromtimestamp(mtime)
    if moment.date() == datetime.date.today():
        return moment.strftime("%H:%M")
    return moment.strftime("%m/%d %H:%M")


class ShotModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[dict] = []

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.items)):
            return None
        record = self.items[index.row()]
        if role == Qt.UserRole:
            return record
        if role == Qt.ToolTipRole:
            if record.get("kind") == "text":
                return (record.get("text") or "")[:400]
            width, height = record.get("width"), record.get("height")
            return f"{_when(record['mtime'])}\n{width} × {height}\n{record['path']}"
        return None

    def set_records(self, records: list[dict]) -> None:
        self.beginResetModel()
        self.items = list(records)
        self.endResetModel()


class ShotDelegate(QStyledItemDelegate):
    """一行一条：左边缩略图，右边两行灰字（时间 / 尺寸），都靠右对齐。"""

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(180, ROW_H)

    def paint(self, painter: QPainter, option, index) -> None:
        record = index.data(Qt.UserRole)
        if record is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        body = list_style.paint_row(painter, option.rect, option)
        self._paint_image(painter, body, record, option.font)
        painter.restore()

    def _paint_image(self, painter: QPainter, body: QRect, record: dict,
                     font: QFont) -> None:
        thumb_h = body.height() - 4
        thumb_box = QRect(body.left() + 2, body.center().y() - thumb_h // 2,
                          THUMB_W, thumb_h)
        thumb = _thumb(record["path"], thumb_box.size())
        if not thumb.isNull():
            path = QPainterPath()
            path.addRoundedRect(QRectF(thumb_box), 4, 4)
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(
                QPoint(thumb_box.left() + (thumb_box.width() - thumb.width()) // 2,
                       thumb_box.top() + (thumb_box.height() - thumb.height()) // 2),
                thumb)
            painter.restore()

        meta_font = list_style.meta_font(font)
        when = _when(record["mtime"])
        width, height = record.get("width"), record.get("height")
        detail = f"{width} × {height}" if width else "截图"
        column_w = max(list_style.width(meta_font, when),
                       list_style.width(meta_font, detail))
        right_edge = body.right() - list_style.PAD
        top = body.center().y() - 17           # 两行作为一组垂直居中
        list_style.draw(painter, QRect(right_edge - column_w, top,
                                       column_w, 16),
                        when, meta_font, theme.color("text2"),
                        align=Qt.AlignRight | Qt.AlignVCenter)
        list_style.draw(painter, QRect(right_edge - column_w, top + 18,
                                       column_w, 16),
                        detail, meta_font, theme.color("dim"),
                        align=Qt.AlignRight | Qt.AlignVCenter)



class ShotList(QListView):
    """只在缩略图上单击才贴图。

    点时间、点尺寸、点右边一大片空白，都只是选中 —— 之前整行都能点，
    想拖滚动条或者随手点一下就蹦出一张图，很烦。
    """

    opened = Signal(QModelIndex)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            index = self.indexAt(pos)
            if index.isValid() and self._in_thumb(index, pos):
                self.setCurrentIndex(index)
                self.opened.emit(index)
                return
        super().mousePressEvent(event)

    def _in_thumb(self, index: QModelIndex, pos: QPoint) -> bool:
        """点到左边那条缩略图带才算「打开」，右边文字和空白不算。"""
        body = list_style.body_rect(self.visualRect(index))
        return body.left() <= pos.x() <= body.left() + THUMB_W + 6


class ShotsPage(QWidget):
    kind = "shot"
    title = "截屏管理器"
    primary_tip = "区域截图"
    can_primary = True
    message = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setObjectName("ShotsPage")
        self.config = config
        self._last_open = 0.0
        self._build()
        self.refresh()
        screenshot.add_shot_listener(self.refresh)      # 截完一张立刻冒出来

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(0)

        self.model = ShotModel(self)
        self.view = ShotList()
        self.view.setObjectName("ShotList")
        self.view.setModel(self.model)
        self.delegate = ShotDelegate(self)
        self.view.setItemDelegate(self.delegate)
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setFrameShape(QListView.NoFrame)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.opened.connect(self._on_clicked)
        self.view.customContextMenuRequested.connect(self._on_menu)
        root.addWidget(self.view, 1)

        self.hint = QLabel("按 Ctrl+Shift+A 截一张", self)
        self.hint.setObjectName("ShotEmpty")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setAttribute(Qt.WA_TransparentForMouseEvents)

    def _hotkey_text(self) -> str:
        """空状态提示里说的快捷键，得是真生效的那个（用户可以自定义）。"""
        hook = getattr(self.window(), "shot_hook", None)
        seq = getattr(hook, "sequence", None) if hook is not None else None
        if not seq:
            seq = self.config.value("shot_hotkey", screenshot.DEFAULT_SHOT_HOTKEY, str)
        return (seq or "").strip()

    def refresh(self) -> None:
        records = screenshot.recent_shots()
        self.model.set_records(records)
        seq = self._hotkey_text()
        self.hint.setText(f"按 {seq} 截一张" if seq else "点右上角 ⋯ →「区域截图」开始")
        self.hint.setVisible(not records)
        self._place_hint()

    def _place_hint(self) -> None:
        if self.hint.isVisible():
            self.hint.setGeometry(self.rect())

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._place_hint()
        super().resizeEvent(event)

    # ------------------------------------------------------------------ 动作
    def _on_clicked(self, index: QModelIndex) -> None:
        now = time.monotonic()
        if now - self._last_open < 0.45:        # 双击的两个 press 只算一次
            return
        self._last_open = now
        record = index.data(Qt.UserRole)
        if record is None:
            return
        self._open(record)                      # 截图 → 贴到屏幕上

    def _open(self, record: dict) -> None:
        image = QImage(record["path"])
        if image.isNull():
            self._flash("这张图读不出来了")
            self.refresh()
            return
        # 左边缘对齐面板本体，宽度也跟面板一样，看起来像从面板"长"出来的
        host = self.window()
        screenshot.open_pinned(image, default_dir=self._dir(), on_message=self._flash,
                               width=host.width(),
                               photo_at=host.mapToGlobal(QPoint(0, 0)))
        self._flash("已贴到屏幕上（拖动可移动，Esc 关闭）")

    def _on_menu(self, pos: QPoint) -> None:
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        record = index.data(Qt.UserRole)
        if record is None:
            return
        menu = QMenu(self)
        menu.addAction("贴到屏幕上", lambda: self._open(record))
        menu.addAction("复制图片", lambda: self._copy(record))
        menu.addAction("另存为…", lambda: self._save(record))
        menu.addSeparator()
        menu.addAction("在资源管理器中显示", lambda: self._reveal(record))
        menu.addAction("删除这一张", lambda: self._delete(record))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _copy(self, record: dict) -> None:
        image = QImage(record["path"])
        if image.isNull():
            self._flash("这张图读不出来了")
            return
        QApplication.clipboard().setPixmap(QPixmap.fromImage(image))
        self._flash("已复制这张截图")

    def _save(self, record: dict) -> None:
        image = QImage(record["path"])
        if image.isNull():
            return
        default = os.path.join(self._dir(), os.path.basename(record["path"]))
        path, _filter = QFileDialog.getSaveFileName(
            self, "另存为", default, "PNG 图片 (*.png);;JPEG 图片 (*.jpg)")
        if path:
            if image.save(path):
                self._flash(f"已保存：{path}")
            else:
                self._flash("保存失败")

    def _reveal(self, record: dict) -> None:
        if fileops.reveal_in_explorer(record["path"]):
            self._flash("已在资源管理器中定位")
        else:
            self._flash("这张截图找不到了（可能已被手动删掉）")
            self.refresh()

    def _delete(self, record: dict) -> None:
        screenshot.delete_shot(record["path"])
        self.refresh()
        self._flash("已删除这张截图")

    def _dir(self) -> str:
        last = self.config.value("last_path", "")
        return last if isinstance(last, str) and os.path.isdir(last) else os.path.expanduser("~")

    def _flash(self, text: str) -> None:
        self.message.emit(text)

    # ------------------------------------------------------------------ 页接口
    def activate(self) -> None:
        self.refresh()

    def deactivate(self) -> None:
        pass

    def primary(self) -> None:
        screenshot.start_region_shot(default_dir=self._dir(),
                                     on_message=self._flash)

    def menu(self, anchor=None) -> None:
        menu = self._build_menu()
        if anchor is not None:
            menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        else:
            menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("区域截图", self.primary)
        seq = self._hotkey_text()
        menu.addAction(f"快捷键设定…（{seq or '未设置'}）", self._edit_hotkey)
        menu.addSeparator()
        clear_pinned = menu.addAction("清理固定屏幕上的截屏", self._clear_pinned)
        count = screenshot.pinned_count()
        clear_pinned.setEnabled(count > 0)
        if count:
            clear_pinned.setText(f"清理固定屏幕上的截屏（{count}）")
        menu.addSeparator()
        menu.addAction("打开截图文件夹", self._open_folder)
        menu.addAction("清空全部截图记录", self._clear)
        return menu

    def _edit_hotkey(self) -> None:
        """改键交给主窗口：存哪、怎么重新注册只有它知道；单独跑页面时给个提示。"""
        func = getattr(self.window(), "_edit_shot_hotkey", None)
        if not callable(func):
            self._flash("这个窗口里改不了快捷键")
            return
        func()
        self.refresh()                    # 改完立刻更新空状态里的快捷键文案

    def _clear_pinned(self) -> None:
        count = screenshot.close_all_pinned()
        self._flash(f"已清理 {count} 张贴在屏幕上的截图" if count else "屏幕上没有固定的截图")

    def _open_folder(self) -> None:
        try:
            os.makedirs(screenshot.SHOT_DIR, exist_ok=True)
            subprocess.Popen(f'explorer "{screenshot.SHOT_DIR}"')
        except OSError:
            self._flash("打不开截图文件夹")

    def _clear(self) -> None:
        screenshot.clear_shots()
        self.refresh()
        self._flash("已清空截图记录")

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
