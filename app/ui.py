from __future__ import annotations

import queue
import tkinter as tk
from pathlib import Path
from tkinter import ttk

THUMB_SIZE = (200, 150)


class ResultWindow:
    """置顶浮窗：显示拍照缩略图、状态、以及流式返回的答案。

    线程安全：其它线程只调用 post() 往队列里塞消息，Tk 主线程用 after 轮询消费。
    """

    def __init__(self, root: tk.Tk, on_trigger) -> None:
        self.root = root
        self.on_trigger = on_trigger
        self.queue: queue.Queue = queue.Queue()
        self._thumb_ref = None

        self._build()
        self.root.after(40, self._poll)

    def _build(self) -> None:
        root = self.root
        root.title("拍照问答")
        root.geometry("560x440+80+80")
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.hide)

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")

        self.thumb = tk.Label(top, text="(未拍照)", width=26, height=8,
                              relief="solid", borderwidth=1, anchor="center")
        self.thumb.pack(side="left")

        info = ttk.Frame(top)
        info.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self.status_var = tk.StringVar(value="就绪：按鼠标快捷键开始")
        ttk.Label(info, textvariable=self.status_var, wraplength=280,
                  justify="left").pack(anchor="w")

        btns = ttk.Frame(info)
        btns.pack(anchor="w", pady=(10, 0))
        ttk.Button(btns, text="复制", command=self.copy_all).pack(side="left")
        ttk.Button(btns, text="清空", command=self.clear).pack(side="left", padx=6)
        ttk.Button(btns, text="退出", command=self.root.destroy).pack(side="left")

        body = ttk.Frame(root, padding=(8, 0, 8, 0))
        body.pack(fill="both", expand=True)

        self.text = tk.Text(body, wrap="word", font=("Sans", 11), state="disabled",
                            background="#fbfbfb", relief="flat")
        scroll = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        hint = ttk.Label(root, text="Esc 隐藏窗口 · 按鼠标快捷键再次拍照", padding=6,
                         foreground="#888888")
        hint.pack(fill="x")

        root.bind("<Escape>", lambda _e: self.hide())
        self._make_draggable(top)
        self._make_draggable(info)

    def _make_draggable(self, widget: tk.Widget) -> None:
        widget.bind("<Button-1>", self._start_move)
        widget.bind("<B1-Motion>", self._on_move)

    def _start_move(self, event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_move(self, event) -> None:
        x, y = self._drag
        self.root.geometry(f"+{event.x_root - x}+{event.y_root - y}")

    # ---- 线程安全接口（任意线程可调用） ----
    def post(self, kind: str, payload=None) -> None:
        self.queue.put((kind, payload))

    def show(self) -> None:
        self.post("show")

    def set_status(self, text: str) -> None:
        self.post("status", text)

    def set_thumbnail(self, path: Path) -> None:
        self.post("thumb", str(path))

    def append(self, text: str) -> None:
        self.post("append", text)

    def clear(self) -> None:
        self.post("clear")

    # ---- 主线程内部 ----
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "trigger":
                    self.on_trigger()
                elif kind == "show":
                    self._show()
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "thumb":
                    self._load_thumb(Path(payload))
                elif kind == "append":
                    self._append(payload)
                elif kind == "clear":
                    self._clear()
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    def _show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)

    def hide(self) -> None:
        self.root.withdraw()

    def _load_thumb(self, path: Path) -> None:
        try:
            from PIL import Image, ImageTk

            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail(THUMB_SIZE, Image.LANCZOS)
                self._thumb_ref = ImageTk.PhotoImage(img)
            self.thumb.configure(image=self._thumb_ref, text="", width=THUMB_SIZE[0],
                                 height=THUMB_SIZE[1])
        except Exception as exc:  # noqa: BLE001 - 缩略图失败不影响主流程
            self.thumb.configure(image="", text=f"缩略图失败:\n{exc}")

    def _append(self, text: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text)
        self.text.configure(state="disabled")
        self.text.see("end")

    def _clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def copy_all(self) -> None:
        content = self.text.get("1.0", "end").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.status_var.set("已复制到剪贴板")
