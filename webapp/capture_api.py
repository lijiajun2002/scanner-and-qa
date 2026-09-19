from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

DEFAULT_PORT = 8503
DEFAULT_WAIT = 25.0
MAX_WAIT = 60.0

_lock = threading.Lock()
_jobs: "queue.Queue[dict]" = queue.Queue()
_last_frame: tuple[bytes | None, float] = (None, 0.0)
_last_result: dict | None = None
_token = ""
_start_lock = threading.Lock()
_started = False
_server: ThreadingHTTPServer | None = None


def request_capture() -> None:
    """S 网页请求 B 截屏（Streamlit 直接调用）。"""
    _jobs.put({"type": "capture"})


def request_type(text: str, interval_ms: int = 60, start_delay_s: float = 3,
                 humanize: bool = True, unicode_only: bool = False,
                 dismiss_suggest: bool = True, paste_mode: bool = False) -> None:
    """S 网页请求 B 输入文本。

    humanize=True 时启用随机间隔/换行停顿/偶发错字纠正；
    unicode_only=True 时跳过无法作为单个 Unicode 码点发送的字符；
    dismiss_suggest=True 时回车/制表前先按 Esc 关掉 IDE 自动补全弹窗；
    paste_mode=True 时整段走剪贴板粘贴，绕过 IDE 补全/自动配对。
    """
    _jobs.put({
        "type": "type",
        "text": text,
        "interval_ms": int(interval_ms),
        "start_delay_s": float(start_delay_s),
        "humanize": bool(humanize),
        "unicode_only": bool(unicode_only),
        "dismiss_suggest": bool(dismiss_suggest),
        "paste_mode": bool(paste_mode),
    })


def set_token(token: str) -> None:
    """更新鉴权 token（每次脚本运行都同步，支持在网页里改）。"""
    global _token
    _token = token or ""


def get_latest_frame() -> tuple[bytes | None, float]:
    """返回最近一次 B 上传的 JPEG 字节与接收时间戳。"""
    with _lock:
        return _last_frame


def clear_frame() -> None:
    """清除内存中的最近截图（用户主动清除会话时调用）。"""
    global _last_frame
    with _lock:
        _last_frame = (None, 0.0)


def get_last_result() -> dict | None:
    """返回 B 客户端最近上报的执行结果。"""
    with _lock:
        return _last_result


def _store_frame(data: bytes) -> None:
    global _last_frame
    with _lock:
        _last_frame = (data, time.time())


def _store_result(payload: dict) -> None:
    global _last_result
    with _lock:
        _last_result = {**payload, "received_at": time.time()}


class _Handler(BaseHTTPRequestHandler):
    server_version = "CaptureAPI/2.0"

    def log_message(self, fmt, *args) -> None:  # 精简日志
        sys.stderr.write("[capture-api] %s\n" % (fmt % args))

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        return not _token or self.headers.get("X-Auth-Token", "") == _token

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return b""
        return self.rfile.read(length) if length > 0 else b""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        path, _, query = self.path.partition("?")
        if path == "/health":
            self._json(200, {"ok": True})
            return
        if path != "/pending":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "bad token"})
            return

        wait = DEFAULT_WAIT
        try:
            wait = min(MAX_WAIT, max(0.0, float(parse_qs(query).get("wait", [DEFAULT_WAIT])[0])))
        except (ValueError, TypeError):
            pass

        try:
            job = _jobs.get(timeout=wait)
        except queue.Empty:
            job = None
        self._json(200, {"job": job})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        if not self._authorized():
            self._json(401, {"error": "bad token"})
            return

        if path == "/frame":
            data = self._read_body()
            if not data:
                self._json(400, {"error": "empty frame"})
                return
            _store_frame(data)
            self._json(200, {"ok": True, "bytes": len(data)})
            return

        if path == "/result":
            raw = self._read_body()
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            _store_result(payload)
            self._json(200, {"ok": True})
            return

        self._json(404, {"error": "not found"})


def ensure_started(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
    """幂等启动采集/任务 API（daemon 线程）。token 由 set_token() 单独维护。"""
    global _started, _server
    with _start_lock:
        if _started:
            return
        try:
            _server = ThreadingHTTPServer((host, port), _Handler)
        except OSError as exc:
            sys.stderr.write(f"[capture-api] 无法监听 {host}:{port}: {exc}\n")
            return
        threading.Thread(
            target=_server.serve_forever, name="capture-api", daemon=True
        ).start()
        _started = True
        sys.stderr.write(f"[capture-api] 已监听 {host}:{port}\n")
