#!/usr/bin/env python3
"""B 端后台客户端（Windows）。

无窗口常驻：长轮询 S 的 /pending 领取任务并执行：
  - capture：静默抓主屏 → POST /frame
  - type   ：把文本逐字注入当前焦点窗口 → POST /result
同时注册全局热键（Ctrl+Shift+Alt+8 截屏 / +9 分析 / +0 输入回答 / +- 清空对话 / ++ 停止输入）。

中文/Unicode 用 SendInput + KEYEVENTF_UNICODE 逐字注入，不占用剪贴板、不受输入法/键盘布局影响。
代码缩进策略（indent_mode）：
  - vscode：预测 VS Code + Python 的自动缩进，仅补差量（默认）
  - target：兜底，无视自动缩进，用 Home×2 + Shift+End 选中后重打目标缩进
  - none  ：原样逐字
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
    _VK_L = 0x4C
    _VK_C = 0x43
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

    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE

    def _get_clipboard_text() -> str:
        """读取剪贴板文本（用于测量 IDE 的实际缩进）。"""
        if not _user32.OpenClipboard(None):
            return ""
        try:
            handle = _user32.GetClipboardData(_CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = _kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                _kernel32.GlobalUnlock(handle)
        finally:
            _user32.CloseClipboard()


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
    "enter_via_paste": False,    # 换行用剪贴板粘贴插入（non-code 模式可用）
    "indent_mode": "vscode",     # vscode / target / none
    "indent_style": "spaces",    # spaces / tabs
    "tab_size": 4,
    "clean_invisibles": True,    # 清理 NBSP/全角空格/零宽/智能引号
}

_TYPO_POOL = "abcdefghijklmnopqrstuvwxyz0123456789"

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


def bracket_delta(body: str) -> int:
    """计算一行括号净变化，忽略字符串与 # 注释。"""
    depth = 0
    quote = None
    i = 0
    while i < len(body):
        c = body[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            break
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        i += 1
    return depth


def predict_indent(prev_body: str, prev_cols: int, bracket_depth: int,
                   bracket_base: int, tab_size: int) -> int:
    """预测 VS Code + Python 回车后自动缩进到的列数。"""
    if bracket_depth > 0:
        return bracket_base + tab_size
    if prev_body.rstrip().endswith(":"):
        return prev_cols + tab_size
    return prev_cols


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
    def stop(self) -> None:
        """请求停止当前输入（供停止热键调用）。"""
        self._stop.set()
        self.stopped = True

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
            time.sleep(random.uniform(self.options["interval_min_ms"],
                                      self.options["interval_max_ms"]) / 1000.0)
        elif fixed_ms:
            time.sleep(fixed_ms / 1000.0)

    def _space_interval(self) -> None:
        time.sleep(self.options["space_interval_ms"] / 1000.0)

    def _ctrl_key(self, vk: int) -> None:
        _send_inputs(
            _vk_input(_VK_CONTROL, False),
            _vk_input(vk, False),
            _vk_input(vk, True),
            _vk_input(_VK_CONTROL, True),
        )

    def _select_line(self, dismiss: bool) -> None:
        if dismiss:
            _send_inputs(_vk_input(_VK_ESCAPE, False), _vk_input(_VK_ESCAPE, True))
            time.sleep(self.options["dismiss_delay_ms"] / 1000.0)
        self._ctrl_key(_VK_L)  # VS Code：选中当前整行

    def _measure_indent(self, dismiss: bool) -> int | None:
        """Ctrl+L 选中当前行 → Ctrl+C → 读剪贴板，返回实际缩进列数（失败返回 None）。"""
        self._select_line(dismiss)
        self._ctrl_key(_VK_C)
        text = ""
        for _ in range(6):
            time.sleep(0.04)
            text = _get_clipboard_text()
            if text:
                break
        if "\n" in text or "\r" in text:
            return None  # 选中包含换行，别用它替换
        cols = 0
        for ch in text:
            if ch == "\t":
                size = int(self.options["tab_size"])
                cols += size - (cols % size)
            elif ch == " ":
                cols += 1
            else:
                return None  # 行内已有正文（并非新行）
        if self.log:
            self.log.info("测得自动缩进 %d 列 (repr=%r)", cols, text)
        return cols

    def _force_indent(self, cols: int, dismiss: bool) -> None:
        """Ctrl+L 选中当前行，再用目标缩进替换：与编辑器的自动缩进无关，结果精确。"""
        self._select_line(dismiss)
        style = self.options["indent_style"]
        self._type_indent(build_indent(cols, style, int(self.options["tab_size"])), dismiss)

    def _tap(self, vk: int, dismiss_suggest: bool = False) -> None:
        # IDE 里回车/制表会被当成"接受补全"，先按 Esc 关掉补全弹窗
        if dismiss_suggest:
            _send_inputs(_vk_input(_VK_ESCAPE, False), _vk_input(_VK_ESCAPE, True))
            time.sleep(self.options["dismiss_delay_ms"] / 1000.0)
        _send_inputs(_vk_input(vk, False), _vk_input(vk, True))

    def _type_char(self, ch: str, humanize: bool, interval_ms: int,
                   unicode_only: bool, dismiss: bool) -> bool:
        """输入一个字符：空格/制表走高速通道；返回 False 表示被跳过。"""
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
            time.sleep(random.uniform(self.options["typo_pause_min_ms"],
                                      self.options["typo_pause_max_ms"]) / 1000.0)
            _send_inputs(_vk_input(_VK_BACK, False), _vk_input(_VK_BACK, True))
        self._unicode_char(ch)
        self._normal_interval(humanize, interval_ms)
        return True

    def _type_indent(self, indent_str: str, dismiss: bool) -> None:
        for ch in indent_str:
            if ch == "\t":
                self._tap(_VK_TAB, dismiss)
            else:
                self._unicode_char(ch)
            self._space_interval()

    def _press_enter(self, humanize: bool, interval_ms: int, dismiss: bool,
                     enter_via_paste: bool) -> None:
        if enter_via_paste:
            if dismiss:
                _send_inputs(_vk_input(_VK_ESCAPE, False), _vk_input(_VK_ESCAPE, True))
                time.sleep(self.options["dismiss_delay_ms"] / 1000.0)
            _set_clipboard_text("\n")
            time.sleep(0.02)
            _paste_shortcut()
        else:
            self._tap(_VK_RETURN, dismiss)
        if humanize:
            time.sleep(random.uniform(self.options["line_pause_min_ms"],
                                      self.options["line_pause_max_ms"]) / 1000.0)
        elif interval_ms:
            time.sleep(interval_ms / 1000.0)

    def type_text(self, text: str, interval_ms: int, start_delay_s: float,
                  humanize: bool = True, unicode_only: bool = False,
                  dismiss_suggest: bool | None = None, paste_mode: bool = False) -> int:
        """输入文本，返回被跳过的"非 Unicode"字符数。

        indent_mode:
          vscode — 预测 VS Code+Python 自动缩进，仅补差量（默认）
          target — 兜底：无视自动缩进，强制到达目标缩进
          none   — 旧行为：原样逐字
        """
        if os.name != "nt":
            raise RuntimeError("SendInput 逐字输入仅支持 Windows")

        if start_delay_s > 0:
            time.sleep(start_delay_s)

        self.skipped = 0
        self.typed = 0
        self.stopped = False
        self._stop.clear()

        if paste_mode:
            if self._stop.is_set():
                self.stopped = True
                return 0
            _set_clipboard_text(text)
            time.sleep(0.05)
            _paste_shortcut()
            self.typed = len(text)
            return 0

        dismiss = self.options["dismiss_suggest"] if dismiss_suggest is None else dismiss_suggest
        text = normalize_code_text(text, bool(self.options["clean_invisibles"]))
        mode = self.options["indent_mode"]

        if mode == "none":
            self._type_plain(text, interval_ms, humanize, unicode_only, dismiss)
        else:
            self._type_code(text, interval_ms, humanize, unicode_only, dismiss, mode)
        return self.skipped

    def _type_plain(self, text: str, interval_ms: int, humanize: bool,
                    unicode_only: bool, dismiss: bool) -> None:
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
                   unicode_only: bool, dismiss: bool, mode: str) -> None:
        tab_size = int(self.options["tab_size"])
        lines = split_code_lines(text, tab_size)
        depth = 0
        base = 0
        prev_body = ""
        prev_cols = 0

        for index, (cols, body) in enumerate(lines):
            if self._stop.is_set():
                self.stopped = True
                break

            if index == 0:
                self._force_indent(cols, dismiss)
            else:
                self._press_enter(humanize, int(interval_ms), dismiss,
                                  bool(self.options["enter_via_paste"]))
                if mode == "target":
                    self._force_indent(cols, dismiss)
                else:  # vscode：测量实际自动缩进，必要时用目标缩进替换
                    actual = self._measure_indent(dismiss)
                    if actual == cols:
                        self._tap(_VK_END, False)  # 缩进已正确，折叠选区到行尾
                    else:
                        # 选区仍覆盖整行，直接用目标缩进替换
                        self._type_indent(
                            build_indent(cols, self.options["indent_style"], tab_size),
                            dismiss,
                        )

            for ch in body:
                if self._stop.is_set():
                    self.stopped = True
                    break
                if self._type_char(ch, humanize, int(interval_ms), unicode_only, dismiss):
                    self.typed += 1
            if self.stopped:
                break

            bd = bracket_delta(body)
            if depth == 0 and bd > 0:
                base = cols
            depth = max(0, depth + bd)
            prev_body, prev_cols = body, cols

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
                error: str | None = None) -> None:
    try:
        requests.post(
            f"{base}/result",
            json={"kind": kind, "ok": ok, "chars": chars, "skipped": skipped, "error": error},
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
    "stop": "ctrl+shift+alt+plus",
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
        if action == "stop":
            if self.typer is not None:
                self.typer.stop()
                self.log.info("停止热键：已请求停止键盘输出")
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
                    try:
                        typer.set_options(job.get("options"))
                        skipped = typer.type_text(
                            text,
                            int(job.get("interval_ms", 60)),
                            float(job.get("start_delay_s", 3)),
                            bool(job.get("humanize", True)),
                            bool(job.get("unicode_only", False)),
                            bool(job.get("dismiss_suggest", True)),
                            bool(job.get("paste_mode", False)),
                        )
                        note = "（已被停止热键中断）" if typer.stopped else ""
                        log.info("已逐字输入 %d 字（跳过 %d）%s", typer.typed, skipped, note)
                        post_result(base, headers, log, "type", True,
                                    chars=typer.typed, skipped=skipped,
                                    error="stopped" if typer.stopped else None)
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
