#!/usr/bin/env python3
"""B 端后台客户端（Windows）。

无窗口常驻：长轮询 S 的 /pending 领取任务并执行：
  - capture：静默抓主屏 → POST /frame
  - type   ：把文本逐字注入当前焦点窗口 → POST /result
同时注册全局热键（Ctrl+Shift+Alt+8 截屏 / +9 分析 / +0 输入回答 / +- 清空对话 / ++ 停止输入）。

中文/Unicode 用 SendInput + KEYEVENTF_UNICODE 逐字注入，不占用剪贴板、不受输入法/键盘布局影响。
代码缩进策略（indent_mode）：
  - human：逐行对齐（回车后选中当前行空白 → 按目标缩进替换 → 逐字输入正文），默认
  - none ：原样逐字，不做缩进对齐
另支持 paste_mode：整段剪贴板粘贴（最保真，会覆盖剪贴板）。
其它参数：indent_style(spaces/tabs)、tab_size、space_interval_ms(空格/缩进专用高速间隔)、
  clean_invisibles、dismiss_suggest/dismiss_delay_ms、humanize/typo_* 等，均可由 S 端网页下发。
全局热键组合由 S 端 /hotkeys 下发（默认 ctrl+shift+alt+8/9/0/minus/plus）。

运行方式（二选一）：
  1. 源码：pythonw capture_agent.py
  2. 打包：double-click ScannerQA-Agent.exe（由 build_exe.bat 生成）
日志写到 %LOCALAPPDATA%\\scanner-qa\\agent.log。

配置：与脚本/exe 同目录的 config.json（参考 config.example.json）
    {
      "server_url": "http://192.168.1.50:8503",
      "token": "和 S 端 B_API_TOKEN 一致",
      "monitor": 1,
      "jpeg_quality": 85,
      "scale": 1.0,
      "poll_wait": 25
    }

源码依赖：pip install -r requirements.txt
"""
from __future__ import annotations

import io
import json
import logging
import os
import random
import sys
import threading
import time
from pathlib import Path

import mss
import requests
from PIL import Image


def app_dir() -> Path:
    """源码运行时用脚本目录；打包成 exe 后用 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()

# 兼容 mss 9/10：新版叫 MSS，旧版只有 mss
_MSS = getattr(mss, "MSS", None) or mss.mss

DEFAULTS = {
    "server_url": "http://127.0.0.1:8503",
    "token": "",
    "monitor": 1,          # 1 = 主屏（0 = 所有屏拼合的虚拟屏）
    "jpeg_quality": 85,
    "scale": 1.0,
    "poll_wait": 25,
    # 全局热键总开关：具体组合由 S 端网页下发（/hotkeys），可用 hotkeys 覆盖
    "hotkeys_enabled": True,
    "hotkeys": {},
}


# ============================ 键盘注入（SendInput） ============================
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _INPUT_KEYBOARD = 1
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_UNICODE = 0x0004
    _VK_RETURN = 0x0D
    _VK_TAB = 0x09
    _VK_BACK = 0x08
    _VK_ESCAPE = 0x1B
    _VK_CONTROL = 0x11
    _VK_V = 0x56
    _VK_SHIFT = 0x10
    _VK_HOME = 0x24
    _VK_END = 0x23
    _VK_UP = 0x26
    _VK_DOWN = 0x28
    _VK_DELETE = 0x2E
    _VK_OEM_4 = 0xDB  # '['；Ctrl+[ = editor.action.outdentLines（减少缩进）
    _CF_UNICODETEXT = 13
    _GMEM_MOVEABLE = 0x0002

    def _send_inputs(*inputs: "_INPUT") -> None:
        count = len(inputs)
        array = (_INPUT * count)(*inputs)
        ctypes.windll.user32.SendInput(count, array, ctypes.sizeof(_INPUT))

    def _unicode_input(code: int, keyup: bool) -> "_INPUT":
        flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if keyup else 0)
        return _INPUT(type=_INPUT_KEYBOARD,
                      u=_INPUTUNION(ki=_KEYBDINPUT(0, code, flags, 0, 0)))

    def _vk_input(vk: int, keyup: bool) -> "_INPUT":
        flags = _KEYEVENTF_KEYUP if keyup else 0
        return _INPUT(type=_INPUT_KEYBOARD,
                      u=_INPUTUNION(ki=_KEYBDINPUT(vk, 0, flags, 0, 0)))

    # 正确声明 64 位下的句柄/指针类型，避免截断
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    def _set_clipboard_text(text: str) -> None:
        """把整段文本放进剪贴板（供整段粘贴模式）。会覆盖用户当前剪贴板。"""
        if not _user32.OpenClipboard(None):
            raise RuntimeError("OpenClipboard 失败")
        try:
            _user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
            if not handle:
                raise RuntimeError("GlobalAlloc 失败")
            ptr = _kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            _kernel32.GlobalUnlock(handle)
            if not _user32.SetClipboardData(_CF_UNICODETEXT, handle):
                raise RuntimeError("SetClipboardData 失败")
        finally:
            _user32.CloseClipboard()

    def _paste_shortcut() -> None:
        _send_inputs(
            _vk_input(_VK_CONTROL, False),
            _vk_input(_VK_V, False),
            _vk_input(_VK_V, True),
            _vk_input(_VK_CONTROL, True),
        )

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    _user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL

    def _cursor_pos() -> tuple[int, int] | None:
        """当前鼠标指针坐标（用于"鼠标一动就停"）。滚轮不改变坐标，天然不影响。"""
        pt = _POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):
            return (pt.x, pt.y)
        return None


# 输入行为的默认参数（可在 config.json 覆盖）
TYPING_DEFAULTS = {
    "interval_min_ms": 200,      # 字符间隔下限
    "interval_max_ms": 1000,     # 字符间隔上限
    "line_pause_min_ms": 1000,   # 换行后停顿下限
    "line_pause_max_ms": 2000,   # 换行后停顿上限
    "space_interval_ms": 15,     # 空格/缩进的专用高速间隔
    "typo_rate": 0.005,          # 每字符出错概率
    "typo_pause_min_ms": 100,    # 错字后停顿下限
    "typo_pause_max_ms": 300,    # 错字后停顿上限
    "dismiss_suggest": True,     # 回车/制表前先按 Esc 关掉 IDE 自动补全弹窗
    "dismiss_delay_ms": 80,      # Esc 与特殊键之间的等待
    "enter_via_paste": False,    # 换行用剪贴板粘贴插入（仅 indent_mode=none 时生效）
    "indent_mode": "human",      # human / none
    "indent_style": "spaces",    # spaces / tabs
    "tab_size": 4,
    "clean_invisibles": True,    # 清理 NBSP/全角空格/零宽/智能引号
    "mouse_stop_enabled": True,  # 逐字输入时鼠标移动超过阈值则自动停止
    "mouse_stop_px": 10,         # 位移阈值（像素）；滚轮不改变指针坐标，不影响翻页
    "keystream_autoindent": True,  # 键盘流校验时假定 VS Code 回车自动缩进（关掉则用 autoIndent:none）
}

_TYPO_POOL = "abcdefghijklmnopqrstuvwxyz0123456789"

# human 模式：回车后用 Ctrl+[ 连续减少缩进（行首为 0 时是 no-op），次数给足即可
# 清到 0，覆盖括号对齐等较深缩进；多按不会删换行，安全。
_OUTDENT_PRESSES = 24

# ---- 文本预处理：归一化 / 拆行 / 缩进预测（纯函数，便于测试） ----
_INVISIBLE_MAP = {0x00A0: " ", 0x3000: " "}
_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF}
_SMART_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
}


def normalize_code_text(text: str, clean: bool = True) -> str:
    """统一换行；可选清理不可见/易错字符。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not clean:
        return text
    out = []
    for ch in text:
        if ord(ch) in _ZERO_WIDTH:
            continue
        out.append(_SMART_MAP.get(ch, _INVISIBLE_MAP.get(ord(ch), ch)))
    return "".join(out)


