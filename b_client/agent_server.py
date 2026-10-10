#!/usr/bin/env python3
"""Target 端 HTTP 服务（Windows）。

给 Controller（agent）提供一组"能力端点"，实现与 agent 解耦：
  GET  /health                          → {ok, width, height}
  GET  /size                            → {width, height}
  GET  /screenshot[?quality=85]         → image/jpeg（mss 抓主屏）
  POST /input   {kind: type|key|move|click|scroll, ...}
  GET  /events?wait=N                   → 长轮询，返回一个热键事件

只保留两个全局热键：
  - prompt_screenshot（默认 ctrl+shift+alt+8）：让 agent 去截屏看情况
  - stop_typing（默认 ctrl+shift+alt+0）     ：停止正在进行的打字

type 只接受 ASCII（0x20-0x7E + \\n + \\t），其它字符被剔除并在返回里报告。
键盘/鼠标用 SendInput 注入；截图用 mss。复用 capture_agent 里的 SendInputTyper。

配置 config.json（与 exe 同目录）：
  { "token": "...", "port": 8600, "monitor": 1, "jpeg_quality": 85, "scale": 1.0,
    "hotkeys": {"prompt_screenshot": "ctrl+shift+alt+8", "stop_typing": "ctrl+shift+alt+0"} }
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import capture_agent as ca

_MSS = ca._MSS


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()

SERVER_DEFAULTS = {
    "token": "",
    "port": 8600,
    "monitor": 1,
    "jpeg_quality": 85,
    "scale": 1.0,
    "hotkeys": {
        "prompt_screenshot": "ctrl+shift+alt+8",
        "stop_typing": "ctrl+shift+alt+0",
    },
}


def load_config() -> dict:
    data = dict(SERVER_DEFAULTS)
    path = Path(os.environ.get("SQA_AGENT_CONFIG", APP_DIR / "config.json"))
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                hk = dict(data["hotkeys"])
                hk.update(loaded.get("hotkeys") or {})
                data.update({k: v for k, v in loaded.items() if k in SERVER_DEFAULTS})
                data["hotkeys"] = hk
        except (OSError, json.JSONDecodeError):
            pass
    return data


def setup_logging() -> logging.Logger:
    base = os.environ.get("LOCALAPPDATA") or str(APP_DIR)
    log_dir = Path(base) / "scanner-qa"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "agent.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger = logging.getLogger("agent-server")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        return logger
    except OSError:
        logger = logging.getLogger("agent-server")
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler())
        return logger


# ============================ ASCII 过滤 ============================
def ascii_only(text: str) -> tuple[str, int]:
    """只保留 0x20-0x7E 与 \\n \\t；返回 (过滤后文本, 被剔除字符数)。"""
    out = []
    rejected = 0
    for ch in text:
        code = ord(ch)
        if ch in ("\n", "\t") or 0x20 <= code <= 0x7E:
            out.append(ch)
        else:
            rejected += 1
    return "".join(out), rejected


# ============================ 鼠标注入（SendInput） ============================
if os.name == "nt":
    _ME_MOVE = 0x0001
    _ME_LEFTDOWN = 0x0002
    _ME_LEFTUP = 0x0004
    _ME_RIGHTDOWN = 0x0008
    _ME_RIGHTUP = 0x0010
    _ME_MIDDLEDOWN = 0x0020
    _ME_MIDDLEUP = 0x0040
    _ME_WHEEL = 0x0800
    _ME_HWHEEL = 0x1000
    _ME_ABSOLUTE = 0x8000

    _BUTTONS = {
        "left": (_ME_LEFTDOWN, _ME_LEFTUP),
        "right": (_ME_RIGHTDOWN, _ME_RIGHTUP),
        "middle": (_ME_MIDDLEDOWN, _ME_MIDDLEUP),
    }
    _MOD_VK = {"ctrl": ca._VK_CONTROL, "shift": ca._VK_SHIFT, "alt": 0x12, "win": 0x5B}
    _MOD_ORDER = ("ctrl", "shift", "alt", "win")


def _send_mouse(dx: int, dy: int, data: int, flags: int) -> None:
    inp = ca._INPUT(type=0,  # 0 = INPUT_MOUSE（之前误用了 keyboard 的 1，导致鼠标事件被忽略）
                    u=ca._INPUTUNION(mi=ca._MOUSEINPUT(
                        int(dx), int(dy), int(data) & 0xFFFFFFFF, int(flags), 0, 0)))
    ca._send_inputs(inp)


def screen_size() -> tuple[int, int]:
    user32 = ctypes.windll.user32
    return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))


def mouse_move_abs(x_norm: float, y_norm: float) -> None:
    """归一化 0..1 → 绝对移动。"""
    x_norm = min(1.0, max(0.0, float(x_norm)))
    y_norm = min(1.0, max(0.0, float(y_norm)))
    ax = int(round(x_norm * 65535))
    ay = int(round(y_norm * 65535))
    _send_mouse(ax, ay, 0, _ME_MOVE | _ME_ABSOLUTE)


def mouse_move_rel(dx: int, dy: int) -> None:
    _send_mouse(int(dx), int(dy), 0, _ME_MOVE)


def mouse_click(button: str = "left", count: int = 1) -> None:
    down, up = _BUTTONS.get(button, _BUTTONS["left"])
    for _ in range(max(1, int(count))):
        _send_mouse(0, 0, 0, down)
        time.sleep(0.02)
        _send_mouse(0, 0, 0, up)
        time.sleep(0.02)


def mouse_scroll(dx: int, dy: int) -> None:
    # 约定：dy>0 = 向下滚。Windows 滚轮正值表示向上，故取负。
    if dy:
        _send_mouse(0, 0, -int(dy) * 120, _ME_WHEEL)
    if dx:
        _send_mouse(0, 0, -int(dx) * 120, _ME_HWHEEL)


def send_key_combo(combo: str) -> None:
    mods, vk = ca.parse_hotkey(combo)
    if vk is None:
        raise ValueError(f"无法解析按键: {combo!r}")
    order = [m for m in _MOD_ORDER if m in mods]
    seq = [ca._vk_input(_MOD_VK[m], False) for m in order]
    seq += [ca._vk_input(vk, False), ca._vk_input(vk, True)]
    seq += [ca._vk_input(_MOD_VK[m], True) for m in reversed(order)]
    ca._send_inputs(*seq)


# ============================ 运行时状态 ============================
class Runtime:
    def __init__(self, cfg: dict, log: logging.Logger) -> None:
        self.cfg = cfg
        self.log = log
        self.token = str(cfg.get("token") or "")
        self.typer = ca.SendInputTyper(cfg)
        self.typer.log = log
        self.events: "queue.Queue[dict]" = queue.Queue()
        self._sct_lock = threading.Lock()
        self._listener = None

    # ---- 截图 ----
    def screenshot(self, quality: int | None = None) -> bytes:
        q = int(quality or self.cfg["jpeg_quality"])
        with self._sct_lock:
            with _MSS() as sct:
                return ca.grab_jpeg(sct, int(self.cfg["monitor"]),
                                    float(self.cfg["scale"]), q)

    # ---- 输入 ----
    def do_type(self, text: str, humanize: bool = True,
                options: dict | None = None) -> dict:
        clean, rejected = ascii_only(text)
        self.typer.set_options(options or {})
        self.typer.type_text(clean, 0, 0, humanize=bool(humanize),
                             unicode_only=False, dismiss_suggest=True, paste_mode=False)
        return {"accepted": len(clean), "rejected": rejected,
                "stopped": bool(self.typer.stopped)}

    def handle_input(self, body: dict) -> dict:
        kind = str(body.get("kind") or "")
        if kind == "type":
            return self.do_type(str(body.get("text") or ""),
                                bool(body.get("humanize", True)),
                                body.get("options"))
        if kind == "key":
            send_key_combo(str(body.get("combo") or ""))
            return {"ok": True}
        if kind == "move":
            if body.get("relative"):
                mouse_move_rel(int(body.get("dx", 0)), int(body.get("dy", 0)))
            else:
                mouse_move_abs(float(body.get("x", 0)), float(body.get("y", 0)))
            return {"ok": True}
        if kind == "click":
            if body.get("x") is not None and body.get("y") is not None:
                mouse_move_abs(float(body["x"]), float(body["y"]))
                time.sleep(0.05)
            mouse_click(str(body.get("button") or "left"), int(body.get("count", 1)))
            return {"ok": True}
        if kind == "scroll":
            mouse_scroll(int(body.get("dx", 0)), int(body.get("dy", 0)))
            return {"ok": True}
        raise ValueError(f"未知 kind: {kind!r}")

    # ---- 热键 ----
    def start_hotkeys(self) -> None:
        try:
            from pynput import keyboard
        except Exception as exc:  # noqa: BLE001
            self.log.warning("pynput 不可用，热键关闭: %s", exc)
            return
        mapping = {}
        for action, combo in (self.cfg.get("hotkeys") or {}).items():
            mods, vk = ca.parse_hotkey(combo)
            if vk is None:
                self.log.warning("无法解析热键 %s=%s", action, combo)
                continue
            mapping[(mods, "vk", vk)] = action
            ch = ca._vk_char(vk)
            if ch:
                mapping[(mods, "char", ch)] = action
        state = {"ctrl": False, "shift": False, "alt": False, "win": False}
        mod_sets = {
            "ctrl": {"ctrl", "ctrl_l", "ctrl_r"},
            "shift": {"shift", "shift_l", "shift_r"},
            "alt": {"alt", "alt_l", "alt_r", "alt_gr"},
            "win": {"cmd", "cmd_l", "cmd_r"},
        }
        mod_for_name = {n: k for k, names in mod_sets.items() for n in names}

        def on_press(key):
            name = getattr(key, "name", None)
            if name in mod_for_name:
                state[mod_for_name[name]] = True
                return
            mods = frozenset(k for k, v in state.items() if v)
            vk = getattr(key, "vk", None)
            ch = getattr(key, "char", None)
            action = mapping.get((mods, "vk", vk))
            if action is None and ch:
                action = mapping.get((mods, "char", ch.lower()))
            if action:
                self.trigger(action)

        def on_release(key):
            name = getattr(key, "name", None)
            if name in mod_for_name:
                state[mod_for_name[name]] = False

        try:
            self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self._listener.daemon = True
            self._listener.start()
            self.log.info("热键已启动: %s", self.cfg.get("hotkeys"))
        except Exception as exc:  # noqa: BLE001
            self.log.warning("启动热键失败: %s", exc)

    def trigger(self, action: str) -> None:
        self.log.info("热键触发: %s", action)
        if action == "stop_typing":
            self.typer.stop("hotkey")
        self.events.put({"type": action, "ts": time.time()})


# ============================ HTTP ============================
class Handler(BaseHTTPRequestHandler):
    runtime: Runtime = None  # type: ignore

    def log_message(self, *a):  # 静默
        pass

    def _auth_ok(self) -> bool:
        tok = self.runtime.token
        if not tok:
            return True
        got = self.headers.get("X-Auth-Token", "")
        if got == tok:
            return True
        qs = parse_qs(urlparse(self.path).query)
        return qs.get("token", [""])[0] == tok

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/health":
            w, h = screen_size()
            self._json(200, {"ok": True, "width": w, "height": h})
            return
        if not self._auth_ok():
            self._json(401, {"error": "bad token"})
            return
        if path == "/size":
            w, h = screen_size()
            self._json(200, {"width": w, "height": h})
            return
        if path == "/mousepos":
            self._json(200, {"pos": ca._cursor_pos(),
                             "size": screen_size()})
            return
        if path == "/screenshot":
            qs = parse_qs(query)
            quality = int(qs.get("quality", ["0"])[0]) or None
            try:
                jpeg = self.runtime.screenshot(quality)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.end_headers()
            self.wfile.write(jpeg)
            return
        if path == "/events":
            qs = parse_qs(query)
            wait = float(qs.get("wait", ["25"])[0])
            try:
                ev = self.runtime.events.get(timeout=max(0.0, wait))
            except queue.Empty:
                ev = None
            self._json(200, {"event": ev})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        if not self._auth_ok():
            self._json(401, {"error": "bad token"})
            return
        if path == "/input":
            body = self._read_json()
            try:
                result = self.runtime.handle_input(body)
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"ok": True, **result})
            return
        self._json(404, {"error": "not found"})


def main() -> int:
    cfg = load_config()
    log = setup_logging()
    if os.name != "nt":
        log.error("agent_server 仅支持 Windows")
        return 2
    # 统一为 per-monitor DPI 感知：让 GetSystemMetrics/GetCursorPos 与 mss 截图、
    # SendInput 绝对坐标都处于"物理像素"同一坐标系，避免 150% 缩放下坐标错位。
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    runtime = Runtime(cfg, log)
    Handler.runtime = runtime
    if cfg.get("hotkeys"):
        runtime.start_hotkeys()
    port = int(cfg["port"])
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("agent_server 启动，监听 :%d token=%s",
             port, "on" if runtime.token else "off")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
