"""Win7 式快捷分屏：贴左/右半、最小化、最大化、关闭、收后台。

面板本身是 AppBar，Windows 计算工作区时已经把面板的宽度扣掉了 —— 所以这里的
「左半」正好是面板右侧那块剩余空间的左半。两个窗口一左一右，加上面板，
整块屏幕就排满了。

交互照搬 Aero Snap 的直觉：点一下贴过去，**再点同一个方向就是还原** ——
贴之前的原位用 ``GetWindowPlacement`` 记下来，用 ``SetWindowPlacement`` 放回去
（不用 SW_RESTORE 还原，否则最大化的窗口会被压成小窗）。

「后」只是 ``SW_HIDE`` 把窗口收起来，**不挂托盘图标**，窗口仍然留在分屏页的列表里，
点「大」「左」「右」就能叫回来。

全程走 Win32 物理像素，和 Qt 的逻辑坐标不混用。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

SPI_GETWORKAREA = 0x0030
SW_RESTORE = 9
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_HIDE = 0
SW_SHOW = 5
WM_CLOSE = 0x0010
SWP_NOACTIVATE = 0x0010
HWND_TOP = 0

SIDES = ("left", "right")

_snapped: dict[int, str] = {}          # hwnd -> 当前贴的方向
_saved: dict[int, "Placement"] = {}    # hwnd -> 贴之前的原位
_hidden: set[int] = set()              # 被「后」收起来的窗口（列表里还得显示）


class Placement(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def work_area() -> tuple[int, int, int, int]:
    """工作区（已扣掉任务栏和 AppBar）：left, top, width, height，物理像素。"""
    rect = wintypes.RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return 0, 0, 1920, 1040
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


# ---------------------------------------------------------------- 状态查询
def is_snapped(hwnd: int, side: str | None = None) -> bool:
    """这个窗口当前是否贴在某一边（不给 side 就问有没有贴）。"""
    if side is None:
        return int(hwnd) in _snapped
    return _snapped.get(int(hwnd)) == side


def side_of(hwnd: int) -> str | None:
    """窗口当前贴在哪一边（没贴返回 None）。"""
    return _snapped.get(int(hwnd))


def state_of(hwnd: int) -> str | None:
    """按钮高亮用：left / right（贴边）、max / min / hidden、None。"""
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        return None
    if hwnd in _hidden or not user32.IsWindowVisible(hwnd):
        return "hidden"
    if _snapped.get(hwnd):
        return _snapped[hwnd]
    if user32.IsZoomed(hwnd):
        return "max"
    if user32.IsIconic(hwnd):
        return "min"
    return None


def snapped_count() -> int:
    return len(_snapped)


def hidden_windows() -> set[int]:
    """被「后」收起来的窗口：窗口枚举时要放行，好让它们继续留在列表里。"""
    return set(_hidden)


def prune_hidden() -> None:
    """窗口已经关掉的，从后台名单里剔掉。"""
    for hwnd in list(_hidden):
        if not user32.IsWindow(hwnd):
            _hidden.discard(hwnd)
            _forget(hwnd)


# ---------------------------------------------------------------- 内部工具
def _forget(hwnd: int) -> None:
    _snapped.pop(hwnd, None)
    _saved.pop(hwnd, None)


def _bring_front(hwnd: int) -> None:
    if not user32.SetForegroundWindow(hwnd):      # 前台锁可能拒绝，退一步至少提到顶上
        user32.BringWindowToTop(hwnd)
        user32.SetFocus(hwnd)


def _show(hwnd: int) -> None:
    """把隐藏的、最小化的窗口叫回来（最大化状态保持不变）。"""
    if not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, SW_SHOW)          # SW_SHOW 按原状态显示，不会取消最大化
        _hidden.discard(hwnd)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)


# ---------------------------------------------------------------- 动作
def snap(hwnd: int, side: str) -> str:
    """贴到指定半边；已经在同一边则还原。

    返回 ``"left"`` / ``"right"``（贴上了）、``"restored"``（还原了）、
    ``"gone"``（窗口没了）或 ``"failed"``。
    """
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        _forget(hwnd)
        _hidden.discard(hwnd)
        return "gone"

    if _snapped.get(hwnd) == side:        # 再点同一边 = 还原
        return "restored" if restore(hwnd) else "failed"

    place = Placement()
    place.length = ctypes.sizeof(Placement)
    if not user32.GetWindowPlacement(hwnd, ctypes.byref(place)):
        return "failed"

    _show(hwnd)
    if user32.IsZoomed(hwnd):             # 最大化的先落回来才能挪
        user32.ShowWindow(hwnd, SW_RESTORE)

    left, top, width, height = work_area()
    half = width // 2
    x = left if side == "left" else left + width - half
    # 放到最上层：只挪位置不提 Z 序的话，窗口要是本来被盖着，用户会以为「点了没反应」
    if not user32.SetWindowPos(hwnd, HWND_TOP, x, top, half, height, SWP_NOACTIVATE):
        return "failed"
    _bring_front(hwnd)

    _saved[hwnd] = place
    _snapped[hwnd] = side
    return side


def maximize(hwnd: int) -> str:
    """最大化；已经最大化就还原。返回 "maximized" / "restored" / "gone"。"""
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        _forget(hwnd)
        _hidden.discard(hwnd)
        return "gone"
    _show(hwnd)
    if user32.IsZoomed(hwnd):
        _forget(hwnd)
        user32.ShowWindow(hwnd, SW_RESTORE)
        _bring_front(hwnd)
        return "restored"
    _forget(hwnd)                     # 之前贴在半边的话，最大化后就作废了
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    _bring_front(hwnd)
    return "maximized"


def minimize(hwnd: int) -> str:
    """最小化；已经最小化就还原。返回 "minimized" / "restored" / "gone"。"""
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        _forget(hwnd)
        _hidden.discard(hwnd)
        return "gone"
    _show(hwnd)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        _bring_front(hwnd)
        return "restored"
    _forget(hwnd)
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    return "minimized"


def hide_window(hwnd: int) -> str:
    """「后」：把窗口收起来（不挂托盘图标）。再点一次就放出来。

    返回 "hidden" / "shown" / "gone"。
    """
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        _forget(hwnd)
        _hidden.discard(hwnd)
        return "gone"
    if hwnd in _hidden or not user32.IsWindowVisible(hwnd):
        _show(hwnd)
        _bring_front(hwnd)
        return "shown"
    _forget(hwnd)
    user32.ShowWindow(hwnd, SW_HIDE)
    _hidden.add(hwnd)
    return "hidden"


def close_window(hwnd: int) -> bool:
    """发 WM_CLOSE，走程序自己的关闭流程（该弹「要保存吗」就弹）。"""
    hwnd = int(hwnd)
    _forget(hwnd)
    _hidden.discard(hwnd)
    if not user32.IsWindow(hwnd):
        return False
    return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


def restore(hwnd: int) -> bool:
    """把窗口放回分屏前的原位。"""
    hwnd = int(hwnd)
    place = _saved.pop(hwnd, None)
    _snapped.pop(hwnd, None)
    if place is None or not user32.IsWindow(hwnd):
        return False
    if not user32.SetWindowPlacement(hwnd, ctypes.byref(place)):
        return False
    user32.SetForegroundWindow(hwnd)
    return True


def restore_all() -> int:
    """把所有贴过的窗口都放回去（「⋯ → 全部还原」用）。"""
    count = 0
    for hwnd in list(_snapped):
        if restore(hwnd):
            count += 1
    return count
