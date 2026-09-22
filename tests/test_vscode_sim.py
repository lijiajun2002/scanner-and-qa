"""在"VS Code 编辑器语义模拟器"上验证 human 模式缩进是否真的正确。

只打桩键盘收发，不依赖 Windows。模拟器按 VS Code 源码实现：
- Home 智能行为 (cursorMoveOperations.moveToBeginningOfLine)
- Shift+End 选到行尾 (不含换行)
- Enter + Python 自动缩进
- Tab：整行选区=缩进整行；非整行选区=替换为缩进；空行=跳到语言缩进
  (cursorTypeEditOperations.TabOperation)
- 括号/引号自动配对 + 覆盖输入，且"仅空白选区不触发 surround"
  (SurroundSelectionOperation._isSurroundSelectionType)

运行：python tests/test_vscode_sim.py
"""
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import b_client.capture_agent as ca  # noqa: E402

VK = {"_VK_RETURN": 0x0D, "_VK_TAB": 0x09, "_VK_BACK": 0x08, "_VK_ESCAPE": 0x1B,
      "_VK_SHIFT": 0x10, "_VK_HOME": 0x24, "_VK_END": 0x23, "_VK_DELETE": 0x2E,
      "_VK_CONTROL": 0x11, "_VK_V": 0x56, "_VK_L": 0x4C, "_VK_C": 0x43,
      "_VK_END": 0x23, "_VK_UP": 0x26, "_VK_DOWN": 0x28, "_VK_OEM_4": 0xDB}
VK_RETURN, VK_TAB, VK_BACK, VK_ESCAPE = 0x0D, 0x09, 0x08, 0x1B
VK_SHIFT, VK_HOME, VK_END, VK_DELETE = 0x10, 0x24, 0x23, 0x2E
VK_CONTROL, VK_OEM_4, VK_UP, VK_DOWN = 0x11, 0xDB, 0x26, 0x28


