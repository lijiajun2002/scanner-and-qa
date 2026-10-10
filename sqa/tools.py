from __future__ import annotations

import json
import time
from pathlib import Path

from .backends import Backend


# ---- OpenAI function-calling 工具定义 ----
TOOL_SPECS: list[dict] = [
    {"type": "function", "function": {
        "name": "screenshot",
        "description": "截取目标机当前屏幕，返回一张图片供你观察。做题/核对时使用。",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string", "description": "这次截图的用途（可选）"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "type_text",
        "description": "在目标机当前焦点处逐字符输入一段文本（只支持 ASCII）。用于写代码/答案。"
                       "会阻塞直到输入完成。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "要输入的文本（仅 ASCII）"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "key",
        "description": "按一个按键或组合键，如 enter / tab / backspace / ctrl+a / ctrl+enter / f5。",
        "parameters": {"type": "object", "properties": {
            "combo": {"type": "string"}}, "required": ["combo"]}}},
    {"type": "function", "function": {
        "name": "move",
        "description": "移动鼠标到归一化坐标（左上 0,0 到右下 1,1）。",
        "parameters": {"type": "object", "properties": {
            "x": {"type": "number"}, "y": {"type": "number"},
            "relative": {"type": "boolean", "description": "相对移动（像素增量，需给 dx/dy）"},
            "dx": {"type": "number"}, "dy": {"type": "number"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "click",
        "description": "点击鼠标。可先给归一化 x,y 再点击。button 默认 left。",
        "parameters": {"type": "object", "properties": {
            "x": {"type": "number"}, "y": {"type": "number"},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "count": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "scroll",
        "description": "滚动页面。dy 为正向下、为负向上；一格=1。",
        "parameters": {"type": "object", "properties": {
            "dx": {"type": "integer"}, "dy": {"type": "integer"}}, "required": ["dy"]}}},
    {"type": "function", "function": {
        "name": "wait",
        "description": "等待若干毫秒（页面加载/渲染）。",
        "parameters": {"type": "object", "properties": {
            "ms": {"type": "integer"}}, "required": ["ms"]}}},
    {"type": "function", "function": {
        "name": "checkpoint",
        "description": "把当前关键进展/结论记到磁盘（上下文被压缩后仍可保留）。",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"}}, "required": ["note"]}}},
    {"type": "function", "function": {
        "name": "handoff",
        "description": "当上下文过长或任务告一段落时，写一份交接：背景 + 下一步要做什么，"
                       "用于开启一个全新的会话继续。",
        "parameters": {"type": "object", "properties": {
            "background": {"type": "string"}, "next_steps": {"type": "string"}},
            "required": ["background", "next_steps"]}}},
    {"type": "function", "function": {
        "name": "report_tool_issue",
        "description": "当你发现工具本身有问题、走了明显弯路或反复碰壁时，说明问题并给出改进建议，"
                       "会写入文件供开发者查看。",
        "parameters": {"type": "object", "properties": {
            "issue": {"type": "string"}, "suggestion": {"type": "string"}},
            "required": ["issue"]}}},
]

HANDOFF_MARKER = "handoff"


class ToolContext:
    def __init__(self, backend: Backend, run_dir: Path) -> None:
        self.backend = backend
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.shots_dir = self.run_dir / "shots"
        self.shots_dir.mkdir(exist_ok=True)
        self._shot_n = 0
        self.calls_log = self.run_dir / "tool_calls.jsonl"
        self.issues_file = self.run_dir / "tool_issues.md"
        self.notes_file = self.run_dir / "notes.md"
        self.handoff_file = self.run_dir / "handoff.md"
        self.handoff: dict | None = None
        self.last_image: Path | None = None

    def _log(self, name: str, args: dict, ok: bool, ms: int, extra: dict | None = None) -> None:
        rec = {"ts": time.time(), "name": name, "args": args, "ok": ok, "ms": ms}
        if extra:
            rec.update(extra)
        with self.calls_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def save_frame(self, frame) -> Path:
        self._shot_n += 1
        path = self.shots_dir / f"shot_{self._shot_n:03d}.jpg"
        path.write_bytes(frame.data)
        return path


def dispatch(ctx: ToolContext, name: str, args: dict) -> dict:
    """执行一个工具调用。返回 {content: str, images: [Path], is_error: bool}。"""
    t0 = time.time()
    images: list[Path] = []
    is_error = False
    try:
        if name == "screenshot":
            frame = ctx.backend.screenshot()
            path = ctx.save_frame(frame)
            images.append(path)
            content = (f"已截图 {frame.width}x{frame.height}，保存为 {path.name}。"
                       f"完整分辨率图片已附上。")
        elif name == "type_text":
            res = ctx.backend.type(str(args.get("text", "")), humanize=True)
            content = f"已输入 {res.get('accepted', 0)} 字符（剔除非 ASCII {res.get('rejected', 0)}）"
            if res.get("stopped"):
                content += "；**被用户中断**"
            content += "。"
        elif name == "key":
            ctx.backend.key(str(args.get("combo", "")))
            content = f"已按键 {args.get('combo')}。"
        elif name == "move":
            ctx.backend.move(float(args.get("x", 0)), float(args.get("y", 0)),
                             bool(args.get("relative")))
            content = "已移动鼠标。"
        elif name == "click":
            ctx.backend.click(
                float(args["x"]) if args.get("x") is not None else None,
                float(args["y"]) if args.get("y") is not None else None,
                str(args.get("button", "left")), int(args.get("count", 1)))
            content = "已点击。"
        elif name == "scroll":
            ctx.backend.scroll(int(args.get("dx", 0)), int(args.get("dy", 0)))
            content = "已滚动。"
        elif name == "wait":
            ctx.backend.wait(int(args.get("ms", 0)))
            content = f"已等待 {args.get('ms')} ms。"
        elif name == "checkpoint":
            note = str(args.get("note", ""))
            with ctx.notes_file.open("a", encoding="utf-8") as f:
                f.write(f"- {note}\n")
            content = "已记录 checkpoint。"
        elif name == "handoff":
            ctx.handoff = {"background": str(args.get("background", "")),
                           "next_steps": str(args.get("next_steps", ""))}
            ctx.handoff_file.write_text(
                f"# Handoff\n\n## 背景\n{ctx.handoff['background']}\n\n"
                f"## 下一步\n{ctx.handoff['next_steps']}\n", encoding="utf-8")
            content = "已写交接，准备开新会话。"
        elif name == "report_tool_issue":
            issue = str(args.get("issue", ""))
            suggestion = str(args.get("suggestion", ""))
            with ctx.issues_file.open("a", encoding="utf-8") as f:
                f.write(f"## {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"- 问题: {issue}\n- 建议: {suggestion}\n\n")
            content = "已记录工具改进建议。"
        else:
            is_error = True
            content = f"未知工具: {name}"
    except Exception as exc:  # noqa: BLE001
        is_error = True
        content = f"工具 {name} 执行失败: {exc}"
    ctx._log(name, args, not is_error, int((time.time() - t0) * 1000),
             {"images": [p.name for p in images]})
    return {"content": content, "images": images, "is_error": is_error}
