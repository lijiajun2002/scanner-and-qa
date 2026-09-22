"""B 端注入器逻辑测试（Linux 打桩，不真的注入）。

运行：  python tests/test_typer.py        # 直接跑
或：    pytest tests/test_typer.py

覆盖重点：human 模式的「行内容选中 + 目标缩进替换」动作序列，
以及它不再依赖 Ctrl+L / Ctrl+C / 剪贴板测量。
"""
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import b_client.capture_agent as ca  # noqa: E402

# ---- 打桩：把 Windows 专有符号补上 ----
events = []
ca.os = types.SimpleNamespace(name="nt")

ca._send_inputs = lambda *inputs: events.append(inputs)
ca._vk_input = lambda vk, keyup: ("vk", vk, keyup)
ca._unicode_input = lambda code, keyup: ("uni", code, keyup)
for name, val in {
    "_VK_RETURN": 0x0D, "_VK_TAB": 0x09, "_VK_BACK": 0x08, "_VK_ESCAPE": 0x1B,
    "_VK_HOME": 0x24, "_VK_END": 0x23, "_VK_UP": 0x26, "_VK_DOWN": 0x28,
    "_VK_OEM_4": 0xDB,
    "_VK_CONTROL": 0x11, "_VK_V": 0x56,
}.items():
    setattr(ca, name, val)

clipboard = {}
paste_called = {"n": 0}
ca._set_clipboard_text = lambda t: clipboard.__setitem__("text", t)
ca._paste_shortcut = lambda: paste_called.__setitem__("n", paste_called["n"] + 1)
ca._cursor_pos = lambda: None   # 非 Windows：鼠标守卫打桩


def reset():
    events.clear()
    clipboard.clear()
    paste_called["n"] = 0


def action_trace():
    """把事件压成动作序列，忽略 keyup。"""
    acts = []
    for call in events:
        for inp in call:
            tag, val, keyup = inp
            if keyup:
                continue
            acts.append(f"VK{val:02X}" if tag == "vk" else
                        (chr(val) if val < 128 else f"U+{val:04X}"))
    return acts


# ============================ 纯函数 ============================
def test_pure():
    assert ca.normalize_code_text("a\r\nb\rc") == "a\nb\nc"
    assert ca.normalize_code_text("x\u00a0y\u3000z") == "x y z"
    assert ca.normalize_code_text("a\u200bb") == "ab"
    assert ca.normalize_code_text("\u201cq\u201d") == '"q"'
    assert ca.normalize_code_text("a\r\nb", clean=False) == "a\nb"

    lines = ca.split_code_lines("def f():\n\tif True:\n        return 1\n", 4)
    assert lines == [(0, "def f():"), (4, "if True:"), (8, "return 1")], lines

    assert ca.build_indent(8, "spaces", 4) == "        "
    assert ca.build_indent(8, "tabs", 4) == "\t\t"
    assert ca.build_indent(6, "tabs", 4) == "\t  "

    assert ca.parse_hotkey("ctrl+shift+alt+8") == (
        frozenset({"ctrl", "shift", "alt"}), 0x38)
    assert ca.parse_hotkey("ctrl+shift+alt+minus")[1] == 0xBD
    assert ca.parse_hotkey("ctrl+shift+alt+plus")[1] == 0xBB
    assert ca.parse_hotkey("garbage") == (None, None)
    print("[ok] 纯函数：归一/拆行/缩进/热键解析")


# ============================ none 模式 ============================
def make_typer(**opts):
    base = {"indent_mode": "none", "typo_rate": 0}
    base.update(opts)
    return ca.SendInputTyper(base)


def test_plain():
    reset()
    typer = make_typer()
    typer.type_text("a\nb", 0, 0, humanize=False, dismiss_suggest=False)
    assert action_trace() == ["a", "VK0D", "b"], action_trace()
    print("[ok] none 模式固定序列")


def test_unicode_only_skip():
    reset()
    typer = make_typer()
    skipped = typer.type_text("A\U0001F600B\ud800C", 0, 0, humanize=False,
                              unicode_only=True)
    assert skipped == 2
    assert "A" in action_trace() and "B" in action_trace() and "C" in action_trace()
    print("[ok] 纯 Unicode 跳过非 BMP/代理")


