#!/usr/bin/env python3
"""B 端后台客户端（Windows）。

无窗口常驻：长轮询 S 的 /pending 领取任务并执行：
  - capture：静默抓主屏 → POST /frame
  - type   ：把文本逐字注入当前焦点窗口 → POST /result
同时注册全局热键（Ctrl+Shift+Alt+8 截屏 / +9 分析 / +0 输入回答 / +- 清空对话 / ++ 停止输入）。

中文/Unicode 用 SendInput + KEYEVENTF_UNICODE 逐字注入，不占用剪贴板、不受输入法/键盘布局影响。
打字任务可选：
  - humanize=True：字符间隔 50–120ms 随机、换行后停顿 300–1000ms、0.5% 错字后 Backspace 纠正
  - unicode_only=True：跳过无法作为单个 Unicode 码点发送的字符（emoji、代理区等）
  - dismiss_suggest=True：回车/制表前先按 Esc 关掉 IDE 自动补全弹窗
  - paste_mode=True：整段走剪贴板粘贴，绕过 IDE 补全/自动配对（会覆盖剪贴板）

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
    # 全局热键总开关：Ctrl+Shift+Alt+8/9/0/-/+（Windows 虚拟键码判定）
    "hotkeys_enabled": True,
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


# 输入行为的默认参数（可在 config.json 覆盖）
TYPING_DEFAULTS = {
    "interval_min_ms": 200,      # 字符间隔下限
    "interval_max_ms": 1000,     # 字符间隔上限
    "line_pause_min_ms": 1000,   # 换行后停顿下限
    "line_pause_max_ms": 2000,   # 换行后停顿上限
    "typo_rate": 0.005,          # 每字符出错概率
    "typo_pause_min_ms": 100,    # 错字后停顿下限
    "typo_pause_max_ms": 300,    # 错字后停顿上限
    "dismiss_suggest": True,     # 回车/制表前先按 Esc 关掉 IDE 自动补全弹窗
}

_TYPO_POOL = "abcdefghijklmnopqrstuvwxyz0123456789"


class SendInputTyper:
    """用 Windows SendInput 逐字注入文本，可选拟人化节奏与纯 Unicode 过滤。"""

    def __init__(self, options: dict | None = None) -> None:
        self.options = dict(TYPING_DEFAULTS)
        if options:
            self.options.update({k: options[k] for k in TYPING_DEFAULTS if k in options})
        self.skipped = 0
        self.typed = 0
        self.stopped = False
        self._stop = threading.Event()

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
            span = self.options
            time.sleep(random.uniform(span["interval_min_ms"], span["interval_max_ms"]) / 1000.0)
        elif fixed_ms:
            time.sleep(fixed_ms / 1000.0)

    def _tap(self, vk: int, dismiss_suggest: bool) -> None:
        # IDE 里回车/制表会被当成"接受补全"，先按 Esc 关掉补全弹窗
        if dismiss_suggest:
            _send_inputs(_vk_input(_VK_ESCAPE, False), _vk_input(_VK_ESCAPE, True))
            time.sleep(0.03)
        _send_inputs(_vk_input(vk, False), _vk_input(vk, True))

    def type_text(self, text: str, interval_ms: int, start_delay_s: float,
                  humanize: bool = True, unicode_only: bool = False,
                  dismiss_suggest: bool | None = None, paste_mode: bool = False) -> int:
        """输入文本，返回被跳过的"非 Unicode"字符数。

        paste_mode=True：整段走剪贴板粘贴，绕过 IDE 自动补全/自动配对，最可靠。
        否则逐字输入；dismiss_suggest=True 时回车/制表前先按 Esc 关掉补全弹窗。
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

        for ch in text:
            if self._stop.is_set():
                self.stopped = True
                break
            if unicode_only and not self._sendable_unicode(ch):
                self.skipped += 1
                continue

            if ch in ("\n", "\r"):
                self._tap(_VK_RETURN, dismiss)
                self.typed += 1
                if humanize:
                    time.sleep(random.uniform(self.options["line_pause_min_ms"],
                                              self.options["line_pause_max_ms"]) / 1000.0)
                elif interval_ms:
                    time.sleep(interval_ms / 1000.0)
                continue

            if ch == "\t":
                self._tap(_VK_TAB, dismiss)
                self.typed += 1
                self._normal_interval(humanize, int(interval_ms))
                continue

            # 低频错字：打错 -> 停顿 -> Backspace -> 再打正确的
            if humanize and random.random() < self.options["typo_rate"]:
                self._unicode_char(random.choice(_TYPO_POOL))
                time.sleep(random.uniform(self.options["typo_pause_min_ms"],
                                          self.options["typo_pause_max_ms"]) / 1000.0)
                _send_inputs(_vk_input(_VK_BACK, False), _vk_input(_VK_BACK, True))

            self._unicode_char(ch)
            self.typed += 1
            self._normal_interval(humanize, int(interval_ms))
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


# ============================ 全局热键 ============================
# Ctrl+Shift+Alt + 主键盘 8/9/0/-/=（+）；Windows 用虚拟键码判定，避免 Shift 改写字符。
_VK_ACTION = {
    0x38: "capture",       # 8
    0x39: "analyze",       # 9
    0x30: "type_answer",   # 0
    0xBD: "clear",         # 主键盘 -
    0xBB: "stop_type",     # 主键盘 = / +
    0x6D: "clear",         # 小键盘 -
    0x6B: "stop_type",     # 小键盘 +
}
_CHAR_ACTION = {
    "8": "capture", "9": "analyze", "0": "type_answer",
    "-": "clear", "=": "stop_type", "+": "stop_type",
}
_MOD_CTRL = {"ctrl", "ctrl_l", "ctrl_r"}
_MOD_SHIFT = {"shift", "shift_l", "shift_r"}
_MOD_ALT = {"alt", "alt_l", "alt_r", "alt_gr"}


class GlobalHotkeys:
    """Ctrl+Shift+Alt+8/9/0/-/+ 全局热键：另外三键投递动作给 S，+ 键本地停止输入。"""

    def __init__(self, base: str, headers: dict, log: logging.Logger,
                 typer: "SendInputTyper | None") -> None:
        self.base = base
        self.headers = headers
        self.log = log
        self.typer = typer
        self.ctrl = self.shift = self.alt = False
        self.listener = None

    def _action_for(self, key) -> str | None:
        vk = getattr(key, "vk", None)
        if vk in _VK_ACTION:
            return _VK_ACTION[vk]
        return _CHAR_ACTION.get(getattr(key, "char", None))

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
        if not (self.ctrl and self.shift and self.alt):
            return
        action = self._action_for(key)
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

    def _trigger(self, action: str) -> None:
        if action == "stop_type":
            if self.typer is not None:
                self.typer.stop()
                self.log.info("停止热键：已请求停止当前键盘输出")
            return
        payload = {"extract": True} if action == "analyze" else None
        post_action(self.base, self.headers, self.log, action, payload)

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
            self.log.info("全局热键已启用: Ctrl+Shift+Alt+8/9/0/-/+")
            return self.listener
        except Exception as exc:  # noqa: BLE001
            self.log.warning("启动全局热键失败: %s", exc)
            return None


def start_hotkeys(base: str, headers: dict, log: logging.Logger, typer):
    return GlobalHotkeys(base, headers, log, typer).start()


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
    log.info("启动: S=%s monitor=%s", base, cfg["monitor"])
    if cfg.get("hotkeys_enabled", True):
        start_hotkeys(base, headers, log, typer)

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