def split_code_lines(text: str, tab_size: int = 4) -> list[tuple[int, str]]:
    """返回 [(缩进列数, 正文)]；行首 Tab 按 tab stop 展开计列。"""
    lines: list[tuple[int, str]] = []
    for raw in text.split("\n"):
        cols = 0
        i = 0
        while i < len(raw) and raw[i] in (" ", "\t"):
            if raw[i] == "\t":
                cols += tab_size - (cols % tab_size)
            else:
                cols += 1
            i += 1
        lines.append((cols, raw[i:]))
    if text.endswith("\n") and lines:
        lines.pop()  # 末尾换行不产生额外空行
    return lines


def build_indent(cols: int, style: str, tab_size: int) -> str:
    if style == "tabs":
        return "\t" * (cols // tab_size) + " " * (cols % tab_size)
    return " " * cols


# ==================== 键盘流（LLM 直接给按键序列） ====================
# 合法 token：普通文本 / <Enter> / <Tab> / <S-Tab> / <Up> / <Down>
_KEY_TOKENS = ("<Enter>", "<Tab>", "<S-Tab>", "<Shift+Tab>", "<Up>", "<Down>")


class _KeyMirror:
    """模拟 VS Code：回车自动缩进、Tab/S-Tab、上下移动、括号自动配对。

    仅用于「执行前校验」和「记录进度」，不参与真实按键发送。
    """

    def __init__(self, tab_size: int = 4, insert_spaces: bool = True,
                 auto_indent: bool = True) -> None:
        self.lines = [""]
        self.li = 0
        self.ci = 0
        self.tab_size = max(1, int(tab_size))
        self.insert_spaces = insert_spaces
        self.auto_indent = auto_indent

    def text(self) -> str:
        return "\n".join(self.lines)

    def _unit(self) -> str:
        return " " * self.tab_size if self.insert_spaces else "\t"

    def _type_char(self, ch: str) -> None:
        line = self.lines[self.li]
        if ch in ")]}'\"`" and self.ci < len(line) and line[self.ci] == ch:
            self.ci += 1
            return
        pairs = {"(": ")", "[": "]", "{": "}", "'": "'", '"': '"'}
        if ch in pairs:
            nxt = line[self.ci] if self.ci < len(line) else ""
            if not (ch in "([{" and nxt and (nxt.isalnum() or nxt == "_")):
                self.lines[self.li] = line[:self.ci] + ch + pairs[ch] + line[self.ci:]
                self.ci += 1
                return
        self.lines[self.li] = line[:self.ci] + ch + line[self.ci:]
        self.ci += 1

    def type(self, s: str) -> None:
        for ch in s:
            self._type_char(ch)

    def enter(self) -> None:
        line = self.lines[self.li]
        left, right = line[:self.ci], line[self.ci:]
        if self.auto_indent:
            base = left[:len(left) - len(left.lstrip(" \t"))]
            ind = base + self._unit() if left.rstrip().endswith(":") else base
        else:
            ind = ""
        self.lines[self.li] = left
        self.lines.insert(self.li + 1, ind + right)
        self.li += 1
        self.ci = len(ind)

    def tab(self) -> None:
        self.type(self._unit())

    def shift_tab(self) -> None:
        line = self.lines[self.li]
        n = 0
        while n < len(line) and line[n] in " \t":
            n += 1
        if not n:
            return
        cut = 1 if line[0] == "\t" else min(n, self.tab_size)
        self.lines[self.li] = line[cut:]
        self.ci = max(0, self.ci - cut)

    def up(self) -> None:
        if self.li > 0:
            self.li -= 1
            self.ci = min(self.ci, len(self.lines[self.li]))

    def down(self) -> None:
        if self.li < len(self.lines) - 1:
            self.li += 1
            self.ci = min(self.ci, len(self.lines[self.li]))

    def apply(self, token: str) -> None:
        if token == "<Enter>":
            self.enter()
        elif token == "<Tab>":
            self.tab()
        elif token in ("<S-Tab>", "<Shift+Tab>"):
            self.shift_tab()
        elif token == "<Up>":
            self.up()
        elif token == "<Down>":
            self.down()
        elif isinstance(token, str):
            self.type(token)


def simulate_keys(keys, tab_size: int = 4, insert_spaces: bool = True,
                  auto_indent: bool = True) -> str:
    mirror = _KeyMirror(tab_size, insert_spaces, auto_indent)
    for token in keys:
        if isinstance(token, str):
            mirror.apply(token)
    return mirror.text()


def keys_valid(keys, target: str, tab_size: int = 4, insert_spaces: bool = True,
               auto_indent: bool = True) -> bool:
    if not isinstance(keys, list) or not keys:
        return False
    if any(not isinstance(t, str) for t in keys):
        return False
    got = simulate_keys(keys, tab_size, insert_spaces, auto_indent)
    return got.rstrip("\n") == normalize_code_text(target).rstrip("\n")


class SendInputTyper:
    """用 Windows SendInput 逐字注入文本，可选拟人化节奏与纯 Unicode 过滤。"""

    def __init__(self, options: dict | None = None) -> None:
        self._base = dict(TYPING_DEFAULTS)
        if options:
            self._base.update({k: options[k] for k in TYPING_DEFAULTS if k in options})
        self.options = dict(self._base)
        self.skipped = 0
        self.typed = 0
        self.stopped = False
        self.log = None
        self._stop = threading.Event()
        self.stop_reason: str | None = None
        # 断点状态（保存在 B 端）：已写完的行 + 当前所在代码块行
        self.progress: dict = {"done_lines": [], "current_line": 0, "total_lines": 0}
        self._mouse_baseline: tuple[int, int] | None = None

    def set_options(self, overrides: dict | None) -> None:
        """按任务覆盖拟人化参数（min>max 时自动交换）。"""
        self.options = dict(self._base)
        if overrides:
            self.options.update({k: overrides[k] for k in TYPING_DEFAULTS if k in overrides})
        for lo, hi in (("interval_min_ms", "interval_max_ms"),
                       ("line_pause_min_ms", "line_pause_max_ms"),
                       ("typo_pause_min_ms", "typo_pause_max_ms")):
            if self.options[lo] > self.options[hi]:
                self.options[lo], self.options[hi] = self.options[hi], self.options[lo]

    def stop(self, reason: str = "stop") -> None:
        """请求停止当前输入（鼠标移动 / 网页停止按钮）。reason: mouse / web / stop。"""
        if not self._stop.is_set():
            self.stop_reason = reason
        self._stop.set()
        self.stopped = True

    def clear_breakpoint(self) -> None:
        """清掉断点（新会话/完整重新输出时调用）。"""
        self.progress = {"done_lines": [], "current_line": 0, "total_lines": 0}

    def reset_breakpoint(self) -> None:
        self.clear_breakpoint()

    # ---- 可中断 sleep：长停顿也要能被停止打断 ----
    def _sleep(self, ms: float) -> None:
        remaining = float(ms) / 1000.0
        while remaining > 0 and not self._stop.is_set():
            chunk = min(0.05, remaining)
            time.sleep(chunk)
            remaining -= chunk

    # ---- 鼠标守卫：仅在逐字输入时检测；滚轮不影响 ----
    def _arm_mouse_guard(self) -> None:
        self._mouse_baseline = _cursor_pos() if self.options.get("mouse_stop_enabled") else None

    def _mouse_moved(self) -> bool:
        if self._mouse_baseline is None:
            return False
        pos = _cursor_pos()
        if pos is None:
            return False
        dx = pos[0] - self._mouse_baseline[0]
        dy = pos[1] - self._mouse_baseline[1]
        return (dx * dx + dy * dy) ** 0.5 > float(self.options.get("mouse_stop_px", 10))


    @staticmethod
    def _sendable_unicode(ch: str) -> bool:
        """能否作为单个 BMP Unicode 码点发送；否则视为"非 Unicode"跳过。"""
        code = ord(ch)
        if code > 0xFFFF or 0xD800 <= code <= 0xDFFF:  # 非 BMP / 代理区
            return False
        if ch in ("\n", "\r", "\t"):
            return True
        return ch.isprintable()

    def _normal_interval(self, humanize: bool, fixed_ms: int) -> None:
        if humanize:
            self._sleep(random.uniform(self.options["interval_min_ms"],
                                       self.options["interval_max_ms"]))
        elif fixed_ms:
            self._sleep(fixed_ms)

    def _space_interval(self) -> None:
        self._sleep(self.options["space_interval_ms"])

    def _ctrl_key(self, vk: int) -> None:
        _send_inputs(
            _vk_input(_VK_CONTROL, False),
            _vk_input(vk, False),
            _vk_input(vk, True),
            _vk_input(_VK_CONTROL, True),
        )

    def _clear_line_indent(self, dismiss: bool = False) -> None:
        """把当前行缩进清到 0，全程不产生选区（避免行首变蓝）。

        用 Ctrl+[（editor.action.outdentLines）连续减小缩进；行首本就为 0 时是
        no-op，所以多按几次安全（不会像 Delete 那样删掉换行）。最后 Home 确保
        光标落在第 1 列，随后直接输入目标缩进即可。
        """
        if dismiss:
            _send_inputs(_vk_input(_VK_ESCAPE, False), _vk_input(_VK_ESCAPE, True))
            self._sleep(self.options["dismiss_delay_ms"])
        for _ in range(_OUTDENT_PRESSES):
            self._ctrl_key(_VK_OEM_4)
        _send_inputs(_vk_input(_VK_HOME, False), _vk_input(_VK_HOME, True))

    def _tap(self, vk: int, dismiss_suggest: bool = False) -> None:
        # IDE 里回车/制表会被当成"接受补全"，先按 Esc 关掉补全弹窗
        if dismiss_suggest:
            _send_inputs(_vk_input(_VK_ESCAPE, False), _vk_input(_VK_ESCAPE, True))
            self._sleep(self.options["dismiss_delay_ms"])
        _send_inputs(_vk_input(vk, False), _vk_input(vk, True))

    def _type_char(self, ch: str, humanize: bool, interval_ms: int,
                   unicode_only: bool, dismiss: bool) -> bool:
        """输入一个字符：空格/制表走高速通道；返回 False 表示被跳过。"""
        if self._mouse_moved():
            self.stop("mouse")
            return False
        if ch == " ":
            self._unicode_char(" ")
            self._space_interval()
            return True
        if ch == "\t":
            self._tap(_VK_TAB, dismiss)
            self._space_interval()
            return True
        if unicode_only and not self._sendable_unicode(ch):
            self.skipped += 1
            return False
        if humanize and random.random() < self.options["typo_rate"]:
            self._unicode_char(random.choice(_TYPO_POOL))
            self._sleep(random.uniform(self.options["typo_pause_min_ms"],
                                       self.options["typo_pause_max_ms"]))
            _send_inputs(_vk_input(_VK_BACK, False), _vk_input(_VK_BACK, True))
        self._unicode_char(ch)
        self._normal_interval(humanize, interval_ms)
        return True

    def _type_indent_cols(self, cols: int, dismiss: bool) -> int:
        """输入 cols 列缩进（调用前已把当前行缩进清 0、光标在第 1 列，无选区）。

        spaces：逐字输入空格，保留拟人节奏，无选区无闪烁。
        tabs：VS Code 的 Tab 键在选中整行时是“缩进整行”、空行时会跳到语言推导
              缩进，无法精确到目标列；因此把缩进串粘贴进来，Tab 字符 100% 保真。
        返回实际输入的逻辑字符数（供断点清理用）。
        """
        if cols <= 0:
            return 0
        tab_size = max(1, int(self.options["tab_size"]))
        if self.options["indent_style"] == "tabs":
            # 真机验证：粘贴后必须给 VS Code 足够时间处理（30ms 会被随后键入的正文
            # 抢先，导致缩进丢失）。0.12s + 0.25s 实测稳定。
            indent = build_indent(cols, "tabs", tab_size)
            _set_clipboard_text(indent)
            self._sleep(120)
            _paste_shortcut()
            self._sleep(250)
            return len(indent)
        for ch in build_indent(cols, "spaces", tab_size):
            self._unicode_char(ch)
            self._space_interval()
        return cols

    def _press_enter(self, humanize: bool, interval_ms: int, dismiss: bool,
                     enter_via_paste: bool) -> None:
        if enter_via_paste:
            if dismiss:
                _send_inputs(_vk_input(_VK_ESCAPE, False), _vk_input(_VK_ESCAPE, True))
                self._sleep(self.options["dismiss_delay_ms"])
            _set_clipboard_text("\n")
            self._sleep(20)
            _paste_shortcut()
        else:
            self._tap(_VK_RETURN, dismiss)
        if humanize:
            self._sleep(random.uniform(self.options["line_pause_min_ms"],
                                       self.options["line_pause_max_ms"]))
        elif interval_ms:
            self._sleep(interval_ms)

    def type_text(self, text: str, interval_ms: int, start_delay_s: float,
                  humanize: bool = True, unicode_only: bool = False,
                  dismiss_suggest: bool | None = None, paste_mode: bool = False) -> int:
        """输入文本，返回被跳过的"非 Unicode"字符数。

        indent_mode:
          human — 逐行对齐：回车后选中该行空白，用目标缩进替换，再逐字输入正文（默认）
          none  — 原样逐字，不做缩进对齐
        paste_mode=True 时整段走剪贴板粘贴（最保真，会覆盖剪贴板）。
        """
        if os.name != "nt":
            raise RuntimeError("SendInput 逐字输入仅支持 Windows")

        if start_delay_s > 0:
            self._sleep(start_delay_s * 1000.0)

        self.skipped = 0
        self.typed = 0
        self.stopped = False
        self._stop.clear()
        self.clear_breakpoint()   # 完整重新输出：丢弃旧断点

        if paste_mode:
            if self._stop.is_set():
                self.stopped = True
                return 0
            _set_clipboard_text(text)
            self._sleep(50)
            _paste_shortcut()
            self.typed = len(text)
            return 0

        dismiss = self.options["dismiss_suggest"] if dismiss_suggest is None else dismiss_suggest
        text = normalize_code_text(text, bool(self.options["clean_invisibles"]))
        mode = self.options["indent_mode"]

        if mode == "none":
            self._type_plain(text, interval_ms, humanize, unicode_only, dismiss)
        else:
            self._type_code(text, interval_ms, humanize, unicode_only, dismiss)
        return self.skipped

    def _type_plain(self, text: str, interval_ms: int, humanize: bool,
                    unicode_only: bool, dismiss: bool) -> None:
        self._arm_mouse_guard()
        for ch in text:
            if self._stop.is_set():
                self.stopped = True
                break
            if ch == "\n":
                self._press_enter(humanize, int(interval_ms), dismiss,
                                  bool(self.options["enter_via_paste"]))
                self.typed += 1
                continue
            if self._type_char(ch, humanize, int(interval_ms), unicode_only, dismiss):
                self.typed += 1

    def _type_code(self, text: str, interval_ms: int, humanize: bool,
                   unicode_only: bool, dismiss: bool) -> None:
        """逐行对齐输入：每行独立处理缩进，正文仍逐字输入以保留拟人节奏。

        首行不整行替换（避免毁掉光标所在行已有内容）；其余行先回车，
        再选中该行空白并用目标缩进替换，然后输入正文。
        """
        tab_size = int(self.options["tab_size"])
        lines = split_code_lines(text, tab_size)
        done: set[int] = set()
        total = len(lines)

        for index, (cols, body) in enumerate(lines):
            if self._stop.is_set():
                self.stopped = True
                break
            line_no = index + 1
            self.progress = {"done_lines": sorted(done), "current_line": line_no,
                             "total_lines": total}

            if index == 0:
                typed = self._type_indent_cols(cols, dismiss)
            else:
                # 代码模式一律用真实回车，避免剪贴板竞态与不触发自动缩进的问题
                self._press_enter(humanize, int(interval_ms), dismiss, False)
                # 不选中整行（会变蓝），改用 Ctrl+[ 清掉自动缩进再输入目标缩进
                self._clear_line_indent(dismiss)
                typed = self._type_indent_cols(cols, dismiss)

            self._arm_mouse_guard()
            for ch in body:
                if self._stop.is_set():
                    self.stopped = True
                    break
                if self._type_char(ch, humanize, int(interval_ms), unicode_only, dismiss):
                    typed += 1
                    self.typed += 1
            if self.stopped:
                if typed > 0:
                    self._clear_current_line()  # 安全清掉当前半行
                self.progress = {"done_lines": sorted(done), "current_line": line_no,
                                 "total_lines": total}
                break
            done.add(line_no)

    # ==================== 第三阶段：按 LLM 给的"人类写作顺序"输入 ====================
    @staticmethod
    def _parse_plan(plan, n: int, subset: bool = False):
        """校验并归一化 plan，返回 (order, pauses, revisits)；非法返回 None。

        subset=True 时 order 只要求是 1..n 的一个子集（断点续传只用剩余行）。
        """
        if not isinstance(plan, dict):
            return None
        raw = plan.get("order")
        if not isinstance(raw, list):
            return None
        try:
            order = [int(x) for x in raw]
        except (TypeError, ValueError):
            return None
        if subset:
            if not order or len(set(order)) != len(order) or any(not (1 <= x <= n) for x in order):
                return None
        elif sorted(order) != list(range(1, n + 1)):
            return None
        pauses: dict[int, float] = {}
        for item in plan.get("pauses") or []:
            if not isinstance(item, dict):
                continue
            try:
                after = int(item.get("after"))
                ms = float(item.get("ms"))
            except (TypeError, ValueError):
                continue
            if 1 <= after <= n and ms > 0:
                pauses[after] = min(ms, 180000.0)  # 允许"思考"长停顿（上限 3 分钟）
        revisits: list[int] = []
        for item in plan.get("revisit") or []:
            try:
                line_no = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= line_no <= n and line_no not in revisits:
                revisits.append(line_no)
        return order, pauses, revisits[:3]

    def _move_to_block_line(self, cur: int, target: int, dismiss: bool) -> int:
        """在已建好的代码块内横向/纵向移动到第 target 行的行首。"""
        if dismiss:
            _send_inputs(_vk_input(_VK_ESCAPE, False), _vk_input(_VK_ESCAPE, True))
            self._sleep(self.options["dismiss_delay_ms"])
        delta = target - cur
        if delta:
            vk = _VK_DOWN if delta > 0 else _VK_UP
            for _ in range(abs(delta)):
                _send_inputs(_vk_input(vk, False), _vk_input(vk, True))
        _send_inputs(_vk_input(_VK_HOME, False), _vk_input(_VK_HOME, True))
        return target

    def _backspace(self, n: int) -> None:
        for _ in range(max(0, int(n))):
            _send_inputs(_vk_input(_VK_BACK, False), _vk_input(_VK_BACK, True))

    def _clear_current_line(self) -> None:
        """清空当前行内容（不含换行）：Home → Shift+End 选中 → Delete。

        不用"退格 N 次"：自动补全/自动配对的 overtype 会让逻辑字数≠实际字数，
        多退格会吃掉上一行。选中删除不依赖计数，且不会碰换行，最安全。
        """
        _send_inputs(_vk_input(_VK_HOME, False), _vk_input(_VK_HOME, True))
        _send_inputs(_vk_input(_VK_HOME, False), _vk_input(_VK_HOME, True))
        _send_inputs(
            _vk_input(_VK_SHIFT, False),
            _vk_input(_VK_END, False),
            _vk_input(_VK_END, True),
            _vk_input(_VK_SHIFT, True),
        )
        _send_inputs(_vk_input(_VK_DELETE, False), _vk_input(_VK_DELETE, True))

    def _type_line_text(self, line: str, interval_ms: int, humanize: bool,
                        unicode_only: bool, dismiss: bool) -> int:
        """逐字输入一整行；被停止时把已输入的这一行(半行)退格清掉。

        返回该行实际输入的逻辑字符数（用于断点/清理）。
        """
        self._arm_mouse_guard()  # 每行逐字开始前重置鼠标基线
        i = 0
        while i < len(line) and line[i] in (" ", "\t"):
            i += 1
        indent, body = line[:i], line[i:]
        typed = 0
        if indent:
            # 缩进整段粘贴：瞬间到位（像按了 Tab），避免"从行首逐个空格挪过去"的观感；
            # 也避开 VS Code 对 Tab 键的智能处理。空白内容不会被自动缩进重排。
            _set_clipboard_text(indent)
            self._sleep(100)
            _paste_shortcut()
            self._sleep(150)
            typed += len(indent)
        for ch in body:
            if self._stop.is_set():
                break
            if self._type_char(ch, humanize, int(interval_ms), unicode_only, dismiss):
                typed += 1
                self.typed += 1
        if self._stop.is_set():
            if typed > 0:
                self._clear_current_line()   # 安全清掉当前半行（不依赖字数、不碰上一行）
            return -typed
        return typed

    def _revisit_line(self, unicode_only: bool, dismiss: bool) -> None:
        """回到行尾，敲几个字再退掉：看起来像回头检查/修改，但不改变内容。"""
        _send_inputs(_vk_input(_VK_END, False), _vk_input(_VK_END, True))
        n = random.randint(1, 3)
        for _ in range(n):
            self._unicode_char(random.choice(_TYPO_POOL))
            self._sleep(random.uniform(50, 150))
        self._sleep(random.uniform(200, 500))
        self._backspace(n)

    def type_plan(self, text: str, plan, interval_ms: int = 0,
                  start_delay_s: float = 0.0, humanize: bool = True,
                  unicode_only: bool = False,
                  dismiss_suggest: bool | None = None, resume: bool = False) -> int:
        """按 plan 的行序逐行输入 text（内容取自 text，顺序/停顿来自 plan）。

        - 全新输出(resume=False)：按回车准备 N 行空行，再按 plan["order"] 逐行填入。
        - 断点续传(resume=True)：不再建行，直接在已有文档里写 plan["order"] 指定的
          剩余行（order 为其子集）；当前光标所在行由 self.progress["current_line"] 给出。
        被停止时记录断点（已写完的行 + 当前行号），并把半行退格清掉。
        order 非法时抛 ValueError（调用方回退到逐字 human 模式）。
        """
        if os.name != "nt":
            raise RuntimeError("SendInput 逐字输入仅支持 Windows")
        tab_size = max(1, int(self.options["tab_size"]))
        style = self.options["indent_style"]
        normalized = normalize_code_text(text, bool(self.options["clean_invisibles"]))
        lines = [build_indent(cols, style, tab_size) + body
                 for cols, body in split_code_lines(normalized, tab_size)]
        n = len(lines)
        if n == 0:
            return 0
        parsed = self._parse_plan(plan, n, subset=resume)
        if parsed is None:
            raise ValueError("plan.order 不是合法的行序")
        order, pauses, revisits = parsed

        if start_delay_s > 0:
            self._sleep(start_delay_s * 1000.0)
        self.skipped = 0
        self.typed = 0
        self.stopped = False
        self._stop.clear()
        if not resume:
            self.clear_breakpoint()
        done = set(self.progress.get("done_lines") or []) if resume else set()

        dismiss = self.options["dismiss_suggest"] if dismiss_suggest is None else dismiss_suggest
        typo_saved = self.options.get("typo_rate", 0.0)
        self.options["typo_rate"] = 0.0  # 计划模式不主动打错，保证内容 100% 正确
        try:
            if resume:
                # 键盘流中断后续传：文档可能还没有 N 行，先补足空白行（清掉自动缩进）
                if self.progress.get("mode") == "keys":
                    need = int(self.progress.get("total_lines") or 0) - int(
                        self.progress.get("current_line") or 0)
                    self._ensure_blank_lines(need, dismiss)
                # 续传：以计划 order 为准定位；仅当进度里的当前行确实在 order 中时才用它
                cur = int(self.progress.get("current_line") or 0)
                if cur not in order:
                    cur = order[0] if order else 1
            else:
                self._tap(_VK_HOME, False)
                for _ in range(n - 1):
                    if self._stop.is_set():
                        self.stopped = True
                        self.progress = {"done_lines": sorted(done),
                                         "current_line": 0, "total_lines": n}
                        return self.skipped
                    self._tap(_VK_RETURN, dismiss)
                cur = n
            for line_no in order:
                if self._stop.is_set():
                    self.stopped = True
                    break
                cur = self._move_to_block_line(cur, line_no, dismiss)
                self.progress = {"done_lines": sorted(done),
                                 "current_line": line_no, "total_lines": n}
                self._type_line_text(lines[line_no - 1], int(interval_ms),
                                     humanize, unicode_only, dismiss)
                if self._stop.is_set():
                    self.stopped = True
                    self.progress = {"done_lines": sorted(done),
                                     "current_line": line_no, "total_lines": n}
                    return self.skipped
                done.add(line_no)
                if line_no in pauses:
                    self._sleep(pauses[line_no])
            for line_no in revisits:
                if self._stop.is_set():
                    self.stopped = True
                    break
                cur = self._move_to_block_line(cur, line_no, dismiss)
                self._revisit_line(unicode_only, dismiss)
            self.progress = {"done_lines": sorted(done),
                             "current_line": cur, "total_lines": n}
        finally:
            self.options["typo_rate"] = typo_saved
        return self.skipped

    def _ensure_blank_lines(self, need: int, dismiss: bool) -> None:
        """在文末补 need 个空白行（补充回车后清掉自动缩进），供键盘流续传使用。"""
        if need <= 0:
            return
        self._ctrl_key(_VK_END)
        for _ in range(need):
            self._tap(_VK_RETURN, False)
            self._clear_line_indent(dismiss)   # 安全：把该行缩进清到 0（不删换行）

    def _shift_tab(self) -> None:
        _send_inputs(
            _vk_input(_VK_SHIFT, False),
            _vk_input(_VK_TAB, False),
            _vk_input(_VK_TAB, True),
            _vk_input(_VK_SHIFT, True),
        )

    def type_keys(self, keys, target: str, interval_ms: int = 0,
                  start_delay_s: float = 0.0, humanize: bool = True,
                  unicode_only: bool = False,
                  dismiss_suggest: bool | None = None) -> int:
        """执行 LLM 给的"键盘流"按键序列（Enter/Tab/S-Tab/Up/Down/文本）。

        执行前用 _KeyMirror 回放校验：simulate_keys(keys) 必须等于 target，
        否则抛 ValueError（调用方回退到行序计划 / 逐字模式），保证绝不写错。
        """
        if os.name != "nt":
            raise RuntimeError("SendInput 逐字输入仅支持 Windows")
        tab_size = max(1, int(self.options["tab_size"]))
        insert_spaces = self.options["indent_style"] != "tabs"
        auto_indent = bool(self.options.get("keystream_autoindent", True))
        if not keys_valid(keys, target, tab_size, insert_spaces, auto_indent):
            raise ValueError("键盘流回放与目标不一致")
        normalized = normalize_code_text(target)
        total = normalized.rstrip("\n").count("\n") + 1

        if start_delay_s > 0:
            self._sleep(start_delay_s * 1000.0)
        self.skipped = 0
        self.typed = 0
        self.stopped = False
        self._stop.clear()
        self.clear_breakpoint()

        dismiss = self.options["dismiss_suggest"] if dismiss_suggest is None else dismiss_suggest
        typo_saved = self.options.get("typo_rate", 0.0)
        self.options["typo_rate"] = 0.0  # 保证内容 100% 正确
        mirror = _KeyMirror(tab_size, insert_spaces, auto_indent)
        self._arm_mouse_guard()
        try:
            for token in keys:
                if self._stop.is_set():
                    self.stopped = True
                    break
                if token == "<Enter>":
                    self._tap(_VK_RETURN, dismiss)
                elif token == "<Tab>":
                    self._tap(_VK_TAB, dismiss)
                elif token in ("<S-Tab>", "<Shift+Tab>"):
                    self._shift_tab()
                elif token == "<Up>":
                    self._tap(_VK_UP, dismiss)
                elif token == "<Down>":
                    self._tap(_VK_DOWN, dismiss)
                else:
                    for ch in token:
                        if self._stop.is_set():
                            self.stopped = True
                            break
                        if self._type_char(ch, humanize, int(interval_ms),
                                           unicode_only, dismiss):
                            self.typed += 1
                mirror.apply(token)
                self.progress = {
                    "done_lines": list(range(1, mirror.li + 1)),
                    "current_line": mirror.li + 1,
                    "total_lines": total,
                    "mode": "keys",
                }
                if self.stopped:
                    break
        finally:
            self.options["typo_rate"] = typo_saved
        if self.stopped:
            self._clear_current_line()   # 安全清掉当前半行
        self.progress["done_lines"] = list(range(1, mirror.li + 1))
        self.progress["current_line"] = mirror.li + 1
        return self.skipped

    @staticmethod
    def _unicode_char(ch: str) -> None:
        code = ord(ch)
        if code > 0xFFFF:  # 非 BMP（如部分 emoji）拆成代理对
            code -= 0x10000
            high = 0xD800 + (code >> 10)
            low = 0xDC00 + (code & 0x3FF)
            units = (high, low)
        else:
            units = (code,)
        for unit in units:
            _send_inputs(_unicode_input(unit, False), _unicode_input(unit, True))


# ============================ 通用工具 ============================
def fatal(message: str) -> None:
    """无控制台模式下也能让用户看到启动错误。"""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "ScannerQA Agent", 0x10)
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(message)


