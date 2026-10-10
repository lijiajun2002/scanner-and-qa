from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from sqa.tools import TOOL_SPECS, ToolContext, dispatch

from .llm import AgentLLM, encode_image_bytes
from .prompt import SYSTEM_PROMPT, build_goal


def _assistant_to_dict(msg) -> dict:
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if getattr(msg, "tool_calls", None):
        out["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ]
    return out


def _est_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c) // 3
        elif isinstance(c, list):
            for part in c:
                if part.get("type") == "text":
                    total += len(part.get("text", "")) // 3
                elif part.get("type") == "image_url":
                    total += 1100  # 一张图的粗略 token
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"])) // 3
    return total


class EventPoller:
    """后台长轮询 Target 的热键事件。"""

    def __init__(self, backend) -> None:
        self.backend = backend
        self.q: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for ev in self.backend.poll_events(20.0):
                    self.q.put(ev)
            except Exception:  # noqa: BLE001
                time.sleep(2)

    def drain(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out


def _user_image_message(paths: list[Path], caption: str) -> dict:
    parts: list[dict] = [{"type": "text", "text": caption}]
    for p in paths:
        parts.append({"type": "image_url",
                      "image_url": {"url": encode_image_bytes(p.read_bytes())}})
    return {"role": "user", "content": parts}


def run_session(llm: AgentLLM, ctx: ToolContext, poller: EventPoller,
                messages: list[dict], max_steps: int, token_limit: int,
                log=print) -> str:
    """跑一个会话，返回 'handoff' 或 'done'。"""
    last_image: Path | None = None
    for step in range(max_steps):
        # 注入热键事件
        for ev in poller.drain():
            kind = ev.get("type")
            if kind == "prompt_screenshot":
                messages.append({"role": "user",
                                 "content": "（用户按下热键提示你：现在截图看看情况。）"})
                log("[event] prompt_screenshot")

        est = _est_tokens(messages)
        if est > token_limit:
            messages.append({"role": "user", "content":
                             "（上下文即将超限：请立刻调用 handoff 写清背景和下一步。）"})
            log(f"[compact] est={est} > {token_limit}")

        resp = llm.chat(messages, tools=TOOL_SPECS)
        msg = resp.choices[0].message
        messages.append(_assistant_to_dict(msg))

        if not getattr(msg, "tool_calls", None):
            log(f"[step {step}] 模型无工具调用，结束会话：{(msg.content or '')[:200]}")
            return "done"

        images: list[Path] = []
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            log(f"[step {step}] {name} {json.dumps(args, ensure_ascii=False)[:160]}")
            res = dispatch(ctx, name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": res["content"]})
            images.extend(res["images"])
            if res["images"]:
                last_image = res["images"][-1]
            if ctx.handoff:
                ctx.last_image = last_image
                return "handoff"

        if images:
            messages.append(_user_image_message(images, "（以上为最新截图。）"))

        # 只保留最新一张图：把历史里的旧图替换为文字，控制上下文
        _prune_images(messages)
    log(f"[warn] 达到最大步数 {max_steps}，强制结束会话")
    return "done"


def _prune_images(messages: list[dict]) -> None:
    """保证上下文里最多只有最近 1 条带图消息。"""
    image_idx = [i for i, m in enumerate(messages)
                 if isinstance(m.get("content"), list)
                 and any(p.get("type") == "image_url" for p in m["content"])]
    for i in image_idx[:-1]:
        m = messages[i]
        texts = [p.get("text", "") for p in m["content"] if p.get("type") == "text"]
        n = sum(1 for p in m["content"] if p.get("type") == "image_url")
        messages[i] = {"role": m["role"],
                       "content": "".join(texts) + f"（此前 {n} 张截图已省略。）"}


def build_initial_messages(handoff_text: str | None, image_path: Path | None,
                           goal: str | None = None,
                           system_prompt: str | None = None) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}]
    if handoff_text:
        msgs.append({"role": "user", "content":
                     "这是上一个会话的交接，请据此继续：\n\n" + handoff_text})
    else:
        msgs.append({"role": "user", "content": build_goal(goal)})
    if image_path and image_path.exists():
        msgs.append(_user_image_message([image_path], "（交接时的最新截图。）"))
    return msgs
