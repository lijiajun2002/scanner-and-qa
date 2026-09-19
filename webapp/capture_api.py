from __future__ import annotations

import hashlib
import json
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

DEFAULT_PORT = 8503
DEFAULT_WAIT = 25.0
MAX_WAIT = 60.0
DEFAULT_EXTRACT_PROMPT = (
    "上述所有图片都是同一个题目的截图，请把它们的内容整理成规范、完整、可直接作答的题干。"
)
CAPTURE_MIN_INTERVAL = 0.6  # 快捷键连按时，两次被采纳的截屏最小间隔（秒）

# 整段被 ``` 包裹的代码块：去掉首尾围栏（含开头的语言标注）
_FENCE_RE = re.compile(r"^```[^\n]*\r?\n(.*?)\r?\n?```\s*$", re.DOTALL)


def strip_code_fence(text: str) -> str:
    """若整段回答是一个 ``` 代码块，去掉首尾的 ```（保留中间内容）。"""
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1)
    return text

# ------- B 端任务（截屏 / 打字） -------
_jobs: "queue.Queue[dict]" = queue.Queue()
_last_frame: tuple[bytes | None, float] = (None, 0.0)
_last_result: dict | None = None

# ------- 服务端会话语义状态（内存，无持久化） -------
_state_lock = threading.Lock()
_images: list[bytes] = []
_messages: list[dict] = []
_status = "就绪"
_extracted = ""
_use_images = True
_extract_enabled = False
_last_answer = ""
_last_capture_ts = 0.0
_last_image_hash = ""

# ------- 高层动作队列（网页按钮 / B 端热键都可投递） -------
_actions: "queue.Queue[tuple[str, dict]]" = queue.Queue()

_token = ""
_start_lock = threading.Lock()
_started = False
_server: ThreadingHTTPServer | None = None


# ============================ 供网页直接调用的接口 ============================
def request_capture() -> None:
    """请求 B 截屏（入 B 任务队列）。"""
    _jobs.put({"type": "capture"})


def request_type(text: str, interval_ms: int = 60, start_delay_s: float = 3,
                 humanize: bool = True, unicode_only: bool = False,
                 dismiss_suggest: bool = True, paste_mode: bool = False) -> None:
    """请求 B 输入文本。"""
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


def append_action(action: str, payload: dict | None = None) -> None:
    """投递一个高层动作：capture / analyze / ask / clear。"""
    _actions.put((action, payload or {}))


def set_extract_enabled(enabled: bool) -> None:
    global _extract_enabled
    _extract_enabled = bool(enabled)


def set_token(token: str) -> None:
    global _token
    _token = token or ""


def get_state() -> dict:
    with _state_lock:
        return {
            "images": list(_images),
            "messages": [dict(m) for m in _messages],
            "status": _status,
            "extracted": _extracted,
            "use_images": _use_images,
            "last_answer": _last_answer,
        }


def get_latest_frame() -> tuple[bytes | None, float]:
    with _state_lock:
        return _last_frame


def get_last_result() -> dict | None:
    with _state_lock:
        return _last_result


def clear_frame() -> None:
    global _last_frame
    with _state_lock:
        _last_frame = (None, 0.0)


def add_image(data: bytes) -> None:
    """把一张图片加入待分析集合（浏览器拍照等其他来源）。"""
    if data:
        _store_frame(data)


def clear_all() -> None:
    global _status, _extracted, _use_images, _last_frame
    global _last_answer, _last_capture_ts, _last_image_hash
    with _state_lock:
        _images.clear()
        _messages.clear()
        _status = "就绪"
        _extracted = ""
        _use_images = True
        _last_frame = (None, 0.0)
        _last_answer = ""
        _last_capture_ts = 0.0
        _last_image_hash = ""


# ============================ 内部：状态更新 ============================
def _set_status(text: str) -> None:
    global _status
    with _state_lock:
        _status = text


def _store_frame(data: bytes) -> bool:
    """追加一张图；连按快捷键时按最小间隔+内容去重，避免重复入上下文。"""
    global _last_frame, _last_capture_ts, _last_image_hash
    digest = hashlib.sha1(data).hexdigest()
    now = time.time()
    with _state_lock:
        if now - _last_capture_ts < CAPTURE_MIN_INTERVAL:
            return False
        if digest == _last_image_hash:
            return False
        _last_capture_ts = now
        _last_image_hash = digest
        _last_frame = (data, now)
        _images.append(data)
        return True


def _store_result(payload: dict) -> None:
    global _last_result
    with _state_lock:
        _last_result = {**payload, "received_at": time.time()}