def load_config() -> dict:
    path = Path(os.environ.get("CAPTURE_AGENT_CONFIG", APP_DIR / "config.json"))
    if not path.exists():
        fatal(f"找不到配置文件: {path}\n请把 config.example.json 复制为 config.json 并填写。")
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        fatal(f"配置文件无法解析: {path}\n{exc}")
    return data


def setup_logging() -> logging.Logger:
    base = os.environ.get("LOCALAPPDATA") or str(APP_DIR)
    log_dir = Path(base) / "scanner-qa"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "agent.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger("capture-agent")


def grab_jpeg(sct, monitor: int, scale: float, quality: int) -> bytes:
    mon = sct.monitors[monitor]
    shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if scale and scale != 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def post_result(base: str, headers: dict, log: logging.Logger,
                kind: str, ok: bool, chars: int = 0, skipped: int = 0,
                error: str | None = None, progress: dict | None = None) -> None:
    body = {"kind": kind, "ok": ok, "chars": chars, "skipped": skipped, "error": error}
    if progress is not None:
        body["progress"] = progress
    try:
        requests.post(
            f"{base}/result",
            json=body,
            headers=headers,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("上报结果失败: %s", exc)


def post_action(base: str, headers: dict, log: logging.Logger, action: str,
                payload: dict | None = None) -> None:
    body = {"action": action}
    if payload:
        body.update(payload)
    try:
        requests.post(f"{base}/action", json=body, headers=headers, timeout=10)
        log.info("已投递热键动作: %s", body)
    except Exception as exc:  # noqa: BLE001
        log.warning("投递热键动作失败: %s", exc)


# ============================ 全局热键（网页可配置） ============================
_DEFAULT_HOTKEYS = {
    "capture": "ctrl+shift+alt+8",
    "analyze": "ctrl+shift+alt+9",
    "type_answer": "ctrl+shift+alt+0",
    "clear": "ctrl+shift+alt+minus",
    "resume": "ctrl+shift+alt+plus",
}
_MOD_CTRL = {"ctrl", "ctrl_l", "ctrl_r"}
_MOD_SHIFT = {"shift", "shift_l", "shift_r"}
_MOD_ALT = {"alt", "alt_l", "alt_r", "alt_gr"}
_MOD_WIN = {"cmd", "cmd_l", "cmd_r"}
_VK_NAMES = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "escape": 0x1B,
    "esc": 0x1B, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "minus": 0xBD, "-": 0xBD, "plus": 0xBB, "=": 0xBB, "equal": 0xBB,
}


