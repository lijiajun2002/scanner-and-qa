from __future__ import annotations

import argparse
import sys
import tempfile
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path

from app.capture import capture_photo
from app.config import load_config
from app.hotkey import MouseHotkey, detect_buttons
from app.llm import LLMClient
from app.ui import ResultWindow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="热键拍照 -> 询问视觉 LLM -> 浮窗显示")
    parser.add_argument("--detect", action="store_true", help="探测鼠标按键名称后退出")
    parser.add_argument("--config", default="config.toml", help="配置文件路径")
    return parser.parse_args()


def photo_dest(cfg) -> Path:
    if cfg.save_photos:
        return cfg.output_dir / f"{datetime.now():%Y%m%d_%H%M%S}.jpg"
    return Path(tempfile.gettempdir()) / "scanner-and-qa-latest.jpg"


def main() -> int:
    args = parse_args()
    if args.detect:
        detect_buttons()
        return 0

    cfg = load_config(args.config)
    if not cfg.api_key:
        print("[警告] 未设置 OPENAI_API_KEY（.env 或环境变量），调用会失败。", file=sys.stderr)

    client = LLMClient(cfg)
    root = tk.Tk()
    busy = threading.Event()

    def on_trigger() -> None:
        if busy.is_set():
            win.set_status("上一次请求还没结束，已忽略本次触发")
            return
        busy.set()
        win.show()
        win.clear()
        win.set_status("拍照中…")
        threading.Thread(target=worker, daemon=True).start()

    def worker() -> None:
        try:
            path = capture_photo(cfg.camera_index, photo_dest(cfg))
            win.set_thumbnail(path)
            win.set_status(f"已拍照，正在询问 {cfg.model} …")

            received = False
            for token in client.stream_answer(path):
                if not received:
                    received = True
                    win.set_status("接收回答中…")
                win.append(token)
            if not received:
                win.append("(模型没有返回内容)")
            win.set_status("完成")
        except Exception as exc:  # noqa: BLE001 - 任何错误都显示到窗口
            win.append(f"\n[出错] {exc}")
            win.set_status("出错")
        finally:
            busy.clear()

    win = ResultWindow(root, on_trigger)
    win.hide()

    try:
        hotkey = MouseHotkey(cfg.mouse_button, lambda: win.post("trigger"))
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    hotkey.start()
    print(f"已启动：鼠标键 '{cfg.mouse_button}' 触发，模型 {cfg.model}")
    print(f"提示词：{cfg.prompt}")
    print("窗口按 Esc 隐藏，点『退出』或 Ctrl+C 结束程序。")

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        hotkey.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
