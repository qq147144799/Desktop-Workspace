"""
桌面工作台 —— 界面实现。

窗口通过 AppBar 机制固定在屏幕左边缘，成为桌面工作区的一部分，
所以任何窗口最大化时都只会占用右侧剩余空间，不会把它盖住。
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

from PySide6.QtCore import (QAbstractListModel, QEvent, QFileInfo, QMimeData,
                            QModelIndex, QObject, QPoint, QPointF, QRect, QRectF,
                            QSize, Qt, QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QDrag, QFont, QFontMetrics,
                           QIcon, QKeySequence, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog,
                               QDialogButtonBox, QFileIconProvider, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QLayout,
                               QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox,
                               QProgressDialog, QPushButton, QScrollArea,
                               QSizePolicy, QSplitter, QStyle,
                               QStyledItemDelegate, QSystemTrayIcon, QToolButton,
                               QVBoxLayout, QWidget)

import archive
import fileops
import shelldrag
import shellnew
import theme
from appbar import AppBar, primary_screen_size
from config import Config
from notes_panel import NotesPanel

FILE_ATTRIBUTE_HIDDEN = 0x2
ROW_HEIGHT = 48

# --------------------------------------------------------------------------- 数据


@dataclass(slots=True)
class Entry:
    path: str
    name: str
    is_dir: bool
    size: int
    mtime: float
    subtitle: str = ""          # 非空时替掉「大小 · 时间」那一行
    expr: str = ""              # 非空表示这是一条算式历史
    result: str = ""            # 算式结果
    note: str = ""              # 备注，显示在右侧


_num_re = re.compile(r"(\d+)")

# ---------------------------------------------------------------- 地址栏算式
_MATH_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "sqrt": math.sqrt, "cbrt": lambda x: x ** (1 / 3), "pow": pow,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
}
_MATH_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


_EXPR_MARKS = set("+-*/=×÷＋－")


def looks_like_expression(text: str) -> bool:
    """敲进地址栏的东西像不像算式（含运算符、括号或函数名）。"""
    text = (text or "").strip()
    if not text:
        return False
    if any(ch in _EXPR_MARKS for ch in text):
        return True
    if "(" in text or ")" in text:
        return True
    lowered = text.lower()
    return any(name in lowered for name in ("sqrt", "log", "sin", "cos", "tan", "pi"))


def eval_expression(text: str):
    """安全求值：只认数字、四则运算、括号、常用函数。失败返回 (None, 原因)。"""
    import ast

    expr = (text or "").strip()
    for src, dst in (("×", "*"), ("÷", "/"), ("，", ","), ("（", "("),
                     ("）", ")"), ("－", "-"), ("＋", "+")):
        expr = expr.replace(src, dst)
    expr = expr.replace("^", "**")
    if not expr or len(expr) > 300:
        return None, "空表达式"

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None, "看不懂这个算式"

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError
            return node.value
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            op = node.op
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, ast.Div):
                return left / right
            if isinstance(op, ast.FloorDiv):
                return left // right
            if isinstance(op, ast.Mod):
                return left % right
            if isinstance(op, ast.Pow):
                if abs(right) > 512:
                    raise ValueError
                return left ** right
            raise ValueError
        if isinstance(node, ast.UnaryOp):
            value = walk(node.operand)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value
            raise ValueError
        if isinstance(node, ast.Name):
            if node.id in _MATH_CONSTS:
                return _MATH_CONSTS[node.id]
            raise ValueError
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _MATH_FUNCS:
                raise ValueError
            args = [walk(a) for a in node.args]
            return _MATH_FUNCS[node.func.id](*args)
        raise ValueError

    try:
        value = walk(tree)
    except ZeroDivisionError:
        return None, "除数是 0"
    except (ValueError, TypeError, OverflowError):
        return None, "看不懂这个算式"

    if isinstance(value, complex):
        return None, "结果是复数"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None, "结果超出范围"
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value)), None
        return f"{value:.10g}", None
    return str(value), None


def _natkey(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in _num_re.split(text)]


def human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    units = ["KB", "MB", "GB", "TB", "PB"]
    value = float(num)
    for unit in units:
        value /= 1024.0
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    return f"{num} B"


def human_time(ts: float) -> str:
    lt = time.localtime(ts)
    now = time.localtime()
    if (lt.tm_year, lt.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime("%H:%M", lt)
    if lt.tm_year == now.tm_year:
        return time.strftime("%m-%d %H:%M", lt)
    return time.strftime("%Y-%m-%d", lt)


def _volume_label(root: str) -> str:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        vol = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        serial, maxlen, flags = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), vol, 261,
            ctypes.byref(serial), ctypes.byref(maxlen), ctypes.byref(flags), fs, 261)
        return vol.value if ok else ""
    except Exception:
        return ""


def computer_entries() -> list[Entry]:
    """「此电脑」：列出所有盘符。"""
    items: list[Entry] = []
    try:
        drives = sorted(os.listdrives())
    except Exception:
        drives = []
    for drive in drives:
        letter = drive.rstrip("\\/")
        root = letter + "\\"
        label = _volume_label(root)
        name = f"{label} ({letter})" if label else f"本地磁盘 ({letter})"
        items.append(Entry(root, name, True, 0, 0.0))
    return items


def archive_entries(zip_path: str, inner: str, show_hidden: bool,
                    sort_key: str, sort_desc: bool) -> list[Entry]:
    """列出压缩包某一层的内容，转成和普通目录一样的 Entry。"""
    raw = archive.list_entries(zip_path, inner, show_hidden)
    items = [Entry(archive.join(zip_path, inner, name), name, is_dir, size, mtime)
             for name, is_dir, size, mtime in raw]

    if sort_key == "mtime":
        keyfn = lambda e: e.mtime                                   # noqa: E731
    elif sort_key == "size":
        keyfn = lambda e: e.size                                    # noqa: E731
    elif sort_key == "type":
        keyfn = lambda e: (os.path.splitext(e.name)[1].lower(), _natkey(e.name))  # noqa: E731
    else:
        keyfn = lambda e: _natkey(e.name)                            # noqa: E731

    dirs = [e for e in items if e.is_dir]
    files = [e for e in items if not e.is_dir]
    dirs.sort(key=keyfn, reverse=sort_desc)
    files.sort(key=keyfn, reverse=sort_desc)
    return dirs + files


class ArchiveWatcher(QObject):
    """盯着从压缩包里解出来的临时文件，一有改动就写回压缩包。"""

    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: dict[str, tuple[str, str, float]] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._check)
        self._timer.start()

    def watch(self, local_path: str, zip_path: str, inner: str) -> None:
        try:
            stamp = os.path.getmtime(local_path)
        except OSError:
            return
        self._items[local_path] = (zip_path, inner, stamp)

    def _check(self) -> None:
        for local_path, (zip_path, inner, stamp) in list(self._items.items()):
            try:
                current = os.path.getmtime(local_path)
            except OSError:
                continue
            if abs(current - stamp) < 0.001:
                continue
            try:
                archive.write_back(zip_path, inner, local_path)
            except Exception:          # 包被占用/加密之类，下一轮再试
                continue
            self._items[local_path] = (zip_path, inner, current)
            self.changed.emit(f"「{os.path.basename(inner)}」改动已存回压缩包")


def resource_path(name: str) -> str:
    """打包成 exe 后资源在临时解压目录里。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def app_dir() -> str:
    """程序所在目录（打包成 exe 后指向 exe 所在文件夹，而不是解压临时目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


SORT_LABELS = {"name": "名称", "mtime": "修改时间", "size": "大小", "type": "类型"}

COMPUTER_PATH = "::computer"
COMPUTER_LABEL = "此电脑"


def _shorten(text: str, limit: int = 26) -> str:
    """菜单里的窗口标题太长会撑爆菜单，截断一下。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def load_entries(path: str, show_hidden: bool, sort_key: str = "name",
                 sort_desc: bool = False) -> list[Entry]:
    items: list[Entry] = []
    with os.scandir(path) as it:
        for de in it:
            try:
                name = de.name
                if not show_hidden and name.startswith("."):
                    continue
                st = de.stat(follow_symlinks=False)
                is_dir = de.is_dir(follow_symlinks=False)
                if not show_hidden and (getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_HIDDEN):
                    continue
                items.append(Entry(de.path, name, is_dir, st.st_size, st.st_mtime))
            except OSError:
                continue

    if sort_key == "mtime":
        keyfn = lambda e: e.mtime                                   # noqa: E731
    elif sort_key == "size":
        keyfn = lambda e: e.size                                    # noqa: E731
    elif sort_key == "type":
        keyfn = lambda e: (os.path.splitext(e.name)[1].lower(), _natkey(e.name))  # noqa: E731
    else:
        keyfn = lambda e: _natkey(e.name)                           # noqa: E731

    dirs = [e for e in items if e.is_dir]
    files = [e for e in items if not e.is_dir]
    dirs.sort(key=keyfn, reverse=sort_desc)
    files.sort(key=keyfn, reverse=sort_desc)
    return dirs + files


def default_quick_items() -> list[dict]:
    home = os.path.expanduser("~")
    items: list[dict] = []
    for label, sub in (("桌面", "Desktop"), ("下载", "Downloads"), ("文档", "Documents"),
                       ("图片", "Pictures"), ("视频", "Videos")):
        p = os.path.join(home, sub)
        if os.path.isdir(p):
            items.append({"label": label, "path": p})
    try:
        items.extend({"label": d.rstrip("\\"), "path": d} for d in os.listdrives())
    except Exception:
        pass
    return items


# --------------------------------------------------------------------------- 模型


