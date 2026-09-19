from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"
DEFAULT_ENV = ROOT / ".env"


@dataclass
class Config:
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    max_tokens: int = 1024
    prompt: str = "这张照片里有什么要素？"
    camera_index: int = 0
    mouse_button: str = "x2"
    save_photos: bool = True
    output_dir: Path = Path("~/Pictures/scanner-and-qa")
    api_key: str = ""


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，不覆盖已存在的环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_config(config_path: Path | str = DEFAULT_CONFIG, env_path: Path | str = DEFAULT_ENV) -> Config:
    config_path = Path(config_path)
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)

    _load_dotenv(Path(env_path))

    output_dir = Path(os.path.expanduser(str(data.get("output_dir", "~/Pictures/scanner-and-qa"))))

    return Config(
        base_url=str(data.get("base_url", "https://api.openai.com/v1")).rstrip("/"),
        model=str(data.get("model", "gpt-4o-mini")),
        max_tokens=int(data.get("max_tokens", 1024)),
        prompt=str(data.get("prompt", "这张照片里有什么要素？")),
        camera_index=int(data.get("camera_index", 0)),
        mouse_button=str(data.get("mouse_button", "x2")).lower(),
        save_photos=bool(data.get("save_photos", True)),
        output_dir=output_dir,
        api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
    )
