"""
Windows AppBar 支持。

通过 shell32!SHAppBarMessage 把窗口注册成"应用栏"(Application Desktop Toolbar)，
系统会把这块区域从桌面工作区里扣掉 —— 效果和任务栏一样：
其它窗口最大化时只会填满剩下的区域，不会盖住它。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", RECT),
        ("lParam", wintypes.LPARAM),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hWnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", ctypes.c_ulonglong),
        ("lParam", ctypes.c_ssize_t),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


# SHAppBarMessage 的消息号
ABM_NEW = 0x00000000
ABM_REMOVE = 0x00000001
ABM_QUERYPOS = 0x00000002
ABM_SETPOS = 0x00000003
ABM_GETSTATE = 0x00000004

# 边缘
ABE_LEFT = 0
ABE_TOP = 1
ABE_RIGHT = 2
ABE_BOTTOM = 3

# 通知消息
ABN_STATECHANGE = 0
ABN_POSCHANGED = 1
ABN_FULLSCREENAPP = 2
ABN_WINDOWARRANGE = 3

shell32.SHAppBarMessage.argtypes = (wintypes.DWORD, ctypes.POINTER(APPBARDATA))
shell32.SHAppBarMessage.restype = ctypes.c_size_t
user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
user32.RegisterWindowMessageW.restype = wintypes.UINT


class AppBar:
    """把某个 HWND 注册为屏幕左/右边缘的应用栏。"""

    def __init__(self, hwnd: int, width_px: int = 340, edge: str = "left"):
        self.hwnd = hwnd
        self.width = width_px                      # 物理像素
        self.edge = "right" if edge == "right" else "left"
        self.msg = user32.RegisterWindowMessageW("SideExplorer_AppBar_Message")
        self.registered = False
        self.rect: tuple[int, int, int, int] | None = None

    # ------------------------------------------------------------------ 内部
    def _abd(self) -> APPBARDATA:
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        abd.hWnd = self.hwnd
        abd.uCallbackMessage = self.msg
        abd.uEdge = ABE_RIGHT if self.edge == "right" else ABE_LEFT
        return abd

    # ------------------------------------------------------------------ 公开
    def register(self) -> bool:
        if self.registered:
            return True
        abd = self._abd()
        ok = shell32.SHAppBarMessage(ABM_NEW, ctypes.byref(abd))
        self.registered = bool(ok)
        return self.registered

    def place(self, screen_size: tuple[int, int] | None = None) -> tuple[int, int, int, int] | None:
        """把应用栏钉在屏幕左/右边，返回最终物理矩形 (l, t, r, b)。"""
        if not self.registered:
            return None
        screen_w, screen_h = screen_size or primary_screen_size()

        abd = self._abd()
        if self.edge == "right":
            abd.rc = RECT(screen_w - self.width, 0, screen_w, screen_h)
        else:
            abd.rc = RECT(0, 0, self.width, screen_h)

        # 1) 先问系统这块位置能不能用（会避开任务栏 / 其它应用栏）
        if not shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(abd)):
            return None
        # 2) 高度听系统的，宽度和贴边方向坚持自己的。
        #    系统有时会把边界推到屏幕外（多屏/DPI 切换后），必须钳回来，
        #    否则窗口会被拉宽到看不见的地方。
        if self.edge == "right":
            abd.rc.right = min(int(abd.rc.right), screen_w)
            abd.rc.left = abd.rc.right - self.width
        else:
            abd.rc.left = max(int(abd.rc.left), 0)
            abd.rc.right = abd.rc.left + self.width
        abd.rc.top = max(int(abd.rc.top), 0)
        abd.rc.bottom = min(int(abd.rc.bottom), screen_h)
        # 3) 正式占位，系统会广播工作区变化，所有最大化窗口自动收缩
        if not shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(abd)):
            return None

        self.rect = (abd.rc.left, abd.rc.top, abd.rc.right, abd.rc.bottom)
        return self.rect

    def query(self, screen_size: tuple[int, int] | None = None) -> tuple[int, int, int, int] | None:
        """只问系统这块位置能不能用，不登记占位（用于响应 ABN_POSCHANGED，避免广播循环）。"""
        if not self.registered:
            return None
        screen_w, screen_h = screen_size or primary_screen_size()
        abd = self._abd()
        if self.edge == "right":
            abd.rc = RECT(screen_w - self.width, 0, screen_w, screen_h)
        else:
            abd.rc = RECT(0, 0, self.width, screen_h)
        if not shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(abd)):
            return None
        if self.edge == "right":
            abd.rc.right = min(int(abd.rc.right), screen_w)
            abd.rc.left = abd.rc.right - self.width
        else:
            abd.rc.left = max(int(abd.rc.left), 0)
            abd.rc.right = abd.rc.left + self.width
        abd.rc.top = max(int(abd.rc.top), 0)
        abd.rc.bottom = min(int(abd.rc.bottom), screen_h)
        return (abd.rc.left, abd.rc.top, abd.rc.right, abd.rc.bottom)

    def set_width(self, width_px: int, screen_size: tuple[int, int] | None = None):
        self.width = max(220, int(width_px))
        return self.place(screen_size)

    def set_edge(self, edge: str, screen_size: tuple[int, int] | None = None):
        """切换吸附边：先解绑再重新注册，避免系统里留下两块占位。"""
        target = "right" if edge == "right" else "left"
        if target == self.edge:
            return self.place(screen_size)
        was_registered = self.registered
        if was_registered:
            self.remove()
        self.edge = target
        if was_registered:
            self.register()
        return self.place(screen_size)

    def remove(self) -> None:
        """解除占位，桌面工作区恢复原状。"""
        if not self.registered:
            return
        abd = self._abd()
        shell32.SHAppBarMessage(ABM_REMOVE, ctypes.byref(abd))
        self.registered = False
        self.rect = None

    def owns_message(self, message: int) -> bool:
        return self.msg != 0 and message == self.msg


def primary_screen_size() -> tuple[int, int]:
    """主屏物理分辨率（应用栏固定在主屏左边缘）。"""
    return (
        user32.GetSystemMetrics(0),   # SM_CXSCREEN
        user32.GetSystemMetrics(1),   # SM_CYSCREEN
    )
