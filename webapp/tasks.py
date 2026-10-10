from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import store

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Task:
    client_id: str
    run_dir: Path
    proc: subprocess.Popen | None = None
    status: str = "idle"      # idle|running|stopped|done|error
    goal: str = ""
    started_at: float = 0.0
    error: str = ""
    resume: bool = False


def _latest_run_dir(client_id: str) -> Path | None:
    base = store.runs_dir() / client_id
    if not base.exists():
        return None
    dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name)
    return dirs[-1] if dirs else None


class TaskManager:
    """管理每个客户端的 agent 任务（子进程）。进程内单例。"""

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    def _env(self, client: dict, settings: dict, goal: str) -> dict:
        env = dict(os.environ)
        env["SQA_TARGET_URL"] = client["url"]
        env["SQA_TARGET_TOKEN"] = client.get("token", "")
        env["SQA_LLM_BASE_URL"] = settings.get("base_url", "")
        env["SQA_LLM_API_KEY"] = settings.get("api_key", "")
        env["SQA_LLM_MODEL"] = settings.get("model", "")
        env["SQA_GOAL"] = goal or ""
        env["SQA_SHOT_SOURCE"] = settings.get("shot_source", "software")
        env["SQA_VIDEO_SOURCE"] = settings.get("video_source", "v4l2:/dev/video0")
        env["SQA_VIDEO_SIZE"] = settings.get("video_size", "1920x1080")
        if settings.get("system_prompt"):
            env["SQA_SYSTEM_PROMPT"] = settings["system_prompt"]
        env["SQA_DATA_DIR"] = str(store.data_dir())
        env["SQA_RUNS_DIR"] = str(store.runs_dir())
        return env

    def start(self, client: dict, settings: dict, goal: str, resume: bool = False) -> Task:
        if resume:
            run_dir = _latest_run_dir(client["id"])
            if run_dir is None:
                resume = False
        if not resume:
            run_dir = store.runs_dir() / client["id"] / time.strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(ROOT / "run_agent.py"), "--run-dir", str(run_dir)]
        if resume:
            cmd += ["--resume-from", str(run_dir)]
        log_path = run_dir / "agent.log"
        logf = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=self._env(client, settings, goal),
                                stdout=logf, stderr=subprocess.STDOUT)
        task = Task(client_id=client["id"], run_dir=run_dir, proc=proc,
                    status="running", goal=goal, started_at=time.time(), resume=resume)
        self.tasks[client["id"]] = task
        return task

    def stop(self, client_id: str) -> None:
        t = self.tasks.get(client_id)
        if t and t.proc and t.proc.poll() is None:
            t.proc.terminate()
            try:
                t.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                t.proc.kill()
        if t:
            t.status = "stopped"

    def status(self, client_id: str) -> Task | None:
        t = self.tasks.get(client_id)
        if t and t.proc and t.status == "running":
            rc = t.proc.poll()
            if rc is not None:
                t.status = "done" if rc == 0 else "error"
                if rc != 0:
                    t.error = f"exit {rc}"
        return t

    def log_tail(self, client_id: str, n: int = 40) -> str:
        t = self.tasks.get(client_id)
        run_dir = t.run_dir if t else _latest_run_dir(client_id)
        if not run_dir:
            return ""
        p = run_dir / "agent.log"
        if not p.exists():
            return ""
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    def tool_calls_tail(self, client_id: str, n: int = 20) -> str:
        t = self.tasks.get(client_id)
        run_dir = t.run_dir if t else _latest_run_dir(client_id)
        if not run_dir:
            return ""
        p = run_dir / "tool_calls.jsonl"
        if not p.exists():
            return ""
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return ""


_MANAGER: TaskManager | None = None


def manager() -> TaskManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = TaskManager()
    return _MANAGER
