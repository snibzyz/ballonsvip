import cv2, re, json, os
from pathlib import Path
import numpy as np
import os.path as osp
from qtpy.QtGui import QPixmap,  QColor, QImage, QTextDocument, QTextCursor, QFont
from qtpy.QtCore import Qt, QPointF

from utils import shared as C
from utils.structures import Tuple, Union, List, Dict, Config, field, nested_dataclass


QKEY = Qt.Key
QNUMERIC_KEYS = {QKEY.Key_0:0,QKEY.Key_1:1,QKEY.Key_2:2,QKEY.Key_3:3,QKEY.Key_4:4,QKEY.Key_5:5,QKEY.Key_6:6,QKEY.Key_7:7,QKEY.Key_8:8,QKEY.Key_9:9}

ARROWKEY2DIRECTION = {
    QKEY.Key_Left: QPointF(-1., 0.),
    QKEY.Key_Right: QPointF(1., 0.),
    QKEY.Key_Up: QPointF(0., -1.),
    QKEY.Key_Down: QPointF(0., 1.),
}

# Layout-independent shortcut helpers.
#
# Qt's `event.key()` returns a Qt.Key value that reflects the produced
# character, and `event.modifiers() == ...` is brittle when a driver/IME
# sets spurious bits (Keypad, GroupSwitch, ...). Both bite hard on Thai
# (Kedmanee) and other non-Latin layouts: physical D emits Qt.Key_thai_*
# instead of Qt.Key_D, so any shortcut keyed on `Key_D` silently dies.
#
# Use `match_shortcut(event, mods, vk)` for letter/symbol shortcuts and
# `match_mods(event, mods)` for modifier-only checks (Ctrl+wheel etc.).
# vk values are Windows VK_* codes (event.nativeVirtualKey()), which are
# position-based and identical across every layout on Windows.
def _modint(m) -> int:
    # PyQt6 6.x: int(Qt.KeyboardModifier.X) raises TypeError even for a
    # single enum value -- the enum class is not directly castable.
    # `.value` always returns the underlying int and works on enum values,
    # bitwise-OR results, and integer literals (after isinstance check).
    # We keep the function tolerant so callers can pass any of: a raw int,
    # an enum value, a bitwise-OR of enums, or a QFlags object.
    if isinstance(m, int):
        return m
    if hasattr(m, 'value'):
        return int(m.value)
    return int(m)

# Pre-computed integer modifier constants. Use these at call sites instead
# of `int(Qt.KeyboardModifier.X)` (which raises on PyQt6 6.x).
MOD_NONE = 0
MOD_CTRL = _modint(Qt.KeyboardModifier.ControlModifier)
MOD_SHIFT = _modint(Qt.KeyboardModifier.ShiftModifier)
MOD_ALT = _modint(Qt.KeyboardModifier.AltModifier)
MOD_META = _modint(Qt.KeyboardModifier.MetaModifier)
MOD_CTRL_SHIFT = MOD_CTRL | MOD_SHIFT
MOD_CTRL_ALT = MOD_CTRL | MOD_ALT
MOD_SHIFT_ALT = MOD_SHIFT | MOD_ALT

SHORTCUT_MOD_MASK = MOD_CTRL | MOD_SHIFT | MOD_ALT | MOD_META

def match_mods(event, mods) -> bool:
    """True iff event's relevant modifier bits exactly equal `mods`."""
    return (_modint(event.modifiers()) & SHORTCUT_MOD_MASK) == _modint(mods)

def match_shortcut(event, mods, vk) -> bool:
    """Layout-independent shortcut match by Windows VK + masked modifiers.

    `mods` is an int (e.g. `int(Qt.ControlModifier)`) or 0 for no modifier.
    `vk` is a Windows VK_* code (0x41 = A, 0x44 = D, ...). On non-Windows
    platforms nativeVirtualKey returns a different code system, so callers
    that need cross-platform coverage must fall back to event.key().
    """
    if not match_mods(event, mods):
        return False
    return event.nativeVirtualKey() == vk

# Windows VK_* codes for the letter/symbol keys we care about. Kept here
# so call sites don't sprinkle raw 0x?? constants.
class VK:
    A = 0x41; B = 0x42; C = 0x43; D = 0x44; E = 0x45; F = 0x46; G = 0x47
    H = 0x48; I = 0x49; J = 0x4A; K = 0x4B; L = 0x4C; M = 0x4D; N = 0x4E
    O = 0x4F; P = 0x50; Q = 0x51; R = 0x52; S = 0x53; T = 0x54; U = 0x55
    V = 0x56; W = 0x57; X = 0x58; Y = 0x59; Z = 0x5A
    LBRACKET = 0xDB; RBRACKET = 0xDD
    PLUS = 0xBB; MINUS = 0xBD
    SPACE = 0x20; ESC = 0x1B; DEL = 0x2E
    UP = 0x26; DOWN = 0x28; LEFT = 0x25; RIGHT = 0x27
    HOME = 0x24; END = 0x23
    PAGEUP = 0x21; PAGEDOWN = 0x22
    RETURN = 0x0D
    F1 = 0x70  # ... add more as needed

