from __future__ import annotations

import hashlib
import json
import queue
import random
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
DEFAULT_HOTKEYS = {
    "capture": "ctrl+shift+alt+8",
    "analyze": "ctrl+shift+alt+9",
    "type_answer": "ctrl+shift+alt+0",
    "clear": "ctrl+shift+alt+minus",
    "resume": "ctrl+shift+alt+plus",
}

# 整段被 ``` 包裹的代码块：去掉首尾围栏（含开头的语言标注）
_FENCE_RE = re.compile(r"^```[^\n]*\r?\n(.*?)\r?\n?```\s*$", re.DOTALL)


def strip_code_fence(text: str) -> str:
    """若整段回答是一个 ``` 代码块，去掉首尾的 ```（保留中间内容）。"""
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1)
    return text


def _parse_plan_json(text: str) -> dict | None:
    """从 LLM 输出里解析键盘序列 JSON（容忍 ``` 包裹和前后杂字）。"""
    stripped = text.strip()
    stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    candidates = [stripped]
    brace = re.search(r"\{.*\}", stripped, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("order"), list):
            return data
    return None

# ------- B 端任务（截屏 / 打字） -------
_jobs: "queue.Queue[dict]" = queue.Queue()
_last_frame: tuple[bytes | None, float] = (None, 0.0)
_last_result: dict | None = None

