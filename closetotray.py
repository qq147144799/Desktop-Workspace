"""把**别的程序**的关闭按钮改成「缩到托盘」。

原理：装一个 WH_MOUSE_LL 低级鼠标钩子，在点击的瞬间判断落点是不是目标的
关闭按钮（用 WM_NCHITTEST 反查，返回 HTCLOSE 即是），是就把这次点击吞掉，
并通知主界面把那个窗口隐藏起来。

注意：低级钩子必须装在**有消息循环的线程**上（这里就是 Qt 主线程），
回调里不能做耗时操作，否则系统会认为钩子超时并把它摘掉。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import POINTER, wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
except OSError:
    dwmapi = None

WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_NCHITTEST = 0x0084
HTCLOSE = 20
GA_ROOT = 2
SMTO_ABORTIFHUNG = 0x0002
SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9

SM_CYCAPTION = 4
SM_CXSIZEFRAME = 32
SM_CXPADDEDBORDER = 92
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wt.UINT), ("flags", wt.UINT), ("showCmd", wt.UINT),
                ("ptMinPosition", wt.POINT), ("ptMaxPosition", wt.POINT),
                ("rcNormalPosition", wt.RECT), ("rcDevice", wt.RECT)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


# 自绘标题栏的程序拿不到 HTCLOSE，只能按实测区域兜底：(按钮宽, 标题栏高)@96DPI
_DBG_PATH = os.path.join(os.environ.get("TEMP", "."), "ctt_debug.log")


def _dbg(msg: str) -> None:
    try:
        with open(_DBG_PATH, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass


CUSTOM_CLOSE = {
    "et.exe": (40, 40),
    "wps.exe": (40, 40),
    "wpp.exe": (40, 40),
    "wechat.exe": (46, 36),
    "weixin.exe": (46, 36),
}


def _metric(index: int, hwnd: int | None = None) -> int:
    try:
        if hwnd:
            v = user32.GetSystemMetricsForDpi(index, user32.GetDpiForWindow(hwnd) or 96)
            if v:
                return v
    except Exception:
        pass
    return user32.GetSystemMetrics(index)


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


def list_windows(exclude_pid: int | None = None) -> list[dict]:
    """枚举当前所有有标题的顶层窗口，按 exe 归并。

    返回 ``[{"exe", "exe_path", "title", "hwnd", "count"}]``，按 exe 名排序。
    过滤掉了工具窗口、UWP 幽灵窗口、以及 ``exclude_pid`` 自己的窗口。
    """
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080

    found: dict[str, dict] = {}

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex_style & WS_EX_TOOLWINDOW and not ex_style & WS_EX_APPWINDOW:
            return True
        owner = user32.GetWindow(hwnd, GW_OWNER)
        if owner and not ex_style & WS_EX_APPWINDOW:
            return True
        if _is_cloaked(hwnd):
            return True
        if cls := ctypes.create_unicode_buffer(64):
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
                return True

        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if exclude_pid and pid.value == exclude_pid:
            return True

        exe = exe_of_window(hwnd)
        if not exe:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)

        entry = found.get(exe)
        if entry is None:
            found[exe] = {
                "exe": exe,
                "exe_path": exe_path_of_window(hwnd),
                "title": title.value,
                "hwnd": int(hwnd),
                "count": 1,
            }
        else:
            entry["count"] += 1
            if len(title.value) > len(entry["title"]):
                entry["title"] = title.value
                entry["hwnd"] = int(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return sorted(found.values(), key=lambda d: (d["exe"], d["title"].lower()))


def _in_close_zone(hwnd: int, x: int, y: int) -> bool:
    """便宜的几何预筛：只处理标题栏右上角那一小片，其它点击直接放过。"""
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    if not (r.left <= x < r.right and r.top <= y < r.bottom):
        return False
    cap = _metric(SM_CYCAPTION, hwnd) or 23
    frame = (_metric(SM_CXSIZEFRAME, hwnd) or 8) + (_metric(SM_CXPADDEDBORDER, hwnd) or 4)
    bottom = max(cap + frame + 4, int(cap * 2.0))
    return x >= r.right - int(cap * 3.2) and y <= r.top + bottom


def _geom_is_close(hwnd: int, x: int, y: int) -> bool:
    """目标无响应时的几何兜底：宁可漏拦，也不要把「最大化」误判成关闭。"""
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    cap = _metric(SM_CYCAPTION, hwnd) or 23
    frame = (_metric(SM_CXSIZEFRAME, hwnd) or 8) + (_metric(SM_CXPADDEDBORDER, hwnd) or 4)
    left = r.right - frame - int(cap * 1.6)
    return left <= x < r.right and r.top <= y < r.top + frame + cap


def _custom_hit(hwnd: int, x: int, y: int, exe: str) -> bool:
    spec = CUSTOM_CLOSE.get(exe)
    if not spec:
        return False
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    try:
        dpi = user32.GetDpiForWindow(hwnd) or 96
    except Exception:
        dpi = 96
    s = dpi / 96.0
    bw, bh = int(spec[0] * s), int(spec[1] * s)
    return r.right - bw <= x < r.right and r.top <= y < r.top + bh


def is_close_click(hwnd: int, x: int, y: int) -> bool:
    """问目标窗口「这个点是什么」，答 HTCLOSE 就是关闭按钮。"""
    lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    result = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(hwnd, WM_NCHITTEST, 0, lparam,
                                    SMTO_ABORTIFHUNG, 50, ctypes.byref(result))
    if ok:
        return int(result.value) == HTCLOSE
    return _geom_is_close(hwnd, x, y)


class CloseToTray:
    """目标窗口的关闭点击 → 隐藏到托盘。"""

    def __init__(self, on_hide=None):
        self.targets: list[str] = []          # exe 名，小写
        self.hidden: dict[int, str] = {}      # hwnd -> 标题
        self._placements: dict[int, WINDOWPLACEMENT] = {}   # hwnd -> 隐藏前的位置与状态
        self.paused = False
        self._hook = None
        self._proc = HOOKPROC(self._mouse_proc)
        self._on_hide = on_hide

    # ------------------------------------------------------------ 钩子
    def install(self) -> bool:
        self.uninstall()
        # 低级鼠标钩子的回调在「安装它的进程」里执行，所以 hMod 必须传 NULL；
        # 传进程模块句柄会报 126（找不到指定的模块）
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
        return bool(self._hook)

    def uninstall(self) -> None:
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    @property
    def active(self) -> bool:
        return bool(self._hook) and bool(self.targets) and not self.paused

    def _mouse_proc(self, nCode, wParam, lParam):
        # 这个回调在钩子链上，必须几毫秒内返回，重活一律丢给主线程
        try:
            if nCode == 0 and not self.paused and self.targets and wParam == WM_LBUTTONDOWN:
                info = ctypes.cast(lParam, POINTER(MSLLHOOKSTRUCT)).contents
                x, y = info.pt.x, info.pt.y
                hwnd = user32.WindowFromPoint(wt.POINT(x, y))
                if hwnd:
                    root = int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
                    if self._should_take(root, x, y):
                        if self._on_hide is not None:
                            self._on_hide(root)
                        return 1          # 吞掉这次点击，窗口就不会收到关闭
        except Exception:
            pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _should_take(self, hwnd: int, x: int, y: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return False
        exe = exe_of_window(hwnd)
        if not exe or exe not in self.targets:
            return False                      # 不是目标程序，立刻放过（钩子里要快）
        if not _in_close_zone(hwnd, x, y):
            return False
        hit = is_close_click(hwnd, x, y) or _custom_hit(hwnd, x, y, exe)
        if hit:
            _dbg(f"take exe={exe} hwnd={hwnd}")
        return hit

    # ------------------------------------------------------------ 隐藏 / 恢复
    def hide_window(self, hwnd: int) -> bool:
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return False
        title = self._title(hwnd) or exe_of_window(hwnd) or "未命名窗口"
        # 先把位置/最大化状态记下来，不然恢复时 SW_RESTORE 会把最大化的窗口缩回去
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(wp)
        if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            self._placements[hwnd] = wp
        user32.ShowWindow(hwnd, SW_HIDE)
        self.hidden[hwnd] = title
        return True

    def restore_window(self, hwnd: int) -> bool:
        if not user32.IsWindow(hwnd):
            self.hidden.pop(hwnd, None)
            self._placements.pop(hwnd, None)
            return False
        # 用隐藏前记下的位置还原：最大化还是最大化，大小位置都不变
        wp = self._placements.pop(hwnd, None)
        if wp is not None:
            wp.length = ctypes.sizeof(wp)
            user32.SetWindowPlacement(hwnd, ctypes.byref(wp))
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.SetForegroundWindow(hwnd)
        self.hidden.pop(hwnd, None)
        return True

    def restore_all(self) -> None:
        for hwnd in list(self.hidden):
            self.restore_window(hwnd)

    def prune(self) -> None:
        """清掉已经被关掉（窗口不存在）的条目。"""
        for hwnd in list(self.hidden):
            if not user32.IsWindow(hwnd):
                self.hidden.pop(hwnd, None)
                self._placements.pop(hwnd, None)

    @staticmethod
    def _title(hwnd: int) -> str:
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value.strip()

    # ------------------------------------------------------------ 目标管理
    def add_target(self, exe: str) -> bool:
        exe = (exe or "").strip().lower()
        if not exe or exe in self.targets:
            return False
        self.targets.append(exe)
        return True

    def remove_target(self, exe: str) -> bool:
        exe = (exe or "").strip().lower()
        if exe in self.targets:
            self.targets.remove(exe)
            return True
        return False