# return bgr tuple
def qrgb2bgr(color: Union[QColor, Tuple, List] = None) -> Tuple[int, int, int]:
    if color is not None:
        if isinstance(color, QColor):
            color = (color.blue(), color.green(), color.red())
        else:
            assert isinstance(color, (tuple, list))
            color = (color[2], color[1], color[0])
    return color

# https://stackoverflow.com/questions/45020672/convert-pyqt5-qpixmap-to-numpy-ndarray
def pixmap2ndarray(pixmap: Union[QPixmap, QImage], keep_alpha=True):
    size = pixmap.size()
    h = size.width()
    w = size.height()
    if isinstance(pixmap, QPixmap):
        qimg = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    else:
        qimg = pixmap.convertToFormat(QImage.Format.Format_RGBA8888)

    byte_str = qimg.bits()
    if byte_str is None:
        return None

    if hasattr(byte_str, 'asstring'):
        byte_str = qimg.bits().asstring(h * w * 4)
    else:
        byte_str = byte_str.tobytes()

    img = np.frombuffer(byte_str, dtype=np.uint8).reshape((w,h,4)).copy()
    
    if keep_alpha:
        return img
    else:
        return np.ascontiguousarray(img[:,:,:3])

def ndarray2pixmap(img, return_qimg=False):
    if len(img.shape) == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    height, width, channel = img.shape
    bytesPerLine = channel * width
    if channel == 4:
        img_format = QImage.Format.Format_RGBA8888
    else:
        img_format = QImage.Format.Format_RGB888
    img = np.ascontiguousarray(img)
    qImg = QImage(img.data, width, height, bytesPerLine, img_format)
    if return_qimg:
        return qImg
    return QPixmap(qImg)


class LruIgnoreArg:

    def __init__(self, **kwargs) -> None:
        for key in kwargs:
            setattr(self, key, kwargs[key])

    def __hash__(self) -> int:
        return hash(type(self))

    def __eq__(self, other):
        return isinstance(other, type(self))


span_pattern = re.compile(r'<span style=\"(.*?)\">', re.DOTALL)
p_pattern = re.compile(r'<p style=\"(.*?)\">', re.DOTALL)
fragment_pattern = re.compile(r'<!--(.*?)Fragment-->', re.DOTALL)
color_pattern = re.compile(r'color:(.*?);', re.DOTALL)
td_pattern = re.compile(r'<td(.*?)>(.*?)</td>', re.DOTALL)
table_pattern = re.compile(r'(.*?)<table', re.DOTALL)
fontsize_pattern = re.compile(r'font-size:(.*?)pt;', re.DOTALL)
ffamily_pattern = re.compile(r'font-family:\'(.*?)\'', re.DOTALL)


def span_repl_func(matched, color):
    style = "<p style=\"" + matched.group(1) + " color:" + color + ";\">"
    return style

def p_repl_func(matched, color):
    style = "<p style=\"" + matched.group(1) + " color:" + color + ";\">"
    return style

def set_html_color(html, rgb):
    hex_color = '#%02x%02x%02x' % (rgb[0], rgb[1], rgb[2])
    html = fragment_pattern.sub('', html)
    html = p_pattern.sub(lambda matched: p_repl_func(matched, hex_color), html)
    if color_pattern.findall(html):
        return color_pattern.sub(f'color:{hex_color};', html)
    else:
        return span_pattern.sub(lambda matched: span_repl_func(matched, hex_color), html)
    
def set_html_family(html, family):
    return ffamily_pattern.sub(f'font-family:\'{family}\'', html)


def apply_fontformat_to_html(html, ffmat):
    if not html:
        return html

    doc = QTextDocument()
    doc.setHtml(html)

    cursor = QTextCursor(doc)
    cursor.select(QTextCursor.SelectionType.Document)
    char_format = cursor.charFormat()
    font = doc.defaultFont()

    font.setFamily(ffmat.font_family)
    font.setPointSizeF(ffmat.size_pt)
    font.setBold(ffmat.bold)

    font_weight = ffmat.font_weight
    if font_weight is None:
        font_weight = font.weight()
    if not ffmat.bold:
        char_format.setFontWeight(font_weight)

    char_format.setFont(font)
    char_format.setForeground(QColor(*ffmat.foreground_color()))
    char_format.setFontItalic(ffmat.italic)
    char_format.setFontUnderline(ffmat.underline)
    if not ffmat.vertical:
        char_format.setFontLetterSpacingType(QFont.SpacingType.PercentageSpacing)
        char_format.setFontLetterSpacing(ffmat.letter_spacing * 100)

    cursor.setCharFormat(char_format)
    cursor.setBlockCharFormat(char_format)

    alignment_qt_flag = [Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignCenter, Qt.AlignmentFlag.AlignRight][ffmat.alignment]
    text_option = doc.defaultTextOption()
    text_option.setAlignment(alignment_qt_flag)
    doc.setDefaultTextOption(text_option)

    return doc.toHtml()

