"""桌面工作台 —— 程序入口。"""

from __future__ import annotations

import ctypes
import os
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from PySide6.QtCore import QLibraryInfo, Qt, QTranslator  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def _install_translations(app) -> QTranslator | None:
    """加载 Qt 自带的中文翻译，让标准对话框的按钮显示「是/否/取消」。"""
    translator = QTranslator(app)
    folders = [
        QLibraryInfo.path(QLibraryInfo.TranslationsPath),
        os.path.join(BASE_DIR, "translations"),
    ]
    for folder in folders:
        if folder and translator.load("qtbase_zh_CN", folder):
            app.installTranslator(translator)
            return translator
    return None


def _mutex_handle():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.SetLastError(0)          # 必须清零，否则会读到上一步残留的 183
    handle = kernel32.CreateMutexW(None, False, "DesktopWorkbench_SingleInstance_v1")
    return handle, ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def _install_excepthook() -> None:
    log_dir = os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "SideExplorer")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "error.log")

    def hook(exc_type, exc_value, exc_tb):
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("=" * 60 + "\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=fh)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def main() -> int:
    if os.environ.get("SIDEEXPLORER_DEBUG"):
        import faulthandler
        print("[boot] enter main", flush=True)
        faulthandler.dump_traceback_later(8, repeat=False)

    handle, exists = _mutex_handle()
    if exists:
        return 0
    _ = handle  # 保持引用，进程退出时由系统释放

    _install_excepthook()

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("DesktopWorkbench")
    app.setApplicationDisplayName("桌面工作台")
    app.setQuitOnLastWindowClosed(False)
    _translator = _install_translations(app)     # 保持引用，避免被回收
    if os.environ.get("SIDEEXPLORER_DEBUG"):
        print("[boot] qapp ready", flush=True)

    from ui import SideExplorer  # 延迟导入，确保 QApplication 已存在
    if os.environ.get("SIDEEXPLORER_DEBUG"):
        print("[boot] ui imported", flush=True)

    window = SideExplorer()
    if os.environ.get("SIDEEXPLORER_DEBUG"):
        print("[boot] window built", flush=True)
    window.start()
    if os.environ.get("SIDEEXPLORER_DEBUG"):
        print("[boot] window started", flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
