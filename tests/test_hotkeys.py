"""B 端全局热键：解析 / 映射 / 重映射 / resume 投递（纯打桩，Linux 可跑）。

运行：  python tests/test_hotkeys.py
或：    pytest tests/test_hotkeys.py
"""
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import b_client.capture_agent as ca  # noqa: E402


class FakeKey:
    def __init__(self, name=None, vk=None, char=None):
        self.name = name
        self.vk = vk
        self.char = char


class FakeTyper:
    def __init__(self):
        self.progress = {"done_lines": [1, 2], "total_lines": 5, "current_line": 3}

    def stop(self, reason="stop"):
        self.stopped = True


posted = []
payloads = []


def fake_post_action(base, headers, log, action, payload=None):
    posted.append(action)
    payloads.append(payload)


ca.post_action = fake_post_action
log = logging.getLogger("t")
typer = FakeTyper()
gh = ca.GlobalHotkeys("http://x", {}, log, typer)
gh.set_mapping({
    "capture": "ctrl+shift+alt+8",
    "analyze": "ctrl+shift+alt+9",
    "type_answer": "ctrl+shift+alt+0",
    "clear": "ctrl+shift+alt+minus",
    "resume": "ctrl+shift+alt+plus",
})


def press(trigger):
    gh._on_press(FakeKey(name="ctrl_l"))
    gh._on_press(FakeKey(name="shift"))
    gh._on_press(FakeKey(name="alt_l"))
    gh._on_press(trigger)
    gh._on_release(FakeKey(name="ctrl_l"))
    gh._on_release(FakeKey(name="shift"))
    gh._on_release(FakeKey(name="alt_l"))


def test_parse_hotkey():
    assert ca.parse_hotkey("ctrl+shift+alt+8") == (frozenset({"ctrl", "shift", "alt"}), 0x38)
    assert ca.parse_hotkey("ctrl+shift+alt+minus")[1] == 0xBD
    assert ca.parse_hotkey("ctrl+shift+alt+plus")[1] == 0xBB
    assert ca.parse_hotkey("ctrl+alt+j")[1] == 0x4A
    assert ca.parse_hotkey("f5")[1] == 0x74
    assert ca.parse_hotkey("garbage") == (None, None)
    print("[ok] 热键解析")


def test_mapping_and_resume():
    posted.clear()
    payloads.clear()
    press(FakeKey(vk=0x38))
    press(FakeKey(vk=0x39))
    press(FakeKey(vk=0x30))
    press(FakeKey(vk=0xBD))
    press(FakeKey(vk=0xBB))
    assert posted == ["capture", "analyze", "type_answer", "clear", "resume"], posted
    # resume 会带上 B 端断点进度
    assert payloads[-1] == {"done_lines": [1, 2], "total_lines": 5}, payloads[-1]
    print("[ok] 映射生效 + resume 带断点进度")


def test_requires_modifiers():
    posted.clear()
    gh._on_press(FakeKey(vk=0x38))          # 缺 ctrl/shift/alt
    assert posted == []
    gh._on_release(FakeKey(vk=0x38))
    print("[ok] 缺修饰键不触发")


def test_char_fallback():
    posted.clear()
    gh._on_press(FakeKey(name="ctrl_l"))
    gh._on_press(FakeKey(name="shift"))
    gh._on_press(FakeKey(name="alt_l"))
    gh._on_press(FakeKey(char="8"))          # 无 vk 时按 char 匹配
    gh._on_release(FakeKey(name="ctrl_l"))
    gh._on_release(FakeKey(name="shift"))
    gh._on_release(FakeKey(name="alt_l"))
    assert posted == ["capture"], posted
    print("[ok] char 回退匹配")


def test_remap():
    posted.clear()
    gh.set_mapping({"capture": "ctrl+alt+j"})
    gh._on_press(FakeKey(name="ctrl_l"))
    gh._on_press(FakeKey(name="alt_l"))
    gh._on_press(FakeKey(char="j"))
    gh._on_release(FakeKey(name="ctrl_l"))
    gh._on_release(FakeKey(name="alt_l"))
    assert posted == ["capture"], posted
    print("[ok] 热键可重新映射")


def test_vk_char():
    assert ca._vk_char(0x41) == "a" and ca._vk_char(0x38) == "8"
    assert ca._vk_char(0x24) is None
    print("[ok] vk->char")


def _run_all():
    test_parse_hotkey()
    test_mapping_and_resume()
    test_requires_modifiers()
    test_char_fallback()
    test_remap()
    test_vk_char()
    print("HOTKEY OK")


if __name__ == "__main__":
    _run_all()