# ------- 服务端会话语义状态（内存，无持久化） -------
_state_lock = threading.Lock()
_images: list[bytes] = []          # 当前一轮的截图（连按追加；分析后保留，新截图才开新会话）
_messages: list[dict] = []         # 与 LLM 的对话上下文（固定保留，直到新图片/清空）
_status = "就绪"
_extracted = ""
_use_images = True
_analyze_extract = True
_typing_options: dict = {}
_last_answer = ""
_last_capture_ts = 0.0
_last_image_hash = ""
_round_analyzed = False            # 当前这轮是否已经"分析"过（用于判断新截图=新会话）
_transcript: list[dict] = []       # 会话记录（网页对话式回看）：role/kind/text/images

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
                 dismiss_suggest: bool = True, paste_mode: bool = False,
                 options: dict | None = None, plan: dict | None = None,
                 keys: list | None = None, resume: bool = False) -> None:
    """请求 B 输入文本；options 可覆盖 B 端拟人化参数，plan 为第三阶段键盘序列。

    resume=True 表示断点续传（plan.order 为剩余行子集，B 端不重建空行）。
    keys 为第三阶段"键盘流"（LLM 直接给按键序列），B 端会回放校验后执行。
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
        "options": dict(options or {}),
        "plan": plan if isinstance(plan, dict) else None,
        "keys": keys if isinstance(keys, list) else None,
        "resume": bool(resume),
    })


def set_typing_options(options: dict) -> None:
    """记住网页里调整的拟人化参数，供 Ctrl+Shift+Alt+0 输入回答时使用。"""
    global _typing_options
    _typing_options = dict(options or {})


# ------- 控制通道：网页"停止"按钮 → B 端（打字时 B 不在 /pending，必须独立通道） -------
_control_lock = threading.Lock()
_stop_event = threading.Event()
_stop_reason = "web"


def append_action(action: str, payload: dict | None = None) -> None:
    """投递一个高层动作：capture / analyze / type_answer / resume / stop / ask / clear。"""
    _actions.put((action, payload or {}))


def request_stop(reason: str = "web") -> None:
    """请求 B 端停止键盘输出（网页停止按钮）。"""
    global _stop_reason
    with _control_lock:
        _stop_reason = reason
    _stop_event.set()


def _consume_stop(wait: float) -> bool:
    """B 端 /control 长轮询：有停止请求就消费并返回 True。"""
    if _stop_event.wait(timeout=max(0.0, wait)):
        _stop_event.clear()
        return True
    return False


def set_analyze_extract(enabled: bool) -> None:
    """分析是否经过题干抽离层（网页配置，快捷键分析与网页按钮共用这一个开关）。"""
    global _analyze_extract
    _analyze_extract = bool(enabled)


def get_hotkeys() -> dict:
    from webapp.settings import load_settings

    s = load_settings()
    return {**DEFAULT_HOTKEYS, **(s.get("hotkeys") or {})}


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
            "transcript": [dict(t) for t in _transcript],
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
    """把一张图片加入待分析集合（浏览器拍照等其他来源）；新截图=新一轮。"""
    if data:
        _store_frame(data)


def clear_all() -> None:
    global _status, _extracted, _use_images, _last_frame
    global _last_answer, _last_capture_ts, _last_image_hash, _round_analyzed
    with _state_lock:
        _images.clear()
        _messages.clear()
        _transcript.clear()
        _status = "就绪"
        _extracted = ""
        _use_images = True
        _last_frame = (None, 0.0)
        _last_answer = ""
        _last_capture_ts = 0.0
        _last_image_hash = ""
        _round_analyzed = False


def _reset_session() -> None:
    """开新一轮会话：清空图片/上下文/记录（保留最近回答会给网页用，但这里也清）。"""
    clear_all()


def _begin_round_if_needed() -> None:
    """收到新截图时：若上一轮已分析过（或本就没有待分析的图），则视为新一轮。

    同一题的连拍（分析前反复截图）会追加到同一轮，不会清上下文。
    """
    global _round_analyzed, _extracted, _use_images, _last_answer
    global _last_image_hash, _last_frame
    with _state_lock:
        if _round_analyzed or not _images:
            _images.clear()
            _messages.clear()
            _transcript.clear()
            _extracted = ""
            _use_images = True
            _last_answer = ""
            _last_image_hash = ""
            _last_frame = (None, 0.0)
        _round_analyzed = False


def _append_transcript(role: str, text: str = "", kind: str = "text",
                       images: list[bytes] | None = None) -> None:
    with _state_lock:
        _transcript.append({
            "role": role, "kind": kind, "text": text,
            "images": list(images or []),
        })


# ============================ 内部：状态更新 ============================
def _set_status(text: str) -> None:
    global _status
    with _state_lock:
        _status = text


def _store_frame(data: bytes) -> bool:
    """追加一张图；连按快捷键时按最小间隔+内容去重，避免重复入上下文。"""
    global _last_frame, _last_capture_ts, _last_image_hash
    _begin_round_if_needed()
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


def _language_prompt(s: dict) -> str:
    lang = str(s.get("code_language") or "python")
    prompts = s.get("language_prompts") or {}
    return str(prompts.get(lang) or s.get("prompt") or "")


def _parse_route(raw: str) -> tuple[str, str]:
    """解析第一层的首行标记，返回 (kind, text)；kind ∈ {code, choice}。"""
    text = (raw or "").strip()
    upper = text.upper()
    for tag, kind, n in (("[[CHOICE]]", "choice", 10), ("[[CODE]]", "code", 8)):
        idx = upper.find(tag)
        if idx != -1 and idx <= 40:      # 标记出现在开头附近才算
            return kind, text[idx + n:].strip()
    return "code", text                    # 兜底：当代码题


def _route_and_extract(client, s: dict, images: list[bytes]) -> tuple[str, str]:
    """第一层：判断题型并分支（一次调用）。返回 (kind, text)。"""
    prompt = str(s.get("route_prompt") or "")
    acc = "".join(client.stream_chat([{"role": "user", "content": prompt}], images))
    return _parse_route(acc)


def _finish_round(status: str) -> None:
    global _round_analyzed
    with _state_lock:
        _round_analyzed = True
    _set_status(status)


def _run_choice(client, s: dict, images: list[bytes]) -> None:
    """选择题：单层作答，仅在网页显示（不键盘输出、不追问）。"""
    global _last_answer, _extracted
    _set_status("选择题作答中…")
    answer = "".join(client.stream_chat(
        [{"role": "user", "content": str(s.get("choice_prompt") or "")}], images,
    )).strip()
    with _state_lock:
        _last_answer = strip_code_fence(answer)
        _extracted = ""
        _messages.clear()
    _append_transcript("assistant", _last_answer, kind="answer")
    _finish_round("完成（选择题：仅在网页显示，不输出到 B）")


def _run_free_ask(client, s: dict, images: list[bytes]) -> None:
    """自由问答：单层调用主 LLM 提示词作答；保留上下文、可追问、可输出到 B。"""
    global _messages, _last_answer, _use_images, _extracted
    with _state_lock:
        _messages = [{"role": "user", "content": str(s.get("prompt") or "")}]
        _use_images = True
        _extracted = ""
    _set_status("回答中…")
    _stream_assistant(images)
    _append_transcript("assistant", _last_answer, kind="answer")
    _finish_round("完成（自由问答；可继续追问或按 +0 输出到 B）")


def _run_code(client, s: dict, images: list[bytes], stem: str | None,
              extract: bool = True) -> None:
    """代码题：题干抽离(可选) → 用所选语言作答 → 保留上下文。"""
    global _extracted, _use_images, _messages
    lang_prompt = _language_prompt(s)
    if stem is None and extract:
        _set_status("抽离题干中…")
        stem = "".join(client.stream_chat(
            [{"role": "user", "content": s.get("extract_prompt") or DEFAULT_EXTRACT_PROMPT}],
            images,
        )).strip()
    if stem:
        with _state_lock:
            _extracted = stem
            _messages = [{"role": "user", "content": f"{lang_prompt}\n\n【题干】\n{stem}"}]
            _use_images = False
        _append_transcript("assistant", f"题干：\n{stem}", kind="extract")
    else:
        with _state_lock:
            _extracted = ""
            _messages = [{"role": "user", "content": lang_prompt}]
            _use_images = True

    _set_status("回答中…")
    with _state_lock:
        send_images = list(_images) if _use_images else None
    _stream_assistant(send_images)
    _append_transcript("assistant", _last_answer, kind="answer")
    _finish_round("完成（代码题；上下文已保留，可继续追问或按 +0 输出到 B）")


def _run_analyze(extract: bool | None = None) -> None:
    global _last_answer, _extracted
    with _state_lock:
        images = list(_images)
    if not images:
        _set_status("没有可分析的图片")
        return

    client, s = _llm_client()
    qtype = str(s.get("question_type") or "auto")
    _append_transcript("user", "题目截图", kind="images", images=images)

    if qtype == "choice":
        _run_choice(client, s, images)
        return
    if qtype == "ask":
        _run_free_ask(client, s, images)
        return
    if qtype == "auto":
        set_status_was = None
        _set_status("判断题型 / 整理题干中…")
        kind, text = _route_and_extract(client, s, images)
        if kind == "choice":
            with _state_lock:
                _last_answer = strip_code_fence(text)
                _extracted = ""
                _messages.clear()
            _append_transcript("assistant", _last_answer, kind="answer")
            _finish_round("完成（自动识别为选择题：仅网页显示）")
            return
        _run_code(client, s, images, stem=text)
        return
    # 手动指定代码题
    _run_code(client, s, images, stem=None, extract=_analyze_extract)



_TYPING_NUMERIC_KEYS = (
    "interval_min_ms", "interval_max_ms", "line_pause_min_ms", "line_pause_max_ms",
    "space_interval_ms", "typo_rate", "typo_pause_min_ms", "typo_pause_max_ms",
    "enter_via_paste", "indent_mode", "indent_style", "tab_size",
    "clean_invisibles", "dismiss_delay_ms",
)


def _parse_keys_json(text: str):
    """从 LLM 输出解析键盘流 {"keys":[...]}（容忍 ``` 与前后杂字）。"""
    stripped = text.strip()
    stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    candidates = [stripped]
    brace = re.search(r"\{.*\}", stripped, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("keys"), list):
            return data["keys"]
    return None


def _ask_keystream(client, prompt: str, user_content: str, tries: int = 2):
    """请求键盘流 JSON，失败时追加"只输出 JSON"重试；并记录原始输出。"""
    last = ""
    for attempt in range(1, max(1, tries) + 1):
        content = user_content if attempt == 1 else \
            user_content + "\n\n（只输出那个 JSON，不要任何其他文字。）"
        last = "".join(client.stream_chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ]))
        keys = _parse_keys_json(last)
        if keys:
            return keys
    import sys as _sys
    _sys.stderr.write("[keystream] 解析失败，原始输出=%r\n" % (last[:500],))
    return None


def _generate_keys(answer: str):
    from webapp.settings import DEFAULT_KEYSTREAM_PROMPT, load_settings

    s = load_settings()
    client, _ = _llm_client()
    prompt = str(s.get("keystream_prompt") or DEFAULT_KEYSTREAM_PROMPT)
    return _ask_keystream(client, prompt, answer)


def _ask_plan_json(client, prompt: str, user_content: str, tries: int = 2):
    """请求计划 JSON，解析失败时追加"只输出 JSON"再重试；并原样记录便于排错。"""
    last = ""
    for attempt in range(1, max(1, tries) + 1):
        content = user_content if attempt == 1 else \
            user_content + "\n\n（只输出那个 JSON，不要任何其他文字。）"
        last = "".join(client.stream_chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ]))
        data = _parse_plan_json(last)
        if data:
            return data
    import sys as _sys
    _sys.stderr.write("[plan] 解析失败，原始输出=%r\n" % (last[:500],))
    return None


def _generate_plan(answer: str) -> dict | None:
    """第三阶段：让主 LLM 为回答生成"人类写作顺序"计划（不含正文）。"""
    from webapp.settings import DEFAULT_PLAN_PROMPT, load_settings

    s = load_settings()
    client, _ = _llm_client()
    prompt = str(s.get("plan_prompt") or DEFAULT_PLAN_PROMPT)
    lines = answer.split("\n")
    numbered = "\n".join(f"{i + 1}: {t}" for i, t in enumerate(lines))
    return _ask_plan_json(client, prompt, f"N={len(lines)}\n{numbered}")


def _human_timing(text: str, target_minutes: float) -> dict:
    """按目标总时长算拟人化参数：普通字符用真人速度，剩余时间摊成逐行"思考"停顿。

    这样无论答案多长，整体都接近 target_minutes；逐字速度始终保持在人类区间。
    """
    chars = len(text)
    spaces = text.count(" ")
    lines = max(1, text.count("\n") + 1)
    typing = max(0, chars - spaces) * 0.175 + spaces * 0.06   # 普通字符175ms、空格60ms
    target_s = max(0.0, float(target_minutes or 0)) * 60.0
    line_ms = 1200.0
    if target_s > typing:
        line_ms = max(line_ms, (target_s - typing) / lines * 1000.0)
    line_ms = min(line_ms, 180000.0)
    return {
        "interval_min_ms": 110,
        "interval_max_ms": 300,
        "space_interval_ms": 70,
        "line_pause_min_ms": int(line_ms * 0.8),
        "line_pause_max_ms": int(line_ms * 1.3),
        "line_ms": line_ms,
        "lines": lines,
    }


def _apply_plan_pauses(plan: dict, lines: int, line_ms: float) -> dict:
    """把目标时长摊成"写完每行后的思考停顿"，替换 LLM 给的短停顿。"""
    new_plan = dict(plan)
    new_plan["pauses"] = [
        {"after": i, "ms": int(line_ms * random.uniform(0.75, 1.3))}
        for i in range(1, lines + 1)
    ]
    return new_plan


def _generate_plan_for_lines(answer: str, remaining: list[int]) -> list[int] | None:
    """对"剩余未写的行"重新生成一份新计划，返回按原行号表示的乱序 order。"""
    from webapp.settings import DEFAULT_PLAN_PROMPT, load_settings

    lines = answer.split("\n")
    numbered = "\n".join(f"{k}: {lines[orig - 1]}"
                         for k, orig in enumerate(remaining, 1))
    s = load_settings()
    client, _ = _llm_client()
    prompt = str(s.get("plan_prompt") or DEFAULT_PLAN_PROMPT)
    data = _ask_plan_json(client, prompt, f"N={len(remaining)}\n{numbered}")
    if not data:
        return None
    order = data.get("order")
    if not isinstance(order, list) or sorted(order) != list(range(1, len(remaining) + 1)):
        return None
    return [remaining[k - 1] for k in order]


def _run_resume(payload: dict) -> None:
    """断点续传：对剩余未写的行重新生成计划，让 B 从断点继续输出。"""
    from webapp.settings import load_settings

    with _state_lock:
        answer = _last_answer
        opts = dict(_typing_options)
    if not answer.strip():
        _set_status("没有可续传的回答")
        return
    total = int(payload.get("total_lines") or 0)
    if total <= 0:
        total = answer.count("\n") + 1
    done = {int(x) for x in (payload.get("done_lines") or []) if str(x).lstrip("-").isdigit()}
    remaining = [i for i in range(1, total + 1) if i not in done]
    if not remaining:
        _set_status("没有剩余内容可续传")
        return

    settings = load_settings()
    target_min = float(settings.get("type_target_minutes") or 0)
    timing = _human_timing(answer, target_min) if target_min > 0 else None
    numeric = {k: opts[k] for k in _TYPING_NUMERIC_KEYS if k in opts}
    if timing:
        for key in ("interval_min_ms", "interval_max_ms", "space_interval_ms",
                    "line_pause_min_ms", "line_pause_max_ms"):
            numeric[key] = timing[key]

    order = None
    try:
        _set_status("断点续传：重新生成剩余行计划…")
        order = _generate_plan_for_lines(answer, remaining)
    except Exception as exc:  # noqa: BLE001
        _set_status(f"续传计划生成失败，按顺序续写：{exc}")
    if not order:
        order = remaining
    # 保险：order 只能包含"剩余未写"的行号，且去重
    allowed = set(remaining)
    order = [ln for ln in dict.fromkeys(order) if ln in allowed] or list(remaining)
    _append_transcript("system",
                       f"断点续传：剩余 {len(remaining)} 行，计划序 {order}", kind="action")

    plan = {"order": order, "pauses": [], "revisit": []}
    if timing:
        plan["pauses"] = [
            {"after": ln, "ms": int(timing["line_ms"] * random.uniform(0.75, 1.3))}
            for ln in order
        ]
    request_type(
        answer,
        int(opts.get("interval_ms", 60)),
        float(opts.get("start_delay_s", 3)),
        bool(opts.get("humanize", True)),
        bool(opts.get("unicode_only", True)),
        bool(opts.get("dismiss_suggest", True)),
        bool(opts.get("paste_mode", False)),
        numeric,
        plan=plan,
        resume=True,
    )
    _set_status(f"已发送断点续传（剩余 {len(remaining)} 行）")
    _append_transcript("system", f"断点续传（剩余 {len(remaining)} 行）", kind="action")


def _run_type_answer() -> None:
    from webapp.settings import load_settings

    with _state_lock:
        answer = _last_answer
        opts = dict(_typing_options)
    if not answer.strip():
        _set_status("没有可输入的回答")
        return
    numeric = {k: opts[k] for k in _TYPING_NUMERIC_KEYS if k in opts}
    settings = load_settings()
    target_min = float(settings.get("type_target_minutes") or 0)
    timing = _human_timing(answer, target_min) if target_min > 0 else None
    if timing:
        for key in ("interval_min_ms", "interval_max_ms", "space_interval_ms",
                    "line_pause_min_ms", "line_pause_max_ms"):
            numeric[key] = timing[key]
    plan = None
    keys = None
    if settings.get("plan_enabled"):
        method = str(settings.get("plan_method") or "keys")
        try:
            if method == "keys":
                _set_status("第三阶段：生成键盘流…")
                keys = _generate_keys(answer)
            # 行序计划始终准备一份作为兜底
            plan = _generate_plan(answer)
            if plan and timing:
                plan = _apply_plan_pauses(plan, timing["lines"], timing["line_ms"])
        except Exception as exc:  # noqa: BLE001 - 失败就回退逐字
            plan = plan if plan else None
            _set_status(f"第三阶段生成失败，尽量回退：{exc}")
    request_type(
        answer,
        int(opts.get("interval_ms", 60)),
        float(opts.get("start_delay_s", 3)),
        bool(opts.get("humanize", True)),
        bool(opts.get("unicode_only", True)),
        bool(opts.get("dismiss_suggest", True)),
        bool(opts.get("paste_mode", False)),
        numeric,
        plan=plan,
        keys=keys,
    )
    if keys:
        _set_status("已按大模型键盘流发送到 B 输入")
        mode = "大模型键盘流"
    elif plan:
        _set_status("已按大模型键盘序列发送到 B 输入")
        mode = "大模型键盘序列"
    else:
        _set_status("已把最近回答发送到 B 输入")
        mode = "逐字输入"
    _append_transcript("system", f"已把最近回答输出到 B（{mode}）", kind="action")


def _run_ask(text: str) -> None:
    if not text.strip():
        return
    with _state_lock:
        has_context = bool(_messages)
    if not has_context:
        _set_status("当前会话无可追问的上下文（选择题不支持追问）")
        return
    _append_transcript("user", text, kind="question")
    with _state_lock:
        _messages.append({"role": "user", "content": text})
        send_images = list(_images) if _use_images else None
    _set_status("回答中…")
    _stream_assistant(send_images)
    _append_transcript("assistant", _last_answer, kind="answer")
    _set_status("完成")


def _dispatch(action: str, payload: dict) -> None:
    if action == "capture":
        request_capture()
    elif action == "analyze":
        _run_analyze(payload.get("extract"))
    elif action == "type_answer":
        _run_type_answer()
    elif action == "resume":
        _run_resume(payload)
    elif action == "stop":
        request_stop(str(payload.get("reason") or "web"))
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
            dict(payload.get("options") or {}),
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
        if path == "/hotkeys":
            if not self._authorized():
                self._json(401, {"error": "bad token"})
                return
            self._json(200, {"hotkeys": get_hotkeys()})
            return
        if path == "/control":
            if not self._authorized():
                self._json(401, {"error": "bad token"})
                return
            try:
                wait = min(MAX_WAIT, max(0.0, float(parse_qs(query).get("wait", ["25"])[0])))
            except (ValueError, TypeError):
                wait = DEFAULT_WAIT
            stopped = _consume_stop(wait)
            self._json(200, {"stop": stopped, "reason": _stop_reason})
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
