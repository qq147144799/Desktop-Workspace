"""
配置读写：程序目录下的 SideExplorer.json（绿色便携，删掉即恢复默认）。

没用 QSettings，是因为它写 ini 时走「临时文件 + rename 覆盖」的原子写，
在部分目录（只读、被占用、U 盘）会直接失败且没有回退。这里改成
「直接覆盖写 + 失败重试 + 退到 LocalAppData」，更稳也更好读。
"""

from __future__ import annotations

import json
import os
import shutil
import sys


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _portable_folders() -> list[str]:
    folders = [app_dir()]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        folders.append(os.path.join(local, "SideExplorer"))
    return folders


class Config:
    FILENAME = "SideExplorer.json"
    BAK_SUFFIX = ".bak"          # 永远保留上一版，写坏了能捞回来

    def __init__(self) -> None:
        self.path = ""
        # 测试/多开时可以指定独立配置文件，避免动到用户的真实数据
        override = os.environ.get("SIDEEXPLORER_CONFIG")
        if override:
            self.path = os.path.abspath(override)
            self.data: dict = {}
            self.load()
            return

        for folder in _portable_folders():
            candidate = os.path.join(folder, self.FILENAME)
            if os.path.isfile(candidate):
                self.path = candidate
                break
        if not self.path:
            self.path = os.path.join(_portable_folders()[0], self.FILENAME)
        self.data: dict = {}
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self) -> None:
        """主文件读不了就退到 .bak，避免一次写坏就彻底丢数据。"""
        for path in (self.path, self.path + self.BAK_SUFFIX):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                self.data = data
                return
        self.data = {}

    def value(self, key: str, default=None, type=None):
        if key not in self.data:
            return default
        val = self.data[key]
        if type is bool:
            return bool(val)
        if type is int:
            try:
                return int(val)
            except (TypeError, ValueError):
                return default
        if type is str:
            return val if isinstance(val, str) else str(val)
        return val

    def setValue(self, key: str, value) -> None:  # noqa: N802
        if self.data.get(key) == value:
            return
        self.data[key] = value
        self.save()

    def sync(self) -> None:
        """兼容旧调用；写入已在 setValue 内完成。"""

    def save(self) -> bool:
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        candidates = [self.path]
        for folder in _portable_folders():
            candidate = os.path.join(folder, self.FILENAME)
            if candidate not in candidates:
                candidates.append(candidate)

        for path in candidates:
            try:
                folder = os.path.dirname(path)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                # 落盘前先把当前版本留一份 .bak
                if os.path.isfile(path):
                    try:
                        shutil.copyfile(path, path + self.BAK_SUFFIX)
                    except OSError:
                        pass
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(payload)
            except OSError:
                continue
            self.path = path
            return True
        return False