class VSCode:
    """极简但按源码语义模拟 VS Code 文本编辑。"""

    def __init__(self, lines=None, insert_spaces=True, tab_size=4,
                 auto_indent=True, auto_close=True):
        self.lines = list(lines) if lines is not None else [""]
        self.pos = (0, 0)          # (line_index, col_index) 0-based
        self.anchor = (0, 0)
        self.insert_spaces = insert_spaces
        self.tab_size = tab_size
        self.auto_indent = auto_indent
        self.auto_close = auto_close
        self.shift = False
        self.ctrl = False
        self.clipboard = ""
        self.paste_count = 0

    # ---------- basics ----------
    def text(self):
        return "\n".join(self.lines)

    def _unit(self):
        return " " * self.tab_size if self.insert_spaces else "\t"

    def _sel(self):
        a, b = self.anchor, self.pos
        return (a, b) if a <= b else (b, a)

    def empty(self):
        return self.anchor == self.pos

    def collapse(self):
        self.anchor = self.pos

    def _maxcol(self, li):
        return len(self.lines[li]) + 1

    def _fnw(self, li):
        for i, c in enumerate(self.lines[li]):
            if c not in " \t":
                return i + 1
        return 0

    def _del_sel(self):
        if self.empty():
            return
        (sli, sci), (eli, eci) = self._sel()
        if sli == eli:
            self.lines[sli] = self.lines[sli][:sci] + self.lines[sli][eci:]
        else:
            self.lines[sli] = self.lines[sli][:sci] + self.lines[eli][eci:]
            del self.lines[sli + 1:eli + 1]
        self.pos = (sli, sci)
        self.collapse()

    def _insert(self, s):
        self._del_sel()
        li, ci = self.pos
        self.lines[li] = self.lines[li][:ci] + s + self.lines[li][ci:]
        self.pos = (li, ci + len(s))
        self.collapse()

    # ---------- VS Code commands ----------
    def home(self, select=False):
        li = self.pos[0]
        fnb = self._fnw(li) or 1
        cur = self.pos[1] + 1
        col = 1 if cur == fnb else fnb
        self.pos = (li, col - 1)
        if not select:
            self.collapse()

    def end(self, select=False):
        self.pos = (self.pos[0], len(self.lines[self.pos[0]]))
        if not select:
            self.collapse()

    def up(self):
        if self.pos[0] > 0:
            li = self.pos[0] - 1
            self.pos = (li, min(self.pos[1], len(self.lines[li])))
        self.collapse()

    def down(self):
        if self.pos[0] < len(self.lines) - 1:
            li = self.pos[0] + 1
            self.pos = (li, min(self.pos[1], len(self.lines[li])))
        self.collapse()

    def enter(self):
        self._del_sel()
        li = self.pos[0]
        left = self.lines[li][:self.pos[1]]
        right = self.lines[li][self.pos[1]:]
        if self.auto_indent:
            base = left[:len(left) - len(left.lstrip(" \t"))]
            ind = base + self._unit() if left.rstrip().endswith(":") else base
        else:
            ind = ""
        self.lines[li] = left
        self.lines.insert(li + 1, ind + right)
        self.pos = (li + 1, len(ind))
        self.collapse()

    def _good_indent(self, li):
        for k in range(li - 1, -1, -1):
            if self.lines[k].strip(" \t"):
                base = self.lines[k][:len(self.lines[k]) - len(self.lines[k].lstrip(" \t"))]
                return base + self._unit() if self.lines[k].rstrip().endswith(":") else base
        return self._unit()

    def tab(self, select=False):
        s, e = self._sel()
        if s == e:
            li = self.pos[0]
            line = self.lines[li]
            if all(c in " \t" for c in line):
                good = self._good_indent(li)
                if not line.startswith(good):
                    self.lines[li] = good
                    self.pos = (li, len(good))
                    self.collapse()
                    return
            self._insert(self._unit())
            return
        if s[0] == e[0]:
            if not (s[1] == 0 and e[1] == self._maxcol(s[0]) - 1):
                self._del_sel()
                self._insert(self._unit())
                return
        unit = self._unit()
        for li in range(s[0], e[0] + 1):
            self.lines[li] = unit + self.lines[li]
        self.anchor = (s[0], s[1] + len(unit))
        self.pos = (e[0], e[1] + len(unit))

    def backspace(self):
        if not self.empty():
            self._del_sel()
            return
        li, ci = self.pos
        if ci > 0:
            self.lines[li] = self.lines[li][:ci - 1] + self.lines[li][ci:]
            self.pos = (li, ci - 1)
        elif li > 0:
            prev = len(self.lines[li - 1])
            self.lines[li - 1] += self.lines[li]
            del self.lines[li]
            self.pos = (li - 1, prev)
        self.collapse()

    def delete(self):
        if not self.empty():
            self._del_sel()
            return
        li, ci = self.pos
        if ci < len(self.lines[li]):
            self.lines[li] = self.lines[li][:ci] + self.lines[li][ci + 1:]
        elif li < len(self.lines) - 1:
            self.lines[li] += self.lines[li + 1]
            del self.lines[li + 1]
        self.collapse()

    def type_char(self, ch):
        if self.auto_close and self.empty():
            li, ci = self.pos
            line = self.lines[li]
            nxt = line[ci] if ci < len(line) else ""
            if nxt == ch and ch in ")]}'\"`":
                self.pos = (li, ci + 1)
                self.collapse()
                return
            pairs = {"(": ")", "[": "]", "{": "}", "'": "'", '"': '"'}
            if ch in pairs:
                if ch in "([{" and nxt and (nxt.isalnum() or nxt == "_"):
                    self._insert(ch)
                    return
                self._insert(ch + pairs[ch])
                self.pos = (li, self.pos[1] - 1)
                self.collapse()
                return
        self._insert(ch)

    def paste(self, text):
        self.paste_count += 1
        self._del_sel()
        self._insert(text)

    def _shift_left(self, amount):
        def sh(p):
            li, ci = p
            return (li, max(0, ci - amount))
        self.pos = sh(self.pos)
        self.anchor = sh(self.anchor)

    def outdent(self):
        """editor.action.outdentLines：去掉当前行一个缩进单位（ShiftCommand.unshiftIndent）。"""
        li = self.pos[0]
        line = self.lines[li]
        n = 0
        while n < len(line) and line[n] in " \t":
            n += 1
        if n == 0:
            return
        cut = 1 if line[0] == "\t" else min(n, self.tab_size)
        self.lines[li] = line[cut:]
        self._shift_left(cut)

    def feed(self, inp):
        tag, val, keyup = inp
        if tag == "uni":
            if not keyup:
                self.type_char(chr(val))
            return
        if val == VK_SHIFT:
            self.shift = not keyup
            return
        if val == VK_CONTROL:
            self.ctrl = not keyup
            return
        if keyup:
            return
        if self.ctrl:
            if val == VK_OEM_4:
                self.outdent()
            return
        if val == VK_RETURN:
            self.enter()
        elif val == VK_HOME:
            self.home(select=self.shift)
        elif val == VK_END:
            self.end(select=self.shift)
        elif val == VK_TAB:
            if self.shift:
                self.outdent()
            else:
                self.tab(select=self.shift)
        elif val == VK_UP:
            self.up()
        elif val == VK_DOWN:
            self.down()
        elif val == VK_BACK:
            self.backspace()
        elif val == VK_DELETE:
            self.delete()
        # ESC: no-op


