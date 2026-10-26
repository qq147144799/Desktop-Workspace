"""
主题与共用尺寸。

换肤只需要在 PALETTES 里加一套颜色，界面所有颜色都从这里取。
LIST_FONT_PT / META_FONT_PT 让文件列表和便签列表的字号保持一致。
"""

LIST_FONT_PT = 9.0
META_FONT_PT = 7.8

PALETTES = {
    "light": {
        "label": "浅色",
        "bg": "#FFFFFF", "bg_soft": "#FCFCFD", "bg_bar": "#FAFBFC", "bg_header": "#F7F8FA",
        "hover": "#EFF2F6", "sel": "#EDF2FE", "split": "#EDEFF3", "sep": "#ECEEF1",
        "border": "#E9EBEF", "btn_hover": "#E8EBF0", "btn_press": "#DCE0E6",
        "line1": "#E6EAF0", "line2": "#E6E8EB", "line3": "#E3E8EF", "line4": "#D9DFE7",
        "line5": "#D6DAE0", "line6": "#C0C6CE", "divider": "#E3E6EA",
        "input_border": "#E1E5EA", "scroll_hover": "#D8DEE7", "split_hover": "#D6DCE5",
        "disabled": "#C2C8D0", "placeholder": "#C2C7CF", "muted": "#A9AFB8",
        "dim": "#8A9099", "icon": "#5A626C", "text2": "#4A5159", "text3": "#3E4650",
        "title": "#343B44", "text": "#1F2329", "accent": "#2F6FEB", "sel_border": "#CFE0FD",
    },
    "dark": {
        "label": "深色",
        "bg": "#1E2024", "bg_soft": "#24262B", "bg_bar": "#191B1E", "bg_header": "#232529",
        "hover": "#2B2E34", "sel": "#2D3B55", "split": "#2A2D33", "sep": "#2E3238",
        "border": "#33373E", "btn_hover": "#2C2F35", "btn_press": "#3A3E45",
        "line1": "#35393F", "line2": "#35393F", "line3": "#35393F", "line4": "#3C4047",
        "line5": "#3E424A", "line6": "#4A4F57", "divider": "#34383E",
        "input_border": "#3A3E45", "scroll_hover": "#454A52", "split_hover": "#42474F",
        "disabled": "#4B5058", "placeholder": "#5D636C", "muted": "#7C838C",
        "dim": "#8B929B", "icon": "#A0A7B0", "text2": "#C6CBD2", "text3": "#B4BAC2",
        "title": "#DCDFE4", "text": "#E5E7EB", "accent": "#5B9BFF", "sel_border": "#3E5C8C",
    },
    "warm": {
        "label": "暖沙",
        "bg": "#FDFBF6", "bg_soft": "#FBF7EF", "bg_bar": "#F8F3E8", "bg_header": "#F4EEE0",
        "hover": "#F0E9D9", "sel": "#F3E4C8", "split": "#EDE5D4", "sep": "#EDE5D4",
        "border": "#E5DAC3", "btn_hover": "#EEE6D4", "btn_press": "#E2D6BF",
        "line1": "#E8DECD", "line2": "#E8DECD", "line3": "#E8DECD", "line4": "#DCD0B9",
        "line5": "#D8CCB5", "line6": "#C7BBA7", "divider": "#E3D8C4",
        "input_border": "#DFD3BC", "scroll_hover": "#D3C6AC", "split_hover": "#D3C6AC",
        "disabled": "#C3B7A2", "placeholder": "#BFB3A0", "muted": "#9A8E7C",
        "dim": "#8C806E", "icon": "#7D7263", "text2": "#5C5346", "text3": "#574E41",
        "title": "#453D31", "text": "#3A332A", "accent": "#B4713A", "sel_border": "#D9A876",
    },
    "green": {
        "label": "护眼绿",
        "bg": "#F3F8F1", "bg_soft": "#EFF6EC", "bg_bar": "#EAF2E7", "bg_header": "#E6EFE2",
        "hover": "#E2EDDD", "sel": "#D8EBCE", "split": "#DCE8D6", "sep": "#DCE8D6",
        "border": "#CFDFC8", "btn_hover": "#DDEAD7", "btn_press": "#CBDCC3",
        "line1": "#D5E3CE", "line2": "#D5E3CE", "line3": "#D5E3CE", "line4": "#C4D6BB",
        "line5": "#BFD1B6", "line6": "#ADC0A4", "divider": "#D0DFC9",
        "input_border": "#C8D9C0", "scroll_hover": "#B7CCAD", "split_hover": "#B7CCAD",
        "disabled": "#A8BC9F", "placeholder": "#A3B79A", "muted": "#7D9174",
        "dim": "#70866A", "icon": "#5F7558", "text2": "#40533B", "text3": "#3C4F37",
        "title": "#2E4029", "text": "#253420", "accent": "#3E8E5A", "sel_border": "#8FBF7E",
    },
}

ORDER = ["light", "dark", "warm", "green"]

_current = "light"


def set_theme(key: str) -> None:
    global _current
    if key in PALETTES:
        _current = key


def current_key() -> str:
    return _current


