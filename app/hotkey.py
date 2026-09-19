from __future__ import annotations

from pynput import mouse

# 常见叫法到 pynput 键名的映射（X11 下侧键通常是 button8/button9）
_ALIASES = {
    "x1": "button8",
    "x2": "button9",
    "back": "button8",
    "forward": "button9",
}


def resolve_button(name: str) -> mouse.Button:
    key = _ALIASES.get(name.lower(), name.lower())
    try:
        return mouse.Button[key]
    except KeyError as exc:
        valid = ", ".join(sorted(button.name for button in mouse.Button if button.name))
        raise ValueError(f"未知鼠标键 '{name}'，可选: {valid}") from exc


class MouseHotkey:
    """监听某个鼠标按键的按下事件。"""

    def __init__(self, button_name: str, callback) -> None:
        self.button = resolve_button(button_name)
        self.callback = callback
        self._listener: mouse.Listener | None = None

    def _on_click(self, x, y, button, pressed) -> None:
        if pressed and button == self.button:
            self.callback()

    def start(self) -> None:
        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


def detect_buttons() -> None:
    """探测模式：按下任意鼠标键就打印其名称，Ctrl+C 退出。"""
    print("探测模式已启动：按下你想绑定的鼠标键（Ctrl+C 退出）…")

    def on_click(x, y, button, pressed) -> None:
        if pressed:
            print(f"  检测到按键: {button.name}  (value={button.value})")

    listener = mouse.Listener(on_click=on_click)
    listener.start()
    try:
        listener.join()
    except KeyboardInterrupt:
        print("\n已退出探测模式。")
    finally:
        listener.stop()
