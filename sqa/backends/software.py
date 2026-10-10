from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

from PIL import Image

from .base import Backend, Frame


class SoftwareBackend(Backend):
    """通过 Target 上的 agent_server（HTTP）截图与注入键鼠。"""

    name = "software"

    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    # ---- 内部 ----
    def _headers(self, extra: dict | None = None) -> dict:
        h = {"X-Auth-Token": self.token} if self.token else {}
        if extra:
            h.update(extra)
        return h

    def _get(self, path: str, timeout: float | None = None) -> bytes:
        req = urllib.request.Request(self.base + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return resp.read()

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers=self._headers({"Content-Type": "application/json"}))
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read() or b"{}")

    # ---- 能力 ----
    def screenshot(self) -> Frame:
        data = self._get("/screenshot")
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        return Frame(data=data, width=w, height=h)

    def size(self) -> tuple[int, int]:
        payload = json.loads(self._get("/size"))
        return int(payload["width"]), int(payload["height"])

    def type(self, text: str, humanize: bool = True) -> dict:
        # 给一套较快的打字节奏（仍逐字符），避免 agent 端等太久
        options = {
            "interval_min_ms": 25, "interval_max_ms": 70,
            "space_interval_ms": 6, "typo_rate": 0.0,
            "line_pause_min_ms": 80, "line_pause_max_ms": 200,
            "indent_mode": "human", "dismiss_suggest": True, "clean_invisibles": True,
        }
        return self._post("/input", {"kind": "type", "text": text,
                                     "humanize": humanize, "options": options})

    def key(self, combo: str) -> None:
        self._post("/input", {"kind": "key", "combo": combo})

    def move(self, x: float, y: float, relative: bool = False) -> None:
        self._post("/input", {"kind": "move", "x": x, "y": y, "relative": relative})

    def click(self, x=None, y=None, button: str = "left", count: int = 1) -> None:
        self._post("/input", {"kind": "click", "x": x, "y": y,
                              "button": button, "count": count})

    def scroll(self, dx: int, dy: int) -> None:
        self._post("/input", {"kind": "scroll", "dx": dx, "dy": dy})

    def poll_events(self, wait: float = 0.0) -> list[dict]:
        payload = json.loads(self._get(f"/events?wait={wait:.1f}", timeout=wait + 10))
        ev = payload.get("event")
        return [ev] if ev else []
