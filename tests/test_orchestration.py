"""服务端编排测试：题型分流 / 上下文保留 / 会话记录 / 第三阶段 / 续传 / 控制通道。

内嵌一个 mock LLM（HTTP SSE），不访问外网。运行：  python tests/test_orchestration.py
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PORT_LLM = 8791
PORT_API = 8621
CALLS = []
CALL_NO = {"n": 0}
ROUTE = {"kind": "CODE"}          # 自动分流返回 CODE / CHOICE


class _LLM(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = body.get("messages", [])
        first = msgs[0] if msgs else {}
        content = first.get("content")
        imgs = sum(1 for p in content if p.get("type") == "image_url") \
            if isinstance(content, list) else 0
        CALL_NO["n"] += 1
        CALLS.append((len(msgs), imgs))

        def _txt(c):
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "".join(p.get("text", "") for p in c
                               if isinstance(p, dict) and p.get("type") == "text")
            return ""

        sys_txt = _txt(content)
        user_txt = _txt(msgs[1].get("content")) if len(msgs) > 1 else ""

        if "[[CHOICE]]" in sys_txt:                       # 第一层：路由
            if ROUTE["kind"] == "CHOICE":
                out = "[[CHOICE]]\nB"
            else:
                out = "[[CODE]]\n题干"
        elif "键盘按键" in sys_txt:                        # 键盘流
            out = json.dumps({"keys": [user_txt]}, ensure_ascii=False)
        elif "书写顺序" in sys_txt:                        # 行序计划
            m = re.search(r"N=(\d+)", user_txt)
            n = int(m.group(1)) if m else max(1, user_txt.count("\n") + 1)
            out = json.dumps({"order": list(range(n, 0, -1)), "pauses": [], "revisit": []})
        elif "选择题" in sys_txt:                          # 手动选择题
            out = "B"
        else:                                             # 主答 / 抽离 / 自由问答
            out = f"[call{CALL_NO['n']}:msgs={len(msgs)}:imgs={imgs}]"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for ch in out:
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': ch}}]})}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


threading.Thread(target=HTTPServer(("127.0.0.1", PORT_LLM), _LLM).serve_forever, daemon=True).start()

_TMP = Path(tempfile.mkdtemp(prefix="sqa_orch_"))
SETTINGS = _TMP / "settings.json"
os.environ["WEBAPP_SETTINGS"] = str(SETTINGS)


def set_settings(**over):
    base = {"base_url": f"http://127.0.0.1:{PORT_LLM}/v1", "model": "mock",
            "api_key": "k", "prompt": "自由问答提示词", "max_tokens": 512,
            "b_api_token": "tok", "extract_prompt": "抽离提示词",
            "type_target_minutes": 0}
    base.update(over)
    SETTINGS.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")


set_settings()
from webapp import capture_api as capi  # noqa: E402

capi.set_token("tok")
capi.ensure_started(host="127.0.0.1", port=PORT_API)
BASE = f"http://127.0.0.1:{PORT_API}"


def wait_until(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            time.sleep(0.15)
            return True
        time.sleep(0.05)
    return False


def wait_calls(n, timeout=15):
    return wait_until(lambda: len(CALLS) >= n, timeout)


def jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, format="JPEG")
    return buf.getvalue()


def add(data):
    old = capi.CAPTURE_MIN_INTERVAL
    capi.CAPTURE_MIN_INTERVAL = 0
    try:
        capi.add_image(data)
    finally:
        capi.CAPTURE_MIN_INTERVAL = old


def reset():
    capi.clear_all()
    CALLS.clear()
    CALL_NO["n"] = 0
    ROUTE["kind"] = "CODE"
    try:
        while True:
            capi._jobs.get_nowait()
    except Exception:
        pass


def drain_jobs():
    jobs = []
    try:
        while True:
            jobs.append(capi._jobs.get_nowait())
    except Exception:
        pass
    return jobs


def last_type_job():
    return [j for j in drain_jobs() if j.get("type") == "type"][-1]


def test_capture_and_dedup():
    reset()
    req = urllib.request.Request(f"{BASE}/frame", data=jpeg((9, 9, 9)), method="POST",
                                 headers={"X-Auth-Token": "tok", "Content-Type": "image/jpeg"})
    assert json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"]
    assert len(capi.get_state()["images"]) == 1
    dup = jpeg((1, 1, 1))
    add(dup)
    add(dup)                       # 与上一张相同 -> 去重
    assert len(capi.get_state()["images"]) == 2      # 第 2 张入库；重复的被去重
    print("[ok] /frame 入库 + 去重")


def test_auto_code():
    reset()
    add(jpeg((200, 0, 0)))
    add(jpeg((0, 200, 0)))
    capi.append_action("analyze")
    assert wait_calls(2), CALLS                       # 路由(带图) + 主答(不带图)
    s = capi.get_state()
    assert CALLS[0] == (1, 2) and CALLS[1] == (1, 0), CALLS
    assert len(s["messages"]) == 2 and len(s["images"]) == 2   # 上下文/图片保留
    assert any(t.get("kind") == "images" for t in s["transcript"])
    assert any(t.get("kind") == "answer" for t in s["transcript"])
    print("[ok] 自动分流-代码题：路由+主答，上下文保留，有会话记录")


def test_followup_context():
    before = len(capi.get_state()["messages"])
    capi.append_action("ask", {"text": "再解释"})
    assert wait_calls(3), CALLS
    s = capi.get_state()
    assert len(s["messages"]) == before + 2
    assert any(t.get("kind") == "question" for t in s["transcript"])
    print("[ok] 追问保留上下文并记录")


def test_new_image_new_session():
    time.sleep(capi.CAPTURE_MIN_INTERVAL + 0.05)
    add(jpeg((5, 5, 5)))
    s = capi.get_state()
    assert s["messages"] == [] and s["transcript"] == [] and len(s["images"]) == 1
    print("[ok] 新截图开启新一轮（清上下文/记录）")


def test_auto_choice():
    reset()
    add(jpeg((30, 30, 30)))
    ROUTE["kind"] = "CHOICE"
    capi.append_action("analyze")
    assert wait_calls(1), CALLS
    s = capi.get_state()
    assert CALLS == [(1, 1)], CALLS                   # 单层
    assert s["messages"] == []                        # 选择题不建上下文
    assert any(t.get("kind") == "answer" for t in s["transcript"])
    print("[ok] 自动分流-选择题：单层、无上下文")


def test_manual_ask():
    reset()
    add(jpeg((60, 60, 60)))
    set_settings(question_type="ask")
    capi.append_action("analyze")
    assert wait_calls(1), CALLS
    s = capi.get_state()
    assert CALLS == [(1, 1)] and len(s["messages"]) == 2
    capi.append_action("ask", {"text": "再解释"})
    assert wait_calls(2), CALLS
    print("[ok] 自由问答：单层 + 保留上下文 + 可追问")
    set_settings()


def test_type_answer_carries_plan_and_keys():
    reset()
    capi._last_answer = "def f():\n    return 1"
    set_settings(plan_enabled=True, plan_method="keys")
    capi.set_typing_options({"paste_mode": False})
    capi.append_action("type_answer")
    assert wait_until(lambda: any(j.get("type") == "type" for j in _peek()), timeout=10)
    job = last_type_job()
    assert isinstance(job.get("keys"), list) and job["keys"], job.get("keys")
    assert isinstance(job.get("plan"), dict) and job["plan"].get("order"), job.get("plan")
    assert job.get("resume") is False
    print("[ok] 输入回答：同时带 keys（键盘流）与 plan（行序兜底）")


def _peek():
    return list(capi._jobs.queue)


def test_resume():
    reset()
    capi._last_answer = "aa\nbb\ncc\ndd"
    set_settings(plan_enabled=True, plan_method="order")
    capi.append_action("resume", {"done_lines": [1], "total_lines": 4})
    assert wait_until(lambda: any(j.get("type") == "type" for j in _peek()), timeout=10)
    job = last_type_job()
    assert job.get("resume") is True
    assert set(job["plan"]["order"]) == {2, 3, 4}
    assert job.get("keys") is None
    print("[ok] 断点续传：剩余行重新生成计划 + resume 透传")
    set_settings()


def test_control_channel():
    capi.request_stop("web")
    resp = json.loads(urllib.request.urlopen(
        urllib.request.Request(f"{BASE}/control?wait=3",
                               headers={"X-Auth-Token": "tok"}), timeout=6).read())
    assert resp.get("stop") is True, resp
    resp = json.loads(urllib.request.urlopen(
        urllib.request.Request(f"{BASE}/control?wait=0",
                               headers={"X-Auth-Token": "tok"}), timeout=6).read())
    assert resp.get("stop") is False, resp
    print("[ok] 控制通道：/control 停止信号消费并清空")


def test_helpers():
    assert capi._parse_plan_json('```json\n{"order":[1]}\n```') == {"order": [1]}
    assert capi._parse_plan_json("x {\"order\":[2,1]} y") == {"order": [2, 1]}
    assert capi._parse_plan_json("nope") is None
    assert capi._parse_keys_json('{"keys":["a","<Enter>","b"]}') == ["a", "<Enter>", "b"]
    assert capi._parse_keys_json("x") is None
    assert capi.strip_code_fence("```python\nprint(1)\n```") == "print(1)"
    print("[ok] plan/keys JSON 解析容错 + 去围栏")


def _run_all():
    test_capture_and_dedup()
    test_auto_code()
    test_followup_context()
    test_new_image_new_session()
    test_auto_choice()
    test_manual_ask()
    test_type_answer_carries_plan_and_keys()
    test_resume()
    test_control_channel()
    test_helpers()
    print("ORCHESTRATION OK")


if __name__ == "__main__":
    _run_all()
