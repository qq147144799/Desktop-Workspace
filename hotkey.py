"""全局快捷键：低级键盘钩子（WH_KEYBOARD_LL）。

和 closetotray 里的鼠标钩子是一套写法。几个必须注意的点：

- ``SetWindowsHookExW`` 的 **hMod 必须传 NULL**，传进程模块句柄会报 126。
- 回调里不能干重活：超时（默认 300ms）系统会直接把钩子摘掉。
  所以命中后只 ``QTimer.singleShot(0, ...)`` 丢回事件循环，立刻 return。
- 回调在**装钩子的线程**（主线程）执行，所以从里面用 QTimer 是安全的。
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C

MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN = 1, 2, 4, 8

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


_NAMED = {
    "space": 0x20, "tab": 0x09, "backspace": 0x08, "return": 0x0D, "enter": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "print": 0x2C, "printscreen": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
}
_MODS = {"ctrl": MOD_CTRL, "control": MOD_CTRL, "shift": MOD_SHIFT,
         "alt": MOD_ALT, "win": MOD_WIN, "meta": MOD_WIN, "super": MOD_WIN}


def parse(sequence: str) -> tuple[int, int] | tuple[None, None]:
    """``"Ctrl+Shift+A"`` → ``(修饰键位掩码, 虚拟键码)``；认不出来返回 (None, None)。"""
    text = (sequence or "").strip()
    if not text:
        return None, None
    mods = 0
    vk = None
    for part in text.replace(" ", "").split("+"):
        low = part.lower()
        if low in _MODS:
            mods |= _MODS[low]
        elif low in _NAMED:
            vk = _NAMED[low]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
            vk = 0x6F + int(low[1:])          # F1 = 0x70
        else:
            return None, None
    if vk is None:
        return None, None
    return mods, vk


def is_safe(sequence: str) -> bool:
    """光秃秃的字母/数字会把正常打字全吃掉，只允许「带修饰键」或「F 键 / PrintScreen」。"""
    mods, vk = parse(sequence)
    if vk is None:
        return False
    if mods:
        return True
    return vk >= 0x70 or vk == 0x2C


class HotkeyHook:
    """一个全局快捷键。改键 / 暂停都走这里。"""

    def __init__(self, on_trigger):
        self._on_trigger = on_trigger
        self._hook = None
        self._proc_ref = None
        self._mods = 0
        self._vk = 0
        self.sequence = ""
        self._held = False

    # ------------------------------------------------------------ 生命周期
    @property
    def active(self) -> bool:
        return bool(self._hook)

    def set_sequence(self, sequence: str) -> bool:
        """设成新键位并（重）装钩子。空字符串表示停用。"""
        mods, vk = parse(sequence)
        self._mods = mods or 0
        self._vk = vk or 0
        self.sequence = sequence or ""
        if not self._vk:
            self.uninstall()
            return True
        self.uninstall()
        return self.install()

    def install(self) -> bool:
        if self._hook is not None:
            return True
        if not self._vk:
            return False
        self._proc_ref = HOOKPROC(self._proc)
        # hMod 必须是 NULL：回调就在本进程里跑
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc_ref, None, 0)
        return bool(self._hook)

    def uninstall(self) -> None:
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._proc_ref = None

    # ------------------------------------------------------------ 钩子
    def _proc(self, n_code, w_param, l_param):
        try:
            if n_code == 0 and self._vk:
                info = ctypes.cast(l_param, POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(info.vkCode)
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    if vk == self._vk and self._current_mods() == self._mods:
                        if not self._held:
                            self._held = True
                            self._fire()
                        return 1                       # 吞掉，别让下面的程序再收到
                elif w_param in (WM_KEYUP, WM_SYSKEYUP) and vk == self._vk:
                    self._held = False
        except Exception:
            pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _fire(self) -> None:
        # 钩子里不能做大动作，丢回事件循环再干活
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._on_trigger)

    @staticmethod
    def _current_mods() -> int:
        mods = 0
        if user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
            mods |= MOD_CTRL
        if user32.GetAsyncKeyState(VK_SHIFT) & 0x8000:
            mods |= MOD_SHIFT
        if user32.GetAsyncKeyState(VK_MENU) & 0x8000:
            mods |= MOD_ALT
        if user32.GetAsyncKeyState(VK_LWIN) & 0x8000 or user32.GetAsyncKeyState(VK_RWIN) & 0x8000:
            mods |= MOD_WIN
        return mods
