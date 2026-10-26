"""
便签数据层：结构定义、颜色方案、时间解析、CSV 导入导出。

存储直接挂在 SideExplorer.json 的 notes 键下，一行一条：
    {"text": "买牛奶", "color": "yellow", "remind": "2026-10-22 18:00", "fired": false}
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

TIME_FMT = "%Y-%m-%d %H:%M"
CSV_HEADER = ["内容", "颜色", "提醒时间"]

# 名称 -> (中文, 行底色, 色条)
COLORS: dict[str, tuple[str, str, str]] = {
    "yellow": ("黄", "#FFF6CC", "#F0B429"),
    "green": ("绿", "#E3F6E5", "#3FB950"),
    "blue": ("蓝", "#DFEBFF", "#2F6FEB"),
    "pink": ("粉", "#FFE6EE", "#E85C8A"),
    "gray": ("灰", "#F4F6F8", "#9AA3AD"),
}
COLOR_ORDER = ["yellow", "green", "blue", "pink", "gray"]
DEFAULT_COLOR = "yellow"


def color_pair(name: str) -> tuple[str, str]:
    return COLORS.get(name, COLORS[DEFAULT_COLOR])[1:]


def color_label(name: str) -> str:
    return COLORS.get(name, COLORS[DEFAULT_COLOR])[0]


# --------------------------------------------------------------------------- 时间


def parse_time(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("/", "-")
    for fmt in (TIME_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def format_time(moment: datetime) -> str:
    return moment.strftime(TIME_FMT)


def human_time(moment: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    tomorrow = (now + timedelta(days=1)).date()
    if moment.date() == tomorrow:
        return "明天 " + moment.strftime("%H:%M")
    return moment.strftime("%m-%d %H:%M")


# --------------------------------------------------------------------------- 数据


@dataclass
class Note:
    text: str = ""
    color: str = DEFAULT_COLOR
    remind: str = ""
    fired: bool = False

    @property
    def remind_dt(self) -> datetime | None:
        return parse_time(self.remind)

    def is_due(self, now: datetime | None = None) -> bool:
        moment = self.remind_dt
        if moment is None or self.fired:
            return False
        return moment <= (now or datetime.now())

    def to_dict(self) -> dict:
        return {"text": self.text, "color": self.color,
                "remind": self.remind, "fired": self.fired}

    @classmethod
    def from_dict(cls, data) -> "Note | None":
        if not isinstance(data, dict):
            return None
        text = str(data.get("text") or "").strip()
        if not text:
            return None
        color = str(data.get("color") or DEFAULT_COLOR)
        if color not in COLORS:
            color = DEFAULT_COLOR
        remind = str(data.get("remind") or "")
        if parse_time(remind) is None:
            remind = ""
        return cls(text=text, color=color, remind=remind, fired=bool(data.get("fired")))


def load_notes(raw) -> list[Note]:
    if not isinstance(raw, list):
        return []
    result: list[Note] = []
    for item in raw:
        note = Note.from_dict(item)
        if note is not None:
            result.append(note)
    return result


def dump_notes(notes: list[Note]) -> list[dict]:
    return [note.to_dict() for note in notes]


# --------------------------------------------------------------------------- CSV


def export_csv(path: str, notes: list[Note]) -> int:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for note in notes:
            writer.writerow([note.text, note.color, note.remind])
    return len(notes)


def _read_text(path: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        try:
            with open(path, "r", encoding=encoding, newline="") as fh:
                return fh.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise OSError("无法识别文件编码（试过 UTF-8 / GBK / UTF-16）")


def import_csv(path: str) -> list[Note]:
    """读取 CSV，兼容有无表头、逗号/分号/制表符分隔。"""
    import io

    text = _read_text(path)
    if not text.strip():
        return []

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    result: list[Note] = []
    for row in csv.reader(io.StringIO(text), dialect):
        if not row:
            continue
        text_cell = (row[0] if len(row) > 0 else "").strip()
        if not text_cell or text_cell == CSV_HEADER[0]:
            continue
        color = (row[1] if len(row) > 1 else "").strip().lower()
        if color not in COLORS:
            color = DEFAULT_COLOR
        remind = (row[2] if len(row) > 2 else "").strip()
        moment = parse_time(remind)
        result.append(Note(text=text_cell, color=color,
                           remind=format_time(moment) if moment else ""))
    return result


def default_export_name() -> str:
    return f"便签_{datetime.now().strftime('%Y%m%d')}.csv"


def ensure_csv_ext(path: str) -> str:
    if os.path.splitext(path)[1]:
        return path
    return path + ".csv"


# --------------------------------------------------------------------------- 回收站

TRASH_LIMIT = 100


def make_trash_entry(note: Note) -> dict:
    return {
        "note": Note(text=note.text, color=note.color, remind=note.remind, fired=note.fired),
        "deleted_at": format_time(datetime.now()),
    }


def load_trash(raw) -> list[dict]:
    """回收站：[{'note': Note, 'deleted_at': '2026-10-22 14:30'}]"""
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        note = Note.from_dict(item)
        if note is None:
            continue
        result.append({
            "note": note,
            "deleted_at": str(item.get("deleted_at") or ""),
        })
    return result[:TRASH_LIMIT]


def dump_trash(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for entry in items[:TRASH_LIMIT]:
        note = entry.get("note")
        if not isinstance(note, Note):
            continue
        data = note.to_dict()
        data["deleted_at"] = entry.get("deleted_at", "")
        out.append(data)
    return out
