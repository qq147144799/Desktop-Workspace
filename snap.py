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

# SetWindowPos 的 hWndInsertAfter 是指针宽度：直接传 -1 当 c_int 会在 64 位下
# 变成 0x00000000FFFFFFFF，系统认不出 HWND_TOPMOST，置顶就静默失败。
user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT)
user32.SetWindowPos.restype = wintypes.BOOL

SPI_GETWORKAREA = 0x0030
SW_RESTORE = 9
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_HIDE = 0
SW_SHOW = 5
SW_SHOWNOACTIVATE = 4
WM_CLOSE = 0x0010
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
HWND_TOP = ctypes.c_void_p(0)
HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_NOTOPMOST = ctypes.c_void_p(-2)
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008

SIDES = ("left", "right")

_snapped: dict[int, str] = {}          # hwnd -> 当前贴的方向
_saved: dict[int, "Placement"] = {}    # hwnd -> 贴之前的原位
_hidden: set[int] = set()              # 被「后」收起来的窗口（列表里还得显示）
_flags: dict[int, set[str]] = {}       # hwnd -> 用户给这个窗口设的常驻标记

# 标记说明：
#   "topmost"    窗口置顶
#   "background" 后台运行：点「X」不关窗，改成收起来
FLAG_TOPMOST = "topmost"
FLAG_BACKGROUND = "background"


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
    if user32.IsZoomed(hwnd) or _fills_work_area(hwnd):
        return "max"                 # 手动铺满的（Foxmail）也算最大化
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


def prune_flags() -> None:
    """窗口关掉了，它身上的常驻标记也跟着作废。"""
    for hwnd in list(_flags):
        if not user32.IsWindow(hwnd):
            _flags.pop(hwnd, None)


# ---------------------------------------------------------------- 常驻标记
def flags_of(hwnd: int) -> set[str]:
    """这个窗口被设了哪些常驻标记（topmost / background）。"""
    return set(_flags.get(int(hwnd), ()))


def has_flag(hwnd: int, name: str) -> bool:
    return name in _flags.get(int(hwnd), ())


def set_flag(hwnd: int, name: str, on: bool = True) -> None:
    hwnd = int(hwnd)
    flags = _flags.setdefault(hwnd, set())
    if on:
        flags.add(name)
    else:
        flags.discard(name)
    if not flags:
        _flags.pop(hwnd, None)


def is_topmost(hwnd: int) -> bool:
    """问系统本体，别只信标记 —— 用户可能自己把置顶取消了。"""
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        return False
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    return bool(style & WS_EX_TOPMOST)


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
def _fills_work_area(hwnd: int) -> bool:
    """窗口是不是正好铺满工作区（用来判断「已经是最大化」）。"""
    left, top, width, height = work_area()
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    return (abs(rect.left - left) <= 8 and abs(rect.top - top) <= 8
            and abs((rect.right - rect.left) - width) <= 16
            and abs((rect.bottom - rect.top) - height) <= 16)


def _fill_work_area(hwnd: int) -> bool:
    """手动把窗口摆满工作区（不经过系统的最大化流程）。"""
    left, top, width, height = work_area()
    return bool(user32.SetWindowPos(hwnd, HWND_TOP, left, top, width, height,
                                    SWP_NOACTIVATE))


def _manual_max(hwnd: int) -> bool:
    """这个窗口的最大化要自己铺，不能用 SW_MAXIMIZE。

    Foxmail 在 ``WM_GETMINMAXINFO`` 里给出的是屏幕右下的坐标，系统最大化会把它
    摆到 (960,540) 这种超出屏幕的位置（实测 zoomed 为真、窗口却看不见）。
    """
    import closetotray
    return "foxmail" in closetotray.exe_of_window(hwnd)


