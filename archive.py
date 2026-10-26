"""
压缩包当作文件夹浏览。

路径表示沿用资源管理器那一套：`D:\资料\归档.zip\子目录\文件.txt`，
也就是「存在的压缩包文件」之后的全部内容都算包内路径。

- **zip**：用内置 zipfile，可读可写，改完自动写回
- **7z / rar / tar / gz 等**：调用本机 7-Zip（7z.exe），只读浏览 + 解压；
  这些格式不支持自动写回
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime

# 只要是「单层」压缩包都能当文件夹浏览。
# 像 .tar.gz 这种套娃格式不列在内，否则只会看到里面那个 .tar
ARCHIVE_EXTS = {
    ".zip", ".7z", ".rar", ".tar",
    ".cab", ".iso", ".wim", ".arj", ".lzh",
}

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_SEVENZIP = None
_SEVENZIP_PROBED = False


def _find_sevenzip():
    """找本机 7-Zip；找不到就返回 None（zip 仍可用）。"""
    global _SEVENZIP, _SEVENZIP_PROBED
    if _SEVENZIP_PROBED:
        return _SEVENZIP

    for path in (r"C:\Program Files\7-Zip\7z.exe",
                 r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.isfile(path):
            _SEVENZIP = path
            break
    else:
        _SEVENZIP = shutil.which("7z") or shutil.which("7za")

    _SEVENZIP_PROBED = True
    return _SEVENZIP


def is_archive(path: str) -> bool:
    """是不是能当文件夹打开的压缩包。"""
    if not path:
        return False
    ext = os.path.splitext(str(path))[1].lower()
    if ext not in ARCHIVE_EXTS:
        return False
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def is_writable(path: str) -> bool:
    """只有 zip 支持改完自动写回。"""
    return str(path).lower().endswith(".zip")


def split_path(path: str):
    """拆成 (压缩包路径, 包内路径)。不是包内路径就返回 None。"""
    if not path:
        return None
    text = str(path)
    for pos, ch in enumerate(text):
        if ch in "\\/" and pos > 2:
            head = text[:pos]
            if is_archive(head):
                return head, text[pos + 1:].replace("\\", "/")
    if is_archive(text):
        return text, ""
    return None


def join(archive_path: str, inner: str, name: str = "") -> str:
    inner = inner.strip("/")
    parts = [p for p in (inner, name) if p]
    tail = "/".join(parts)
    return archive_path + ("\\" + tail.replace("/", "\\") if tail else "")


def parent_of(archive_path: str, inner: str) -> str:
    inner = inner.strip("/")
    if not inner:
        return os.path.dirname(archive_path) or archive_path
    parts = inner.split("/")
    if len(parts) <= 1:
        return archive_path
    return archive_path + "\\" + "\\".join(parts[:-1])


# --------------------------------------------------------------------------- 列举

def _stamp(text: str) -> float:
    text = (text or "").strip()
    if not text:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(datetime.strptime(text[:19], fmt).timetuple())
        except ValueError:
            continue
    return 0.0


def _zip_entries(path: str):
    out = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            is_dir = name.endswith("/")
            try:
                stamp = time.mktime(info.date_time + (0, 0, -1))
            except (ValueError, OverflowError):
                stamp = time.time()
            out.append((name.rstrip("/"), is_dir, info.file_size, stamp))
    return out


def _seven_entries(path: str):
    exe = _find_sevenzip()
    if not exe:
        raise OSError("这台机器上没有 7-Zip，打不开这种格式")
    proc = subprocess.run(
        [exe, "l", "-slt", "-ba", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "读取压缩包失败").strip().splitlines()
        raise OSError(tail[-1] if tail else "读取压缩包失败")

    blocks, cur = [], {}
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line:
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            cur[key.strip()] = value.strip()
    if cur:
        blocks.append(cur)

    out = []
    for info in blocks:
        name = info.get("Path", "")
        if not name or name in (".", ".."):
            continue
        attrs = info.get("Attributes", "")
        is_dir = info.get("Folder", "").startswith("+") or attrs.startswith("D")
        try:
            size = int(info.get("Size", "0") or 0)
        except ValueError:
            size = 0
        out.append((name.replace("\\", "/").rstrip("/"), is_dir, size,
                    _stamp(info.get("Modified", ""))))
    return out


def _all_entries(path: str):
    if str(path).lower().endswith(".zip"):
        return _zip_entries(path)
    return _seven_entries(path)


def list_entries(archive_path: str, inner: str = "", show_hidden: bool = False):
    """返回 [(名称, 是否目录, 大小, mtime)]，只列当前这一层。"""
    prefix = inner.strip("/")
    prefix = prefix + "/" if prefix else ""

    dirs: dict[str, int] = {}
    files: list[tuple[str, bool, int, float]] = []

    for name, is_dir, size, mtime in _all_entries(archive_path):
        name = name.replace("\\", "/")
        if prefix and not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        if not rest:
            continue
        if not show_hidden and rest.startswith("."):
            continue
        if is_dir or "/" in rest:
            sub = rest.split("/")[0]
            if sub and (show_hidden or not sub.startswith(".")):
                dirs[sub] = dirs.get(sub, 0) + size
        else:
            files.append((rest, False, size, mtime))

    result = [(name, True, size, 0.0) for name, size in dirs.items()]
    result.extend(files)
    return result


# --------------------------------------------------------------------------- 读 / 写

def read_file(archive_path: str, inner: str) -> bytes:
    if str(archive_path).lower().endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            return zf.read(inner)
    exe = _find_sevenzip()
    if not exe:
        raise OSError("没有 7-Zip")
    proc = subprocess.run([exe, "x", "-so", archive_path, inner],
                          capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise OSError("读取失败")
    return proc.stdout


def temp_root() -> str:
    return os.path.join(tempfile.gettempdir(), "DesktopWorkbench")


def extract_to_temp(archive_path: str, inner: str) -> str:
    """把包内文件解到临时目录，返回本地路径（供默认程序打开）。"""
    stem = os.path.splitext(os.path.basename(archive_path))[0]
    target = os.path.join(temp_root(), stem, inner.replace("/", os.sep))
    os.makedirs(os.path.dirname(target), exist_ok=True)

    if str(archive_path).lower().endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf, zf.open(inner) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return target

    exe = _find_sevenzip()
    if not exe:
        raise OSError("没有 7-Zip")
    proc = subprocess.run(
        [exe, "x", archive_path, f"-o{os.path.dirname(target)}", inner, "-y"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0 or not os.path.isfile(target):
        tail = (proc.stderr or "").strip().splitlines()
        raise OSError(tail[-1] if tail else "解压失败")
    return target


def write_back(archive_path: str, inner: str, local_path: str) -> None:
    """把本地改好的文件写回压缩包（仅 zip；重建整包保证同名条目被替换）。"""
    if not str(archive_path).lower().endswith(".zip"):
        raise OSError("这种格式不支持自动写回")
    inner = inner.replace("\\", "/")
    tmp = archive_path + ".dwtmp"
    with zipfile.ZipFile(archive_path) as src:
        infos = src.infolist()
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            replaced = False
            for info in infos:
                if info.filename.replace("\\", "/") == inner:
                    dst.write(local_path, inner)
                    replaced = True
                else:
                    dst.writestr(info, src.read(info.filename))
            if not replaced:
                dst.write(local_path, inner)
    os.replace(tmp, archive_path)


def _safe_target(dest_dir: str, member: str):
    name = member.replace("\\", "/")
    if name.startswith("/") or ".." in name.split("/"):
        return None
    return os.path.join(dest_dir, name.replace("/", os.sep))


def extract_all(archive_path: str, dest_dir: str) -> int:
    """整包解压，跳过越界路径。返回解出的文件数。"""
    os.makedirs(dest_dir, exist_ok=True)

    if str(archive_path).lower().endswith(".zip"):
        count = 0
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                target = _safe_target(dest_dir, info.filename)
                if target is None:
                    continue
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                count += 1
        return count

    exe = _find_sevenzip()
    if not exe:
        raise OSError("没有 7-Zip")
    proc = subprocess.run(
        [exe, "x", archive_path, f"-o{dest_dir}", "-y"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise OSError("解压失败")
    count = 0
    for _root, _dirs, files in os.walk(dest_dir):
        count += len(files)
    return count


def extract_member(archive_path: str, inner: str, dest_dir: str) -> str:
    """把包内单个条目解到指定目录。"""
    name = inner.replace("\\", "/").rstrip("/")
    base_target = _safe_target(dest_dir, os.path.basename(name))
    if base_target is None:
        raise OSError("包内路径不合法")

    if not str(archive_path).lower().endswith(".zip"):
        out_parent = os.path.dirname(base_target)
        os.makedirs(out_parent, exist_ok=True)
        exe = _find_sevenzip()
        if not exe:
            raise OSError("没有 7-Zip")
        subprocess.run([exe, "x", archive_path, f"-o{out_parent}", name, "-y"],
                       capture_output=True, creationflags=_NO_WINDOW)
        return base_target

    os.makedirs(os.path.dirname(base_target), exist_ok=True)
    with zipfile.ZipFile(archive_path) as zf:
        names = zf.namelist()
        if name + "/" in names:
            for entry in names:
                if not entry.startswith(name + "/") or entry.endswith("/"):
                    continue
                sub_target = _safe_target(dest_dir, entry[len(name) + 1:])
                if sub_target is None:
                    continue
                os.makedirs(os.path.dirname(sub_target), exist_ok=True)
                with zf.open(entry) as src, open(sub_target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            return base_target
        with zf.open(name) as src, open(base_target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return base_target


class Cancelled(Exception):
    """压缩过程中被用户取消。"""


def _iter_files(paths: list[str]):
    """展开成 (本地完整路径, 包内路径)。"""
    for path in paths:
        if os.path.isdir(path):
            base = os.path.dirname(path.rstrip("\\/"))
            for root, _dirs, files in os.walk(path):
                for name in files:
                    full = os.path.join(root, name)
                    yield full, os.path.relpath(full, base).replace(os.sep, "/")
        elif os.path.isfile(path):
            yield path, os.path.basename(path)


def total_size(paths: list[str]) -> int:
    """待压缩内容的总字节数（用于决定要不要显示进度）。"""
    total = 0
    for path in paths:
        try:
            if os.path.isdir(path):
                for root, _dirs, files in os.walk(path):
                    for name in files:
                        try:
                            total += os.path.getsize(os.path.join(root, name))
                        except OSError:
                            pass
            elif os.path.isfile(path):
                total += os.path.getsize(path)
        except OSError:
            pass
    return total


def compress(paths: list[str], out_zip: str, progress=None, cancelled=None) -> int:
    """把若干文件/文件夹打成 zip。

    progress(done, total, name)：每写完一个文件回调一次
    cancelled()：返回 True 就中止，半成品会被删掉
    """
    items = list(_iter_files(paths))
    total = len(items)
    done = 0
    try:
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for full, arc in items:
                if cancelled is not None and cancelled():
                    raise Cancelled
                zf.write(full, arc)
                done += 1
                if progress is not None:
                    progress(done, total, os.path.basename(full))
    except Cancelled:
        try:
            os.remove(out_zip)
        except OSError:
            pass
        raise
    return done