def test_enter_via_paste():
    reset()
    typer = ca.SendInputTyper({"indent_mode": "none", "enter_via_paste": True,
                               "typo_rate": 0})
    typer.type_text("a\nb", 0, 0, humanize=False, dismiss_suggest=False)
    assert "VK0D" not in action_trace()
    assert clipboard.get("text") == "\n"
    assert paste_called["n"] == 1
    print("[ok] none 模式 enter_via_paste")


def test_timing_defaults():
    assert ca.TYPING_DEFAULTS["indent_mode"] == "human"
    assert ca.TYPING_DEFAULTS["space_interval_ms"] == 15
    assert ca.TYPING_DEFAULTS["enter_via_paste"] is False
    print("[ok] 默认参数")


def test_space_speed():
    import unittest.mock as mock
    reset()
    sleeps = []
    typer = make_typer(space_interval_ms=7)
    with mock.patch("time.sleep", lambda s: sleeps.append(s)):
        typer.type_text("a b", 100, 0, humanize=False, dismiss_suggest=False)
    # _sleep 会分块（≤50ms）：空格总计 7ms，两个普通字符各 100ms
    assert any(abs(s - 0.007) < 1e-9 for s in sleeps), sleeps
    assert abs(sum(sleeps) - 0.207) < 0.03, sum(sleeps)
    print("[ok] 空格走独立高速间隔")


# ============================ human 模式 ============================
CODE = 'def test():\n    if True:\n        print("A")\n    print("B")'


def test_clear_line_indent():
    reset()
    typer = ca.SendInputTyper({"typo_rate": 0})
    typer._clear_line_indent()
    trace = action_trace()
    assert trace.count("VKDB") == ca._OUTDENT_PRESSES, trace
    assert trace[-1] == "VK24", trace
    assert "VK10" not in trace and "VK23" not in trace and "VK4C" not in trace, trace
    print("[ok] _clear_line_indent：Ctrl+[ 清缩进 + Home，无选区")


def test_human_mode():
    reset()
    typer = ca.SendInputTyper({"indent_mode": "human", "typo_rate": 0})
    typer.type_text(CODE, 0, 0, humanize=False, dismiss_suggest=False)
    trace = action_trace()
    assert trace.count("VK0D") == 3, trace
    assert trace.count("VK24") == 3, trace                          # 每行 1 次 Home
    assert trace.count("VKDB") == ca._OUTDENT_PRESSES * 3, trace    # 每行 Ctrl+[
    assert "VK4C" not in trace and "VK43" not in trace, trace      # 不再用 Ctrl+L/C
    assert "VK10" not in trace and "VK23" not in trace, trace      # 不再 Shift+End 选区
    assert "VK08" not in trace, trace
    assert trace.index("d") == 0, trace                            # 首行直接输入
    print("[ok] human 模式：逐行对齐，无选区、不碰剪贴板/Ctrl+L")


def test_human_ignores_enter_via_paste():
    reset()
    typer = ca.SendInputTyper({"indent_mode": "human", "enter_via_paste": True,
                               "typo_rate": 0})
    typer.type_text("a\nb", 0, 0, humanize=False, dismiss_suggest=False)
    assert "VK0D" in action_trace()
    assert paste_called["n"] == 0 and "\n" not in clipboard.values()
    print("[ok] human 模式忽略 enter_via_paste（无剪贴板竞态）")


def test_indent_spaces_and_tabs():
    reset()
    typer = ca.SendInputTyper({"indent_mode": "human", "indent_style": "tabs",
                               "tab_size": 4, "typo_rate": 0})
    typer.type_text("if x:\n    y()", 0, 0, humanize=False, dismiss_suggest=False)
    trace = action_trace()
    assert "VK09" not in trace, trace          # 不再依赖 Tab 键
    assert clipboard.get("text") == "\t"       # 缩进串整体粘贴，防 VS Code 智能 Tab
    assert paste_called["n"] == 1
    print("[ok] human + tabs：缩进串粘贴，不用 Tab 键")


def test_parse_plan():
    P = ca.SendInputTyper._parse_plan
    assert P({"order": [2, 1, 3]}, 3) is not None
    assert P({"order": [1, 1, 3]}, 3) is None      # 重复
    assert P({"order": [1, 2]}, 3) is None         # 缺行
    assert P("x", 3) is None
    order, pauses, rev = P({"order": [1, 2, 3],
                            "pauses": [{"after": 1, "ms": 500}, {"after": 9, "ms": 1}],
                            "revisit": [2, 99, 2]}, 3)
    assert order == [1, 2, 3]
    assert pauses == {1: 500.0}
    assert rev == [2]
    print("[ok] plan 校验/归一化")