def token_to_vk(token: str) -> int | None:
    token = token.lower()
    if token in _VK_NAMES:
        return _VK_NAMES[token]
    if len(token) == 1 and token.isalnum():
        return ord(token.upper())
    if token.startswith("f") and token[1:].isdigit():
        n = int(token[1:])
        if 1 <= n <= 24:
            return 0x70 + n - 1
    return None


def parse_hotkey(combo: str):
    """把 'ctrl+shift+alt+8' 解析为 (frozenset(mods), vk)。"""
    mods = set()
    vk = None
    for token in str(combo).lower().replace(" ", "").split("+"):
        if token in ("ctrl", "control"):
            mods.add("ctrl")
        elif token == "shift":
            mods.add("shift")
        elif token == "alt":
            mods.add("alt")
        elif token in ("win", "super", "meta"):
            mods.add("win")
        elif token:
            value = token_to_vk(token)
            if value is not None:
                vk = value
    if vk is None:
        return None, None
    return frozenset(mods), vk


def _vk_char(vk: int) -> str | None:
    if 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    return None


class GlobalHotkeys:
    """热键映射来自网页配置；除 stop 本地执行外，其余投递动作给 S。"""

    def __init__(self, base: str, headers: dict, log: logging.Logger,
                 typer: "SendInputTyper | None") -> None:
        self.base = base
        self.headers = headers
        self.log = log
        self.typer = typer
        self.ctrl = self.shift = self.alt = self.win = False
        self.listener = None
        self._lock = threading.Lock()
        self._by_key: dict = {}

    def set_mapping(self, hotkeys: dict) -> None:
        mapping = {}
        for action, combo in (hotkeys or {}).items():
            mods, vk = parse_hotkey(combo)
            if vk is None:
                self.log.warning("无法解析热键 %s=%s", action, combo)
                continue
            mapping[(mods, "vk", vk)] = action
            ch = _vk_char(vk)
            if ch:
                mapping[(mods, "char", ch)] = action
        with self._lock:
            self._by_key = mapping
        self.log.info("热键映射已更新: %s", hotkeys)

    def _pressed_mods(self) -> frozenset:
        return frozenset(
            name for name, on in (("ctrl", self.ctrl), ("shift", self.shift),
                                  ("alt", self.alt), ("win", self.win)) if on
        )

    def _on_press(self, key) -> None:
        name = getattr(key, "name", None)
        if name in _MOD_CTRL:
            self.ctrl = True
            return
        if name in _MOD_SHIFT:
            self.shift = True
            return
        if name in _MOD_ALT:
            self.alt = True
            return
        if name in _MOD_WIN:
            self.win = True
            return
        mods = self._pressed_mods()
        vk = getattr(key, "vk", None)
        ch = getattr(key, "char", None)
        with self._lock:
            action = self._by_key.get((mods, "vk", vk))
            if action is None and ch:
                action = self._by_key.get((mods, "char", ch.lower()))
        if action:
            self._trigger(action)

    def _on_release(self, key) -> None:
        name = getattr(key, "name", None)
        if name in _MOD_CTRL:
            self.ctrl = False
        elif name in _MOD_SHIFT:
            self.shift = False
        elif name in _MOD_ALT:
            self.alt = False
        elif name in _MOD_WIN:
            self.win = False

    def _trigger(self, action: str) -> None:
        if action == "resume":
            # 断点续传：把 B 端的断点进度一起告诉 S，由 S 重新生成剩余行的计划
            progress = self.typer.progress if self.typer is not None else {}
            post_action(self.base, self.headers, self.log, "resume",
                        {"done_lines": list(progress.get("done_lines") or []),
                         "total_lines": int(progress.get("total_lines") or 0)})
            return
        post_action(self.base, self.headers, self.log, action)

    def start(self):
        try:
            from pynput import keyboard
        except Exception as exc:  # noqa: BLE001
            self.log.warning("pynput 不可用，全局热键关闭: %s", exc)
            return None
        try:
            self.listener = keyboard.Listener(
                on_press=self._on_press, on_release=self._on_release)
            self.listener.daemon = True
            self.listener.start()
            return self.listener
        except Exception as exc:  # noqa: BLE001
            self.log.warning("启动全局热键失败: %s", exc)
            return None