def palette() -> dict:
    return PALETTES.get(_current, PALETTES["light"])


def color(name: str, fallback: str = "#FF00FF") -> str:
    return palette().get(name, fallback)


def build_qss() -> str:
    qss = _TEMPLATE
    for key, value in palette().items():
        if key != "label":
            qss = qss.replace("{" + key + "}", value)
    return qss


_TEMPLATE = """
QWidget { color: {text}; font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 12px; }
#Root { background: {bg}; }
#Header { background: {bg_header}; border-bottom: 1px solid {border}; }
#Header QLabel#Title { font-size: 12px; font-weight: 600; color: {title}; }
#Status { background: {bg_bar}; border-top: 1px solid {border}; }
#Status QLabel { color: {dim}; font-size: 11px; }
QToolButton { border: none; border-radius: 6px; color: {text2}; background: transparent; }
QToolButton:hover { background: {btn_hover}; }
QToolButton:pressed { background: {btn_press}; }
QToolButton#NavBtn { font-family: "Segoe UI Symbol"; font-size: 15px; color: {text3}; }
QToolButton#NavBtn:disabled { color: {disabled}; }
QToolButton#TitleBtn { font-family: "Segoe UI Symbol"; font-size: 13px; color: {icon}; }
#NavBar { background: {bg_soft}; border-bottom: 1px solid {line1}; }
QToolButton#SortBtn { color: {dim}; font-size: 11px; padding: 0 4px; border-radius: 4px; }
QToolButton#SortBtn:hover { color: {accent}; background: transparent; }
QLineEdit#Path {
    background: {bg_header}; border: 1px solid {line2}; border-radius: 7px;
    padding: 3px 8px; color: {text}; font-size: 12px;
    selection-background-color: {sel_border}; selection-color: {text};
}
QLineEdit#Path:focus { border: 1px solid {accent}; }
QLineEdit#RenameEditor {
    background: {bg}; border: 1px solid {accent}; border-radius: 5px;
    padding: 0 5px; color: {text}; font-size: 12px;
    selection-background-color: {sel_border}; selection-color: {text};
}
QListView { background: transparent; border: none; outline: none; }
#ChipBox { background: {bg_bar}; border-bottom: 1px solid {border}; }
QPushButton#Chip {
    background: {hover}; border: 1px solid {line1}; border-radius: 11px;
    padding: 0 9px; color: {text2}; font-size: 11px;
}
QPushButton#Chip:hover { background: {line3}; border-color: {line4}; }
QPushButton#Chip:pressed { background: {scroll_hover}; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: {line5}; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: {line6}; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QMenu {
    background: {bg}; border: 1px solid {divider}; border-radius: 8px; padding: 5px;
}
QMenu::item { padding: 6px 22px 6px 16px; border-radius: 5px; font-size: 12px; }
QMenu::item:selected { background: {sel}; }
QMenu::separator { height: 1px; background: {sep}; margin: 4px 6px; }
QMenu::indicator { width: 12px; height: 12px; }

#Notes { background: {bg}; }
#NotesHeader { background: {bg_header}; border-top: 1px solid {border}; border-bottom: 1px solid {border}; }
QLabel#NotesTitle { font-size: 11px; font-weight: 600; color: {title}; }
QLabel#NotesCount { font-size: 10px; color: {muted}; }
QLabel#NoteEmpty { color: {placeholder}; font-size: 11px; }
QToolButton#NotesBtn { font-family: "Segoe UI Symbol"; font-size: 12px; color: {icon}; border-radius: 5px; }
QLineEdit#NoteInput {
    border: none; border-top: 1px solid {border}; background: {bg};
    padding: 0 12px; font-size: 11px; color: {text};
    selection-background-color: {sel_border}; selection-color: {text};
}
QLineEdit#NoteInput:focus { background: {bg_soft}; }
QListView#NoteList { background: {bg}; }
QSplitter::handle:vertical { background: {split}; height: 6px; }
QSplitter::handle:vertical:hover { background: {split_hover}; }
QDateTimeEdit { border: 1px solid {input_border}; border-radius: 6px; padding: 4px 8px; font-size: 12px; }
QDateTimeEdit:focus { border-color: {accent}; }
#PanelPopup { background: {bg}; border: 1px solid {border}; border-radius: 10px; }
QLabel#PopupTitle { color: {title}; font-size: 11px; font-weight: 600; background: transparent; }
QLabel#PopupBody { color: {text}; font-size: 11px; background: transparent; }
QPushButton#PopupChip {
    border: 1px solid {border}; border-radius: 6px; background: {bg_header};
    color: {text2}; padding: 4px 10px; font-size: 11px;
}
QPushButton#PopupChip:hover { background: {hover}; }
QPushButton#PopupChip:disabled { color: {disabled}; }
QLineEdit#NoteEditor {
    border: 1px solid #2F6FEB; border-radius: 6px;
    background: #FFFFFF; color: #1F2329; padding: 0 5px; font-size: 12px;
}
QLabel#PopupBodyStrong {
    color: {text}; font-size: 13px; font-weight: 600;
    background: {sel}; border-radius: 7px; padding: 9px 11px;
    border: 1px solid {sel_border};
}
"""
