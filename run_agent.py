#!/usr/bin/env python3
"""启动轻量 agent 循环，驱动被控机答题。

用法示例：
  .venv/bin/python run_agent.py \
      --target-url http://100.102.134.42:8600 --token admin \
      --shot-source video --video-source v4l2:/dev/video0 --video-size 1920x1080 \
      --run-dir runs/two-sum --max-sessions 6

LLM 配置默认读 data/settings.json（base_url/model/api_key），可用 env 覆盖：
  SQA_LLM_BASE_URL / SQA_LLM_API_KEY / SQA_LLM_MODEL
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.llm import AgentLLM  # noqa: E402
from agent.loop import EventPoller, build_initial_messages, run_session  # noqa: E402
from sqa.backends import CompositeBackend, SoftwareBackend, VideoBackend  # noqa: E402
from sqa.tools import ToolContext  # noqa: E402


def _load_llm_settings() -> dict:
    base = os.environ.get("SQA_LLM_BASE_URL")
    key = os.environ.get("SQA_LLM_API_KEY")
    model = os.environ.get("SQA_LLM_MODEL")
    if not (base and key and model):
        path = ROOT / "data" / "settings.json"
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            base = base or d.get("base_url")
            key = key or d.get("api_key")
            model = model or d.get("model")
    return {"base_url": base, "api_key": key, "model": model}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-url", default=os.environ.get("SQA_TARGET_URL",
                                                           "http://100.102.134.42:8600"))
    ap.add_argument("--token", default=os.environ.get("SQA_TARGET_TOKEN", "admin"))
    ap.add_argument("--shot-source", choices=["software", "video"], default="software")
    ap.add_argument("--video-source", default="v4l2:/dev/video0")
    ap.add_argument("--video-size", default="1920x1080")
    ap.add_argument("--run-dir", default="runs/agent")
    ap.add_argument("--resume-from", default="",
                    help="续跑：从该 run 目录读取 handoff + 最新截图，续写同一目录")
    ap.add_argument("--goal", default=os.environ.get("SQA_GOAL", ""),
                    help="交给 agent 的任务描述；留空则让它自行理解屏幕内容")
    ap.add_argument("--max-sessions", type=int, default=8)
    ap.add_argument("--max-steps", type=int, default=120)
    ap.add_argument("--token-limit", type=int, default=int(os.environ.get(
        "SQA_TOKEN_LIMIT", "200000")))
    args = ap.parse_args()

    s = _load_llm_settings()
    if not (s["base_url"] and s["api_key"] and s["model"]):
        print("缺少 LLM 配置（base_url/api_key/model）", file=sys.stderr)
        return 2

    software = SoftwareBackend(args.target_url, args.token)
    if args.shot_source == "video":
        video = VideoBackend(args.video_source, size=args.video_size)
        backend = CompositeBackend(video, software)
    else:
        backend = software

    run_dir = Path(args.run_dir)
    if args.resume_from:
        run_dir = Path(args.resume_from)
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(backend, run_dir)
    poller = EventPoller(backend)
    poller.start()

    llm = AgentLLM(s["base_url"], s["api_key"], s["model"])

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with (run_dir / "agent.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # 续跑：从已有 handoff + 最新截图恢复
    handoff_text = None
    seed_image = None
    if args.resume_from:
        hf = run_dir / "handoff.md"
        if hf.exists():
            handoff_text = hf.read_text(encoding="utf-8")
        shots = sorted((run_dir / "shots").glob("*.jpg")) if (run_dir / "shots").exists() else []
        if shots:
            seed_image = shots[-1]
        log(f"续跑：handoff={'有' if handoff_text else '无'} 截图={seed_image}")

    messages = build_initial_messages(handoff_text, seed_image, goal=args.goal or None,
                                      system_prompt=os.environ.get("SQA_SYSTEM_PROMPT") or None)
    for session_no in range(1, args.max_sessions + 1):
        log(f"===== 会话 {session_no} 开始（shot={args.shot_source}）=====")
        result = run_session(llm, ctx, poller, messages, args.max_steps,
                             args.token_limit, log=log)
        if result != "handoff":
            log("===== agent 结束 =====")
            break
        handoff_text = ctx.handoff_file.read_text(encoding="utf-8") \
            if ctx.handoff_file.exists() else ""
        messages = build_initial_messages(handoff_text, ctx.last_image, goal=args.goal or None)
        ctx.handoff = None
    poller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
