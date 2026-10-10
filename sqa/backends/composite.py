from __future__ import annotations

from .base import Backend, Frame


class CompositeBackend(Backend):
    """截图与输入来源可分离：截图走 video，输入走 software（本版推荐）。"""

    name = "composite"

    def __init__(self, screenshot_backend: Backend, input_backend: Backend) -> None:
        self.shot = screenshot_backend
        self.input = input_backend

    def screenshot(self) -> Frame:
        return self.shot.screenshot()

    def size(self) -> tuple[int, int]:
        return self.shot.size()

    def type(self, text: str, humanize: bool = True) -> dict:
        return self.input.type(text, humanize)

    def key(self, combo: str) -> None:
        self.input.key(combo)

    def move(self, x: float, y: float, relative: bool = False) -> None:
        self.input.move(x, y, relative)

    def click(self, x=None, y=None, button: str = "left", count: int = 1) -> None:
        self.input.click(x, y, button, count)

    def scroll(self, dx: int, dy: int) -> None:
        self.input.scroll(dx, dy)

    def poll_events(self, wait: float = 0.0) -> list[dict]:
        return self.input.poll_events(wait)
