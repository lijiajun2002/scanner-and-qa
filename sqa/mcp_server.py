#!/usr/bin/env python3
"""极简 MCP stdio 服务器：把 sqa 工具暴露为 MCP tools（无第三方依赖）。

实现 JSON-RPC 2.0 over stdio：initialize / notifications/initialized /
tools/list / tools/call / ping。截图以 image 内容返回。

启动：  python -m sqa.mcp_server
配置（同 CLI）：SQA_TARGET_URL / SQA_TARGET_TOKEN / SQA_SHOT_SOURCE 等。
"""
from __future__ import annotations

import base64
import json
import sys
import traceback
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqa.factory import make_backend  # noqa: E402
from sqa.tools import TOOL_SPECS, ToolContext, dispatch  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"


def _mcp_tools() -> list[dict]:
    tools = []
    for spec in TOOL_SPECS:
        fn = spec["function"]
        tools.append({"name": fn["name"], "description": fn.get("description", ""),
                      "inputSchema": fn.get("parameters", {"type": "object", "properties": {}})})
    return tools


class Server:
    def __init__(self) -> None:
        self.ctx = ToolContext(make_backend(), Path("runs/mcp"))

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            return self._ok(mid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sqa", "version": "0.1.0"}})
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": _mcp_tools()})
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            res = dispatch(self.ctx, name, args)
            content: list[dict] = [{"type": "text", "text": res["content"]}]
            for p in res["images"]:
                content.append({"type": "image", "mimeType": "image/jpeg",
                                "data": base64.b64encode(p.read_bytes()).decode("ascii")})
            return self._ok(mid, {"content": content, "isError": res["is_error"]})
        return self._err(mid, -32601, f"method not found: {method}")

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main() -> int:
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = server.handle(msg)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": str(exc)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
