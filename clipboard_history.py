"""剪贴板历史：把复制过的**文字**攒起来，和截图一起列在「截屏」页里。

- 存在配置里（`clip_texts`，最多 100 条，自动去重）
- 只监听本次运行期间的复制动作；关掉面板就不再记录
- 图片不记（截图已经覆盖了那个需求，混在一起反而乱）

注意：从列表里点一条复制回去时，会把自己的这次写入当成新复制再记一遍，
所以用 `_suppress` 摁掉一次相同内容的回调。
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

import screenshot

MAX_TEXT = 100
MAX_TEXT_LEN = 200_000          # 超过这个长度多半是整篇文档，不记
CONFIG_KEY = "clip_texts"


def clip_dir() -> str:
    """旧版本存剪贴板图片的目录，现在只用来清理。"""
    return os.path.join(screenshot.SHOT_DIR, "clip")


class ClipStore(QObject):
    changed = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.texts: list[dict] = self._load_texts()
        self._suppress: str | None = None
        self._purge_old_images()
        self._clip = QApplication.clipboard()
        self._clip.dataChanged.connect(self._on_changed)

    # ------------------------------------------------------------------ 存取
    def _load_texts(self) -> list[dict]:
        raw = self.config.value(CONFIG_KEY, [])
        if not isinstance(raw, list):
            return []
        kept = []
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                kept.append({"text": item["text"],
                             "ts": float(item.get("ts") or 0)})
        return kept[:MAX_TEXT]

    def _save_texts(self) -> None:
        self.config.setValue(CONFIG_KEY, self.texts[:MAX_TEXT])

    def _purge_old_images(self) -> None:
        """早期版本会记剪贴板图片，现在不记了，把残留的删掉。"""
        folder = clip_dir()
        if not os.path.isdir(folder):
            return
        for name in os.listdir(folder):
            if name.startswith("clip_"):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass

    # ------------------------------------------------------------------ 监听
    def _on_changed(self) -> None:
        mime = self._clip.mimeData()
        if mime is None:
            return
        if mime.hasImage():                 # 图片不记，但也不能把它当文字读
            return
        if not mime.hasText():
            return
        text = self._clip.text()
        if not text or not text.strip() or len(text) > MAX_TEXT_LEN:
            return
        if self._suppress == text:          # 我们自己写回去的那次
            self._suppress = None
            return
        self._add_text(text)

    def _add_text(self, text: str) -> None:
        if self.texts and self.texts[0]["text"] == text:
            return                              # 连着复制同一段，只留一条
        self.texts.insert(0, {"text": text, "ts": time.time()})
        del self.texts[MAX_TEXT:]
        self._save_texts()
        self.changed.emit()

    # ------------------------------------------------------------------ 读取
    def items(self) -> list[dict]:
        return [{"kind": "text", "text": item["text"], "mtime": item["ts"]}
                for item in self.texts]

    def count(self) -> int:
        return len(self.texts)

    # ------------------------------------------------------------------ 写回
    def copy_text(self, text: str) -> None:
        self._suppress = text                  # 别把自己的写回记成新的一条
        self._clip.setText(text)

    def delete_text(self, text: str) -> None:
        self.texts = [item for item in self.texts if item["text"] != text]
        self._save_texts()
        self.changed.emit()

    def clear(self) -> None:
        self.texts = []
        self._save_texts()
        self.changed.emit()
