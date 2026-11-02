"""窗口枚举：把「别的程序」的顶层窗口列出来。

分屏页用 ``list_top_windows()``（每个窗口一条，不归并）；
需要按程序分组的地方用 ``list_windows()``（同 exe 归并成一行带 count）。

统一过滤掉：无标题窗口、工具窗口、UWP 的幽灵窗口、桌面/任务栏，
以及 ``exclude_pid`` 那一下自己的窗口。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
except OSError:
    dwmapi = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def exe_of_window(hwnd: int) -> str:
    """窗口所属进程的 exe 文件名（小写）。"""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def exe_path_of_window(hwnd: int) -> str:
    """窗口所属进程的完整 exe 路径（用来取图标）。"""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(2048)
        size = wt.DWORD(2048)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _is_cloaked(hwnd: int) -> bool:
    """UWP 应用会有隐藏的「幽灵窗口」，用 DWM 的 cloaked 标记排掉。"""
    if dwmapi is None:
        return False
    cloaked = wt.DWORD(0)
    try:
        dwmapi.DwmGetWindowAttribute(
            wt.HWND(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    except (OSError, AttributeError):
        return False
    return bool(cloaked.value)


def _window_entry(hwnd, exclude_pid: int | None = None,
                  include: set[int] | None = None) -> dict | None:
    """这个顶层窗口值不值得列出来；值得就返回 {"exe","exe_path","title","hwnd"}。

    ``include`` 是「虽然不可见也要列出来」的白名单（分屏页把它收起来的窗口放这儿，
    好让它们继续留在列表里）。
    """
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080

    if not user32.IsWindowVisible(hwnd) and (include is None or int(hwnd) not in include):
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return None
    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if ex_style & WS_EX_TOOLWINDOW and not ex_style & WS_EX_APPWINDOW:
        return None
    owner = user32.GetWindow(hwnd, GW_OWNER)
    if owner and not ex_style & WS_EX_APPWINDOW:
        return None
    if _is_cloaked(hwnd):
        return None
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return None

    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if exclude_pid and pid.value == exclude_pid:
        return None

    exe = exe_of_window(hwnd)
    if not exe:
        return None
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title, length + 1)
    return {
        "exe": exe,
        "exe_path": exe_path_of_window(hwnd),
        "title": title.value,
        "hwnd": int(hwnd),
    }


def list_top_windows(exclude_pid: int | None = None,
                     include: set[int] | None = None) -> list[dict]:
    """每个顶层窗口一条，**不按 exe 归并**，保持 Z 序（前台窗口在最前）。

    分屏要针对「具体的那个窗口」，归并会让同程序的第 2、3 个窗口根本选不到 ——
    浏览器开三个窗口，列表里只有一行，那就没法分屏了。
    ``include`` 里的窗口即使被隐藏也会列出来（分屏页「后」收起来的那些）。
    """
    result: list[dict] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        entry = _window_entry(hwnd, exclude_pid, include)
        if entry is not None:
            result.append(entry)
        return True

    user32.EnumWindows(_cb, 0)
    return result


def list_windows(exclude_pid: int | None = None) -> list[dict]:
    """枚举当前所有有标题的顶层窗口，按 exe 归并。

    返回 ``[{"exe", "exe_path", "title", "hwnd", "count"}]``，按 exe 名排序。
    过滤掉了工具窗口、UWP 幽灵窗口、以及 ``exclude_pid`` 自己的窗口。
    """
    found: dict[str, dict] = {}

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        entry = _window_entry(hwnd, exclude_pid)
        if entry is None:
            return True
        exe = entry["exe"]
        existing = found.get(exe)
        if existing is None:
            found[exe] = {**entry, "count": 1}
        else:
            existing["count"] += 1
            if len(entry["title"]) > len(existing["title"]):
                existing["title"] = entry["title"]
                existing["hwnd"] = entry["hwnd"]
                existing["exe_path"] = entry["exe_path"]
        return True

    user32.EnumWindows(_cb, 0)
    return sorted(found.values(), key=lambda d: (d["exe"], d["title"].lower()))