# ============================ 服务端 LLM 编排 ============================
def _llm_client():
    from app.config import Config
    from app.llm import LLMClient
    from webapp.settings import load_settings

    s = load_settings()
    client = LLMClient(Config(
        base_url=str(s["base_url"]).rstrip("/"),
        model=s["model"],
        max_tokens=int(s["max_tokens"]),
        prompt=s["prompt"],
        api_key=s["api_key"],
    ))
    return client, s


def _stream_assistant(images: list[bytes] | None) -> None:
    global _last_answer
    client, _ = _llm_client()
    with _state_lock:
        _messages.append({"role": "assistant", "content": ""})
        history = [dict(m) for m in _messages[:-1]]
    acc = ""
    for token in client.stream_chat(history, images):
        acc += token
        with _state_lock:
            _messages[-1]["content"] = acc
    with _state_lock:
        _last_answer = strip_code_fence(acc)


def _clear_conversation() -> None:
    """清空图片/对话/题干，但保留最近回答（供 Ctrl+Shift+L 输入到 B）。"""
    global _extracted, _use_images, _last_frame
    with _state_lock:
        _images.clear()
        _messages.clear()
        _extracted = ""
        _use_images = True
        _last_frame = (None, 0.0)


def _run_analyze(extract: bool | None = None) -> None:
    global _messages, _extracted, _use_images

    if extract is None:
        extract = _extract_enabled

    with _state_lock:
        images = list(_images)
    if not images:
        _set_status("没有可分析的图片")
        return

    client, s = _llm_client()
    if extract:
        _set_status("抽离题干中…")
        extracted = "".join(client.stream_chat(
            [{"role": "user", "content": s.get("extract_prompt") or DEFAULT_EXTRACT_PROMPT}],
            images,
        )).strip()
        with _state_lock:
            _extracted = extracted
            _messages = [{"role": "user", "content": f"{s['prompt']}\n\n【题干】\n{extracted}"}]
            _use_images = False
    else:
        with _state_lock:
            _extracted = ""
            _messages = [{"role": "user", "content": s["prompt"]}]
            _use_images = True

    _set_status("回答中…")
    with _state_lock:
        send_images = list(_images) if _use_images else None
    _stream_assistant(send_images)
    _clear_conversation()
    _set_status("完成（会话已清空，可按 Ctrl+Shift+L 把回答输入到 B）")


def _run_type_answer() -> None:
    with _state_lock:
        answer = _last_answer
    if not answer.strip():
        _set_status("没有可输入的回答")
        return
    request_type(
        answer, 60, 3,
        humanize=True, unicode_only=True, dismiss_suggest=True, paste_mode=False,
    )
    _set_status("已把最近回答发送到 B 逐字输入")


def _run_ask(text: str) -> None:
    if not text.strip():
        return
    with _state_lock:
        _messages.append({"role": "user", "content": text})
        send_images = list(_images) if _use_images else None
    _set_status("回答中…")
    _stream_assistant(send_images)
    _set_status("完成")


def _dispatch(action: str, payload: dict) -> None:
    if action == "capture":
        request_capture()
    elif action == "analyze":
        _run_analyze(payload.get("extract"))
    elif action == "type_answer":
        _run_type_answer()
    elif action == "ask":
        _run_ask(str(payload.get("text", "")))
    elif action == "clear":
        clear_all()
    elif action == "type":
        request_type(
            str(payload.get("text", "")),
            int(payload.get("interval_ms", 60)),
            float(payload.get("start_delay_s", 3)),
            bool(payload.get("humanize", True)),
            bool(payload.get("unicode_only", False)),
            bool(payload.get("dismiss_suggest", True)),
            bool(payload.get("paste_mode", False)),
        )
    else:
        _set_status(f"未知动作: {action}")


def _worker_loop() -> None:
    while True:
        action, payload = _actions.get()
        try:
            _dispatch(action, payload)
        except Exception as exc:  # noqa: BLE001 - 后台线程需继续服务
            _set_status(f"出错：{exc}")


# ============================ HTTP 传输（B 端调用） ============================
class _Handler(BaseHTTPRequestHandler):
    server_version = "CaptureAPI/3.0"

    def log_message(self, fmt, *args) -> None:
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

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/health":
            self._json(200, {"ok": True, "status": _status})
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
            self._json(200, {"ok": True, "bytes": len(data), "count": len(_images)})
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

        if path == "/action":
            raw = self._read_body()
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            action = str(payload.get("action", ""))
            if not action:
                self._json(400, {"error": "missing action"})
                return
            append_action(action, payload)
            self._json(200, {"ok": True})
            return

        self._json(404, {"error": "not found"})


def ensure_started(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
    """幂等启动 HTTP API（动作 worker 在模块导入时已启动）。"""
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


# 动作 worker 随模块加载启动（处理网页按钮与 B 端热键投递的动作）
threading.Thread(target=_worker_loop, name="action-worker", daemon=True).start()