def _install(editor):
    ca.os = types.SimpleNamespace(name="nt")
    for name, val in VK.items():
        setattr(ca, name, val)
    ca._vk_input = lambda vk, keyup: ("vk", vk, keyup)
    ca._unicode_input = lambda code_, keyup: ("uni", code_, keyup)
    ca._send_inputs = lambda *inputs: [editor.feed(i) for i in inputs]
    ca._set_clipboard_text = lambda t: setattr(editor, "clipboard", t)
    ca._paste_shortcut = lambda: editor.paste(editor.clipboard)
    ca._cursor_pos = lambda: None   # 非 Windows：鼠标守卫打桩


def _base_opts(**opts):
    base = {"typo_rate": 0, "dismiss_delay_ms": 0, "space_interval_ms": 0,
            "humanize": False}
    base.update(opts)
    return base


def run_injector(editor, code, **opts):
    _install(editor)
    typer = ca.SendInputTyper(_base_opts(**opts))
    import unittest.mock as mock
    with mock.patch("time.sleep", lambda *a, **k: None):
        typer.type_text(code, 0, 0, humanize=False, dismiss_suggest=False)
    return typer


def run_plan(editor, code, plan, **opts):
    _install(editor)
    typer = ca.SendInputTyper(_base_opts(**opts))
    import unittest.mock as mock
    with mock.patch("time.sleep", lambda *a, **k: None):
        typer.type_plan(code, plan, 0, 0, humanize=False, dismiss_suggest=False)
    return typer


def run_keys(editor, code, keys, **opts):
    _install(editor)
    typer = ca.SendInputTyper(_base_opts(**opts))
    import unittest.mock as mock
    with mock.patch("time.sleep", lambda *a, **k: None):
        typer.type_keys(keys, code, 0, 0, humanize=False, dismiss_suggest=False)
    return typer


TRAP = '''def trap(height):
    if not height:
        return 0
    left, right = 0, len(height) - 1
    left_max, right_max = height[left], height[right]
    ans = 0
    while left < right:
        if height[left] < height[right]:
            left += 1
            left_max = max(left_max, height[left])
            ans += left_max - height[left]
        else:
            right -= 1
            right_max = max(right_max, height[right])
            ans += right_max - height[right]
    return ans'''


