"""
窗口收纳：把其它程序的窗口藏起来。

用 ShowWindow(SW_HIDE) 而不是最小化 —— 最小化之后任务栏还留着按钮，
而 SW_HIDE 会让窗口连任务栏一起消失，程序本身照常在后台跑。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

SW_HIDE = 0
SW_SHOW = 5
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
GW_OWNER = 4

# 这些窗口藏着会出问题（桌面、任务栏、输入法、UWP 宿主等），一律跳过
SKIP_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow",
    "ApplicationManager_DesktopShellWindow",
    "TaskListThumbnailWnd",
    "IME",
    "MSCTFIME UI",
    "Default IME",
    "SideExplorerPanel",
}

_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetWindow.restype = wintypes.HWND
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindow.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.EnumWindows.argtypes = (_EnumProc, wintypes.LPARAM)


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def list_windows(skip_hwnd: int = 0) -> list[tuple[int, str]]:
    """列出可以收纳的窗口：可见、有标题、非工具窗口、不是桌面/任务栏、不是我们自己。"""
    own_pid = os.getpid()
    found: list[tuple[int, str]] = []

    def callback(hwnd, _lparam):
        if not hwnd or hwnd == skip_hwnd:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, GW_OWNER):          # 有 owner 的弹窗（对话框）不动
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if _class_name(hwnd) in SKIP_CLASSES:
            return True
        title = _title(hwnd)
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        found.append((int(hwnd), title))
        return True

    user32.EnumWindows(_EnumProc(callback), 0)
    return found


def hide_windows(hwnds) -> list[int]:
    """隐藏窗口，返回真正被藏起来的那些。"""
    hidden: list[int] = []
    for hwnd in hwnds:
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            continue
        user32.ShowWindow(hwnd, SW_HIDE)
        if not user32.IsWindowVisible(hwnd):
            hidden.append(hwnd)
    return hidden


def show_windows(hwnds) -> int:
    """把之前藏起来的窗口还原。已经关掉的会自动跳过。"""
    shown = 0
    for hwnd in hwnds:
        if user32.IsWindow(hwnd):
            user32.ShowWindow(hwnd, SW_SHOW)
            shown += 1
    return shown