class FileListModel(QAbstractListModel):
    rename_requested = Signal(str, str)      # (旧路径, 新名字)

    def __init__(self, icon_provider: QFileIconProvider, parent=None):
        super().__init__(parent)
        self._items: list[Entry] = []
        self._icons = icon_provider
        self._type_cache: dict[str, QIcon] = {}
        self._dir_icon = icon_provider.icon(QFileIconProvider.Folder)
        self._file_icon = icon_provider.icon(QFileIconProvider.File)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        # ItemIsDragEnabled 是拖出文件的前提，少了它 startDrag 根本不会被调用
        return (Qt.ItemIsEnabled | Qt.ItemIsSelectable
                | Qt.ItemIsDragEnabled | Qt.ItemIsEditable)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        entry = self._items[index.row()]
        if role == Qt.DisplayRole:
            return entry.name
        if role == Qt.UserRole:
            return entry
        if role == Qt.DecorationRole:
            if entry.expr:               # 算式历史不是文件，别给文件图标
                return None
            return self.icon_for(entry)
        if role == Qt.ToolTipRole:
            if entry.expr:
                return f"{entry.expr} = {entry.result}" + (
                    f"\n备注：{entry.note}" if entry.note else "")
            return entry.path
        return None

    def icon_for(self, entry: Entry) -> QIcon:
        if entry.is_dir:
            return self._dir_icon
        ext = os.path.splitext(entry.name)[1].lower()
        icon = self._type_cache.get(ext)
        if icon is None:
            icon = self._icons.icon(QFileInfo(entry.path))
            if icon.isNull():
                icon = self._file_icon
            self._type_cache[ext] = icon
        return icon

    def set_items(self, items: list[Entry]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()

    def setData(self, index, value, role=Qt.EditRole) -> bool:  # noqa: N802
        if role != Qt.EditRole or not index.isValid():
            return False
        entry = self.entry_at(index.row())
        if entry is None:
            return False
        new_name = str(value).strip()
        if not new_name or new_name == entry.name:
            return False
        self.rename_requested.emit(entry.path, new_name)
        return True

    def entry_at(self, row: int) -> Entry | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def row_of_name(self, name: str) -> int:
        for i, e in enumerate(self._items):
            if e.name.lower() == name.lower():
                return i
        return -1


class ItemDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(0, ROW_HEIGHT)

    # ---------------------------------------------------------- 行内重命名
    def createEditor(self, parent, option, index):  # noqa: N802
        editor = QLineEdit(parent)
        editor.setObjectName("RenameEditor")
        return editor

    def setEditorData(self, editor, index) -> None:  # noqa: N802
        entry = index.data(Qt.UserRole)
        if entry is None or not isinstance(editor, QLineEdit):
            super().setEditorData(editor, index)
            return
        editor.setText(entry.name)
        # 文件夹、或者本来就没有扩展名 —— 全选；否则只选中扩展名前面那段。
        # QLineEdit 获得焦点时会自己全选，所以延后一拍再覆盖成我们要的范围。
        stem, ext = os.path.splitext(entry.name)
        if entry.is_dir or not ext or not stem:
            start, length = 0, len(entry.name)
        else:
            start, length = 0, len(stem)
        QTimer.singleShot(0, lambda e=editor, a=start, n=length: e.setSelection(a, n))

    def updateEditorGeometry(self, editor, option, index) -> None:  # noqa: N802
        rect = option.rect
        left = rect.left() + 42
        width = max(rect.right() - left - 8, 80)
        editor.setGeometry(left, rect.top() + 6, width, 22)

    _calc_icons: dict = {}

    @classmethod
    def calc_icon(cls, color: str) -> QPixmap:
        """画个计算器小图标（缓存）。"""
        cached = cls._calc_icons.get(color)
        if cached is not None:
            return cached
        size, dpr = 22, 2
        pm = QPixmap(size * dpr, size * dpr)
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(color), 1.7)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(3.0, 1.8, 16.0, 18.4), 3.4, 3.4)
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(5.7, 4.5, 10.6, 4.4), 1.2, 1.2)
        for row in range(2):
            for col in range(3):
                p.drawEllipse(QRectF(5.9 + col * 3.9, 11.6 + row * 3.6, 2.3, 2.3))
        p.end()
        cls._calc_icons[color] = pm
        return pm

    def paint(self, painter: QPainter, option, index) -> None:
        entry = index.data(Qt.UserRole)
        if entry is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = option.rect

        if option.state & QStyle.State_Selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.color("sel")))
            painter.drawRoundedRect(rect.adjusted(6, 2, -6, -2), 7, 7)
        elif option.state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.color("hover")))
            painter.drawRoundedRect(rect.adjusted(6, 2, -6, -2), 7, 7)

        is_expr = bool(entry.expr)
        icon_rect = QRect(rect.left() + 14, rect.top() + 13, 22, 22)
        if is_expr:
            painter.drawPixmap(QPoint(rect.left() + 14, rect.top() + 13),
                               self.calc_icon(theme.color("dim")))
        else:
            icon = index.data(Qt.DecorationRole)
            if isinstance(icon, QIcon):
                icon.paint(painter, icon_rect)

        text_left = rect.left() + 46

        note = entry.note
        note_font = QFont(option.font)
        note_font.setPointSizeF(theme.META_FONT_PT * 1.18)
        fm_note = QFontMetrics(note_font)
        note_w = 0
        if note:
            note_w = min(fm_note.horizontalAdvance(note), int(rect.width() * 0.36))

        avail = rect.right() - text_left - 10 - (note_w + 10 if note_w else 0)
        if avail < 20:
            painter.restore()
            return

        # 第一行：算式（或文件名）
        name_font = QFont(option.font)
        name_font.setPointSizeF(theme.LIST_FONT_PT * (0.88 if is_expr else 1.0))
        painter.setFont(name_font)
        painter.setPen(QColor(theme.color("dim" if is_expr else "text")))
        fm = QFontMetrics(name_font)
        painter.drawText(QRect(text_left, rect.top() + 8, avail, 17),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         fm.elidedText(entry.name, Qt.ElideMiddle, avail))

        # 第二行：算式突出显示结果，普通文件显示大小/时间
        if is_expr:
            sub = entry.subtitle or f"=  {entry.result}"
            result_font = QFont(option.font)
            result_font.setPointSizeF(theme.LIST_FONT_PT * 0.96)
            result_font.setBold(True)
            painter.setFont(result_font)
            painter.setPen(QColor(theme.color("text")))
            fm_r = QFontMetrics(result_font)
            painter.drawText(QRect(text_left, rect.top() + 25, avail, 18),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             fm_r.elidedText(sub, Qt.ElideRight, avail))
        else:
            if entry.subtitle:
                sub = entry.subtitle
            elif entry.is_dir:
                sub = "文件夹"
            else:
                sub = f"{human_size(entry.size)}   ·   {human_time(entry.mtime)}"
            sub_font = QFont(option.font)
            sub_font.setPointSizeF(theme.META_FONT_PT)
            painter.setFont(sub_font)
            painter.setPen(QColor(theme.color("dim")))
            fm2 = QFontMetrics(sub_font)
            painter.drawText(QRect(text_left, rect.top() + 26, avail, 16),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             fm2.elidedText(sub, Qt.ElideRight, avail))

        if note_w:
            painter.setFont(note_font)
            painter.setPen(QColor(theme.color("dim")))
            painter.drawText(QRect(rect.right() - note_w - 10, rect.top() + 9,
                                   note_w, 17),
                             Qt.AlignRight | Qt.AlignVCenter,
                             fm_note.elidedText(note, Qt.ElideRight, note_w))
        painter.restore()


# --------------------------------------------------------------------------- 图标


def _nav_pixmap(kind: str, color: str, size: int = 22, dpr: int = 2) -> QPixmap:
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color), 1.9)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    c = size / 2.0
    if kind in ("back", "forward", "up"):
        path = QPainterPath()
        if kind == "forward":
            # 带杆的箭头，比单纯的「>」更像"前进"
            p.drawLine(QPointF(c - 6.0, c), QPointF(c + 4.6, c))
            path.moveTo(c + 0.4, c - 4.8)
            path.lineTo(c + 5.4, c)
            path.lineTo(c + 0.4, c + 4.8)
        elif kind == "back":
            p.drawLine(QPointF(c + 6.0, c), QPointF(c - 4.6, c))
            path.moveTo(c - 0.4, c - 4.8)
            path.lineTo(c - 5.4, c)
            path.lineTo(c - 0.4, c + 4.8)
        else:
            path.moveTo(c - 4.6, c + 2.4)
            path.lineTo(c, c - 2.6)
            path.lineTo(c + 4.6, c + 2.4)
        p.drawPath(path)
    elif kind == "home":
        roof = QPainterPath()
        roof.moveTo(c - 6.4, c - 0.2)
        roof.lineTo(c, c - 6.2)
        roof.lineTo(c + 6.4, c - 0.2)
        p.drawPath(roof)
        body = QPainterPath()
        body.moveTo(c - 4.6, c - 1.1)
        body.lineTo(c - 4.6, c + 5.6)
        body.lineTo(c + 4.6, c + 5.6)
        body.lineTo(c + 4.6, c - 1.1)
        p.drawPath(body)
        door = QPainterPath()
        door.moveTo(c - 1.4, c + 5.6)
        door.lineTo(c - 1.4, c + 1.6)
        door.lineTo(c + 1.4, c + 1.6)
        door.lineTo(c + 1.4, c + 5.6)
        p.drawPath(door)
    elif kind == "refresh":
        r = size * 0.275
        p.drawArc(QRectF(c - r, c - r, 2 * r, 2 * r), 35 * 16, 285 * 16)
        # 在圆弧末端用两条短线拼一个 V 形箭头（和圆弧同粗细，小尺寸下更清楚）
        ang = math.radians(320)
        ax = c + r * math.cos(ang)
        ay = c - r * math.sin(ang)
        for delta in (math.radians(48), math.radians(-48)):
            p.drawLine(QPointF(ax, ay),
                       QPointF(ax + 5.4 * math.cos(ang + delta),
                               ay - 5.4 * math.sin(ang + delta)))
    p.end()
    return pm


def nav_icon(kind: str) -> QIcon:
    icon = QIcon()
    icon.addPixmap(_nav_pixmap(kind, theme.color("text3")), QIcon.Normal, QIcon.Off)
    icon.addPixmap(_nav_pixmap(kind, theme.color("text")), QIcon.Active, QIcon.Off)
    icon.addPixmap(_nav_pixmap(kind, theme.color("dim")), QIcon.Disabled, QIcon.Off)
    return icon


# --------------------------------------------------------------------------- 控件


CHIP_MIME = "application/x-sideexplorer-chip"


class ChipButton(QPushButton):
    """快捷位置按钮：单击跳转，拖动换位，右键出菜单。"""

    def __init__(self, label: str, index: int, owner, parent=None):
        super().__init__(label, parent)
        self.index = index
        self.owner = owner
        self._press_pos: QPoint | None = None
        self._dragging = False

        self.setObjectName("Chip")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(22)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.owner.show_chip_menu(self.index, self.mapToGlobal(pos)))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_pos is not None:
            if (event.position().toPoint() - self._press_pos).manhattanLength() > 8:
                self._press_pos = None
                self._dragging = True
                drag = QDrag(self)
                mime = QMimeData()
                mime.setData(CHIP_MIME, str(self.index).encode())
                drag.setMimeData(mime)
                drag.setPixmap(self.grab())
                drag.setHotSpot(event.position().toPoint())
                drag.exec(Qt.MoveAction)
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was_dragging = self._dragging
        self._press_pos = None
        self._dragging = False
        if was_dragging:                 # 拖动过就不要触发点击跳转
            event.accept()
            return
        super().mouseReleaseEvent(event)


