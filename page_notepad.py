"""记事本页：一个长文本框，边打边存。

平时不用管保存 —— 停手 0.8 秒就写进配置，切页、退出都再补存一次。
另外可以从 .txt 导入、导出成 .txt（纯文本，编码自动认 BOM / UTF-8 / GBK）。
"""

from __future__ import annotations

import datetime
import os

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (QApplication, QFileDialog, QLabel, QMenu,
                               QMessageBox, QPlainTextEdit, QSizePolicy,
                               QVBoxLayout, QWidget)

import theme

SAVE_DELAY = 800


class PadEdit(QPlainTextEdit):
    """Tab 用制表符而不是跳焦点，写代码片段时更顺手。"""

    def event(self, event):
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Tab:
            self.insertPlainText("\t")
            return True
        return super().event(event)


class NotepadPage(QWidget):
    kind = "pad"
    title = "记事本"
    primary_tip = ""
    can_primary = False
    message = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setObjectName("PadPage")
        self.config = config
        self._build()

        # 先有定时器再灌内容：setPlainText 会触发 textChanged
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY)
        self._timer.timeout.connect(self.save)

        self._load()

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(0)

        self.edit = PadEdit()
        self.edit.setObjectName("PadEdit")
        self.edit.setPlaceholderText("随手记点什么，会自动保存")
        self.edit.setFrameShape(QPlainTextEdit.NoFrame)
        self.edit.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(9.6)
        self.edit.setFont(font)
        self.edit.textChanged.connect(self._on_changed)
        root.addWidget(self.edit, 1)

        self.meta = QLabel("")
        self.meta.setObjectName("PadMeta")
        self.meta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # 和状态栏一个道理：别让这段小字把窗口最小宽度顶大
        self.meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.meta.setMinimumWidth(0)
        self.meta.setFixedHeight(18)
        root.addWidget(self.meta)

    # ------------------------------------------------------------------ 存取
    def _load(self) -> None:
        text = self.config.value("notepad", "")
        if isinstance(text, str) and text:
            self.edit.setPlainText(text)
        self._update_meta()

    def save(self) -> None:
        self._timer.stop()
        text = self.edit.toPlainText()
        if self.config.value("notepad", "") != text:
            self.config.setValue("notepad", text)

    def _on_changed(self) -> None:
        self._timer.start()
        self._update_meta()

    def _update_meta(self) -> None:
        text = self.edit.toPlainText()
        chars = len(text.replace("\n", "").replace("\r", ""))
        lines = self.edit.document().blockCount()
        self.meta.setText(f"{chars} 字 · {lines} 行" if chars else "")

    # ------------------------------------------------------------------ 页接口
    def activate(self) -> None:
        self.edit.setFocus()

    def deactivate(self) -> None:
        self.save()

    def primary(self) -> None:
        self.edit.setFocus()

    def menu(self, anchor=None) -> None:
        menu = self._build_menu()
        if anchor is not None:
            menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        else:
            menu.exec(self.edit.mapToGlobal(self.edit.rect().bottomLeft()))

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("导入 txt…", self._import_txt)
        menu.addAction("导出为 txt…", self._export_txt)
        menu.addSeparator()
        menu.addAction("复制全部", self._copy_all)
        menu.addAction("全选", self.edit.selectAll)
        menu.addSeparator()
        menu.addAction("清空（可 Ctrl+Z 撤回）", self._clear)
        return menu

    # ------------------------------------------------------------------ 导入/导出
    @staticmethod
    def _read_text(path: str) -> str | None:
        """按 BOM 判断编码，没 BOM 就先试 UTF-8 再退 GBK（国内 txt 多半这两种）。"""
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            QMessageBox.warning(None, "导入失败", str(exc))
            return None
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16", "replace")
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw.decode("utf-8-sig", "replace")
        for encoding in ("utf-8", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    def _import_txt(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入文本", os.path.expanduser("~"),
            "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        text = self._read_text(path)
        if text is None:
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")   # 统一成 LF，编辑框里不会有怪字符
        if self.edit.toPlainText().strip():
            answer = QMessageBox.question(
                self, "导入文本",
                f"用「{os.path.basename(path)}」替换当前内容？\n（替换后还能 Ctrl+Z 撤回）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer != QMessageBox.Yes:
                return
        # 走光标插入而不是 setPlainText：这样整段替换也能撤销
        cursor = self.edit.textCursor()
        cursor.select(QTextCursor.Document)
        cursor.insertText(text)
        self.edit.setTextCursor(cursor)
        self.save()
        self.message.emit(f"已导入 {os.path.basename(path)}（{len(text)} 字符）")

    def _export_txt(self) -> None:
        text = self.edit.toPlainText()
        if not text.strip():
            self.message.emit("记事本还是空的，没有可导出的内容")
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        default = os.path.join(os.path.expanduser("~"), f"记事本-{stamp}.txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出为文本", default, "文本文件 (*.txt)")
        if not path:
            return
        if not path.lower().endswith(".txt"):
            path += ".txt"
        error = self._write_text(path, text)
        if error:
            QMessageBox.warning(self, "导出失败", error)
            return
        self.message.emit(f"已导出到 {path}")

    @staticmethod
    def _write_text(path: str, text: str) -> str:
        """UTF-8 + CRLF 落盘；出错返回原因，成功返回空串。"""
        try:
            with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
                fh.write(text)
        except OSError as exc:
            return str(exc)
        return ""

    def _copy_all(self) -> None:
        text = self.edit.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    def _clear(self) -> None:
        self.edit.selectAll()
        self.edit.cut()                 # cut 之后还能撤销
        self.edit.setFocus()

    def refresh_theme(self) -> None:
        self.meta.setStyleSheet(f"color: {theme.color('muted')}; font-size: 10px;")
        self.edit.viewport().update()

    def handle_key(self, action: str) -> bool:
        if action == "all":
            self.edit.selectAll()
            return True
        return False
