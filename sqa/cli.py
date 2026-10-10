#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqa.factory import make_backend  # noqa: E402
from sqa.tools import ToolContext, dispatch  # noqa: E402


def _ctx(args) -> ToolContext:
    backend = make_backend(shot_source=args.shot_source, target_url=args.target_url,
                           token=args.token, video_source=args.video_source,
                           video_size=args.video_size)
    return ToolContext(backend, Path(args.run_dir))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sqa", description="答题键鼠/截图工具（CLI）")
    p.add_argument("--target-url", default=os.environ.get("SQA_TARGET_URL",
                                                          "http://127.0.0.1:8600"))
    p.add_argument("--token", default=os.environ.get("SQA_TARGET_TOKEN", "admin"))
    p.add_argument("--shot-source", choices=["software", "video"], default="software")
    p.add_argument("--video-source", default=os.environ.get("SQA_VIDEO_SOURCE",
                                                            "v4l2:/dev/video0"))
    p.add_argument("--video-size", default=os.environ.get("SQA_VIDEO_SIZE", "1920x1080"))
    p.add_argument("--run-dir", default="runs/cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("size")
    sub.add_parser("screenshot")
    sp = sub.add_parser("type"); sp.add_argument("text")
    sp = sub.add_parser("key"); sp.add_argument("combo")
    sp = sub.add_parser("move"); sp.add_argument("x", type=float); sp.add_argument("y", type=float)
    sp = sub.add_parser("click")
    sp.add_argument("x", nargs="?", type=float); sp.add_argument("y", nargs="?", type=float)
    sp.add_argument("--button", default="left"); sp.add_argument("--count", type=int, default=1)
    sp = sub.add_parser("scroll"); sp.add_argument("dy", type=int); sp.add_argument("dx", nargs="?", type=int, default=0)
    sp = sub.add_parser("wait"); sp.add_argument("ms", type=int)

    args = p.parse_args(argv)
    ctx = _ctx(args)

    if args.cmd == "size":
        print(json.dumps({"width": ctx.backend.size()[0], "height": ctx.backend.size()[1]}))
        return 0
    if args.cmd == "screenshot":
        res = dispatch(ctx, "screenshot", {})
        print(res["content"])
        return 0
    if args.cmd == "type":
        res = dispatch(ctx, "type_text", {"text": args.text}); print(res["content"]); return 0
    if args.cmd == "key":
        res = dispatch(ctx, "key", {"combo": args.combo}); print(res["content"]); return 0
    if args.cmd == "move":
        res = dispatch(ctx, "move", {"x": args.x, "y": args.y}); print(res["content"]); return 0
    if args.cmd == "click":
        res = dispatch(ctx, "click", {"x": args.x, "y": args.y,
                                      "button": args.button, "count": args.count})
        print(res["content"]); return 0
    if args.cmd == "scroll":
        res = dispatch(ctx, "scroll", {"dx": args.dx, "dy": args.dy}); print(res["content"]); return 0
    if args.cmd == "wait":
        res = dispatch(ctx, "wait", {"ms": args.ms}); print(res["content"]); return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
