from __future__ import annotations

import io

from agent.llm import AgentLLM, encode_image_bytes
from sqa.backends import CompositeBackend, SoftwareBackend, VideoBackend
from sqa.tools import ToolContext, dispatch


def backend_for(client: dict, settings: dict):
    software = SoftwareBackend(client["url"], client.get("token", ""))
    if settings.get("shot_source") == "video":
        video = VideoBackend(settings.get("video_source", "v4l2:/dev/video0"),
                             size=settings.get("video_size", "1920x1080"))
        return CompositeBackend(video, software)
    return software


def _ctx(client: dict, settings: dict) -> ToolContext:
    return ToolContext(backend_for(client, settings), _scratch_dir())


def _scratch_dir():
    from . import store
    d = store.runs_dir() / "_manual"
    d.mkdir(parents=True, exist_ok=True)
    return d


def screenshot(client: dict, settings: dict) -> bytes:
    ctx = _ctx(client, settings)
    frame = ctx.backend.screenshot()
    return frame.data


def type_text(client: dict, settings: dict, text: str) -> dict:
    ctx = _ctx(client, settings)
    return dispatch(ctx, "type_text", {"text": text})


def key(client: dict, settings: dict, combo: str) -> dict:
    ctx = _ctx(client, settings)
    return dispatch(ctx, "key", {"combo": combo})


def qa(client: dict, settings: dict, question: str) -> str:
    """截当前屏 + 问 LLM。返回回答文本。"""
    image = screenshot(client, settings)
    llm = AgentLLM(settings.get("base_url", ""), settings.get("api_key", ""),
                   settings.get("model", ""), max_tokens=int(settings.get("max_tokens", 4096)))
    resp = llm.client.chat.completions.create(
        model=settings.get("model", ""),
        max_tokens=int(settings.get("max_tokens", 4096)),
        messages=[{"role": "user", "content": [
            {"type": "text", "text": question or "请分析这张截图并给出答案。"},
            {"type": "image_url", "image_url": {"url": encode_image_bytes(image)}},
        ]}],
    )
    return resp.choices[0].message.content or ""