class FlowLayout(QLayout):
    """一行放不下就自动换行的流式布局。"""

    def __init__(self, parent=None, h_spacing: int = 6, v_spacing: int = 5):
        super().__init__(parent)
        self._items: list = []
        self._h = h_spacing
        self._v = v_spacing
        self.setContentsMargins(10, 4, 10, 4)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        x, y = area.x(), area.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line_height > 0:
                x = area.x()
                y += line_height + self._v
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class ChipBar(QWidget):
    """快捷位置容器：自动换行，接收拖动排序。"""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.setAcceptDrops(True)
        self.flow = FlowLayout(self)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self.flow.heightForWidth(width)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(CHIP_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(CHIP_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasFormat(CHIP_MIME):
            return
        try:
            src = int(bytes(event.mimeData().data(CHIP_MIME)).decode())
        except (ValueError, UnicodeDecodeError):
            return
        self.owner.drop_chip_at(src, event.position().toPoint())
        event.acceptProposedAction()


class FileListView(QListView):
    """文件列表：既能把文件拖出去，也能把外面的文件拖进来。"""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)

    # ------------------------------------------------------------ 拖出去
    def startDrag(self, supported_actions) -> None:  # noqa: N802
        rows = sorted({i.row() for i in self.selectedIndexes()})
        entries = []
        for row in rows:
            entry = self.model().entry_at(row) if hasattr(self.model(), "entry_at") else None
            if entry is not None:
                entries.append(entry)
        if not entries:
            return

        # 压缩包内部条目没有真实路径，拖不出去（先解压）
        paths = [e.path for e in entries if os.path.exists(e.path)]
        if not paths:
            self.owner._set_status("压缩包里的文件要先解压才能拖出去")
            return

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])

        drag = QDrag(self)
        drag.setMimeData(mime)
        index = self.currentIndex()
        if not index.isValid() and rows:
            index = self.model().index(rows[0], 0)
        rect = self.visualRect(index)
        if rect.isValid() and rect.height() > 4:
            drag.setPixmap(self.viewport().grab(rect))
            drag.setHotSpot(QPoint(26, rect.height() // 2))

        # 让目标（资源管理器/桌面）自己决定复制还是移动
        action = drag.exec(Qt.CopyAction | Qt.MoveAction)
        if action == Qt.MoveAction:
            QTimer.singleShot(1500, self.owner.reload)

    # ------------------------------------------------------------ 拖进来
    @staticmethod
    def _local_paths(mime) -> list[str]:
        if mime.hasUrls():
            return [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        # Win11 的资源管理器只给 Shell IDList，没有 text/uri-list
        fmt = 'application/x-qt-windows-mime;value="Shell IDList Array"'
        if mime.hasFormat(fmt):
            try:
                raw = bytes(mime.data(fmt).data())
            except (AttributeError, TypeError):
                return []
            return shelldrag.paths_from_shell_idlist(raw)
        return []

    @staticmethod
    def _has_files(mime) -> bool:
        if mime.hasUrls():
            return True
        return mime.hasFormat('application/x-qt-windows-mime;value="Shell IDList Array"')

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._has_files(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._has_files(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._local_paths(event.mimeData())
        if not paths:
            event.ignore()
            return

        # 拖到某个文件夹行上就放进那个文件夹，否则放进当前目录
        target_dir = self.owner.current
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            entry = self.model().entry_at(index.row())
            if entry is not None and entry.is_dir and not archive.split_path(entry.path):
                target_dir = entry.path

        # 默认复制（安全）；按住 Shift 时移动
        move = bool(event.modifiers() & Qt.ShiftModifier)
        if not self.owner.receive_dropped_files(paths, target_dir, move=move):
            event.ignore()
            return
        event.setDropAction(Qt.MoveAction if move else Qt.CopyAction)
        event.accept()



class ZipWorker(QThread):
    """后台压缩，往进度条上报，可中途取消。"""

    progressed = Signal(int, str)
    done = Signal(bool, str, int)

    def __init__(self, paths: list[str], out_zip: str, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.out_zip = out_zip
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            count = archive.compress(
                self.paths, self.out_zip,
                progress=lambda done, total, name: self.progressed.emit(
                    int(done * 100 / max(total, 1)), name),
                cancelled=lambda: self._cancel,
            )
            self.done.emit(True, "", count)
        except archive.Cancelled:
            self.done.emit(False, "cancelled", 0)
        except Exception as exc:            # 没权限、包被占用之类
            self.done.emit(False, str(exc), 0)


class ResizeHandle(QFrame):
    """窗口右边缘的宽度拖拽手柄（悬浮在内容之上）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.SizeHorCursor)
        self.setFixedWidth(7)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)


class HScrollArea(QScrollArea):
    """横向滚动的快捷入口条：滚轮直接横向滚，溢出时也能操作到。"""

    def wheelEvent(self, event) -> None:  # noqa: N802
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - event.angleDelta().y())
        event.accept()


class WindowPickerDialog(QDialog):
    """列出当前打开的窗口（按程序去重），勾选要「后台运行」的那些。"""

    def __init__(self, windows: list[dict], existing: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择窗口")
        self.setMinimumWidth(420)
        self.resize(460, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        tip = QLabel("勾选你要保护的程序：以后点它们的关闭按钮，只会缩到托盘并继续运行。")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("输入关键字过滤…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setAlternatingRowColors(True)
        self.list.setIconSize(QSize(20, 20))
        self.list.setWordWrap(False)
        self.list.setTextElideMode(Qt.ElideRight)          # 长标题省略，别硬切
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.shown_exes: list[str] = []
        providers = QFileIconProvider()
        known = {e.lower() for e in existing}
        for win in windows:
            label = f"{win['exe']}    {win['title']}"
            if win["count"] > 1:
                label += f"    （{win['count']} 个窗口）"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, win["exe"])
            item.setToolTip(win.get("exe_path") or win["exe"])
            path = win.get("exe_path") or ""
            if path and os.path.isfile(path):
                item.setIcon(providers.icon(QFileInfo(path)))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if win["exe"] in known else Qt.Unchecked)
            if win["exe"] in known:
                item.setText(label + "    （已添加）")
            self.shown_exes.append(win["exe"])
            self.list.addItem(item)

        if self.list.count() == 0:
            empty = QListWidgetItem("（没找到可选的窗口）")
            empty.setFlags(Qt.NoItemFlags)
            self.list.addItem(empty)

    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for row in range(self.list.count()):
            item = self.list.item(row)
            hide = bool(needle) and needle not in item.text().lower()
            item.setHidden(hide)

    def selected_exes(self) -> list[str]:
        picked = []
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.flags() & Qt.ItemIsUserCheckable and item.checkState() == Qt.Checked:
                exe = item.data(Qt.UserRole)
                if exe:
                    picked.append(exe)
        return picked


STYLE = ""  # 由 theme.build_qss() 动态生成



class SideExplorer(QWidget):
    MIN_WIDTH = 260
    MAX_WIDTH = 620

    def __init__(self):
        super().__init__(None)
        self.settings = Config()
        self.theme_key = str(self.settings.value("theme", "light"))
        theme.set_theme(self.theme_key)

        self.width_logical = int(self.settings.value("width", 340))
        self.show_hidden = self.settings.value("show_hidden", False, bool)
        self.topmost = self.settings.value("topmost", True, bool)
        self.edge = str(self.settings.value("edge", "left"))
        if self.edge not in ("left", "right"):
            self.edge = "left"
        self.click_to_open = self.settings.value("click_to_open", False, bool)
        self._really_quit = False
        self._expr_history: list[dict] = self._load_expr_history()
        if self.settings.value("expr_history", []) != self._expr_history:
            self._save_expr_history()      # 把旧版存成字符串的数据迁移成正常列表
        self._expr_desc = bool(self.settings.value("expr_desc", True, bool))
        self._calc_mode = False
        self._watcher = ArchiveWatcher(self)
        self._watcher.changed.connect(self._set_status)
        self.sort_key = str(self.settings.value("sort_key", "name"))
        if self.sort_key not in SORT_LABELS:
            self.sort_key = "name"
        self.sort_desc = self.settings.value("sort_desc", False, bool)
        self.quick_items: list[dict] = self._load_quick_items()
        self._shortcuts: list[QShortcut] = []

        self.current = ""
        self.history: list[str] = []
        self.hist_idx = -1
        self.appbar: AppBar | None = None
        self._drag_x: float | None = None
        self._drag_w = 0
        self._syncing = False
        self._last_activation: tuple[tuple | None, float] = (None, 0.0)
        self._splitter_timer: QTimer | None = None

        self.setWindowTitle("桌面工作台")
        self.setObjectName("Root")
        self.setStyleSheet(theme.build_qss())
        self.setFont(QFont("Microsoft YaHei UI", 9))
        flags = Qt.Tool | Qt.FramelessWindowHint
        if self.topmost:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)

        self.icon_provider = QFileIconProvider()
        self.model = FileListModel(self.icon_provider, self)
        self.model.rename_requested.connect(self._apply_rename)
        self.delegate = ItemDelegate(self)

        self._build_ui()
        self._build_tray()
        self._setup_shortcuts()

        last = self.settings.value("last_path", "")
        start = last if isinstance(last, str) and os.path.isdir(last) else os.path.expanduser("~")
        self.navigate(start, add_history=True)

    # ------------------------------------------------------------------ 构建
    def _icon_button(self, kind: str, tip: str, slot, size: int = 28) -> QToolButton:
        btn = QToolButton(self)
        btn.setObjectName("IconBtn")
        btn.setIcon(nav_icon(kind))
        btn._nav_kind = kind
        btn.setIconSize(QSize(size - 10, size - 10))
        btn.setToolTip(tip)
        btn.setFixedSize(size, size)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(slot)
        return btn

    def _nav_button(self, text: str, tip: str, slot=None, size: int = 26,
                    obj_name: str = "NavBtn") -> QToolButton:
        btn = QToolButton(self)
        btn.setObjectName(obj_name)
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setFixedSize(size, size)
        btn.setCursor(Qt.PointingHandCursor)
        if slot is not None:
            btn.clicked.connect(slot)
        return btn

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 标题栏
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(38)
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda pos, w=header: self.show_main_menu_at(w.mapToGlobal(pos)))
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 0, 8, 0)
        hl.setSpacing(2)

        title = QLabel("桌面工作台")
        title.setObjectName("Title")
        title.setAlignment(Qt.AlignCenter)
        title.setContextMenuPolicy(Qt.CustomContextMenu)
        title.customContextMenuRequested.connect(
            lambda pos, w=title: self.show_main_menu_at(w.mapToGlobal(pos)))
        hl.addStretch(1)
        hl.addWidget(title)
        hl.addStretch(1)
        root.addWidget(header)

        # 导航栏
        nav = QWidget()
        nav.setObjectName("NavBar")
        nav.setFixedHeight(38)
        nl = QHBoxLayout(nav)
        nl.setContentsMargins(8, 5, 8, 5)
        nl.setSpacing(2)

        self.btn_home = self._icon_button(
            "home", "此电脑 (Alt+Home)", lambda: self.navigate(COMPUTER_PATH))
        self.btn_up = self._icon_button("up", "上一级 (Backspace)", self.go_up)
        nl.addWidget(self.btn_home)
        nl.addWidget(self.btn_up)

        self.path_edit = QLineEdit()
        self.path_edit.setObjectName("Path")
        self.path_edit.setFixedHeight(26)
        self.path_edit.setClearButtonEnabled(False)
        self.path_edit.returnPressed.connect(self._on_path_entered)
        nl.addSpacing(2)
        nl.addWidget(self.path_edit, 1)

        # 右箭头：跳到地址栏里写的那个位置，效果等同在里面按回车
        self.btn_go = self._icon_button(
            "forward", "前往地址栏中的路径（等同按回车）", self._on_path_entered)
        nl.addWidget(self.btn_go)
        root.addWidget(nav)

        # 快速位置 chips
        # 快捷位置栏（一行放不下自动换行）
        self.chip_box = QWidget()
        self.chip_box.setObjectName("ChipBox")
        box_layout = QVBoxLayout(self.chip_box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(0)
        self.chip_host = ChipBar(self)
        self.chip_layout = self.chip_host.flow
        box_layout.addWidget(self.chip_host)
        self.chip_box.setFixedHeight(32)
        root.addWidget(self.chip_box)
        self._build_chips()

        # 文件列表 + 便签（上下分区）
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setObjectName("MainSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)

        file_area = QWidget()
        bl = QVBoxLayout(file_area)
        bl.setContentsMargins(0, 4, 0, 0)
        bl.setSpacing(0)

        self.view = FileListView(self)
        self.view.setModel(self.model)
        self.view.setDragEnabled(True)
        self.view.setDragDropMode(QAbstractItemView.DragDrop)
        self.view.setItemDelegate(self.delegate)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.customContextMenuRequested.connect(self._on_list_context_menu)
        self.view.activated.connect(self._on_activated)
        self.view.doubleClicked.connect(self._on_activated)
        self.view.clicked.connect(self._on_click_open)
        if self.click_to_open:
            self.view.viewport().setCursor(Qt.PointingHandCursor)
        bl.addWidget(self.view, 1)
        file_area.setMinimumHeight(150)
        self.splitter.addWidget(file_area)

        self.notes_panel = NotesPanel(self.settings, self)
        self.notes_panel.setMinimumHeight(120)
        self.notes_panel.message.connect(self._set_status)
        self.splitter.addWidget(self.notes_panel)

        sizes = self.settings.value("splitter_sizes", None)
        if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(v, int) for v in sizes):
            self.splitter.setSizes([max(150, sizes[0]), max(120, sizes[1])])
        else:
            self.splitter.setSizes([700, 210])
        self.splitter.splitterMoved.connect(self._on_splitter_moved)
        root.addWidget(self.splitter, 1)

        # 状态栏
        status = QFrame()
        status.setObjectName("Status")
        status.setFixedHeight(24)
        sl = QHBoxLayout(status)
        sl.setContentsMargins(12, 0, 12, 0)
        self.status_label = QLabel("")
        # 关键：标签默认会把「最长的那句提示」算进窗口最小宽度，
        # 于是状态栏一显示长文字，窗口就再也缩不回去、还会被顶出屏幕。
        # 设成 Ignored 让布局无视它的宽度诉求，文字该省略就省略。
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status_label.setMinimumWidth(0)
        sl.addWidget(self.status_label, 1)

        self.btn_sort = QToolButton()
        self.btn_sort.setObjectName("SortBtn")
        self.btn_sort.setCursor(Qt.PointingHandCursor)
        self.btn_sort.setToolTip("排序方式")
        self.btn_sort.clicked.connect(self.show_sort_menu)
        sl.addWidget(self.btn_sort)
        root.addWidget(status)
        self._update_sort_button()

        # 宽度拖拽手柄（悬浮）
        self.handle = ResizeHandle(self)
        self.handle.installEventFilter(self)
        self.handle.raise_()

    def _setup_shortcuts(self) -> None:
        bindings = (
            ("Ctrl+C", lambda: self._handle_clipboard("copy")),
            ("Ctrl+X", lambda: self._handle_clipboard("cut")),
            ("Ctrl+V", lambda: self._handle_clipboard("paste")),
            ("Ctrl+A", self._select_all),
            ("F2", self.rename_entry),
            ("Delete", self.delete_entries),
            ("F5", self.reload),
        )
        for seq, slot in bindings:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
            self._shortcuts.append(sc)

    # ------------------------------------------------------------------ 剪贴板
    @staticmethod
    def _text_focus() -> QLineEdit | None:
        widget = QApplication.focusWidget()
        return widget if isinstance(widget, QLineEdit) else None

    def _focus_in_notes(self) -> bool:
        """焦点是否在便签区（那边的 F2/Delete 要操作便签，不是文件）。"""
        widget = QApplication.focusWidget()
        if widget is None:
            return False
        panel = getattr(self, "notes_panel", None)
        if panel is None:
            return False
        return widget is panel or panel.isAncestorOf(widget)

    def _on_splitter_moved(self, *_args) -> None:
        if self._splitter_timer is None:
            self._splitter_timer = QTimer(self)
            self._splitter_timer.setSingleShot(True)
            self._splitter_timer.setInterval(500)
            self._splitter_timer.timeout.connect(self._save_splitter_sizes)
        self._splitter_timer.start()

    def _save_splitter_sizes(self) -> None:
        self.settings.setValue("splitter_sizes", list(self.splitter.sizes()))

    def _handle_clipboard(self, mode: str) -> None:
        """地址栏正在编辑时，Ctrl+C/X/V 应作用于文本而不是文件。"""
        edit = self._text_focus()
        if edit is not None:
            {"copy": edit.copy, "cut": edit.cut, "paste": edit.paste}[mode]()
            return
        if self._focus_in_notes():
            return
        if mode == "paste":
            self.paste_entries()
        else:
            self.copy_entries(mode == "cut")

    def _select_all(self) -> None:
        edit = self._text_focus()
        if edit is not None:
            edit.selectAll()
            return
        if self._focus_in_notes():
            return
        self.view.selectAll()

    def copy_entries(self, cut: bool = False) -> None:
        entries = self._selected_entries()
        if not entries:
            self._set_status("先选中要操作的文件")
            return
        if fileops.set_files([e.path for e in entries], cut=cut):
            verb = "剪切" if cut else "复制"
            self._set_status(f"已{verb} {len(entries)} 个项目，可粘贴到其他文件夹")
        else:
            self._set_status("写入剪贴板失败")

    def paste_entries(self) -> None:
        paths, _cut = fileops.get_files()
        if not paths:
            self._set_status("剪贴板里没有可粘贴的文件")
            return
        done, errors, was_cut = fileops.paste_into(self.current)
        self.reload()
        if errors:
            self._set_status(f"粘贴 {len(done)} 项，{len(errors)} 项失败")
        elif done:
            self._set_status(f"已{'移动' if was_cut else '复制'} {len(done)} 个项目")
        else:
            self._set_status("没有可粘贴的内容")

    def delete_entries(self) -> None:
        edit = self._text_focus()
        if edit is not None:
            edit.del_()                  # 地址栏里照常删字符
            return
        if self._focus_in_notes():
            self.notes_panel.delete_selected()
            return
        entries = self._selected_entries()
        if not entries:
            return
        paths = [e.path for e in entries]
        if fileops.recycle(paths):
            self.reload()
            self._set_status(f"已把 {len(entries)} 个项目移到回收站")
        else:
            self._set_status("删除失败（可能被占用或没有权限）")

    def rename_entry(self) -> None:
        if self._text_focus() is not None:
            return
        if self._focus_in_notes():
            self.notes_panel.rename_selected()
            return
        entries = self._selected_entries()
        if len(entries) != 1:
            self._set_status("请选中一个项目再重命名")
            return
        if archive.split_path(entries[0].path):
            self._set_status("压缩包内不支持重命名")
            return
        index = self.view.currentIndex()
        if not index.isValid():
            index = self.model.index(self.model.row_of_name(entries[0].name), 0)
        if not index.isValid():
            return
        # 原位编辑，不弹窗（和资源管理器一样）
        self.view.edit(index)

    def _apply_rename(self, old_path: str, new_name: str) -> None:
        if any(ch in new_name for ch in '\\/:*?"<>|'):
            self._set_status('名称不能包含 \\ / : * ? " < > |')
            self.reload()
            return
        target = os.path.join(os.path.dirname(old_path), new_name)
        if os.path.exists(target) and os.path.normcase(target) != os.path.normcase(old_path):
            self._set_status(f"「{new_name}」已存在")
            self.reload()
            return
        try:
            os.rename(old_path, target)
        except OSError as exc:
            self._set_status(f"重命名失败：{exc}")
            self.reload()
            return
        self.reload()
        row = self.model.row_of_name(new_name)
        if row >= 0:
            index = self.model.index(row, 0)
            self.view.setCurrentIndex(index)
            self.view.scrollTo(index)
        self._set_status(f"已重命名为 {new_name}")

    def _build_chips(self) -> None:
        while self.chip_layout.count():
            item = self.chip_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        for index, item in enumerate(self.quick_items):
            path = item.get("path", "")
            label = item.get("label") or os.path.basename(path.rstrip("\\/")) or path
            button = ChipButton(label, index, self)
            exists = os.path.exists(path)
            is_file = exists and os.path.isfile(path)
            if not exists:
                tip = f"{path}（不存在）"
            elif is_file:
                tip = f"{path}\n（文件，单击打开）"
            else:
                tip = f"{path}\n（文件夹，单击进入）"
            button.setToolTip(tip)
            button.setEnabled(exists)
            button.clicked.connect(lambda _=False, p=path: self.open_path(p))
            self.chip_layout.addWidget(button)
            button.adjustSize()

        # 行数不变时父布局不会重新 setGeometry，新按钮会堆在左上角，
        # 所以这里强制让流式布局按当前宽度重排一次
        self.chip_layout.invalidate()
        self.chip_host.updateGeometry()
        self._relayout_chips()
        self.chip_layout.activate()
        self.chip_host.update()
        QTimer.singleShot(0, self._relayout_chips)

    def _relayout_chips(self) -> None:
        """按当前宽度重算快捷栏高度，最多占四行。"""
        box = getattr(self, "chip_box", None)
        host = getattr(self, "chip_host", None)
        if box is None or host is None:
            return
        width = box.width() or (self.width() - 2)
        height = host.flow.heightForWidth(max(60, width))
        box.setFixedHeight(max(32, min(height, 128)))
        host.flow.activate()

    def receive_dropped_files(self, paths: list[str], target_dir: str,
                              move: bool = False) -> bool:
        """把拖进来的文件放进 target_dir。move=True 时连带删掉源文件。"""
        if self._in_archive():
            self._set_status("压缩包里不能直接拖入，请先解压到普通文件夹")
            return False
        if not os.path.isdir(target_dir):
            return False

        done, failed = 0, 0
        for src in paths:
            if not os.path.exists(src):
                failed += 1
                continue
            try:
                name = os.path.basename(src.rstrip("\\/")) or "未命名"
                dest = fileops.unique_path(os.path.join(target_dir, name))
                if move:
                    shutil.move(src, dest)
                else:
                    fileops.copy_path(src, dest)
                done += 1
            except (OSError, shutil.Error):
                failed += 1

        if done:
            self.reload()
            verb = "移入" if move else "接收"
            message = f"已{verb} {done} 个文件"
            if failed:
                message += f"，{failed} 个失败"
            self._set_status(message)
        elif failed:
            self._set_status(f"{failed} 个文件没能接收")
        return done > 0

    def open_path(self, path: str) -> None:
        """文件用默认程序打开，文件夹进入浏览。"""
        if os.path.isdir(path):
            self.navigate(path)
        elif os.path.isfile(path):
            try:
                os.startfile(path)
            except OSError as exc:
                self._set_status(f"打开失败：{exc}")
        else:
            self._set_status("这个位置已经不存在了")

    # ------------------------------------------------------------------ 快捷位置
    def _load_quick_items(self) -> list[dict]:
        data = self.settings.value("quick_items", None)
        if isinstance(data, list):
            items: list[dict] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "")
                if not path:
                    continue
                label = str(item.get("label") or "").strip()
                items.append({
                    "label": label or os.path.basename(path.rstrip("\\")) or path,
                    "path": path,
                })
            if items:
                return items
        return default_quick_items()

    def _save_quick_items(self) -> None:
        self.settings.setValue("quick_items", [dict(item) for item in self.quick_items])

    def chip_buttons(self) -> list[ChipButton]:
        buttons: list[ChipButton] = []
        for i in range(self.chip_layout.count()):
            widget = self.chip_layout.itemAt(i).widget()
            if isinstance(widget, ChipButton):
                buttons.append(widget)
        return buttons

    def show_chip_menu(self, index: int, global_pos: QPoint) -> None:
        if not (0 <= index < len(self.quick_items)):
            return
        item = self.quick_items[index]
        menu = QMenu(self)
        menu.addAction(f"打开 {item.get('label') or ''}",
                       lambda: self.open_path(item["path"]))
        menu.addSeparator()

        act_left = menu.addAction("左移", lambda: self.move_chip(index, index - 1))
        act_left.setEnabled(index > 0)
        act_right = menu.addAction("右移", lambda: self.move_chip(index, index + 1))
        act_right.setEnabled(index < len(self.quick_items) - 1)

        menu.addSeparator()
        menu.addAction("重命名…", lambda: self.rename_chip(index))
        menu.addAction("移除此快捷方式", lambda: self.remove_chip(index))
        menu.addSeparator()
        menu.addAction("恢复默认", self.reset_chips)
        menu.exec(global_pos)

    def add_to_chips(self, paths: list[str]) -> None:
        """把任意文件或文件夹加到快捷方式栏。"""
        added = 0
        for path in paths:
            if not path or not os.path.exists(path):
                continue
            normalized = os.path.normcase(os.path.abspath(path))
            if any(os.path.normcase(os.path.abspath(item.get("path", ""))) == normalized
                   for item in self.quick_items):
                continue
            label = os.path.basename(path.rstrip("\\/")) or path
            self.quick_items.append({"label": label, "path": path})
            added += 1
        if not added:
            self._set_status("这些已经在快捷方式里了")
            return
        self._save_quick_items()
        self._build_chips()
        self._set_status(f"已添加 {added} 个快捷方式")

    def move_chip(self, src: int, dst: int) -> None:
        items = self.quick_items
        if not (0 <= src < len(items)):
            return
        dst = max(0, min(len(items) - 1, dst))
        if src == dst:
            return
        items.insert(dst, items.pop(src))
        self._save_quick_items()
        self._build_chips()

    def drop_chip_at(self, src: int, pos: QPoint) -> None:
        buttons = self.chip_buttons()
        if not buttons:
            return
        dst = len(buttons)
        for btn in buttons:
            geo = btn.geometry()
            if pos.y() < geo.top():
                dst = btn.index
                break
            if geo.top() <= pos.y() <= geo.bottom() and pos.x() < geo.center().x():
                dst = btn.index
                break
        if dst > src:
            dst -= 1
        self.move_chip(src, dst)

    def rename_chip(self, index: int) -> None:
        if not (0 <= index < len(self.quick_items)):
            return
        item = self.quick_items[index]
        label, ok = QInputDialog.getText(self, "重命名快捷位置", "显示名称：",
                                         text=item.get("label", ""))
        if not ok:
            return
        label = label.strip()
        if not label:
            return
        item["label"] = label
        self._save_quick_items()
        self._build_chips()

    def remove_chip(self, index: int) -> None:
        if 0 <= index < len(self.quick_items):
            self.quick_items.pop(index)
            self._save_quick_items()
            self._build_chips()

    def add_current_to_chips(self) -> None:
        path = self.current
        if not path:
            return
        current = os.path.abspath(path).lower()
        if any(os.path.abspath(i.get("path", "")).lower() == current for i in self.quick_items):
            self._set_status("这个位置已经在快捷栏里了")
            return
        name = os.path.basename(path.rstrip("\\")) or path
        self.quick_items.append({"label": name, "path": path})
        self._save_quick_items()
        self._build_chips()
        self._set_status(f"已把「{name}」加入快捷栏")

    def reset_chips(self) -> None:
        self.quick_items = default_quick_items()
        self._save_quick_items()
        self._build_chips()
        self._set_status("快捷位置已恢复默认")

    # ------------------------------------------------------------------ 排序
    def _update_sort_button(self) -> None:
        label = SORT_LABELS.get(self.sort_key, "名称")
        self.btn_sort.setText(f"{label} {'↓' if self.sort_desc else '↑'}")

    def show_sort_menu(self) -> None:
        menu = QMenu(self)
        for key in ("name", "mtime", "size", "type"):
            act = menu.addAction(SORT_LABELS[key])
            act.setCheckable(True)
            act.setChecked(self.sort_key == key)
            act.triggered.connect(lambda _=False, k=key: self.set_sort_key(k))
        menu.addSeparator()
        act_asc = menu.addAction("升序 ↑")
        act_asc.setCheckable(True)
        act_asc.setChecked(not self.sort_desc)
        act_asc.triggered.connect(lambda: self.set_sort_desc(False))
        act_desc = menu.addAction("降序 ↓")
        act_desc.setCheckable(True)
        act_desc.setChecked(self.sort_desc)
        act_desc.triggered.connect(lambda: self.set_sort_desc(True))

        menu.adjustSize()
        anchor = self.btn_sort.mapToGlobal(QPoint(0, 0))
        x = anchor.x() - (menu.width() - self.btn_sort.width())
        y = anchor.y() - menu.height() - 2
        menu.exec(QPoint(max(0, x), max(0, y)))

    def set_sort_key(self, key: str) -> None:
        if key == self.sort_key or key not in SORT_LABELS:
            return
        self.sort_key = key
        self.settings.setValue("sort_key", key)
        self.settings.sync()
        self._update_sort_button()
        self.reload()

    def set_sort_desc(self, desc: bool) -> None:
        if bool(desc) == self.sort_desc:
            return
        self.sort_desc = bool(desc)
        self.settings.setValue("sort_desc", self.sort_desc)
        self.settings.sync()
        self._update_sort_button()
        self.reload()

    def _build_tray(self) -> None:
        self._init_closetotray()
        self.tray = QSystemTrayIcon(self._make_icon(), self)
        self.tray.setToolTip("桌面工作台（右键打开菜单）")
        # Windows 上不给 contextMenu 的话，右键事件根本不会派给 Qt
        self._tray_menu = QMenu(self)
        self._tray_menu.aboutToShow.connect(
            lambda: self._populate_main_menu(self._tray_menu))
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_visible()

    def _make_icon(self) -> QIcon:
        icon_path = resource_path("app.ico")
        if os.path.isfile(icon_path):
            icon = QIcon(icon_path)
            if not icon.isNull():
                return icon
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.color("accent")))
        p.drawRoundedRect(4, 4, 56, 56, 15, 15)
        p.setBrush(QColor(theme.color("bg")))
        p.drawRoundedRect(13, 17, 22, 11, 3, 3)
        p.drawRoundedRect(11, 22, 42, 26, 4, 4)
        p.setBrush(QColor(theme.color("sel_border")))
        p.drawRoundedRect(16, 28, 32, 3, 1.5, 1.5)
        p.drawRoundedRect(16, 34, 24, 3, 1.5, 1.5)
        p.end()
        return QIcon(pm)

    # ------------------------------------------------------------------ 导航
    def navigate(self, path: str, add_history: bool = True) -> None:
        raw = os.path.expandvars(os.path.expanduser(str(path).strip().strip('"')))
        target = self._resolve_target(raw)
        if target is None:
            self._set_status(f"无法打开：{raw}")
            return
        path = target

        if add_history:
            self.history = self.history[: self.hist_idx + 1]
            if not self.history or self.history[-1] != path:
                self.history.append(path)
            self.hist_idx = len(self.history) - 1

        self.current = path
        self._calc_mode = False
        self.settings.setValue("last_path", path)
        self.settings.sync()
        shown = COMPUTER_LABEL if path == COMPUTER_PATH else path
        self.path_edit.setText(shown)
        self.path_edit.setCursorPosition(len(shown))
        self.reload()

    def _resolve_target(self, path: str):
        """把路径规范成可浏览的目标：此电脑、普通目录、压缩包、或包内路径。"""
        if path in (COMPUTER_PATH, COMPUTER_LABEL, "此电脑"):
            return COMPUTER_PATH
        info = archive.split_path(path)
        if info:
            zip_path, inner = info
            return archive.join(zip_path, inner) if inner else zip_path
        try:
            full = os.path.abspath(path)
        except (OSError, ValueError):
            return None
        if os.path.isdir(full):
            return full
        if archive.is_archive(full):
            return full
        return None

    def _in_archive(self) -> bool:
        return bool(archive.split_path(self.current)) or archive.is_archive(self.current)

    def _load_current(self) -> list[Entry]:
        if self.current == COMPUTER_PATH:
            return computer_entries()
        info = archive.split_path(self.current)
        if info:
            return archive_entries(info[0], info[1], self.show_hidden,
                                   self.sort_key, self.sort_desc)
        if archive.is_archive(self.current):
            return archive_entries(self.current, "", self.show_hidden,
                                   self.sort_key, self.sort_desc)
        return load_entries(self.current, self.show_hidden, self.sort_key, self.sort_desc)

    def reload(self) -> None:
        if not self.current:
            return
        try:
            items = self._load_current()
        except PermissionError:
            self.model.set_items([])
            self._set_status("没有访问权限")
            self._update_nav_state()
            return
        except OSError as exc:
            self.model.set_items([])
            self._set_status(f"读取失败：{exc.strerror or exc}")
            self._update_nav_state()
            return

        self.model.set_items(items)
        self.view.scrollToTop()
        dirs = sum(1 for i in items if i.is_dir)
        if self.current == COMPUTER_PATH:
            self._set_status(f"{len(items)} 个驱动器")
        elif items:
            self._set_status(f"{len(items)} 个项目 · {dirs} 个文件夹")
        else:
            self._set_status("空文件夹")
        self._update_nav_state()

    def _set_status(self, text: str) -> None:
        self.status_label.setToolTip(text)
        width = max(60, self.status_label.width() - 4)
        fm = self.status_label.fontMetrics()
        self.status_label.setText(fm.elidedText(text, Qt.ElideRight, width))

    def _update_nav_state(self) -> None:
        parent = os.path.dirname(self.current.rstrip("\\"))
        can_up = (self.current != COMPUTER_PATH and bool(parent)
                  and parent != self.current)
        self.btn_up.setEnabled(can_up)
        self.btn_up.setToolTip("上一级 (Backspace)" if can_up else "已经是顶层了")

    def go_back(self) -> None:
        if self.hist_idx > 0:
            self.hist_idx -= 1
            self.navigate(self.history[self.hist_idx], add_history=False)

    def go_forward(self) -> None:
        if self.hist_idx < len(self.history) - 1:
            self.hist_idx += 1
            self.navigate(self.history[self.hist_idx], add_history=False)

    def go_up(self) -> None:
        if self.current == COMPUTER_PATH:
            return
        info = archive.split_path(self.current)
        if info:
            self.navigate(archive.parent_of(info[0], info[1]))
            return
        if archive.is_archive(self.current):
            self.navigate(os.path.dirname(self.current))
            return
        root = self.current.rstrip("\\/")
        if len(root) == 2 and root[1] == ":":
            self.navigate(COMPUTER_PATH)       # 盘符根再往上就是此电脑
            return
        parent = os.path.dirname(root)
        if parent and parent != self.current:
            self.navigate(parent)

    def open_archive_file(self, zip_path: str, inner: str) -> None:
        try:
            local = archive.extract_to_temp(zip_path, inner)
        except Exception as exc:
            self._set_status(f"解压失败：{exc}")
            return
        try:
            os.startfile(local)
        except OSError as exc:
            self._set_status(f"打开失败：{exc}")
            return
        if archive.is_writable(zip_path):
            self._watcher.watch(local, zip_path, inner)
            self._set_status(f"已打开「{os.path.basename(inner)}」，改动会自动存回压缩包")
        else:
            ext = os.path.splitext(zip_path)[1].upper()
            self._set_status(
                f"已打开「{os.path.basename(inner)}」（{ext} 格式不支持自动写回，改完请另存）")

    def extract_here(self, zip_path: str) -> None:
        dest = os.path.dirname(zip_path)
        try:
            count = archive.extract_all(zip_path, dest)
        except Exception as exc:
            self._set_status(f"解压失败：{exc}")
            return
        self._set_status(f"已解压 {count} 个文件到当前文件夹")
        self.reload()

    def extract_to_folder(self, zip_path: str) -> None:
        stem = os.path.splitext(os.path.basename(zip_path))[0]
        dest = os.path.join(os.path.dirname(zip_path), stem)
        base, n = dest, 2
        while os.path.isdir(dest):
            dest = f"{base}({n})"
            n += 1
        try:
            count = archive.extract_all(zip_path, dest)
        except Exception as exc:
            self._set_status(f"解压失败：{exc}")
            return
        self._set_status(f"已解压 {count} 个文件到「{os.path.basename(dest)}」")
        self.reload()

    def compress_selected(self, entries: list[Entry]) -> None:
        paths = [e.path for e in entries if not archive.split_path(e.path)]
        if not paths:
            self._set_status("压缩包内部不能再打包")
            return
        if os.path.isfile(self.current):
            self._set_status("请先回到普通文件夹再压缩")
            return
        if len(paths) == 1:
            base = os.path.splitext(os.path.basename(paths[0].rstrip("\\/")))[0]
        else:
            base = os.path.basename(self.current.rstrip("\\/")) or "归档"
        out = os.path.join(self.current, f"{base}.zip")
        index = 2
        while os.path.exists(out):
            out = os.path.join(self.current, f"{base}({index}).zip")
            index += 1

        # 超过 10MB 才弹进度条，小文件直接压完更省事
        if archive.total_size(paths) > 10 * 1024 * 1024:
            self._compress_with_progress(paths, out)
            return

        try:
            count = archive.compress(paths, out)
        except Exception as exc:
            self._set_status(f"压缩失败：{exc}")
            return
        self._set_status(f"已压缩 {count} 个文件为 {os.path.basename(out)}")
        self.reload()

    def _compress_with_progress(self, paths: list[str], out: str) -> None:
        dialog = QProgressDialog("正在压缩…", "取消", 0, 100, self)
        dialog.setWindowTitle("压缩")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumWidth(360)
        dialog.setValue(0)

        worker = ZipWorker(paths, out, self)

        def on_progress(percent: int, name: str) -> None:
            dialog.setValue(percent)
            dialog.setLabelText(f"正在压缩：{name}")

        def on_done(ok: bool, error: str, count: int) -> None:
            dialog.close()
            if ok:
                self._set_status(f"已压缩 {count} 个文件为 {os.path.basename(out)}")
                self.reload()
            elif error == "cancelled":
                self._set_status("已取消压缩")
            else:
                self._set_status(f"压缩失败：{error}")

        worker.progressed.connect(on_progress)
        worker.done.connect(on_done)
        dialog.canceled.connect(worker.cancel)
        worker.start()
        dialog.exec()
        worker.wait(3000)

    def _on_path_entered(self) -> None:
        text = self.path_edit.text().strip()
        # 先按路径处理：真实存在的路径优先，不会被当成算式
        if text and os.path.exists(text):
            self.navigate(text)
            self.view.setFocus()
            return

        # 含 + - * / = 这类符号就当算式，进计算界面
        if looks_like_expression(text):
            result, _error = eval_expression(text)
            if result is not None:
                self._show_expression(text, result)
            else:
                self._enter_calc_mode()
            self.path_edit.clear()          # 地址栏清空，方便接着算下一个
            self.view.setFocus()
            return

        self.navigate(text)          # 既不是路径也不是算式，交给 navigate 去提示
        self.view.setFocus()

    def _enter_calc_mode(self) -> None:
        """只把历史铺出来，不新增记录。"""
        self._calc_mode = True
        if self._expr_history:
            self._render_expr_history()
        else:
            self.model.set_items([])
        self._set_status("算式模式：输入算式按回车计算，按 ↑ 回文件列表")

    # ------------------------------------------------------------ 算式历史
    def _load_expr_history(self) -> list[dict]:
        # 配置层自己会做 JSON 序列化，这里直接拿对象就行
        data = self.settings.value("expr_history", [])
        if isinstance(data, str):
            # 兼容早期版本存成字符串的那批数据
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                return []
        if not isinstance(data, list):
            return []
        # 复制成新 dict：否则和配置里那份是同一个对象，
        # 改备注时 setValue 的「值没变就跳过」会误判，导致根本没写盘
        return [{**d} for d in data
                if isinstance(d, dict) and d.get("expr") and d.get("result")][:60]

    def _save_expr_history(self) -> None:
        # 传副本：配置层直接持有这个 list，共享对象会导致后面的修改被判成「没变化」
        self.settings.setValue("expr_history", [dict(r) for r in self._expr_history])

    def _show_expression(self, expr: str, result: str) -> None:
        """记下这条算式，然后把整个历史列表铺到浏览区。"""
        self._expr_history = [r for r in self._expr_history
                              if not (r["expr"] == expr and r["result"] == result)]
        self._expr_history.insert(0, {"expr": expr, "result": result})
        del self._expr_history[60:]
        self._calc_mode = True
        self._save_expr_history()
        self._render_expr_history()
        self._set_status(f"{expr} = {result}    （按 ↑ 或点地址栏回文件列表）")

    def _render_expr_history(self) -> None:
        records = list(self._expr_history)
        if not self._expr_desc:
            records.reverse()
        items = [Entry(path="", name=r["expr"], is_dir=False, size=0, mtime=0.0,
                       subtitle=f"=  {r['result']}", expr=r["expr"],
                       result=r["result"], note=r.get("note", ""))
                 for r in records]
        self.model.set_items(items)

    def _sort_expr(self, newest_first: bool) -> None:
        self._expr_desc = newest_first
        self.settings.setValue("expr_desc", newest_first)
        self._render_expr_history()
        self._set_status("已按" + ("倒序（新的在上）" if newest_first else "顺序（旧的在上）"))

    def _edit_expr_note(self, expr: str, result: str) -> None:
        record = next((r for r in self._expr_history
                       if r["expr"] == expr and r["result"] == result), None)
        if record is None:
            return
        text, ok = QInputDialog.getText(self, "备注", "给这条结果加个备注：",
                                        text=record.get("note", ""))
        if not ok:
            return
        note = text.strip()
        if note:
            record["note"] = note
        else:
            record.pop("note", None)
        self._save_expr_history()
        self._render_expr_history()
        self._set_status("备注已保存" if note else "备注已清除")

    def _delete_expr_record(self, expr: str, result: str) -> None:
        self._expr_history = [r for r in self._expr_history
                              if not (r["expr"] == expr and r["result"] == result)]
        self._save_expr_history()
        if self._expr_history:
            self._render_expr_history()
            self._set_status("已删除这条记录")
        else:
            self._set_status("已删除最后一条记录")
            self.navigate(self.current)

    def _clear_expr_history(self) -> None:
        self._expr_history.clear()
        self._save_expr_history()
        self.navigate(self.current)
        self._set_status("已清空算式记录")

    # ------------------------------------------------------------------ 交互
    def _on_click_open(self, index: QModelIndex) -> None:
        if self.click_to_open and index.isValid():
            self._on_activated(index)

    def _on_activated(self, index: QModelIndex) -> None:
        # activated 与 doubleClicked 可能同时到达，做一次去抖，避免文件被打开两次
        now = time.monotonic()
        key = (self.current, index.row())
        if key == self._last_activation[0] and now - self._last_activation[1] < 0.6:
            return
        self._last_activation = (key, now)

        entry = self.model.entry_at(index.row())
        if entry is None:
            return
        if entry.expr:                      # 算式历史：双击把结果填回地址栏
            self._fill_address(entry.result)
            return
        if not entry.path:
            return
        if entry.is_dir or archive.is_archive(entry.path):
            self.navigate(entry.path)
            return
        info = archive.split_path(entry.path)
        if info:
            self.open_archive_file(info[0], info[1])
            return
        self._open_file(entry.path)

    def _open_file(self, path: str) -> None:
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            subprocess.Popen(["explorer", path])

    def _selected_entries(self) -> list[Entry]:
        rows = sorted({i.row() for i in self.view.selectionModel().selectedIndexes()})
        result: list[Entry] = []
        for row in rows:
            entry = self.model.entry_at(row)
            if entry is not None:
                result.append(entry)
        return result

    def _open_selected(self) -> None:
        entries = self._selected_entries()
        if not entries:
            return
        entry = entries[0]
        if entry.is_dir or archive.is_archive(entry.path):
            self.navigate(entry.path)
            return
        info = archive.split_path(entry.path)
        if info:
            self.open_archive_file(info[0], info[1])
            return
        self._open_file(entry.path)

    @staticmethod
    def _is_drive_root(path: str) -> bool:
        text = path.rstrip("\\/")
        return len(text) == 2 and text[1] == ":"

    def _name_hit(self, pos: QPoint):
        """只有点在图标或文件名那一块才算命中，名称右边的空白不算——和资源管理器一致。"""
        index = self.view.indexAt(pos)
        if not index.isValid():
            return None
        entry = self.model.entry_at(index.row())
        if entry is None or (not entry.path and not entry.expr):
            return None
        rect = self.view.visualRect(index)
        font = QFont(self.view.font())
        font.setPointSizeF(theme.LIST_FONT_PT)
        fm = QFontMetrics(font)
        text_left = rect.left() + 46          # 和 ItemDelegate.paint 保持一致
        avail = max(rect.right() - text_left - 10, 0)
        shown = fm.elidedText(entry.name, Qt.ElideMiddle, avail)
        name_right = text_left + fm.horizontalAdvance(shown)
        if pos.x() > name_right + 10:
            return None
        return index, entry

    def _on_list_context_menu(self, pos: QPoint, dry_run: bool = False):
        hit = self._name_hit(pos)
        menu = QMenu(self)

        if hit is None:
            # 名称右边那片空白（或者压根没点到行）：当作右键背景，并取消选中
            self.view.clearSelection()
            self._build_empty_menu(menu)
        else:
            index, entry = hit
            selected = {i.row() for i in self.view.selectionModel().selectedIndexes()}
            if index.row() in selected:
                entries = self._selected_entries() or [entry]
            else:
                # 右键一个没选中的条目：先选中它，再弹它的专属菜单
                self.view.setCurrentIndex(index)
                entries = [entry]
            if entry.expr:                       # 算式历史
                self._build_history_menu(menu, entry)
            else:
                self._build_entry_menu(menu, entries)

        if dry_run:
            return menu
        menu.exec(self.view.viewport().mapToGlobal(pos))
        return None

    def _add_paste_action(self, menu: QMenu, target_dir: str) -> None:
        """剪贴板里有文件才显示粘贴，粘到 target_dir。"""
        if not fileops.get_files()[0]:
            return
        menu.addAction("粘贴            Ctrl+V",
                       lambda d=target_dir: self.paste_into_dir(d))

    def _fill_address(self, text: str) -> None:
        """在地址栏光标处插入内容，原有文字保留，插完不选中。"""
        self.path_edit.setFocus()
        pos = self.path_edit.cursorPosition()
        self.path_edit.insert(text)
        self.path_edit.setCursorPosition(pos + len(text))

    def _build_history_menu(self, menu: QMenu, entry: Entry) -> None:
        expr, result = entry.expr, entry.result
        menu.addAction("填回计算结果", lambda: self._fill_address(result))
        menu.addAction("填回表达式", lambda: self._fill_address(expr))
        menu.addSeparator()
        menu.addAction("备注…" if not entry.note else "修改备注…",
                       lambda: self._edit_expr_note(expr, result))
        menu.addSeparator()
        menu.addAction("复制结果", lambda: QApplication.clipboard().setText(result))
        menu.addAction("复制表达式", lambda: QApplication.clipboard().setText(expr))
        menu.addSeparator()
        menu.addAction("删除这条记录",
                       lambda: self._delete_expr_record(expr, result))
        menu.addAction("清空全部记录", self._clear_expr_history)

    def _build_entry_menu(self, menu: QMenu, entries: list[Entry]) -> None:
        single = entries[0] if len(entries) == 1 else None
        drive_root = single is not None and self._is_drive_root(single.path)

        menu.addAction("打开", self._open_selected)
        if single is not None:
            target_path, target_is_dir = single.path, single.is_dir
            menu.addAction(
                "在资源管理器中显示",
                lambda p=target_path, d=target_is_dir: subprocess.Popen(
                    f'explorer "{p}"' if d else f'explorer /select,"{p}"'))

        # 盘符根目录：只保留安全的几项，避免误删整个盘
        if drive_root:
            if fileops.get_files()[0]:
                menu.addSeparator()
                self._add_paste_action(menu, single.path)
            menu.addSeparator()
            menu.addAction("添加到快捷方式",
                           lambda: self.add_to_chips([e.path for e in entries]))
            menu.addSeparator()
            menu.addAction("复制名称", lambda: QApplication.clipboard().setText(single.name))
            menu.addAction("复制路径", lambda: QApplication.clipboard().setText(single.path))
            return

        menu.addSeparator()
        menu.addAction("剪切            Ctrl+X", lambda: self.copy_entries(True))
        menu.addAction("复制            Ctrl+C", lambda: self.copy_entries(False))
        # 只有对着文件夹右键才有「粘贴」，粘进那个文件夹
        if single is not None and single.is_dir:
            self._add_paste_action(menu, single.path)

        menu.addSeparator()
        if single is not None and archive.is_archive(single.path):
            menu.addAction("解压到当前文件夹", lambda: self.extract_here(single.path))
            menu.addAction("解压到同名文件夹", lambda: self.extract_to_folder(single.path))
        if not self._in_archive():
            menu.addAction("压缩为 ZIP", lambda: self.compress_selected(entries))
        menu.addAction("添加到快捷方式",
                       lambda: self.add_to_chips([e.path for e in entries]))
        menu.addSeparator()
        rename_act = menu.addAction("重命名         F2", self.rename_entry)
        rename_act.setEnabled(single is not None)
        menu.addAction("删除          Del", self.delete_entries)
        menu.addSeparator()
        if single is not None:
            menu.addAction("复制名称", lambda: QApplication.clipboard().setText(single.name))
            menu.addAction("复制路径", lambda: QApplication.clipboard().setText(single.path))
        else:
            menu.addAction(f"复制 {len(entries)} 个路径",
                           lambda: QApplication.clipboard().setText(
                               "\n".join(e.path for e in entries)))

    def _build_empty_menu(self, menu: QMenu) -> None:
        if self._calc_mode:                 # 算式界面：只管排序和清空
            menu.addAction("顺序显示", lambda: self._sort_expr(False))
            menu.addAction("倒序显示", lambda: self._sort_expr(True))
            menu.addSeparator()
            menu.addAction("清空地址栏",
                           lambda: (self.path_edit.clear(),
                                    self.path_edit.setFocus()))
            return
        # 粘贴（有剪贴板内容才显示）放最前，复制当前路径放最后
        if fileops.get_files()[0]:
            menu.addAction("粘贴            Ctrl+V", self.paste_entries)
            menu.addSeparator()
        self._build_new_menu(menu)
        menu.addSeparator()
        menu.addAction("刷新            F5", self.reload)
        menu.addSeparator()
        menu.addAction("复制当前路径",
                       lambda: QApplication.clipboard().setText(self.current))

    def paste_into_dir(self, target_dir: str) -> None:
        paths, _cut = fileops.get_files()
        if not paths:
            self._set_status("剪贴板里没有可粘贴的文件")
            return
        done, errors, was_cut = fileops.paste_into(target_dir)
        self.reload()
        if errors:
            self._set_status(f"粘贴 {len(done)} 项，{len(errors)} 项失败")
        elif done:
            self._set_status(
                f"已{'移动' if was_cut else '复制'} {len(done)} 个项目"
                f" → {os.path.basename(target_dir.rstrip(chr(92))) or target_dir}")

    def _build_new_menu(self, menu: QMenu) -> None:
        sub = menu.addMenu("新建")
        sub.addAction("文件夹", self.create_folder)
        sub.addSeparator()
        for tpl in shellnew.list_templates():
            sub.addAction(tpl["label"],
                          lambda _=False, t=tpl: self.create_from_template(t))

    def create_folder(self) -> None:
        try:
            path = fileops.new_folder(self.current)
        except OSError as exc:
            self._set_status(f"创建失败：{exc}")
            return
        self._refresh_and_select(os.path.basename(path), "已新建文件夹")

    def create_from_template(self, tpl: dict) -> None:
        try:
            path = fileops.new_from_template(tpl, self.current)
        except (OSError, ValueError) as exc:
            self._set_status(f"新建失败：{exc}")
            return
        name = os.path.basename(path)
        self._refresh_and_select(name, f"已新建 {name}")

    def _refresh_and_select(self, name: str, message: str = "") -> None:
        self.reload()
        if message:
            self._set_status(message)
        row = self.model.row_of_name(name)
        if row >= 0:
            index = self.model.index(row, 0)
            self.view.setCurrentIndex(index)
            self.view.scrollTo(index)

    def show_main_menu(self) -> None:
        """手动弹出完整菜单（托盘右键走 contextMenu，见 _build_tray）。"""
        menu = QMenu(self)
        self._populate_main_menu(menu)
        menu.exec(QCursor.pos())

    def show_main_menu_at(self, global_pos) -> None:
        """在指定位置弹出和托盘右键一样的菜单（标题栏用）。"""
        menu = QMenu(self)
        self._populate_main_menu(menu)
        menu.exec(global_pos)

    def _populate_main_menu(self, menu: QMenu) -> None:
        menu.clear()

        # ── 别的程序：关闭按钮→托盘（放最顶上，第一眼就能看到）
        self._build_closetotray_menu(menu)
        self._build_hidden_menu(menu)
        menu.addSeparator()

        # ── 前往
        places = menu.addMenu("前往")
        home = os.path.expanduser("~")
        for label, sub in (("主目录", ""), ("桌面", "Desktop"), ("下载", "Downloads"),
                           ("文档", "Documents"), ("图片", "Pictures"), ("视频", "Videos")):
            path = os.path.join(home, sub) if sub else home
            if os.path.isdir(path):
                places.addAction(label, lambda _=False, p=path: self.navigate(p))
        try:
            drives = list(os.listdrives())
        except Exception:
            drives = []
        if drives:
            places.addSeparator()
            for drive in drives:
                places.addAction(drive, lambda _=False, p=drive: self.navigate(p))
        places.addSeparator()
        places.addAction("上一级", self.go_up)
        places.addAction("刷新", self.reload)
        places.addAction("在系统资源管理器中打开",
                         lambda: subprocess.Popen(f'explorer "{self.current}"'))

        menu.addSeparator()

        edge_menu = menu.addMenu("吸附位置")
        for key, label in (("left", "屏幕左侧"), ("right", "屏幕右侧")):
            act = edge_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.edge == key)
            act.triggered.connect(lambda _=False, k=key: self.set_edge(k))

        act_top = menu.addAction("总在最前")
        act_top.setCheckable(True)
        act_top.setChecked(self.topmost)
        act_top.triggered.connect(self.set_topmost)

        width_menu = menu.addMenu("窗口宽度")
        for label, w in (("窄", 280), ("中", 340), ("宽", 440)):
            act = width_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(abs(self.width_logical - w) < 8)
            act.triggered.connect(lambda _=False, width=w: self._apply_width(width))

        menu.addSeparator()

        # ── 显示
        act_hidden = menu.addAction("显示隐藏文件")
        act_hidden.setCheckable(True)
        act_hidden.setChecked(self.show_hidden)
        act_hidden.triggered.connect(self.set_show_hidden)

        act_click = menu.addAction("单击打开文件")
        act_click.setCheckable(True)
        act_click.setChecked(self.click_to_open)
        act_click.triggered.connect(self.set_click_to_open)

        theme_menu = menu.addMenu("换肤")
        for key in theme.ORDER:
            act = theme_menu.addAction(theme.PALETTES[key]["label"])
            act.setCheckable(True)
            act.setChecked(self.theme_key == key)
            act.triggered.connect(lambda _=False, k=key: self.apply_theme(k))

        menu.addSeparator()

        # ── 系统
        act_boot = menu.addAction("开机自动启动")
        act_boot.setCheckable(True)
        act_boot.setChecked(self._is_autostart())
        act_boot.triggered.connect(self._toggle_autostart)

        menu.addAction("显示 / 隐藏面板", self.toggle_visible)
        menu.addSeparator()
        menu.addAction("退出", self.quit_app)

    # ------------------------------------------------------------------ 设置项
    def set_show_hidden(self, enabled: bool) -> None:
        self.show_hidden = bool(enabled)
        self.settings.setValue("show_hidden", self.show_hidden)
        self.settings.sync()
        self.reload()

    def set_click_to_open(self, enabled: bool) -> None:
        self.click_to_open = bool(enabled)
        self.settings.setValue("click_to_open", self.click_to_open)
        self.settings.sync()
        self.view.setCursor(Qt.PointingHandCursor if self.click_to_open else Qt.ArrowCursor)
        self.view.viewport().setCursor(
            Qt.PointingHandCursor if self.click_to_open else Qt.ArrowCursor)
        self._set_status("单击打开已开启" if self.click_to_open else "单击打开已关闭")

    def apply_theme(self, key: str) -> None:
        theme.set_theme(key)
        self.theme_key = key
        self.settings.setValue("theme", key)
        self.setStyleSheet(theme.build_qss())
        self._refresh_icons()
        self.view.viewport().update()
        self.notes_panel.refresh_theme()
        self.update()
        self._set_status(f"已切换到「{theme.PALETTES[key]['label']}」主题")

    def _refresh_icons(self) -> None:
        for btn in self.findChildren(QToolButton):
            kind = getattr(btn, "_nav_kind", None)
            if kind:
                btn.setIcon(nav_icon(kind))

    # ------------------------------------------------------------------ 吸附边
    def set_edge(self, edge: str) -> None:
        edge = "right" if edge == "right" else "left"
        if edge == self.edge:
            return
        self.edge = edge
        self.settings.setValue("edge", edge)
        if self.appbar is not None:
            self.appbar.set_edge(edge, primary_screen_size())
            self._sync_appbar()
        self._reposition_handle()
        self.update()
        self._set_status(f"已吸附到屏幕{'右' if edge == 'right' else '左'}侧")

    # ------------------------------------------------------- 别的程序 关闭→托盘
    def _init_closetotray(self) -> None:
        from closetotray import CloseToTray
        self._window_trays: dict[int, QSystemTrayIcon] = {}
        self.closetotray = CloseToTray(
            on_hide=lambda hwnd: QTimer.singleShot(
                0, lambda h=hwnd: self._hide_target_window(h)))
        saved = self.settings.value("closetotray_targets", [])
        if isinstance(saved, str):
            saved = [x for x in saved.replace(";", ",").split(",") if x.strip()]
        for exe in saved or []:
            self.closetotray.add_target(str(exe))
        hook_ok = self.closetotray.install() if self.closetotray.targets else False
        self._geo_log("CTT_INIT", targets=self.closetotray.targets, hook_ok=hook_ok,
                      active=self.closetotray.active)
        if self.closetotray.targets and not hook_ok:
            self._set_status("关闭拦截没装上（可能被安全软件拦了）")

    def _save_closetotray(self) -> None:
        self.settings.setValue("closetotray_targets", list(self.closetotray.targets))
        if self.closetotray.targets:
            self.closetotray.install()      # 重装一次，钩子可能被系统摘掉了
        else:
            self.closetotray.uninstall()

    def _build_closetotray_menu(self, menu: QMenu) -> None:
        ctt = self.closetotray
        sub = menu.addMenu("后台运行被关窗口")
        sub.addAction("选择窗口…", self._pick_from_window_list)
        sub.addSeparator()
        if not ctt.targets:
            act = sub.addAction("（还没添加任何程序）")
            act.setEnabled(False)
        else:
            toggle = sub.addAction("暂停拦截")
            toggle.setCheckable(True)
            toggle.setChecked(ctt.paused)
            toggle.triggered.connect(self._toggle_closetotray)
            sub.addSeparator()
            for exe in list(ctt.targets):
                sub.addAction(f"移除  {exe}",
                              lambda _=False, e=exe: self._remove_closetotray_target(e))

    def _build_hidden_menu(self, menu: QMenu) -> None:
        ctt = self.closetotray
        ctt.prune()
        if not ctt.hidden:
            return
        sub = menu.addMenu(f"已缩到托盘的窗口（{len(ctt.hidden)}）")
        for hwnd, title in list(ctt.hidden.items()):
            label = title if len(title) <= 26 else title[:25] + "…"
            sub.addAction(label, lambda _=False, h=hwnd: self._restore_target_window(h))
        sub.addSeparator()
        sub.addAction("全部恢复", self._restore_all_targets)

    def _pick_from_window_list(self) -> None:
        """列出当前打开的窗口，勾选要保护的程序。"""
        from closetotray import list_windows
        windows = list_windows(exclude_pid=os.getpid())
        if not windows:
            self._set_status("没找到可以选择的窗口")
            return

        dialog = WindowPickerDialog(windows, list(self.closetotray.targets), self)
        if dialog.exec() != QDialog.Accepted:
            return

        picked = dialog.selected_exes()
        added = [exe for exe in picked if self.closetotray.add_target(exe)]
        # 只处理「本次列表里出现过、但被取消勾选」的。
        # 程序没在运行的已有目标不会出现在列表里，不能因此丢掉。
        removed = [exe for exe in self.closetotray.targets
                   if exe in dialog.shown_exes and exe not in picked]
        for exe in removed:
            self.closetotray.remove_target(exe)

        if not added and not removed:
            self._set_status("没有变化")
            return

        self._save_closetotray()
        parts = []
        if added:
            parts.append("添加 " + "、".join(added))
        if removed:
            parts.append("移除 " + "、".join(removed))
        self._set_status("关闭即后台运行：" + "；".join(parts))

    def _remove_closetotray_target(self, exe: str) -> None:
        if self.closetotray.remove_target(exe):
            self._save_closetotray()
            self._set_status(f"已移除 {exe}")

    def _toggle_closetotray(self, checked: bool) -> None:
        self.closetotray.paused = bool(checked)
        self._set_status("关闭拦截已暂停" if checked else "关闭拦截已启用")

    def _hide_target_window(self, hwnd: int) -> None:
        if not self.closetotray.hide_window(hwnd):
            return
        title = self.closetotray.hidden.get(hwnd, "窗口")
        self._add_window_tray(hwnd, title)
        short = title if len(title) <= 20 else title[:19] + "…"
        self._set_status(f"「{short}」已缩到托盘，单击它的托盘图标可以放回来")

    def _add_window_tray(self, hwnd: int, title: str) -> None:
        """给被隐藏的窗口在系统托盘挂一个**它自己**的图标。"""
        existing = self._window_trays.get(hwnd)
        if existing is not None:                 # 已经有图标了，只更新提示
            existing.setToolTip(f"{title}\n单击显示 / 隐藏，右键更多")
            return
        from closetotray import exe_path_of_window

        icon = QIcon()
        exe = exe_path_of_window(hwnd)
        if exe and os.path.isfile(exe):
            icon = self.icon_provider.icon(QFileInfo(exe))
        if icon.isNull():
            icon = self._make_icon()

        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip(f"{title}\n单击显示 / 隐藏，右键更多")
        menu = QMenu(self)
        menu.addAction("显示 / 隐藏", lambda _=False, h=hwnd: self._toggle_target_window(h))
        menu.addAction("关闭窗口", lambda _=False, h=hwnd: self._close_target_window(h))
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason, h=hwnd: (self._toggle_target_window(h)
                                    if reason == QSystemTrayIcon.Trigger else None))
        tray.show()
        self._window_trays[hwnd] = tray

    def _drop_window_tray(self, hwnd: int) -> None:
        tray = self._window_trays.pop(hwnd, None)
        if tray is None:
            return
        tray.hide()
        # 不能在「托盘图标自己发出的信号」里直接删它（Qt 会崩），
        # 也别 setParent(None)，交给事件循环下一拍再销毁
        QTimer.singleShot(0, tray.deleteLater)

    def _toggle_target_window(self, hwnd: int) -> None:
        """点托盘图标：窗口可见就收回去，藏着就放出来。"""
        self._geo_log("TOGGLE_ENTER", hwnd=hwnd)
        if not ctypes.windll.user32.IsWindow(hwnd):
            self._drop_window_tray(hwnd)
            self._set_status("这个窗口已经关掉了，图标已移除")
            return
        visible = bool(ctypes.windll.user32.IsWindowVisible(hwnd))
        self._geo_log("TOGGLE_STATE", hwnd=hwnd, visible=visible)
        if visible:
            self._hide_target_window(hwnd)
        else:
            self._restore_target_window(hwnd)
        self._geo_log("TOGGLE_DONE", hwnd=hwnd)

    def _restore_target_window(self, hwnd: int) -> None:
        ok = self.closetotray.restore_window(hwnd)
        # 图标留着不动：下次再点它就能把窗口收回去
        self._set_status("窗口已恢复（托盘图标保留，单击可以再收回去）" if ok
                         else "这个窗口已经不在了")

    def _close_target_window(self, hwnd: int) -> None:
        """真把这个窗口关掉（走 WM_CLOSE，鼠标钩子不拦）。"""
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
        self.closetotray.hidden.pop(hwnd, None)
        self._drop_window_tray(hwnd)
        self._set_status("已关闭该窗口")

    def _restore_all_targets(self) -> None:
        count = len(self.closetotray.hidden)
        self.closetotray.restore_all()
        self._set_status(f"已恢复 {count} 个窗口（托盘图标保留）")

    def set_topmost(self, enabled: bool) -> None:
        self.topmost = bool(enabled)
        self.settings.setValue("topmost", self.topmost)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWindowPos.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint)
        hwnd = ctypes.c_void_p(int(self.winId()))
        target = ctypes.c_void_p(-1 if self.topmost else -2)
        user32.SetWindowPos(hwnd, target, 0, 0, 0, 0,
                            0x0001 | 0x0002 | 0x0010)  # NOSIZE | NOMOVE | NOACTIVATE

    def _is_autostart(self) -> bool:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, "SideExplorer")
                return True
        except OSError:
            return False

    def _toggle_autostart(self, enabled: bool) -> None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as key:
                if enabled:
                    if getattr(sys, "frozen", False):      # 打包后的 exe
                        cmd = f'"{sys.executable}"'
                    else:
                        script = os.path.abspath(os.path.join(app_dir(), "main.py"))
                        exe = sys.executable
                        if exe.lower().endswith("python.exe"):
                            exe = exe[:-10] + "pythonw.exe"
                        cmd = f'"{exe}" "{script}"'
                    winreg.SetValueEx(key, "SideExplorer", 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(key, "SideExplorer")
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            self._set_status(f"设置失败：{exc}")

    # ------------------------------------------------------------------ AppBar
    def _dpr(self) -> float:
        screen = QApplication.primaryScreen()
        return screen.devicePixelRatio() if screen else 1.0

    def _geo_log(self, tag: str, **kw) -> None:
        try:
            line = tag + " " + " ".join(f"{k}={v}" for k, v in kw.items()) + "\n"
            with open(os.path.join(os.environ.get("TEMP", "."), "geo_debug.log"),
                      "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass

    def _sync_appbar(self) -> None:
        if self._syncing or self.appbar is None:
            return
        self._syncing = True
        try:
            dpr = self._dpr()
            screen_size = primary_screen_size()
            self.appbar.width = max(1, round(self.width_logical * dpr))
            rect = self.appbar.place(screen_size)
            self._geo_log("SYNC", logical=self.width_logical, dpr=dpr,
                          appbar_w=self.appbar.width, screen=screen_size, rect=rect)
            if rect:
                l, t, r, b = rect
                self.setGeometry(round(l / dpr), round(t / dpr),
                                 round((r - l) / dpr), round((b - t) / dpr))
        finally:
            self._syncing = False

    def _init_appbar(self) -> None:
        if self.appbar is not None:
            return
        self.appbar = AppBar(int(self.winId()), round(self.width_logical * self._dpr()), self.edge)
        self.appbar.register()
        self._sync_appbar()

    def start(self) -> None:
        self.winId()          # 强制创建原生窗口，先定位再显示，避免闪一下
        self._init_appbar()
        self.show()
        QTimer.singleShot(60, self._sync_appbar)
        self._watch_screen()

        # 兜底：窗口位置被外部改掉（系统重排、缩放临时变化）时自动纠回来
        self._guard_timer = QTimer(self)
        self._guard_timer.setInterval(4000)
        self._guard_timer.timeout.connect(self._verify_placement)
        self._guard_timer.start()

    def _watch_screen(self) -> None:
        """监听屏幕尺寸 / DPI / 显示器增减，变了就重新钉一次应用栏。"""
        app = QApplication.instance()
        if app is not None and not getattr(self, "_screen_hooked", False):
            app.primaryScreenChanged.connect(self._on_screen_changed)
            app.screenAdded.connect(self._on_screen_changed)
            app.screenRemoved.connect(self._on_screen_changed)
            self._screen_hooked = True

        screen = QApplication.primaryScreen()
        if screen is None or screen is getattr(self, "_screen_obj", None):
            return                              # 同一块屏已经接过了，别重复接
        # 换了屏就重接新的；旧屏对象可能已被销毁，不去 disconnect，
        # 否则 PySide 会刷 "Failed to disconnect" 的 RuntimeWarning
        self._screen_obj = screen
        for sig in (screen.geometryChanged, screen.logicalDotsPerInchChanged):
            sig.connect(self._on_screen_changed)

    def _on_screen_changed(self, *args) -> None:
        # 屏幕一变，原来占的那块地就失效了，重新注册最保险
        QTimer.singleShot(150, self._remount_appbar)

    def _remount_appbar(self) -> None:
        if self.appbar is None:
            return
        self._watch_screen()
        self.appbar.remove()
        self.appbar.register()
        self._sync_appbar()

    def _on_pos_changed(self) -> None:
        """系统通知可用区域变了。

        这里只按新位置摆窗口，**不**再调 ABM_SETPOS —— 因为 SETPOS 会向所有
        应用栏广播 ABN_POSCHANGED（包括自己），一路循环下去窗口就会自己漂移。
        """
        if self.appbar is None or self._syncing:
            return
        now = time.monotonic()
        if now - getattr(self, "_last_pos_change", 0.0) < 0.8:
            return                          # 防抖：同一轮只处理一次
        self._last_pos_change = now

        rect = self.appbar.query(primary_screen_size())
        self._geo_log("POSCHANGED", rect=rect, appbar_w=self.appbar.width,
                      logical=self.width_logical, dpr=self._dpr())
        if not rect:
            return
        dpr = self._dpr()
        l, t, r, b = rect
        # 宽度一律用 appbar 的登记值：系统的 rect 宽度可能已被改坏
        width = max(1, round(self.appbar.width / dpr))
        height = max(1, round((b - t) / dpr))
        self.setGeometry(round(r / dpr) - width, round(t / dpr), width, height)

    def _verify_placement(self) -> None:
        """定时校验：窗口实际位置和应用栏登记的位置不一致就纠正。"""
        if self.appbar is None or not self.appbar.rect or self._syncing:
            return
        if not self.isVisible():
            return
        dpr = self._dpr()
        l, t, r, b = self.appbar.rect
        want = QRect(round(l / dpr), round(t / dpr),
                     round((r - l) / dpr), round((b - t) / dpr))
        if self.geometry() != want:
            self._geo_log("VERIFY_FIX", now=self.geometry().getRect(),
                          want=want.getRect(), appbar_rect=self.appbar.rect,
                          dpr=dpr, min_w=self.minimumSizeHint().width(),
                          minwidth=self.minimumWidth(),
                          logical=self.width_logical)
            self._sync_appbar()

        # 顺手清理：窗口已经被关掉的，它的托盘图标也摘掉
        for hwnd in list(getattr(self, "_window_trays", {})):
            if not ctypes.windll.user32.IsWindow(hwnd):
                self._drop_window_tray(hwnd)

    def _apply_width(self, logical_width: int) -> None:
        width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, int(logical_width)))
        if width == self.width_logical:
            return
        self.width_logical = width
        self.settings.setValue("width", width)
        self._sync_appbar()

    def hide_to_tray(self) -> None:
        if self.appbar is not None:
            self.appbar.remove()
        self.hide()

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide_to_tray()
        else:
            self.restore_panel()

    def restore_panel(self) -> None:
        if self.appbar is None:
            self._init_appbar()
        else:
            self.appbar.register()
            self._sync_appbar()
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self._really_quit = True
        for tray in list(getattr(self, "_window_trays", {}).values()):
            tray.hide()
        if self.appbar is not None:
            self.appbar.remove()
        self.settings.setValue("width", self.width_logical)
        self.tray.hide()
        QApplication.quit()

    # ------------------------------------------------------------------ 事件
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reposition_handle()
        self._relayout_chips()

    def _pin_in_screen(self) -> None:
        """窗口一旦越界就按贴边规则摆回屏幕里（只动窗口，不动 AppBar 占位）。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.geometry()
        geo = self.geometry()
        width = max(1, self.width_logical)
        if self.edge == "right":
            left = area.right() + 1 - width
        else:
            left = area.left()
        if geo.left() != left or geo.width() != width:
            self.setGeometry(left, area.top(), width, area.height())

    def _reposition_handle(self) -> None:
        """缩放条永远贴着屏幕内侧那条边（右侧吸附时在左边）。"""
        x = 0 if self.edge == "right" else self.width() - 7
        self.handle.setGeometry(x, 0, 7, self.height())
        self.handle.raise_()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setPen(QColor(theme.color("divider")))
        x = 0 if self.edge == "right" else self.width() - 1
        p.drawLine(x, 0, x, self.height())
        p.end()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.handle:
            et = event.type()
            if et == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_x = event.globalPosition().x()
                self._drag_w = self.width_logical
                return True
            if et == QEvent.MouseMove and self._drag_x is not None:
                if event.buttons() & Qt.LeftButton:
                    delta = event.globalPosition().x() - self._drag_x
                    sign = -1 if self.edge == "right" else 1
                    self._apply_width(self._drag_w + sign * delta / self._dpr())
                    self._pin_in_screen()
                return True
            if et == QEvent.MouseButtonRelease:
                self._drag_x = None
                # 松手后按登记值把位置钉回去：拖动过程中 Qt 自己 resize 时
                # 右边缘是固定的，会把窗口推到屏幕外面去
                self._sync_appbar()
                return True
        return super().eventFilter(obj, event)

    def nativeEvent(self, eventType, message):  # noqa: N802
        try:
            from appbar import ABN_POSCHANGED, MSG
            msg = ctypes.cast(int(message), ctypes.POINTER(MSG)).contents
            if msg.message == 0x0021:      # WM_MOUSEACTIVATE
                # 未激活时点击面板：激活窗口但不吞掉这次点击，
                # 否则第一次双击会被系统吃掉，用户要连点三下才能进目录
                return True, 1             # MA_ACTIVATE
            if self.appbar is not None and self.appbar.owns_message(msg.message):
                if msg.wParam == ABN_POSCHANGED:
                    QTimer.singleShot(0, self._on_pos_changed)
                return True, 0
        except Exception:
            pass
        return super().nativeEvent(eventType, message)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        if key == Qt.Key_F5:
            self.reload()
            return
        if key == Qt.Key_Backspace and self.view.hasFocus():
            self.go_up()
            return
        if key == Qt.Key_L and mods & Qt.ControlModifier:
            self.path_edit.setFocus()
            self.path_edit.selectAll()
            return
        if key == Qt.Key_Left and mods & Qt.AltModifier:
            self.go_back()
            return
        if key == Qt.Key_Right and mods & Qt.AltModifier:
            self.go_forward()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        """点 × 不退出程序，只缩到托盘（和微信一样）。

        真正退出走托盘右键菜单里的「退出」。
        """
        if self._really_quit or self.tray is None or not self.tray.isVisible():
            if self.appbar is not None:
                self.appbar.remove()
            event.accept()
            return

        event.ignore()
        self.hide_to_tray()
        if not self.settings.value("tray_tip_shown", False, bool):
            self.settings.setValue("tray_tip_shown", True)
            self.tray.showMessage(
                "桌面工作台",
                "已缩到托盘，程序还在后台运行\n点托盘图标可以调回来，右键菜单里有「退出」",
                QSystemTrayIcon.Information, 3000)
