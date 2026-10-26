"""从 Windows 的 Shell IDList 拖拽数据里解析出文件路径。

Win11 的资源管理器拖拽只提供 CFSTR_SHELLIDLIST（`Shell IDList Array`），
不再附带 CF_HDROP，所以 QMimeData.urls() 是空的，得自己按 PIDL 拼路径。
"""
from __future__ import annotations

import ctypes
import struct


def _pidl_length(raw: bytes, start: int) -> int:
    """一个 PIDL 的长度（以 2 字节的 0 结尾）。"""
    index = start
    while index + 2 <= len(raw):
        size = struct.unpack_from("<H", raw, index)[0]
        if size == 0:
            return index + 2 - start
        if size < 2:
            return 0
        index += size
    return 0


def paths_from_shell_idlist(raw: bytes) -> list[str]:
    if len(raw) < 8:
        return []
    try:
        count = struct.unpack_from("<I", raw, 0)[0]
        if not 0 < count <= 4096:
            return []
        offsets = struct.unpack_from("<%dI" % (count + 1), raw, 4)
    except struct.error:
        return []

    parent_len = _pidl_length(raw, offsets[0])
    # 父 PIDL 是「桌面」时就是个空 PIDL（只有 2 字节的结尾 0），这是合法的
    if parent_len < 2:
        return []
    parent = raw[offsets[0]: offsets[0] + parent_len - 2]

    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    except OSError:
        return []

    # 64 位下必须声明原型，否则指针会被当成 int 截断
    get_path = shell32.SHGetPathFromIDListW
    get_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    get_path.restype = ctypes.c_bool

    results: list[str] = []
    for index in range(1, count + 1):
        child_len = _pidl_length(raw, offsets[index])
        if child_len <= 0:
            continue
        full = parent + raw[offsets[index]: offsets[index] + child_len]
        buffer = ctypes.create_string_buffer(full, len(full))
        out = ctypes.create_unicode_buffer(32768)
        if get_path(ctypes.cast(buffer, ctypes.c_void_p), out):
            if out.value:
                results.append(out.value)
    return results
