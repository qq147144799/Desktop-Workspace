r"""
读取 Windows 系统「新建」菜单的模板项。

资源管理器右键「新建」里的 Word/Excel/文本文档等，配置都在
HKEY_CLASSES_ROOT\<扩展名>\ShellNew 下，这里把它们枚举出来。

这台机器上 Office/WPS 都装了，却没有写 ShellNew（.txt 也没有），
所以注册表枚举之外再补一套内置模板：文本文档、docx、xlsx、markdown，
其中 docx/xlsx 用 zip 现场生成合法的 OOXML 包，保证能被 Office/WPS 打开。
"""

from __future__ import annotations

import ctypes
import io
import os
import zipfile
import winreg

MAX_ITEMS = 40

_cache: list[dict] | None = None

_shlwapi = ctypes.WinDLL("shlwapi", use_last_error=True)
_shlwapi.SHLoadIndirectString.argtypes = (
    ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p)
_shlwapi.SHLoadIndirectString.restype = ctypes.c_long


# --------------------------------------------------------------------------- 工具


def _resolve(text) -> str:
    """解析可能是「@C:\\...\\shell32.dll,-123」形式的资源字符串。"""
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if not text:
        return ""
    if text.startswith("@"):
        buf = ctypes.create_unicode_buffer(512)
        try:
            if _shlwapi.SHLoadIndirectString(text, buf, 512, None) == 0:
                return buf.value.strip()
        except OSError:
            pass
        return ""
    return text


def _template_file(name: str) -> str:
    if not name:
        return ""
    name = os.path.expandvars(name)
    if os.path.isabs(name):
        return name if os.path.isfile(name) else ""
    root = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "ShellNew")
    candidate = os.path.join(root, name)
    return candidate if os.path.isfile(candidate) else ""


def _progid_name(root, ext: str) -> str:
    try:
        with winreg.OpenKey(root, ext) as key:
            progid = winreg.QueryValueEx(key, "")[0]
    except OSError:
        return ""
    if not isinstance(progid, str) or not progid:
        return ""
    for sub in (progid, progid + r"\shell\new"):
        try:
            with winreg.OpenKey(root, sub) as key:
                name = _resolve(winreg.QueryValueEx(key, "")[0])
                if name:
                    return name
        except OSError:
            continue
    return ""


def _read_item(root, ext: str, sn) -> dict | None:
    kind = None
    file_path = ""
    data = None

    try:
        winreg.QueryValueEx(sn, "NullFile")
        kind = "null"
    except OSError:
        pass

    if kind is None:
        try:
            file_path = _template_file(winreg.QueryValueEx(sn, "FileName")[0])
            if file_path:
                kind = "file"
        except OSError:
            pass

    if kind is None:
        try:
            raw = winreg.QueryValueEx(sn, "Data")[0]
            data = raw.encode("utf-8", "ignore") if isinstance(raw, str) else bytes(raw or b"")
            kind = "data"
        except OSError:
            pass

    if kind is None:
        return None                     # Command / Directory 之类不支持，跳过

    label = ""
    try:
        label = _resolve(winreg.QueryValueEx(sn, "ItemName")[0])
    except OSError:
        pass
    if not label:
        label = _progid_name(root, ext)
    if not label:
        label = ext.lstrip(".").upper()
    if not label:
        return None

    return {"label": label, "ext": ext, "kind": kind, "file": file_path, "data": data}


# --------------------------------------------------------------------------- OOXML


_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

_DOCX_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_REL}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    ),
    "word/document.xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p/>"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800"'
        ' w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    ),
}

_XLSX_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_REL}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    ),
    "xl/workbook.xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    ),
    "xl/_rels/workbook.xml.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_REL}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    ),
    "xl/worksheets/sheet1.xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData/></worksheet>"
    ),
    "xl/styles.xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        "</styleSheet>"
    ),
}


def _zip_bytes(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            zf.writestr(name, content.encode("utf-8"))
    return buf.getvalue()


def _builtin() -> list[dict]:
    return [
        {"label": "文本文档", "ext": ".txt", "kind": "null", "file": "", "data": None},
        {"label": "Microsoft Word 文档", "ext": ".docx", "kind": "data",
         "file": "", "data": _zip_bytes(_DOCX_PARTS)},
        {"label": "Microsoft Excel 工作表", "ext": ".xlsx", "kind": "data",
         "file": "", "data": _zip_bytes(_XLSX_PARTS)},
        {"label": "Markdown 文档", "ext": ".md", "kind": "null", "file": "", "data": None},
    ]


# --------------------------------------------------------------------------- 对外


def _scan_registry() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    try:
        root = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "", 0, winreg.KEY_READ)
    except OSError:
        return items

    index = 0
    try:
        while index < 6000:
            try:
                ext = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not ext.startswith(".") or len(ext) > 12:
                continue
            try:
                sn = winreg.OpenKey(root, ext + r"\ShellNew")
            except OSError:
                continue
            try:
                item = _read_item(root, ext, sn)
            except OSError:
                item = None
            finally:
                winreg.CloseKey(sn)
            if item and item["label"].lower() not in seen:
                seen.add(item["label"].lower())
                items.append(item)
    finally:
        winreg.CloseKey(root)

    items.sort(key=lambda t: t["label"].lower())
    return items


def list_templates(refresh: bool = False) -> list[dict]:
    """返回新建菜单项，内置常用类型在前，系统注册的模板在后。"""
    global _cache
    if _cache is not None and not refresh:
        return _cache

    registry = _scan_registry()
    have = {t["ext"].lower() for t in registry}
    merged = [t for t in _builtin() if t["ext"].lower() not in have]
    merged.extend(registry)
    _cache = merged[:MAX_ITEMS]
    return _cache