def html_max_fontsize(html:  str) -> float:
    size_list = fontsize_pattern.findall(html)
    size_list = [float(size) for size in size_list]
    if len(size_list) > 0:
        return max(size_list)
    else:
        return None

def doc_replace(doc: QTextDocument, span_list: List, target: str) -> List:
    len_replace = len(target)
    cursor = QTextCursor(doc)
    cursor.setPosition(0)
    cursor.beginEditBlock()
    pos_delta = 0
    sel_list = []
    for span in span_list:
        sel_start = span[0] + pos_delta
        sel_end = span[1] + pos_delta
        cursor.setPosition(sel_start)
        cursor.setPosition(sel_end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(target)
        sel_list.append([sel_start, sel_end])
        pos_delta += len_replace - (sel_end - sel_start)
    cursor.endEditBlock()
    return sel_list

def doc_replace_no_shift(doc: QTextDocument, span_list: List, target: str):
    cursor = QTextCursor(doc)
    cursor.setPosition(0)
    cursor.beginEditBlock()
    for span in span_list:
        cursor.setPosition(span[0])
        cursor.setPosition(span[1], QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(target)
    cursor.endEditBlock()

def hex2rgb(h: str):  # rgb order (PIL)
    return tuple(int(h[1 + i:1 + i + 2], 16) for i in (0, 2, 4))

def parse_stylesheet(theme: str = '', reverse_icon: bool = False) -> str:
    if reverse_icon:
        dark2light = True if theme == 'eva-light' else False
        reverse_icon_color(dark2light)
    with open(C.STYLESHEET_PATH, "r", encoding='utf-8') as f:
        stylesheet = f.read()
    with open(C.THEME_PATH, 'r', encoding='utf8') as f:
        theme_dict: Dict = json.loads(f.read())
    if not theme or theme not in theme_dict:
        tgt_theme: Dict = theme_dict[list(theme_dict.keys())[0]]
    else:
        tgt_theme: Dict = theme_dict[theme]

    C.FOREGROUND_FONTCOLOR = hex2rgb(tgt_theme['@qwidgetForegroundColor'])
    C.SLIDERHANDLE_COLOR = hex2rgb(tgt_theme['@sliderHandleColor'])
    for key, val in tgt_theme.items():
        stylesheet = stylesheet.replace(key, val)
    return stylesheet


ICON_DIR = 'icons'

LIGHTFILL_ACTIVE = "fill=\"#697187\""
LIGHTFILL = "fill=\"#b3b6bf\""
DARKFILL_ACTIVE = "fill=\"#96a4cd\""
DARKFILL = "fill=\"#697186\""

ICONREVERSE_DICT_LIGHT2DARK = {LIGHTFILL_ACTIVE: DARKFILL_ACTIVE, LIGHTFILL: DARKFILL}
ICONREVERSE_DICT_DARK2LIGHT = {DARKFILL_ACTIVE: LIGHTFILL_ACTIVE, DARKFILL: LIGHTFILL}
ICON_LIST = []

def reverse_icon_color(dark2light: bool = False):
    global ICON_LIST
    if not ICON_LIST:
        for filename in os.listdir(ICON_DIR):
            file_suffix = Path(filename).suffix
            if file_suffix.lower() != '.svg':
                continue
            else:
                ICON_LIST.append(osp.join(ICON_DIR, filename))

    if dark2light:
        pattern = re.compile(re.escape(DARKFILL) + '|' + re.escape(DARKFILL_ACTIVE))
        rep_dict = ICONREVERSE_DICT_DARK2LIGHT
    else:
        pattern = re.compile(re.escape(LIGHTFILL) + '|' + re.escape(LIGHTFILL_ACTIVE))
        rep_dict = ICONREVERSE_DICT_LIGHT2DARK
    for svgpath in ICON_LIST:
        with open(svgpath, "r", encoding="utf-8") as f:
            svg_content = f.read()
            svg_content = pattern.sub(lambda m:rep_dict[m.group()], svg_content)
        with open(svgpath, "w", encoding="utf-8") as f:
            f.write(svg_content)

def mutate_dict_key(adict: dict, old_key: Union[str, int], new_key: str):
    # https://stackoverflow.com/questions/12150872/change-key-in-ordereddict-without-losing-order
    key_list = list(adict.keys())
    if isinstance(old_key, int):
        old_key = key_list[old_key]
    
    for key in key_list:
        value = adict.pop(key)
        adict[new_key if old_key == key else key] = value