def fetch_hotkeys(base: str, headers: dict, log: logging.Logger) -> dict:
    try:
        resp = requests.get(f"{base}/hotkeys", headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("hotkeys") or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("拉取热键配置失败: %s", exc)
        return {}


def start_hotkeys(base: str, headers: dict, log: logging.Logger,
                  typer, initial: dict):
    gh = GlobalHotkeys(base, headers, log, typer)
    gh.set_mapping(initial or _DEFAULT_HOTKEYS)
    gh.start()

    def sync_loop() -> None:
        while True:
            time.sleep(60)
            hotkeys = fetch_hotkeys(base, headers, log)
            if hotkeys:
                gh.set_mapping(hotkeys)

    threading.Thread(target=sync_loop, daemon=True).start()
    return gh


def start_control_loop(base: str, headers: dict, log: logging.Logger,
                       typer: "SendInputTyper") -> None:
    """后台长轮询 S 的 /control：网页"停止"按钮 → 立即停止 B 端键盘输出。

    （打字时主循环在同步执行 type 任务、不会去 /pending，所以停止必须走这条独立通道。）
    """
    control_url = f"{base}/control?wait=25"

    def loop() -> None:
        while True:
            try:
                resp = requests.get(control_url, headers=headers, timeout=40)
                resp.raise_for_status()
                data = resp.json()
                if data.get("stop"):
                    typer.stop("web")
                    log.info("收到网页停止指令：已请求停止键盘输出")
            except Exception as exc:  # noqa: BLE001
                log.debug("控制通道轮询失败: %s", exc)
                time.sleep(2)

    threading.Thread(target=loop, name="control-loop", daemon=True).start()


def main() -> int:
    cfg = load_config()
    try:
        log = setup_logging()
    except OSError:
        log = logging.getLogger("capture-agent")

    base = str(cfg["server_url"]).rstrip("/")
    headers = {"X-Auth-Token": cfg["token"]} if cfg["token"] else {}
    pending_url = f"{base}/pending?wait={int(cfg['poll_wait'])}"
    frame_url = f"{base}/frame"
    poll_timeout = int(cfg["poll_wait"]) + 10
    typer = SendInputTyper(cfg)
    typer.log = log
    log.info("启动: S=%s monitor=%s", base, cfg["monitor"])
    if cfg.get("hotkeys_enabled", True):
        initial = fetch_hotkeys(base, headers, log) or cfg.get("hotkeys") or _DEFAULT_HOTKEYS
        start_hotkeys(base, headers, log, typer, initial)
    start_control_loop(base, headers, log, typer)

    backoff = 1
    with _MSS() as sct:
        while True:
            try:
                resp = requests.get(pending_url, headers=headers, timeout=poll_timeout)
                resp.raise_for_status()
                backoff = 1
                job = resp.json().get("job")
                if not job:
                    continue

                kind = job.get("type")
                if kind == "capture":
                    typer.clear_breakpoint()   # 截屏 = 新会话，丢弃旧断点
                    try:
                        jpeg = grab_jpeg(sct, int(cfg["monitor"]),
                                         float(cfg["scale"]), int(cfg["jpeg_quality"]))
                        requests.post(frame_url, data=jpeg,
                                      headers={**headers, "Content-Type": "image/jpeg"},
                                      timeout=30).raise_for_status()
                        log.info("已上传截图 %d bytes", len(jpeg))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("截屏失败: %s", exc)
                        post_result(base, headers, log, "capture", False, error=str(exc))
                elif kind == "type":
                    text = str(job.get("text", ""))
                    plan = job.get("plan")
                    keys = job.get("keys")
                    resume = bool(job.get("resume"))
                    try:
                        typer.set_options(job.get("options"))
                        skipped = None
                        # 第三阶段（首选）：LLM 键盘流；执行前回放校验，失败则回退行序计划
                        if (not resume and isinstance(keys, list) and keys):
                            try:
                                skipped = typer.type_keys(
                                    keys, text,
                                    int(job.get("interval_ms", 60)),
                                    float(job.get("start_delay_s", 3)),
                                    bool(job.get("humanize", True)),
                                    bool(job.get("unicode_only", False)),
                                    bool(job.get("dismiss_suggest", True)),
                                )
                                log.info("已按键盘流输入 %d 字", typer.typed)
                            except ValueError as exc:
                                log.warning("键盘流不可用（%s），回退行序计划", exc)
                                skipped = None
                        if skipped is None and isinstance(plan, dict) and plan.get("order"):
                            try:
                                skipped = typer.type_plan(
                                    text, plan,
                                    int(job.get("interval_ms", 60)),
                                    float(job.get("start_delay_s", 3)),
                                    bool(job.get("humanize", True)),
                                    bool(job.get("unicode_only", False)),
                                    bool(job.get("dismiss_suggest", True)),
                                    resume=resume,
                                )
                                log.info("已按键盘序列输入 %d 字 (%s)",
                                         typer.typed, "续传" if resume else "完整")
                            except ValueError as exc:
                                log.warning("键盘序列不可用（%s）", exc)
                                if resume:
                                    # 续传计划无效时**绝不能**回退成整段重打（会从头再来）
                                    post_result(base, headers, log, "type", False,
                                                error=f"resume plan invalid: {exc}",
                                                progress=typer.progress)
                                    continue
                                log.warning("回退逐字输入")
                                skipped = None
                        if skipped is None and not resume:
                            skipped = typer.type_text(
                                text,
                                int(job.get("interval_ms", 60)),
                                float(job.get("start_delay_s", 3)),
                                bool(job.get("humanize", True)),
                                bool(job.get("unicode_only", False)),
                                bool(job.get("dismiss_suggest", True)),
                                bool(job.get("paste_mode", False)),
                            )
                            log.info("已逐字输入 %d 字（跳过 %d）", typer.typed, skipped)
                        post_result(base, headers, log, "type", True,
                                    chars=typer.typed, skipped=skipped,
                                    error=typer.stop_reason if typer.stopped else None,
                                    progress=typer.progress)
                        if typer.stopped:
                            log.info("输入被中断，原因=%s，进度=%s",
                                     typer.stop_reason, typer.progress)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("输入失败: %s", exc)
                        post_result(base, headers, log, "type", False, error=str(exc))
                else:
                    log.warning("未知任务类型: %s", kind)
            except KeyboardInterrupt:
                log.info("收到中断，退出")
                break
            except Exception as exc:  # noqa: BLE001 - 常驻服务需持续重试
                log.warning("出错: %s（%ss 后重试）", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
