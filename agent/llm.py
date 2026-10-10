from __future__ import annotations

import base64
import io

from openai import OpenAI

MAX_IMAGE_EDGE = 1600


def encode_image_bytes(data: bytes) -> str:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        if max(img.size) > MAX_IMAGE_EDGE:
            img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


class AgentLLM:
    """OpenAI 兼容的 tool-calling 客户端。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 max_tokens: int = 4096, temperature: float = 0.2) -> None:
        self.client = OpenAI(api_key=api_key or "missing", base_url=base_url)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def chat(self, messages: list[dict], tools: list[dict] | None = None):
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return self.client.chat.completions.create(**kwargs)
