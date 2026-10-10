from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Frame:
    """一帧截图。data 为 JPEG 字节；width/height 为像素。"""
    data: bytes
    width: int
    height: int
    path: str | None = None


class Backend:
    """键鼠/视频能力的统一接口。坐标一律用归一化 0..1。"""

    name = "base"

    def screenshot(self) -> Frame:
        raise NotImplementedError

    def size(self) -> tuple[int, int]:
        raise NotImplementedError

    def type(self, text: str, humanize: bool = True) -> dict:
        raise NotImplementedError

    def key(self, combo: str) -> None:
        raise NotImplementedError

    def move(self, x: float, y: float, relative: bool = False) -> None:
        raise NotImplementedError

    def click(self, x: float | None = None, y: float | None = None,
              button: str = "left", count: int = 1) -> None:
        raise NotImplementedError

    def scroll(self, dx: int, dy: int) -> None:
        raise NotImplementedError

    def wait(self, ms: int) -> None:
        import time
        time.sleep(max(0, ms) / 1000.0)

    def poll_events(self, wait: float = 0.0) -> list[dict]:
        return []