def to_style(code, style, tab_size=4):
    """按缩进风格把前导空格重写成 tabs/spaces。"""
    if style != "tabs":
        return code
    out = []
    for line in code.split("\n"):
        cols = len(line) - len(line.lstrip(" "))
        out.append("\t" * (cols // tab_size) + " " * (cols % tab_size) + line.lstrip(" "))
    return "\n".join(out)


def _check(label, insert_spaces, indent_style, auto_indent=True, auto_close=True):
    ed = VSCode(insert_spaces=insert_spaces, tab_size=4,
                auto_indent=auto_indent, auto_close=auto_close)
    run_injector(ed, TRAP, indent_mode="human", indent_style=indent_style,
                 tab_size=4)
    expected = to_style(TRAP, indent_style, 4)
    ok = ed.text() == expected
    print(f"[{'ok' if ok else 'FAIL'}] {label}")
    if not ok:
        got = ed.text().split("\n")
        exp = expected.split("\n")
        for i in range(max(len(got), len(exp))):
            g = got[i] if i < len(got) else "<none>"
            e = exp[i] if i < len(exp) else "<none>"
            if g != e:
                print(f"    line {i+1}: got {g!r} expected {e!r}")
    return ok


def _check_doc(label, lines, pos, expected, indent_style="spaces",
               insert_spaces=True, auto_indent=True):
    ed = VSCode(lines=list(lines), insert_spaces=insert_spaces, tab_size=4,
                auto_indent=auto_indent)
    ed.pos = pos
    ed.anchor = pos
    run_injector(ed, TRAP, indent_mode="human", indent_style=indent_style,
                 tab_size=4)
    ok = ed.text() == expected
    print(f"[{'ok' if ok else 'FAIL'}] {label}")
    if not ok:
        got = ed.text().split("\n")
        exp = expected.split("\n")
        for i in range(max(len(got), len(exp))):
            g = got[i] if i < len(got) else "<none>"
            e = exp[i] if i < len(exp) else "<none>"
            if g != e:
                print(f"    line {i+1}: got {g!r} expected {e!r}")
    return ok


def _check_plan(label, indent_style, auto_indent=True, auto_close=True):
    style = indent_style
    ed = VSCode(insert_spaces=(style == "spaces"), tab_size=4,
                auto_indent=auto_indent, auto_close=auto_close)
    # 明显乱序 + 停顿 + 回看
    plan = {
        "order": [1, 4, 2, 5, 3, 6, 7, 12, 8, 13, 9, 14, 10, 15, 11, 16],
        "pauses": [{"after": 1, "ms": 300}, {"after": 8, "ms": 500}],
        "revisit": [5, 10],
    }
    run_plan(ed, TRAP, plan, indent_style=style, tab_size=4)
    expected = to_style(TRAP, style, 4)
    ok = ed.text() == expected
    print(f"[{'ok' if ok else 'FAIL'}] {label}")
    if not ok:
        got = ed.text().split("\n")
        exp = expected.split("\n")
        for i in range(max(len(got), len(exp))):
            g = got[i] if i < len(got) else "<none>"
            e = exp[i] if i < len(exp) else "<none>"
            if g != e:
                print(f"    line {i+1}: got {g!r} expected {e!r}")
    return ok


def _check_stop_resume(label):
    code = "aa\nbb\ncc\ndd"
    ed = VSCode(insert_spaces=True, tab_size=4)
    _install(ed)
    typer = ca.SendInputTyper(_base_opts(indent_style="spaces", tab_size=4))
    import unittest.mock as mock

    orig = typer._type_char
    counter = {"n": 0}

    def wrapped(ch, *a, **k):
        r = orig(ch, *a, **k)
        if r:
            counter["n"] += 1
            if counter["n"] == 3:       # 停在第 3 个字符（line3 的半行）
                typer.stop("web")
        return r

    typer._type_char = wrapped
    plan = {"order": [1, 3, 2, 4]}
    with mock.patch("time.sleep", lambda *a, **k: None):
        typer.type_plan(code, plan, 0, 0, humanize=False, dismiss_suggest=False)
    stopped_ok = (typer.stopped and typer.stop_reason == "web"
                  and 1 in typer.progress["done_lines"]
                  and typer.progress["current_line"] == 3)
    # 半行 line3 应被退格清空
    partial_cleared = ed.lines[2] == ""
    # 断点续传：对剩余行重新给一份计划
    remaining = [i for i in range(1, 5) if i not in typer.progress["done_lines"]]
    ed2 = ed  # 同一文档继续
    with mock.patch("time.sleep", lambda *a, **k: None):
        typer.type_plan(code, {"order": remaining}, 0, 0, humanize=False,
                        dismiss_suggest=False, resume=True)
    resumed_ok = ed2.text() == code
    ok = stopped_ok and partial_cleared and resumed_ok
    print(f"[{'ok' if ok else 'FAIL'}] {label} (stopped={stopped_ok} "
          f"cleared={partial_cleared} resumed={resumed_ok})")
    if not ok:
        print("    progress:", typer.progress)
        print("    text:", repr(ed2.text()))
    return ok


def _check_keystream(label, auto_indent=True, auto_close=True):
    code = "def f(a):\n    b = a[0]\n    return b"
    keys = ["def f(a):", "<Enter>", "b = a[0]", "<Enter>", "return b"]
    ed = VSCode(insert_spaces=True, tab_size=4, auto_indent=auto_indent,
                auto_close=auto_close)
    run_keys(ed, code, keys, indent_style="spaces", tab_size=4)
    ok = ed.text() == code
    print(f"[{'ok' if ok else 'FAIL'}] {label}")
    if not ok:
        print("    got:", repr(ed.text()), "exp:", repr(code))
    return ok


def _check_keystream_bad(label):
    """回放校验：keyboard 流与目标不一致时必须抛 ValueError（不会写坏）。"""
    ed = VSCode(insert_spaces=True, tab_size=4)
    _install(ed)
    typer = ca.SendInputTyper(_base_opts(indent_style="spaces", tab_size=4))
    import unittest.mock as mock
    try:
        with mock.patch("time.sleep", lambda *a, **k: None):
            typer.type_keys(["nope"], "def f():", 0, 0, humanize=False,
                            dismiss_suggest=False)
    except ValueError:
        print(f"[ok] {label}")
        return True
    print(f"[FAIL] {label}")
    return False


def main():
    results = []
    results.append(_check("spaces + autoIndent + autoClose", True, "spaces"))
    results.append(_check("spaces + no autoIndent + no autoClose", True, "spaces",
                          auto_indent=False, auto_close=False))
    results.append(_check("tabs + autoIndent + autoClose", False, "tabs"))
    results.append(_check("tabs + no autoIndent + no autoClose", False, "tabs",
                          auto_indent=False, auto_close=False))
    results.append(_check_doc("append at end of existing file",
                              ["x = 1", ""], (1, 0), "x = 1\n" + TRAP))
    results.append(_check_doc("insert in middle (content below kept)",
                              ["x = 1", "", "y = 2"], (1, 0),
                              "x = 1\n" + TRAP + "\ny = 2"))
    results.append(_check_plan("plan(order) spaces + autoIndent + autoClose", "spaces"))
    results.append(_check_plan("plan(order) tabs + autoIndent + autoClose", "tabs"))
    results.append(_check_plan("plan(order) spaces + no autoIndent", "spaces",
                               auto_indent=False))
    results.append(_check_stop_resume("stop -> cleanup -> resume"))
    results.append(_check_keystream("keystream spaces + autoIndent + autoClose"))
    results.append(_check_keystream_bad("keystream 校验失败 -> ValueError"))
    print("VSCODE SIM", "OK" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
