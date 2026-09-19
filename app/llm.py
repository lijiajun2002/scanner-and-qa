from __future__ import annotations

import base64
import io
from pathlib import Path

from openai import OpenAI

from .config import Config

MAX_IMAGE_EDGE = 1280


def encode_image_bytes(data: bytes) -> str:
    """把图片字节压到最长边 MAX_IMAGE_EDGE，再编码成 data URL，省 token 和带宽。"""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        if max(img.size) > MAX_IMAGE_EDGE:
            img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=88)

    b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def encode_image(image_path: Path) -> str:
    return encode_image_bytes(Path(image_path).read_bytes())


class LLMClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = OpenAI(api_key=cfg.api_key or "missing", base_url=cfg.base_url)

    def _stream(self, messages: list[dict]):
        response = self.client.chat.completions.create(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            stream=True,
            messages=messages,
        )
        for chunk in response:
            if not chunk.choices:
                continue
            content = getattr(chunk.choices[0].delta, "content", None)
            if content:
                yield content

    def stream_answer(self, image_path: Path):
        yield from self.stream_answer_bytes(Path(image_path).read_bytes())

    def stream_answer_bytes(self, image_data: bytes, prompt: str | None = None):
        data_url = encode_image_bytes(image_data)
        yield from self._stream([
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or self.cfg.prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ])

    def stream_chat(self, messages: list[dict], images: bytes | list[bytes] | None = None):
        """多轮对话：messages 为 [{"role", "content": 文本}] 历史。

        images 可以是单张 bytes 或 bytes 列表；全部注入第一条 user 消息，每轮重发（API 无状态）。
        """
        if images is None:
            image_list: list[bytes] = []
        elif isinstance(images, (bytes, bytearray)):
            image_list = [bytes(images)]
        else:
            image_list = list(images)

        data_urls = [encode_image_bytes(data) for data in image_list]
        api_messages: list[dict] = []
        for index, message in enumerate(messages):
            role = message["role"]
            content = message["content"]
            if index == 0 and role == "user" and data_urls:
                parts: list[dict] = [{"type": "text", "text": content}]
                parts.extend({"type": "image_url", "image_url": {"url": url}} for url in data_urls)
                content = parts
            api_messages.append({"role": role, "content": content})
        yield from self._stream(api_messages)