def test_keys_helpers():
    code = "def f():\n    return 1"
    # 回车对 : 结尾自动加一级缩进，所以无需 <Tab>
    assert ca.keys_valid(["def f():", "<Enter>", "return 1"], code)
    assert not ca.keys_valid(["def f():"], code)
    assert ca.simulate_keys(["a", "<Enter>", "<Tab>", "b"]) == "a\n    b"
    assert ca.simulate_keys(["    x", "<Enter>", "y"]) == "    x\n    y"   # 回车继承缩进
    # 关掉自动缩进时，需要显式 <Tab>
    assert ca.simulate_keys(["if x:", "<Enter>", "<Tab>", "y"], auto_indent=False) \
        == "if x:\n    y"
    print("[ok] 键盘流回放校验/缩进继承")


def test_plan_subset():
    P = ca.SendInputTyper._parse_plan
    assert P({"order": [2, 4]}, 5, subset=True) is not None
    assert P({"order": [1, 2, 3, 4, 5]}, 5, subset=True) is not None
    assert P({"order": []}, 5, subset=True) is None
    assert P({"order": [2, 2]}, 5, subset=True) is None
    assert P({"order": [6]}, 5, subset=True) is None
    assert P({"order": [2, 4]}, 5) is None      # 非子集模式要求全排列
    print("[ok] plan 子集校验（断点续传）")


def test_mouse_stop():
    reset()
    positions = {"n": 0}

    def fake_pos():
        positions["n"] += 1
        return (0, 0) if positions["n"] == 1 else (100, 0)

    ca._cursor_pos = fake_pos
    try:
        typer = ca.SendInputTyper({"indent_mode": "none", "typo_rate": 0,
                                   "mouse_stop_enabled": True, "mouse_stop_px": 10})
        typer.type_text("abcdef", 0, 0, humanize=False, dismiss_suggest=False)
    finally:
        ca._cursor_pos = lambda: None
    assert typer.stopped and typer.stop_reason == "mouse", typer.stop_reason
    assert typer.typed < 6
    print("[ok] 鼠标移动触发停止（mouse）")


def test_plan_invalid_raises():
    typer = ca.SendInputTyper({"indent_mode": "human", "typo_rate": 0})
    try:
        typer.type_plan("a\nb", {"order": [1, 1]}, 0, 0, humanize=False,
                        dismiss_suggest=False)
    except ValueError:
        print("[ok] plan.order 非法时抛 ValueError（供回退）")
        return
    raise AssertionError("expected ValueError")


def test_plan_execution_trace():
    reset()
    typer = ca.SendInputTyper({"indent_mode": "human", "typo_rate": 0})
    typer.type_plan("aa\nbb", {"order": [2, 1]}, 0, 0, humanize=False,
                    dismiss_suggest=False)
    trace = action_trace()
    assert trace.count("VK0D") == 1, trace              # 建 2 行需要 1 个回车
    assert "VK26" in trace and "VK28" not in trace, trace  # 仅向上导航
    assert trace[-1] == "a" and "b" in trace, trace
    print("[ok] plan 执行：建行 + 乱序 + 导航")


def test_line0_not_replaced():
    reset()
    typer = ca.SendInputTyper({"indent_mode": "human", "typo_rate": 0})
    # 首行有缩进时，只输入空格，不做选区替换
    typer.type_text("    x", 0, 0, humanize=False, dismiss_suggest=False)
    trace = action_trace()
    assert trace == [" ", " ", " ", " ", "x"], trace
    print("[ok] 首行不整行替换")


def _run_all():
    test_pure()
    test_plain()
    test_unicode_only_skip()
    test_enter_via_paste()
    test_timing_defaults()
    test_space_speed()
    test_clear_line_indent()
    test_human_mode()
    test_human_ignores_enter_via_paste()
    test_indent_spaces_and_tabs()
    test_line0_not_replaced()
    test_parse_plan()
    test_plan_subset()
    test_keys_helpers()
    test_mouse_stop()
    test_plan_invalid_raises()
    test_plan_execution_trace()
    print("TYPER LOGIC OK")


if __name__ == "__main__":
    _run_all()
