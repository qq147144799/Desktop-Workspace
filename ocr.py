"""文字识别：只用 Windows 自带的 OCR 引擎，不拖任何第三方运行时。

选型经过实测：接过 RapidOCR（PP-OCR），中文确实更准，但要连带装进
onnxruntime + opencv 一百多兆，一个贴边小面板背这个太重，所以撤掉了。

系统引擎的脾气，都是量出来的：

- 一次 20~30 毫秒，比 PP-OCR 快二十倍 —— 贴图即可识别，不用转圈等；
- 界面小字（13px）会认错，整图放大 1.5 倍后准确率从 78% 提到 89%；
  16~22px 的字本来就接近满分，放大到 1.5 倍还能再高一档；
- 中文有几个固定的看错（桌面→卓面），没有可靠办法纠正，只能认；
- 单张图最长边不能超过 ``OcrEngine.max_image_dimension``（一般 2600px）。

对外只有 ``available()`` / ``engine_name()`` / ``analyze(image)`` / ``recognize(image)``。
``analyze`` 返回的每个字都带 **0~1 的归一化矩形**，贴图靠它做「I 形光标 + 拖选」。
"""

from __future__ import annotations

import asyncio
import re
import threading

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage

try:                                     # 系统自带，随 Windows 一起来
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    _HAS_WINRT = True
except ImportError:
    _HAS_WINRT = False

