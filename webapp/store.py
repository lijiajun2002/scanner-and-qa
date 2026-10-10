from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("SQA_DATA_DIR", "data"))


def runs_dir() -> Path:
    return Path(os.environ.get("SQA_RUNS_DIR", "runs"))


SETTINGS_DEFAULTS = {
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "api_key": "",
    "max_tokens": 4096,
    "shot_source": "software",          # software | video
    "video_source": "v4l2:/dev/video0",
    "video_size": "1920x1080",
    "default_goal": "",
    "system_prompt": "",                # 留空则用 agent/prompt.SYSTEM_PROMPT
}


def _settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> dict:
    data = dict(SETTINGS_DEFAULTS)
    p = _settings_path()
    if p.exists():
        try:
            data.update(json.loads(p.read_text(encoding="utf-8")) or {})
        except (OSError, json.JSONDecodeError):
            pass
    for env, key in (("OPENAI_API_KEY", "api_key"), ("OPENAI_BASE_URL", "base_url"),
                     ("SQA_LLM_MODEL", "model")):
        if os.environ.get(env):
            data[key] = os.environ[env]
    return data


def save_settings(values: dict) -> None:
    data = load_settings()
    data.update(values)
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 客户端（B 端）注册表 ----------------
def _clients_path() -> Path:
    return data_dir() / "clients.json"


def load_clients() -> list[dict]:
    p = _clients_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return list(data.get("clients") or [])
    except (OSError, json.JSONDecodeError):
        return []


def save_clients(clients: list[dict]) -> None:
    p = _clients_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"clients": clients}, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def upsert_client(name: str, url: str, token: str, client_id: str | None = None) -> dict:
    clients = load_clients()
    if client_id:
        for c in clients:
            if c["id"] == client_id:
                c.update({"name": name, "url": url, "token": token})
                save_clients(clients)
                return c
    c = {"id": client_id or uuid.uuid4().hex[:8], "name": name or url,
         "url": url, "token": token, "created_at": time.time()}
    clients.append(c)
    save_clients(clients)
    return c


def delete_client(client_id: str) -> None:
    clients = [c for c in load_clients() if c["id"] != client_id]
    save_clients(clients)