def snap(hwnd: int, side: str) -> str:
    """贴到指定半边；已经在同一边就什么都不做（不再来回切换）。

    返回 ``"left"`` / ``"right"``（贴上了）、``"same"``（本来就在这一边）、
    ``"gone"``（窗口没了）或 ``"failed"``。
    """
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        _forget(hwnd)
        _hidden.discard(hwnd)
        return "gone"

    if _snapped.get(hwnd) == side:        # 已经是这个状态，保持不动
        return "same"

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
    """最大化；已经最大化就保持不动。返回 "maximized" / "same" / "gone" / "failed"。"""
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        _forget(hwnd)
        _hidden.discard(hwnd)
        return "gone"
    _show(hwnd)                        # 最小化的先叫回来（Foxmail 就是最小化到托盘的）

    if _manual_max(hwnd):
        # Foxmail 这类程序：系统最大化会摆到屏幕外，只能自己铺满；
        # 而且它可能已经带着「zoomed」标记却停在屏幕外，所以判据是「真的铺满了没有」
        if _fills_work_area(hwnd):
            _bring_front(hwnd)
            return "same"
        _forget(hwnd)
        if not _fill_work_area(hwnd):
            return "failed"
        _bring_front(hwnd)
        return "maximized"

    if user32.IsZoomed(hwnd) or _fills_work_area(hwnd):
        _bring_front(hwnd)                # 已经是最大，只把它叫到前面
        return "same"
    _forget(hwnd)                     # 之前贴在半边的话，最大化后就作废了
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    _bring_front(hwnd)
    return "maximized"


def minimize(hwnd: int) -> str:
    """最小化；已经最小化就保持不动。返回 "minimized" / "same" / "gone"。"""
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        _forget(hwnd)
        _hidden.discard(hwnd)
        return "gone"
    if user32.IsIconic(hwnd) and hwnd not in _hidden:
        return "same"
    _forget(hwnd)
    _hidden.discard(hwnd)
    # 对我们收起来的窗口，SW_MINIMIZE 会让它以「最小化」的形式回到任务栏
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


def activate(hwnd: int) -> str:
    """像点任务栏那样把窗口叫到前台：隐藏的显示、最小化的还原、最大化的保持。"""
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        _forget(hwnd)
        _hidden.discard(hwnd)
        _flags.pop(hwnd, None)
        return "gone"
    _show(hwnd)
    _bring_front(hwnd)
    return "ok"


def set_topmost(hwnd: int, on: bool) -> str:
    """置顶 / 取消置顶，并把这个选择记成窗口的常驻标记。"""
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        _forget(hwnd)
        _hidden.discard(hwnd)
        _flags.pop(hwnd, None)
        return "gone"
    ok = user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST,
                             0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    if not ok:
        return "failed"
    set_flag(hwnd, FLAG_TOPMOST, on)
    return FLAG_TOPMOST if on else "normal"


def close_window(hwnd: int) -> bool:
    """发 WM_CLOSE，走程序自己的关闭流程（该弹「要保存吗」就弹）。"""
    hwnd = int(hwnd)
    _forget(hwnd)
    _hidden.discard(hwnd)
    if not user32.IsWindow(hwnd):
        return False
    return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


def close_or_hide(hwnd: int) -> str:
    """点「X」：设了「后台运行」的窗口只收起来，不真关。

    返回 "closed"（发了关闭消息）/ "hidden"（转到后台）/ "gone"（窗口没了）。
    """
    hwnd = int(hwnd)
    if has_flag(hwnd, FLAG_BACKGROUND):
        if hwnd in _hidden or not user32.IsWindowVisible(hwnd):
            return "hidden"                 # 已经在后台了，再点一次什么都不做
        if hide_window(hwnd) == "hidden":
            return "hidden"
        return "gone"
    return "closed" if close_window(hwnd) else "gone"


def revive_background() -> list[int]:
    """后台运行模式下被「窗口自己的 X」关掉的窗口，重新拉出来。

    跨进程拦不住 WM_CLOSE（那要往对方进程注入代码），但 Foxmail / 微信这类托盘程序
    点 X 只是把自己藏起来，窗口对象还在 —— 所以能在这里发现并叫回来。
    真退出进程的救不了，只能等它从列表消失。

    返回被拉回来的 hwnd 列表。
    """
    revived = []
    for hwnd in list(_flags):
        if not has_flag(hwnd, FLAG_BACKGROUND):
            continue
        if not user32.IsWindow(hwnd):
            _flags.pop(hwnd, None)          # 已经真关了，标记作废
            continue
        if hwnd in _hidden or user32.IsWindowVisible(hwnd):
            continue                        # 我们自己收起来的，或本来就好好显示着
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        revived.append(hwnd)
    return revived


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
