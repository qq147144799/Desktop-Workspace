"""以邮件附件发送：把文件交给系统里默认的邮件客户端。

为什么不用 Simple MAPI：它的 LPSTR 是 ANSI 接口，64 位进程要去加载 32 位的
``FMMAPI32.dll``，实测时通时不通 —— 有时憋出一个隐藏的写邮件窗口、有时直接
返回 MAPI_E_FAILURE，``Mail Recipient.MAPIMail`` 更是报 1155（没有关联应用）。
所以改成「找出默认客户端的 exe，用它自己的命令行」：

- Foxmail：``Foxmail.exe /min <文件…>``。这正是它往「发送到」菜单里注册的那条，
  实测 0.5 秒弹出写邮件窗口、附件已挂好、主题还自动取了文件名。
- 其它客户端：``mailto:`` 打开写邮件，同时把文件放进剪贴板（CF_HDROP），
  按一次 Ctrl+V 就挂成附件 —— 任何客户端都成立，不用为每家写一套参数。
"""

from __future__ import annotations

import os
import subprocess
import winreg

# 客户端名里出现这些词，就按对应主程序名去找
_KNOWN = (
    ("foxmail", "Foxmail.exe"),
    ("outlook", "OUTLOOK.EXE"),
    ("thunderbird", "thunderbird.exe"),
    ("windows mail", "HxOutlook.exe"),
)


def default_client_name() -> str:
    """系统默认邮件客户端的名字；用户级设置优先（Foxmail 就注册在 HKCU）。"""
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"Software\Clients\Mail") as key:
                name = str(winreg.QueryValueEx(key, "")[0]).strip()
                if name:
                    return name
        except OSError:
            continue
    return ""


def _first_token(command: str) -> str:
    """从 `"...\\Foxmail.exe" /min "%1"` 里抠出 exe 路径。"""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 0 else command.strip('"')
    return command.split(" ", 1)[0]


def _guesses(name: str) -> list[str]:
    low = name.lower()
    out = [exe for word, exe in _KNOWN if word in low]
    compact = name.replace(" ", "")
    out += [name + ".exe", compact + ".exe"]
    return out


def _exe_near(folder: str, name: str) -> str:
    """在 folder 及往上两级里找主程序（Foxmail 的 DllPath 常指向版本子目录）。"""
    for _ in range(3):
        if not folder:
            break
        for guess in _guesses(name):
            path = os.path.join(folder, guess)
            if os.path.isfile(path):
                return path
        folder = os.path.dirname(folder)
    return ""


def client_exe(name: str = "") -> str:
    """默认客户端的 exe。找不到返回空串，由调用方降级。"""
    name = name or default_client_name()
    if not name:
        return ""
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        base = rf"Software\Clients\Mail\{name}"
        # 1) 标准注册：shell\open\command
        try:
            with winreg.OpenKey(root, base + r"\shell\open\command") as key:
                exe = _first_token(str(winreg.QueryValueEx(key, "")[0]))
                if os.path.isfile(exe):
                    return exe
        except OSError:
            pass
        # 2) Foxmail 只登记了 MAPI DLL 的路径（DllPath），主程序就在它旁边
        try:
            with winreg.OpenKey(root, base) as key:
                dll = str(winreg.QueryValueEx(key, "DllPath")[0])
        except OSError:
            continue
        if dll:
            exe = _exe_near(os.path.dirname(dll), name)
            if exe:
                return exe
    return ""


def client_label() -> str:
    """状态栏/菜单里显示的名字；认不出来就说「邮件客户端」。"""
    name = default_client_name()
    if "foxmail" in name.lower():
        return "Foxmail"
    return name or "邮件客户端"


def _copy_to_clipboard(paths: list[str]) -> bool:
    try:
        import fileops                       # 延迟导入，免得模块互相牵扯
        return fileops.set_files(paths)
    except Exception:                        # noqa: BLE001 —— 剪贴板失败不该拦住房主流程
        return False


def send_files(paths: list[str]) -> tuple[bool, str]:
    """打开写邮件窗口并把文件挂成附件。返回 (是否成功, 给用户看的一句话)。"""
    real = [os.path.abspath(p) for p in paths if p and os.path.isfile(p)]
    if not real:
        return False, "文件夹不能作为邮件附件"

    name = default_client_name()
    exe = client_exe(name)
    count = len(real)

    if exe and "foxmail" in f"{name} {os.path.basename(exe)}".lower():
        try:
            subprocess.Popen([exe, "/min", *real], cwd=os.path.dirname(exe))
        except OSError as exc:
            return False, f"启动 Foxmail 失败：{exc}"
        return True, f"已在 Foxmail 打开写邮件窗口（{count} 个附件）"

    # 其它客户端：mailto 打开写邮件 + 文件进剪贴板，按一次 Ctrl+V 就挂上
    copied = _copy_to_clipboard(real)
    try:
        os.startfile("mailto:")
    except OSError as exc:
        if copied:
            return True, f"没找到可用的邮件客户端，{count} 个文件已放进剪贴板（{exc}）"
        return False, f"没有可用的邮件客户端：{exc}"
    who = client_label()
    if copied:
        return True, f"已在 {who} 打开写邮件，按 Ctrl+V 把 {count} 个文件挂成附件"
    return True, f"已在 {who} 打开写邮件窗口"
