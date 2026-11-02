"""定时关机 / 重启：交给系统自己的 shutdown.exe。

为什么不自己起 QTimer：面板一退出定时就没了。交给系统之后，关机计划是独立于
本进程的 —— 关掉面板、甚至进程被杀，到点照样关机。取消同样走 ``shutdown /a``，
所以这里只负责「下命令 + 记住时间点」，不做别的。

Windows 的 ``shutdown /a`` 只能撤销通过 shutdown 下的计划，这点正好对上。
"""

from __future__ import annotations

import os
import subprocess
import time

DEADLINE_KEY = "shutdown_deadline"      # 到点关机的 Unix 时间戳（0 / 缺失 = 没有计划）
MODE_KEY = "shutdown_mode"              # "shutdown" / "restart"

CREATE_NO_WINDOW = 0x08000000           # 别让 shutdown.exe 闪一下黑框
MAX_SECONDS = 315_360_000               # shutdown.exe 自己接受的上限（10 年）


def clock(seconds: int) -> str:
    """把剩余秒数写成 29:58 / 1:05:30。"""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _decode(raw: bytes) -> str:
    """shutdown.exe 往控制台写的是 OEM 代码页（中文机器是 GBK），不是 UTF-8。

    直接 ``text=True`` 会在读取线程里抛 UnicodeDecodeError，stdout 变成 None，
    错误信息就彻底丢了 —— 所以这里按字节读、逐个编码试。
    """
    for encoding in ("oem", "mbcs", "utf-8"):
        try:
            return raw.decode(encoding).strip()
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace").strip()


def _run(args: list[str]) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "只支持 Windows"
    try:
        done = subprocess.run(args, capture_output=True,
                              creationflags=CREATE_NO_WINDOW, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode == 0:
        return True, ""
    if done.returncode == 1116:             # ERROR_SHUTDOWN_IS_SCHEDULED
        return False, "当前没有进行中的定时关机"
    message = _decode(done.stdout or done.stderr or b"")
    return False, message or f"shutdown 返回 {done.returncode}"


# ---------------------------------------------------------------- 对外接口
def schedule(config, seconds: int, restart: bool = False) -> tuple[bool, str]:
    """安排一次关机；``seconds`` 是相对现在的秒数。"""
    seconds = max(0, min(int(seconds), MAX_SECONDS))
    flag = "/r" if restart else "/s"
    _run(["shutdown", "/a"])               # 已有计划时先撤掉，否则 /s 会直接报「已经在计划中」
    ok, message = _run(["shutdown", flag, "/t", str(seconds),
                        "/c", "桌面工作台：定时关机"])
    if not ok:
        return False, message
    config.setValue(DEADLINE_KEY, int(time.time()) + seconds)
    config.setValue(MODE_KEY, "restart" if restart else "shutdown")
    return True, ""


def cancel(config) -> tuple[bool, str]:
    """撤销系统里的关机计划。没有计划时关机照样执行 ``/a``，会返回「没有进行中的关闭」。"""
    ok, message = _run(["shutdown", "/a"])
    config.setValue(DEADLINE_KEY, 0)
    config.setValue(MODE_KEY, "")
    return ok, message


def deadline(config) -> float:
    try:
        return float(config.value(DEADLINE_KEY, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def remaining(config) -> int:
    left = deadline(config) - time.time()
    return max(0, int(round(left)))


def mode(config) -> str:
    value = config.value(MODE_KEY, "shutdown", str) or "shutdown"
    return value if value in ("shutdown", "restart") else "shutdown"


def is_pending(config) -> bool:
    return remaining(config) > 0


def verb(config) -> str:
    return "重启" if mode(config) == "restart" else "关机"


def describe(config) -> str:
    """一句话说明当前计划：``21:27 关机（剩 29:58）``；没有计划返回空串。"""
    left = remaining(config)
    if left <= 0:
        return ""
    when = time.strftime("%H:%M", time.localtime(deadline(config)))
    return f"{when} {verb(config)}（剩 {clock(left)}）"


def tidy(config) -> None:
    """启动时清掉已经过期的时间戳（比如上次定时后电脑被硬关了）。"""
    if deadline(config) and remaining(config) <= 0:
        config.setValue(DEADLINE_KEY, 0)
        config.setValue(MODE_KEY, "")
