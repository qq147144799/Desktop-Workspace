"""
文件操作底层：系统文件剪贴板（CF_HDROP）、删除到回收站、复制/移动、按模板新建。

剪贴板刻意用 Windows 原生 CF_HDROP 格式，这样面板里复制的东西可以直接粘到系统资源管理器，
反过来在资源管理器里复制的文件也能粘到面板里。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import struct
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
DROPEFFECT_COPY = 1
DROPEFFECT_MOVE = 2

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", wintypes.DWORD),
        ("pt", wintypes.POINT),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    ]


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.RegisterClipboardFormatW.argtypes = (wintypes.LPCWSTR,)
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalUnlock.restype = wintypes.BOOL

shell32.DragQueryFileW.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
shell32.DragQueryFileW.restype = wintypes.UINT
shell32.SHFileOperationW.argtypes = (ctypes.POINTER(SHFILEOPSTRUCTW),)
shell32.SHFileOperationW.restype = ctypes.c_int


# --------------------------------------------------------------------------- 工具


def unique_path(path: str) -> str:
    """目标已存在时生成「名称 (2).ext」这样的可用路径。"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    if os.path.isdir(path):
        root, ext = path, ""
    i = 2
    while os.path.exists(f"{root} ({i}){ext}"):
        i += 1
    return f"{root} ({i}){ext}"


# --------------------------------------------------------------------------- 剪贴板


def set_files(paths: list[str], cut: bool = False) -> bool:
    """把文件列表放进系统剪贴板（cut=True 时标记为剪切）。"""
    paths = [os.path.abspath(p) for p in paths if p]
    if not paths:
        return False

    ok = False
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        payload = ("\0".join(paths) + "\0\0").encode("utf-16-le")
        header = ctypes.sizeof(DROPFILES)

        hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE, header + len(payload))
        if not hglobal:
            return False
        ptr = kernel32.GlobalLock(hglobal)
        try:
            drop = DROPFILES.from_address(ptr)
            drop.pFiles = header
            drop.fNC = False
            drop.fWide = True
            ctypes.memmove(ptr + header, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(hglobal)
        user32.SetClipboardData(CF_HDROP, hglobal)   # 所有权移交系统

        fmt = user32.RegisterClipboardFormatW("Preferred DropEffect")
        if fmt:
            h2 = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(ctypes.c_uint32))
            if h2:
                p2 = kernel32.GlobalLock(h2)
                if p2:
                    ctypes.c_uint32.from_address(p2).value = DROPEFFECT_MOVE if cut else DROPEFFECT_COPY
                    kernel32.GlobalUnlock(h2)
                user32.SetClipboardData(fmt, h2)
        ok = True
    finally:
        user32.CloseClipboard()
    return ok


def get_files() -> tuple[list[str], bool]:
    """读取剪贴板里的文件列表，返回 (路径列表, 是否为剪切)。"""
    paths: list[str] = []
    cut = False
    if not user32.IsClipboardFormatAvailable(CF_HDROP):
        return paths, cut
    if not user32.OpenClipboard(None):
        return paths, cut
    try:
        handle = user32.GetClipboardData(CF_HDROP)
        if handle:
            count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
            for i in range(count):
                need = shell32.DragQueryFileW(handle, i, None, 0)
                buf = ctypes.create_unicode_buffer(need + 1)
                shell32.DragQueryFileW(handle, i, buf, need + 1)
                if buf.value:
                    paths.append(buf.value)

        fmt = user32.RegisterClipboardFormatW("Preferred DropEffect")
        if fmt:
            h2 = user32.GetClipboardData(fmt)
            if h2:
                p = kernel32.GlobalLock(h2)
                if p:
                    if ctypes.c_uint32.from_address(p).value & DROPEFFECT_MOVE:
                        cut = True
                    kernel32.GlobalUnlock(h2)
    finally:
        user32.CloseClipboard()
    return paths, cut


def clear_files() -> None:
    if user32.OpenClipboard(None):
        try:
            if user32.IsClipboardFormatAvailable(CF_HDROP):
                user32.EmptyClipboard()
        finally:
            user32.CloseClipboard()


# --------------------------------------------------------------------------- 操作


def recycle(paths: list[str]) -> bool:
    """删除到回收站（可撤销），不弹系统确认框。"""
    paths = [os.path.abspath(p) for p in paths if p]
    if not paths:
        return False
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = "\0".join(paths) + "\0\0"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    op.fAnyOperationsAborted = False
    code = shell32.SHFileOperationW(ctypes.byref(op))
    return code == 0 and not op.fAnyOperationsAborted


def copy_path(src: str, dst: str) -> None:
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def paste_into(target_dir: str) -> tuple[list[str], list[str], bool]:
    """把剪贴板里的文件粘贴到 target_dir，返回 (成功列表, 错误列表, 是否剪切)。"""
    paths, cut = get_files()
    done: list[str] = []
    errors: list[str] = []
    for src in paths:
        if not os.path.exists(src):
            errors.append(f"{os.path.basename(src)} 已不存在")
            continue
        name = os.path.basename(src.rstrip("\\")) or src
        same_place = os.path.abspath(src) == os.path.abspath(os.path.join(target_dir, name))
        if cut and same_place:
            continue                      # 剪切到原位置，什么也不用做
        dst = unique_path(os.path.join(target_dir, name))
        try:
            if cut:
                shutil.move(src, dst)
            else:
                copy_path(src, dst)
            done.append(dst)
        except (OSError, shutil.Error) as exc:
            errors.append(f"{name}: {exc}")

    if cut and done and not errors:
        clear_files()                     # 剪切粘贴完成，像资源管理器一样清空
    return done, errors, cut


def new_from_template(tpl: dict, target_dir: str) -> str:
    """按系统「新建」模板在 target_dir 里创建文件，返回新文件路径。"""
    ext = tpl.get("ext", "")
    label = tpl.get("label") or ext.lstrip(".")
    path = unique_path(os.path.join(target_dir, f"新建 {label}{ext}"))

    kind = tpl.get("kind")
    if kind == "file":
        src = tpl.get("file") or ""
        if not os.path.isfile(src):
            raise OSError(f"模板文件不存在：{src}")
        shutil.copyfile(src, path)
    elif kind == "data":
        with open(path, "wb") as fh:
            fh.write(tpl.get("data") or b"")
    else:                                  # null：空文件
        with open(path, "wb"):
            pass
    return path


def new_folder(target_dir: str) -> str:
    path = unique_path(os.path.join(target_dir, "新建文件夹"))
    os.makedirs(path)
    return path
