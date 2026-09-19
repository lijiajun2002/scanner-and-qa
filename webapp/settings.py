from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS: dict = {
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "api_key": "",
    "prompt": "这张照片里有什么要素？",
    "max_tokens": 1024,
    "b_api_token": "",
    "extract_prompt": "上述所有图片都是同一个题目的截图，请把它们的内容整理成规范、完整、可直接作答的题干。",
    "type_interval_min_ms": 200,
    "type_interval_max_ms": 1000,
    "type_line_pause_min_ms": 1000,
    "type_line_pause_max_ms": 2000,
    "type_typo_rate_pct": 0.5,
    "type_typo_pause_min_ms": 100,
    "type_typo_pause_max_ms": 300,
    "type_enter_via_paste": False,
    "type_space_interval_ms": 15,
    "type_indent_mode": "vscode",
    "type_indent_style": "spaces",
    "type_tab_size": 4,
    "type_clean_invisibles": True,
    "type_dismiss_delay_ms": 80,
    "analyze_extract": True,
    "hotkeys": {
        "capture": "ctrl+shift+alt+8",
        "analyze": "ctrl+shift+alt+9",
        "type_answer": "ctrl+shift+alt+0",
        "clear": "ctrl+shift+alt+minus",
        "stop": "ctrl+shift+alt+plus",
    },
}


def _settings_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get("WEBAPP_SETTINGS", "./webapp_settings.json"))


def load_settings(path: Path | str | None = None) -> dict:
    target = _settings_path(path)
    data = dict(DEFAULTS)
    if target.exists():
        try:
            data.update(json.loads(target.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass

    # 环境变量优先，方便在容器里用 secret 注入而不落盘
    if os.environ.get("OPENAI_API_KEY"):
        data["api_key"] = os.environ["OPENAI_API_KEY"]
    if os.environ.get("OPENAI_BASE_URL"):
        data["base_url"] = os.environ["OPENAI_BASE_URL"]
    if os.environ.get("B_API_TOKEN"):
        data["b_api_token"] = os.environ["B_API_TOKEN"]
    return data


def save_settings(data: dict, path: Path | str | None = None) -> None:
    target = _settings_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