UPSCALE = 1.5                            # 小图放大倍率：实测甜点
COMFORT_WIDTH = 1400                     # 比这窄才放大，宽图原样送
HARD_LIMIT = 2600                        # 引擎上限的兜底值
_FALLBACK_LIMITS = ("zh-Hans-CN", "zh-CN", "zh-Hans")

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_PUNCT = "，。、；：？！（）《》“”‘’【】"
_FULLWIDTH = {ord(c): ord(a) for c, a in
              zip("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
                  "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
                  "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "abcdefghijklmnopqrstuvwxyz")}

_local = threading.local()               # 引擎按线程缓存：winrt 对象不能跨线程用


def available() -> bool:
    return _HAS_WINRT


def engine_name() -> str:
    return "Windows OCR" if _HAS_WINRT else "无"


# ---------------------------------------------------------------- 文本整理
def tidy(text: str) -> str:
    """修补排版：汉字之间的空格、全角标点、被认成逗号的小数点。"""
    out = text.translate(_FULLWIDTH)
    out = out.replace("．", ".").replace("％", "%")
    out = re.sub(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])", "", out)
    out = re.sub(rf"\s+(?=[{_CJK_PUNCT}])", "", out)
    out = re.sub(rf"(?<=[{_CJK_PUNCT}])\s+", "", out)
    # 数字之间的逗号多半是小数点：1280，50 → 1280.50（三位那组当千分位留着）
    out = re.sub(r"(?<=\d)\s*[，,]\s*(?=\d{1,2}(?!\d))", ".", out)
    out = re.sub(r"(?<=\d)\s*，\s*(?=\d{3}(?!\d))", ",", out)       # 千分位统一成半角
    out = re.sub(r"(?<=[¥$￥])\s+", "", out)
    out = re.sub(r"\s*([.%])\s*", r"\1", out)
    return out.strip()


def join_words(words: list[dict]) -> str:
    """按每段自带的 ``sep`` 拼起来再整理。"""
    return tidy("".join(word.get("sep", "") + word["text"] for word in words))


# ---------------------------------------------------------------- 引擎
def _engine():
    """拿一个中文 OCR 引擎；拿不到返回 None（结果同样缓存，不必反复试）。"""
    cached = getattr(_local, "engine", None)
    if cached is not None:
        return cached or None

    engine = None
    for tag in _FALLBACK_LIMITS:
        try:
            engine = OcrEngine.try_create_from_language(Language(tag))
        except Exception:
            engine = None
        if engine is not None:
            break
    if engine is None:
        try:
            engine = OcrEngine.try_create_from_user_profile_languages()
        except Exception:
            engine = None

    _local.engine = engine if engine is not None else False
    return engine


def _limit() -> int:
    cached = getattr(_local, "limit", 0)
    if not cached:
        try:
            cached = int(OcrEngine.max_image_dimension)
        except Exception:
            cached = HARD_LIMIT
        cached = cached if cached > 0 else HARD_LIMIT
        _local.limit = cached
    return cached


def _fit(image):
    """按引擎口味缩放：小图放大，超大图必须先缩，否则引擎直接拒收。"""
    width, height = max(1, image.width()), max(1, image.height())
    factor = min(UPSCALE, COMFORT_WIDTH / width)
    factor = max(1.0, factor)

    longest = max(width, height)
    if longest * factor > _limit():                # 4K 截图放大会超限
        factor = _limit() / longest
    if abs(factor - 1.0) < 0.02:
        return image
    return image.scaled(max(1, int(width * factor)), max(1, int(height * factor)),
                        Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def _png(image) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


async def _recognize_async(engine, blob: bytes):
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(blob)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    return await engine.recognize_async(bitmap)


# ---------------------------------------------------------------- 按字切分
class _Ink:
    """灰度快照，用来回答「这一列有没有墨」。

    引擎只给到 word 级框，中文还好（通常一字一个 word），英文是一整词。
    要拖选得跟手就得自己量像素边界 —— 按字宽均分的话，中英混排会越走越偏，
    实测能偏出一个字。
    """

    def __init__(self, image):
        self._image = image
        self._loaded = False
        self.data = b""
        self.stride = 0
        self.width, self.height = image.width(), image.height()
        self._dark = None

    def _load(self) -> bool:
        """整图转灰度只在真要用的时候做（中文基本一字一个 word，多数用不上）。"""
        if self._loaded:
            return bool(self.data)
        self._loaded = True
        gray = self._image.convertToFormat(QImage.Format_Grayscale8)
        self.data = bytes(gray.constBits())
        self.stride = gray.bytesPerLine()
        self.width, self.height = gray.width(), gray.height()
        return bool(self.data)

    def _is_dark(self) -> bool:
        if self._dark is None:
            step = max(1, len(self.data) // 4096)
            sample = self.data[::step] or b"\x80"
            self._dark = (sum(sample) / len(sample)) > 128        # 深色字压浅底
        return self._dark

    def columns(self, x0: float, y0: float, x1: float, y1: float):
        """返回这一区域里连续的「有墨」列区间 ``[(a, b), ...]``。"""
        if not self._load():
            return None
        left = max(0, min(self.width - 1, int(x0)))
        right = max(left + 1, min(self.width, int(x1)))
        top = max(0, min(self.height - 1, int(y0)))
        bottom = max(top + 1, min(self.height, int(y1)))
        if right - left < 2 or bottom - top < 2:
            return None

        data, stride = self.data, self.stride
        dark = self._is_dark()
        rows = [row * stride for row in range(top, bottom)]

        runs: list[tuple[int, int]] = []
        start = None
        for x in range(left, right):
            ink = False
            for offset in rows:
                value = data[offset + x]
                if (value < 140) if dark else (value > 115):
                    ink = True
                    break
            if ink and start is None:
                start = x
            elif not ink and start is not None:
                runs.append((start, x))
                start = None
        if start is not None:
            runs.append((start, right))
        return runs or None


_NARROW = set("iljtfrI.,:;'!|()[]/")


def _char_weight(ch: str) -> float:
    if ch == " ":
        return 0.42
    if ord(ch) > 0x2E80:                 # 中日韩 / 全角
        return 1.0
    if ch in _NARROW:
        return 0.36
    if ch.isupper():
        return 0.62
    return 0.52


def _split_word(ink: _Ink | None, text: str, x0: float, y0: float,
                x1: float, y1: float, line: int, w_img: int, h_img: int,
                lead_space: str = " ") -> list[dict]:
    """把一个 word 拆成单字，每个字带自己的归一化矩形。"""
    glyphs = [ch for ch in text if ch != " "]
    if not glyphs:
        return []

    boxes = None
    runs = ink.columns(x0, y0, x1, y1) if (ink is not None and len(glyphs) > 1) else None
    if runs and len(runs) == len(glyphs):
        boxes = [(float(a), float(b)) for a, b in runs]           # 像素级：最准

    if boxes is None:                                             # 退化：按字宽估算
        weights = [_char_weight(ch) for ch in glyphs]
        total = sum(weights) or 1.0
        span = max(1.0, x1 - x0)
        boxes = []
        cursor = x0
        for weight in weights:
            piece = span * weight / total
            boxes.append((cursor, cursor + piece))
            cursor += piece

    entries: list[dict] = []
    glyph_index = 0
    pending_space = lead_space
    for ch in text:
        if ch == " ":
            pending_space = " "
            continue
        if glyph_index >= len(boxes):
            break
        start, end = boxes[glyph_index]
        glyph_index += 1
        entries.append({
            "text": ch,
            "x": start / w_img,
            "y": y0 / h_img,
            "w": max(1.0, end - start) / w_img,
            "h": max(1.0, y1 - y0) / h_img,
            "line": line,
            "sep": pending_space,
        })
        pending_space = ""
    return entries


# ---------------------------------------------------------------- 对外接口
def analyze(image) -> tuple[list[dict], str]:
    """识别图片，返回 ``(字列表, 错误)``。"""
    if not _HAS_WINRT:
        return [], "这台机器没有系统文字识别（缺 winrt 组件）"

    engine = _engine()
    if engine is None:
        return [], "系统里没装中文识别语言包"

    work = _fit(image)
    try:
        result = asyncio.run(_recognize_async(engine, _png(work)))
    except Exception as exc:
        return [], f"识别失败：{exc}"

    width, height = max(1, work.width()), max(1, work.height())
    ink = _Ink(work)
    entries: list[dict] = []
    for line_index, line in enumerate(result.lines):
        lead = ""                        # 行首不加空格，词与词之间留一个
        for word in line.words:
            text = word.text
            if not text or not text.strip():
                continue
            rect = word.bounding_rect
            entries.extend(_split_word(ink, text, rect.x, rect.y,
                                       rect.x + rect.width, rect.y + rect.height,
                                       line_index, width, height, lead))
            lead = " "
    if not entries:
        return [], "这张图里没认出文字"
    return entries, ""


def recognize(image) -> tuple[str, str]:
    """只要纯文本的简化接口。"""
    words, error = analyze(image)
    if not words:
        return "", error
    by_line: dict[int, list[dict]] = {}
    for word in words:
        by_line.setdefault(word["line"], []).append(word)
    return "\n".join(join_words(by_line[i]) for i in sorted(by_line)), ""
