from __future__ import annotations

import subprocess

from .base import Backend, Frame

_NOT_SUPPORTED = "video backend 只支持截图/尺寸（HID 键鼠本版搁置）"


class VideoBackend(Backend):
    """视频采集卡截图（ffmpeg）。Linux 用 v4l2，Windows 用 dshow。

    source 形如：
      v4l2:/dev/video0            或  /dev/video0
      dshow:video=USB Video
      rtsp://192.168.1.10/live
    """

    name = "video"

    def __init__(self, source: str, size: str = "1920x1080", framerate: int = 30,
                 quality: int = 3, extra_input: list[str] | None = None) -> None:
        self.source = source
        self.size = size
        self.framerate = framerate
        self.quality = quality
        self.extra_input = list(extra_input or [])

    def _input_args(self) -> list[str]:
        src = self.source
        if src.startswith("rtsp://") or src.startswith("http"):
            return ["-rtsp_transport", "tcp", "-i", src]
        if src.startswith("dshow:"):
            return ["-f", "dshow", "-i", src[len("dshow:"):]]
        dev = src[len("v4l2:"):] if src.startswith("v4l2:") else src
        return (["-thread_queue_size", "512", "-rtbufsize", "512M",
                 "-f", "v4l2", "-input_format", "mjpeg",
                 "-video_size", self.size, "-framerate", str(self.framerate),
                 "-i", dev] + self.extra_input)

    def screenshot(self) -> Frame:
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error"]
               + self._input_args()
               + ["-frames:v", "1", "-q:v", str(self.quality),
                  "-f", "image2", "-c:v", "mjpeg", "pipe:1"])
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(f"ffmpeg 抓帧失败: {proc.stderr.decode(errors='ignore')[:300]}")
        from PIL import Image
        import io
        with Image.open(io.BytesIO(proc.stdout)) as im:
            w, h = im.size
        return Frame(data=proc.stdout, width=w, height=h)

    def size(self) -> tuple[int, int]:
        try:
            import re
            m = re.match(r"(\d+)x(\d+)", self.size)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:  # noqa: BLE001
            pass
        frame = self.screenshot()
        return frame.width, frame.height

    def type(self, text: str, humanize: bool = True) -> dict:
        raise NotImplementedError(_NOT_SUPPORTED)

    def key(self, combo: str) -> None:
        raise NotImplementedError(_NOT_SUPPORTED)

    def move(self, x: float, y: float, relative: bool = False) -> None:
        raise NotImplementedError(_NOT_SUPPORTED)

    def click(self, x=None, y=None, button: str = "left", count: int = 1) -> None:
        raise NotImplementedError(_NOT_SUPPORTED)

    def scroll(self, dx: int, dy: int) -> None:
        raise NotImplementedError(_NOT_SUPPORTED)
