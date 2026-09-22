from __future__ import annotations

import base64
import io
import os
import re

import streamlit as st

from webapp import capture_api
from webapp.settings import (
    DEFAULT_CHOICE_PROMPT,
    DEFAULT_KEYSTREAM_PROMPT,
    DEFAULT_LANGUAGE_PROMPTS,
    DEFAULT_PLAN_PROMPT,
    DEFAULT_ROUTE_PROMPT,
    load_settings,
    save_settings,
)


def html_widget(src: str, height: int) -> None:
    """兼容新旧 Streamlit：优先 st.iframe，旧版回退 components.v1.html。"""
    if hasattr(st, "iframe"):
        st.iframe(src, height=height)
    else:  # pragma: no cover
        from streamlit.components.v1 import html as st_html

        st_html(src, height=height)

st.set_page_config(page_title="拍照问答", page_icon="📷", layout="wide")

settings = load_settings()


def persist(values: dict) -> None:
    """把某一段参数合并进 settings.json（分区保存）。"""
    merged = dict(settings)
    merged.update(values)
    save_settings(merged)


with st.sidebar:
    st.header("⚙️ 设置")

    st.subheader("🤖 LLM")
    base_url = st.text_input("Base URL", value=settings["base_url"])
    api_key = st.text_input("API Key", value=settings["api_key"], type="password")
    model = st.text_input("模型", value=settings["model"])
    prompt = st.text_area(
        "提示词", value=settings["prompt"], height=100,
        help="通用提示词：题型选「自由问答」时用它作答；代码题的语言提示词缺失时也用它兜底。",
    )
    max_tokens = st.number_input(
        "max_tokens", min_value=64, max_value=32768,
        value=int(settings["max_tokens"]), step=64,
        help="单次回答的最大长度；回答被截断时调大。范围 64–32768。",
    )
    if st.button("💾 保存 LLM 设置", use_container_width=True):
        persist({
            "base_url": base_url, "api_key": api_key, "model": model,
            "prompt": prompt, "max_tokens": int(max_tokens),
        })
        st.success("已保存 LLM 设置。")

    st.divider()
    st.subheader("📚 题型与语言")
    qt_labels = {
        "自动识别（推荐）": "auto",
        "代码题": "code",
        "选择题": "choice",
        "自由问答": "ask",
    }
    saved_qt = settings.get("question_type", "auto")
    if saved_qt not in qt_labels.values():
        saved_qt = "auto"
    qt_default = next(k for k, v in qt_labels.items() if v == saved_qt)
    question_type = qt_labels[st.selectbox(
        "题型", list(qt_labels), index=list(qt_labels).index(qt_default),
        help="自动识别：第一层 LLM 判断题型并分流。代码题=抽离题干后按语言作答；"
             "选择题=单层作答、仅网页显示、不追问；自由问答=用 LLM『提示词』单层作答。",
    )]

    lang_prompts = dict(DEFAULT_LANGUAGE_PROMPTS)
    lang_prompts.update(settings.get("language_prompts") or {})
    choice_prompt = settings.get("choice_prompt", DEFAULT_CHOICE_PROMPT)
    route_prompt = settings.get("route_prompt", DEFAULT_ROUTE_PROMPT)
    extract_prompt = settings.get("extract_prompt", "")
    code_language = settings.get("code_language", "python")

    if question_type in ("code", "auto"):
        langs = ["python", "java"]
        code_language = st.selectbox(
            "代码语言", langs, index=langs.index(code_language) if code_language in langs else 0,
        )
        with st.expander(f"🧩 语言提示词（{code_language} 等，代码题第二层使用）", expanded=False):
            lang_prompts["python"] = st.text_area(
                "Python 提示词", value=lang_prompts.get("python", ""), height=70)
            lang_prompts["java"] = st.text_area(
                "Java 提示词", value=lang_prompts.get("java", ""), height=70)
        with st.expander("🧩 手动代码题的抽离提示词（仅「手动选代码题」时使用）", expanded=False):
            extract_prompt = st.text_area(
                "抽离提示词", value=extract_prompt, height=80)

    if question_type in ("choice", "auto"):
        with st.expander("🧩 选择题提示词（单层作答）", expanded=(question_type == "choice")):
            choice_prompt = st.text_area("选择题提示词", value=choice_prompt, height=70)

    if question_type == "ask":
        st.caption("自由问答使用上方「🤖 LLM」里的『提示词』作答（单层，可追问、可输出到 B）。")

    if question_type == "auto":
        with st.expander("🔀 自动分流提示词（第一层：判题型 + 抽离/作答）", expanded=False):
            route_prompt = st.text_area("route_prompt", value=route_prompt, height=130)

    if st.button("💾 保存题型与语言", use_container_width=True):
        persist({
            "question_type": question_type,
            "code_language": code_language,
            "language_prompts": {
                "python": lang_prompts.get("python", ""),
                "java": lang_prompts.get("java", ""),
            },
            "choice_prompt": choice_prompt,
            "route_prompt": route_prompt,
            "extract_prompt": extract_prompt,
        })
        st.success("已保存题型与语言。")

    st.divider()
    st.subheader("🖥️ B 通道")
    b_api_token = st.text_input(
        "B_API_TOKEN", value=settings.get("b_api_token", ""), type="password"
    )
    st.caption("B 客户端需用相同 token 访问本机 8503 端口")
    if st.button("💾 保存 B 通道", use_container_width=True):
        persist({"b_api_token": b_api_token})
        st.success("已保存 B 通道。")

    st.divider()
    st.subheader("⌨️ 快捷键")
    hk = settings.get("hotkeys") or {}
    hk_capture = st.text_input("截屏", hk.get("capture", "ctrl+shift+alt+8"))
    hk_analyze = st.text_input("分析", hk.get("analyze", "ctrl+shift+alt+9"))
    hk_type = st.text_input("输入回答", hk.get("type_answer", "ctrl+shift+alt+0"))
    hk_clear = st.text_input("清空", hk.get("clear", "ctrl+shift+alt+minus"))
    hk_resume = st.text_input("断点续传", hk.get("resume", hk.get("stop", "ctrl+shift+alt+plus")))
    st.caption("修饰键 ctrl/shift/alt；特殊键用 minus/plus/8/9/0/a-z。B 端会定期拉取并热注册。"
               "（停止不再是快捷键，由「鼠标移动」和网页停止按钮触发。）")
    if st.button("💾 保存快捷键", use_container_width=True):
        persist({
            "hotkeys": {
                "capture": hk_capture, "analyze": hk_analyze, "type_answer": hk_type,
                "clear": hk_clear, "resume": hk_resume,
            },
        })
        st.success("已保存快捷键。")

    if not api_key:
        st.warning("尚未填写 API Key，无法调用模型。")

capture_api.ensure_started(port=8503)
capture_api.set_token(b_api_token or os.environ.get("B_API_TOKEN", ""))


def stitch_png(images: list[bytes]) -> bytes:
    from PIL import Image

    frames = [Image.open(io.BytesIO(data)).convert("RGB") for data in images]
    width = max(frame.width for frame in frames)
    height = sum(frame.height for frame in frames)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    y = 0
    for frame in frames:
        canvas.paste(frame, (0, y))
        y += frame.height
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def copy_image_widget(data: bytes, mime: str = "image/jpeg") -> None:
    b64 = base64.b64encode(data).decode("ascii")
    html_widget(f"""
    <div style="font-family:sans-serif">
      <img id="im" src="data:{mime};base64,{b64}"
           style="max-width:340px;width:100%;cursor:copy;border:1px solid #ddd;border-radius:6px;display:block"/>
      <div id="msg" style="font-size:12px;color:#888;margin-top:4px">点击图片复制到剪贴板</div>
    </div>
    <script>
    const im = document.getElementById('im');
    im.addEventListener('click', async () => {{
      try {{
        const blob = await (await fetch(im.src)).blob();
        await navigator.clipboard.write([new ClipboardItem({{ [blob.type]: blob }})]);
        const m = document.getElementById('msg');
        m.textContent = '已复制到剪贴板 ✓'; m.style.color = '#0a0';
      }} catch (e) {{
        const m = document.getElementById('msg');
        m.textContent = '复制失败：需 localhost/HTTPS 并允许剪贴板'; m.style.color = '#c00';
      }}
    }});
    </script>
    """, 270)


def copy_all_widget(data: bytes, count: int) -> None:
    b64 = base64.b64encode(data).decode("ascii")
    html_widget(f"""
    <div style="font-family:sans-serif">
      <button id="btn" style="cursor:pointer;padding:6px 12px;border-radius:6px;border:1px solid #bbb;background:#fafafa">
        📋 复制全部图片（已竖向拼成一张，共 {count} 张）
      </button>
      <span id="msg" style="margin-left:8px;font-size:12px"></span>
    </div>
    <script>
    document.getElementById('btn').addEventListener('click', async () => {{
      try {{
        const blob = await (await fetch('data:image/png;base64,{b64}')).blob();
        await navigator.clipboard.write([new ClipboardItem({{ 'image/png': blob }})]);
        const m = document.getElementById('msg');
        m.textContent = '已复制 ✓'; m.style.color = '#0a0';
      }} catch (e) {{
        const m = document.getElementById('msg');
        m.textContent = '复制失败：需 localhost/HTTPS 并允许剪贴板'; m.style.color = '#c00';
      }}
    }});
    </script>
    """, 50)


_CHAT_META = {
    "user": ("🙋", "我"),
    "assistant": ("🤖", "助手"),
    "system": ("⚙️", "系统"),
}
_KIND_LABEL = {"images": "题目截图", "extract": "题干", "answer": "回答",
               "question": "追问", "action": "操作"}

_CODE_START = re.compile(
    r"^\s*(def|class|import|from|for|while|if|elif|else|try|except|finally|with|"
    r"return|yield|async|await|lambda|package|public|private|protected|static|"
    r"void|final|using|namespace|const|let|var|function|struct|enum|#include)\b"
)
_CODE_MARK = re.compile(r"[{};]\s*$|^\s*@|^\s*//")


def _looks_like_code(text: str) -> bool:
    """粗判回答是不是代码：多行 + 有缩进/代码关键词/花括号分号。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    if any(ln[:1] in (" ", "\t") for ln in lines[1:]):
        return True
    if any(_CODE_START.match(ln) or _CODE_MARK.search(ln) for ln in lines):
        return True
    return False


def _render_answer(text: str) -> None:
    """回答：像代码就用代码块（保留缩进+高亮），否则按 Markdown 渲染。"""
    if _looks_like_code(text):
        st.code(text, language=str(settings.get("code_language") or "python"))
    else:
        st.markdown(text)


def _inject_chat_css() -> None:
    """聊天气泡样式（每次会话只注入一次）。"""
    st.markdown(
        """
        <style>
        [data-testid="stChatMessage"] {
            border: 1px solid rgba(128, 128, 128, 0.18);
            border-radius: 16px;
            padding: 12px 16px 8px 16px;
            margin: 4px 0 10px 0;
            background: rgba(128, 128, 128, 0.05);
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
        }
        [data-testid="stChatMessage"] p { margin-bottom: 0.4rem; }
        [data-testid="stChatMessage"] pre { border-radius: 10px; }
        [data-testid="stChatMessage"] img { border-radius: 8px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.fragment(run_every=1.5)
def live_b_tab() -> None:
    """轮询服务端状态并渲染（B 端热键的效果也会在这里体现）。

    以"会话记录"的形式回看整段流程：截图 → 回答 → 追问 → 输出动作。
    题干抽离（格式化）结果不进入问答区，单独收在折叠存档里。
    """
    state = capture_api.get_state()
    st.caption(f"状态：{state['status']}")

    transcript = state.get("transcript") or []
    if not transcript:
        images = state["images"]
        if images:
            st.write(f"已采集 **{len(images)}** 张（尚未分析）")
            columns = st.columns(min(len(images), 4))
            for index, img in enumerate(images):
                with columns[index % len(columns)]:
                    st.image(img, caption=f"#{index + 1}", width=180)
        else:
            st.caption("暂无会话内容。按「📸 截屏」开始。")
        return

    if not st.session_state.get("_chat_css"):
        _inject_chat_css()
        st.session_state["_chat_css"] = True

    extracts = [e for e in transcript if e.get("kind") == "extract" and e.get("text")]
    if extracts:
        with st.expander(f"📝 格式化题干（抽离结果 {len(extracts)} 条）", expanded=False):
            st.caption("仅存档，不占用问答区。")
            for entry in extracts:
                st.markdown(entry.get("text", ""))
    st.divider()

    for entry in transcript:
        kind = entry.get("kind", "text")
        if kind == "extract":
            continue
        role = entry.get("role", "assistant")
        imgs = entry.get("images") or []
        text = entry.get("text", "")

        if kind == "action":
            st.markdown(
                '<div style="display:flex;align-items:center;gap:6px;'
                'color:rgba(128,128,128,0.9);font-size:0.82rem;'
                f'margin:0 0 10px 6px">⚙️ <span>{text}</span></div>',
                unsafe_allow_html=True,
            )
            continue

        avatar, name = _CHAT_META.get(role, ("💬", role))
        label = _KIND_LABEL.get(kind, "")
        with st.chat_message(role, avatar=avatar):
            st.caption(f"{name}{(' · ' + label) if label else ''}")
            if imgs:
                columns = st.columns(min(len(imgs), 4))
                for index, img in enumerate(imgs):
                    with columns[index % len(columns)]:
                        st.image(img, caption=f"#{index + 1}", width=180)
            if text:
                _render_answer(text)


def fill_from_answer() -> None:
    state = capture_api.get_state()
    if state["last_answer"]:
        st.session_state.type_text = state["last_answer"]


if "type_text" not in st.session_state:
    st.session_state.type_text = ""

st.title("📷 拍照问答")

tab_b, tab_kb, tab_cam = st.tabs(["📸 B 截屏分析", "⌨️ 键盘输出", "📷 浏览器拍照"])

with tab_b:
    st.caption(
        "快捷键：`Ctrl+Shift+Alt+8` 截屏（新会话，可连按追加）· `+9` 分析 · `+0` 输入回答"
        "· `++` 断点续传 · `+-` 清空。**打字过程中鼠标一动就自动停止**（仅逐字输入时检测；滚轮翻页不影响）。"
        "停止后按 `++` 从断点继续。快捷键与网页按钮走同一条链路。"
    )

    extract = st.checkbox(
        "先做题干抽离（先用固定提示词整理成规范题干；之后全程不再传图片）",
        value=bool(settings.get("analyze_extract", True)),
    )
    capture_api.set_analyze_extract(extract)   # 网页按钮与 B 端快捷键共用这一个开关

    c1, c2, c3 = st.columns(3)
    c1.button("📸 截屏  Ctrl+Shift+Alt+8", shortcut="Ctrl+Shift+Alt+8", type="primary",
              on_click=capture_api.append_action, args=("capture",), use_container_width=True)
    c2.button("🔎 分析  Ctrl+Shift+Alt+9", shortcut="Ctrl+Shift+Alt+9",
              on_click=capture_api.append_action, args=("analyze",),
              use_container_width=True)
    c3.button("⌨️ 输入回答  Ctrl+Shift+Alt+0", shortcut="Ctrl+Shift+Alt+0",
              on_click=capture_api.append_action, args=("type_answer",),
              use_container_width=True)

    d1, d2, d3 = st.columns(3)
    d1.button("⏹ 停止输入（打字时鼠标一动也会停）",
              on_click=capture_api.append_action, args=("stop",), use_container_width=True)
    d2.button("⏩ 断点续传  Ctrl+Shift+Alt++",
              on_click=capture_api.append_action, args=("resume",), use_container_width=True)
    d3.button("🧹 清空", on_click=capture_api.append_action, args=("clear",),
              use_container_width=True)

    live_b_tab()

    state_now = capture_api.get_state()
    if state_now["images"]:
        with st.expander(f"📋 复制图片（{len(state_now['images'])} 张）", expanded=False):
            st.caption("点击图片即可复制到剪贴板；浏览器需在 localhost/HTTPS 并允许剪贴板。")
            copy_all_widget(stitch_png(state_now["images"]), len(state_now["images"]))
            st.divider()
            columns = st.columns(min(len(state_now["images"]), 3))
            for index, img in enumerate(state_now["images"]):
                with columns[index % len(columns)]:
                    st.markdown(f"**#{index + 1}**")
                    copy_image_widget(img)
        st.button("🔄 刷新图片列表")

    if state_now["messages"]:
        ask = st.chat_input("追问…")
        if ask:
            capture_api.append_action("ask", {"text": ask})

with tab_kb:
    st.caption("把文本逐字输入到 B 的当前焦点窗口（中文用 SendInput Unicode，不占用剪贴板）。")

    st.text_area("要输入到 B 的文本", key="type_text", height=160)

    plan_enabled = st.checkbox(
        "🧠 第三阶段：用大模型生成拟人键盘序列",
        value=bool(settings.get("plan_enabled", False)),
        help="先生成一份人类式的按键计划，再由 B 执行；正文仍取自最近回答，内容不会出错。"
             "快捷键 Ctrl+Shift+Alt+0 也会使用。",
    )
    method_labels = {
        "键盘流（LLM 直接给按键序列，像真人敲 Tab/回车）": "keys",
        "行序（LLM 只定写作顺序，引擎保证缩进/正文）": "order",
    }
    saved_method = settings.get("plan_method", "keys")
    if saved_method not in method_labels.values():
        saved_method = "keys"
    method_default = next(k for k, v in method_labels.items() if v == saved_method)
    plan_method = method_labels[st.selectbox(
        "第三阶段方式", list(method_labels), index=list(method_labels).index(method_default),
        disabled=not plan_enabled,
        help="键盘流：LLM 给 <Enter>/<Tab>/<S-Tab>/<Up>/<Down>+文本，B 端执行前会回放校验，"
             "不合格自动回退到行序方案；行序：LLM 只给行顺序，缩进/正文由引擎保证。",
    )]
    with st.expander("🧠 第三阶段提示词", expanded=False):
        if plan_method == "keys":
            keystream_prompt = st.text_area(
                "keystream_prompt",
                value=settings.get("keystream_prompt", DEFAULT_KEYSTREAM_PROMPT),
                height=240, label_visibility="collapsed",
            )
            plan_prompt = settings.get("plan_prompt", DEFAULT_PLAN_PROMPT)
        else:
            plan_prompt = st.text_area(
                "plan_prompt", value=settings.get("plan_prompt", DEFAULT_PLAN_PROMPT),
                height=180, label_visibility="collapsed",
            )
            keystream_prompt = settings.get("keystream_prompt", DEFAULT_KEYSTREAM_PROMPT)

    indent_labels = {
        "human（拟人逐行：逐行对齐缩进后逐字输入，推荐）": "human",
        "不处理（原样逐字，不做缩进对齐）": "none",
    }
    saved_mode = settings.get("type_indent_mode", "human")
    if saved_mode not in indent_labels.values():
        saved_mode = "human"
    default_label = next(k for k, v in indent_labels.items() if v == saved_mode)
    indent_mode = indent_labels[st.selectbox(
        "缩进策略", list(indent_labels), index=list(indent_labels).index(default_label)
    )]

    target_minutes = st.number_input(
        "目标总时长(分钟)", 0, 120, int(settings.get("type_target_minutes", 10)), 1,
        help="开启后由服务端按这个总时长自动摊平节奏：逐字保持真人速度，其余时间作为逐行“思考”停顿。"
             "此时下面的“字符间隔 / 换行停顿 / 空格间隔”会被自动接管（灰色不可调）；"
             "填 0 = 关闭自动摊平，改用下面的手动参数。",
    )
    target_on = int(target_minutes) > 0
    if target_on:
        st.caption(f"⏱ 已开启总时长自动摊平：按 ≈{int(target_minutes)} 分钟编排节奏，"
                   "「字符间隔 / 换行停顿 / 空格间隔」由服务端按答案长度计算、手动值暂不生效（填 0 可改回手动）。")

    g1, g2, g3 = st.columns(3)
    indent_style = g1.selectbox(
        "缩进字符", ["spaces", "tabs"],
        index=0 if settings.get("type_indent_style", "spaces") == "spaces" else 1,
    )
    tab_size = g2.number_input("tab 宽度", 1, 8, int(settings.get("type_tab_size", 4)))
    space_interval = g3.number_input(
        "空格/缩进间隔(ms)", 0, 200, int(settings.get("type_space_interval_ms", 15)), 1,
        disabled=target_on,
        help="空格与缩进使用这个高速间隔，与普通字符的速率分开。开启总时长后由服务端接管。",
    )

    humanize = st.checkbox("拟人化输入：随机间隔 + 换行停顿 + 偶发错字纠正", value=True)
    unicode_only = st.checkbox(
        "纯 Unicode 输出：跳过非 Unicode 字符（emoji、代理区等）", value=False
    )
    paste_mode = st.checkbox(
        "整段粘贴模式：一次性粘贴，绕过补全/自动配对（会覆盖 B 的剪贴板）", value=False
    )
    dismiss_suggest = st.checkbox(
        "IDE 兼容：回车/制表前先按 Esc 关掉自动补全弹窗", value=True, disabled=paste_mode
    )
    clean_invisibles = st.checkbox(
        "清理不可见/智能字符（NBSP、全角空格、零宽、智能引号）",
        value=bool(settings.get("type_clean_invisibles", True)),
    )
    enter_via_paste = st.checkbox(
        "换行用粘贴插入（仅“不处理”模式）",
        value=bool(settings.get("type_enter_via_paste", False)),
        disabled=(paste_mode or indent_mode != "none"),
    )

    with st.expander(
        "⚙️ 高级：拟人化参数（Ctrl+Shift+Alt+0 也使用）"
        + ("　—　总时长开启时，间隔类参数暂不生效" if target_on else "")
    ):
        a1, a2 = st.columns(2)
        t_imin = a1.number_input("字符间隔最小(ms)", 0, 10000,
                                 int(settings.get("type_interval_min_ms", 200)), 10,
                                 disabled=target_on)
        t_imax = a2.number_input("字符间隔最大(ms)", 0, 10000,
                                 int(settings.get("type_interval_max_ms", 1000)), 10,
                                 disabled=target_on)
        b1, b2 = st.columns(2)
        t_lpmin = b1.number_input("换行停顿最小(ms)", 0, 60000,
                                  int(settings.get("type_line_pause_min_ms", 1000)), 50,
                                  disabled=target_on)
        t_lpmax = b2.number_input("换行停顿最大(ms)", 0, 60000,
                                  int(settings.get("type_line_pause_max_ms", 2000)), 50,
                                  disabled=target_on)
        e1, e2, e3 = st.columns(3)
        t_typo = e1.number_input("错字概率(%)", 0.0, 20.0,
                                 float(settings.get("type_typo_rate_pct", 0.5)), 0.1)
        t_tpmin = e2.number_input("错字停顿最小(ms)", 0, 5000,
                                  int(settings.get("type_typo_pause_min_ms", 100)), 10)
        t_tpmax = e3.number_input("错字停顿最大(ms)", 0, 5000,
                                  int(settings.get("type_typo_pause_max_ms", 300)), 10)
        t_dd = st.number_input("Esc 后等待(ms)", 0, 1000,
                               int(settings.get("type_dismiss_delay_ms", 80)), 5)

    numeric_options = {
        "interval_min_ms": int(t_imin),
        "interval_max_ms": int(t_imax),
        "line_pause_min_ms": int(t_lpmin),
        "line_pause_max_ms": int(t_lpmax),
        "space_interval_ms": int(space_interval),
        "typo_rate": float(t_typo) / 100.0,
        "typo_pause_min_ms": int(t_tpmin),
        "typo_pause_max_ms": int(t_tpmax),
        "enter_via_paste": bool(enter_via_paste),
        "indent_mode": indent_mode,
        "indent_style": indent_style,
        "tab_size": int(tab_size),
        "clean_invisibles": bool(clean_invisibles),
        "dismiss_delay_ms": int(t_dd),
    }

    k1, k2, k3 = st.columns([1, 1, 2])
    delay = k1.number_input("开始前延迟(s)", min_value=0, max_value=60, value=3)
    interval = k2.number_input(
        "固定字符间隔(ms)", min_value=0, max_value=10000, value=60, step=10,
        disabled=(humanize or paste_mode), help="仅在关闭“拟人化输入”时使用。",
    )
    k3.button("⬆️ 用最近回答填充", on_click=fill_from_answer, use_container_width=True)

    capture_api.set_typing_options({
        **numeric_options, "humanize": humanize, "unicode_only": unicode_only,
        "dismiss_suggest": dismiss_suggest, "paste_mode": paste_mode,
        "interval_ms": int(interval), "start_delay_s": float(delay),
    })

    if st.button("💾 保存键盘参数"):
        persist({
            "type_interval_min_ms": int(t_imin), "type_interval_max_ms": int(t_imax),
            "type_line_pause_min_ms": int(t_lpmin), "type_line_pause_max_ms": int(t_lpmax),
            "type_space_interval_ms": int(space_interval),
            "type_typo_rate_pct": float(t_typo),
            "type_typo_pause_min_ms": int(t_tpmin), "type_typo_pause_max_ms": int(t_tpmax),
            "type_enter_via_paste": bool(enter_via_paste),
            "type_indent_mode": indent_mode, "type_indent_style": indent_style,
            "type_tab_size": int(tab_size), "type_clean_invisibles": bool(clean_invisibles),
            "type_dismiss_delay_ms": int(t_dd),
            "plan_enabled": bool(plan_enabled), "plan_prompt": plan_prompt,
            "plan_method": plan_method, "keystream_prompt": keystream_prompt,
            "type_target_minutes": int(target_minutes),
            "analyze_extract": bool(extract),
        })
        st.success("已保存键盘参数。")

    if st.button("⌨️ 输入到 B", type="primary"):
        text = (st.session_state.get("type_text") or "").strip()
        if not text:
            st.warning("文本为空。")
        else:
            capture_api.request_type(
                text, int(interval), float(delay),
                humanize=humanize, unicode_only=unicode_only,
                dismiss_suggest=dismiss_suggest, paste_mode=paste_mode,
                options=numeric_options,
            )
            st.success(
                f"已发送：约 {int(delay)} 秒后开始输入（共 {len(text)} 字）。"
                "请切到 B 的目标输入框。"
            )

    result = capture_api.get_last_result()
    if result:
        if result.get("ok"):
            skipped = result.get("skipped") or 0
            extra = f"，跳过 {skipped} 字" if skipped else ""
            st.success(f"最近任务 {result.get('kind')} 成功（{result.get('chars', '')} 字{extra}）")
        else:
            st.error(f"最近任务 {result.get('kind')} 失败：{result.get('error')}")
    st.button("🔄 刷新状态")

with tab_cam:
    st.caption("用浏览器摄像头拍照（仅在 localhost 等安全上下文可用）。")
    left, right = st.columns([1, 1], gap="large")
    with left:
        image = st.camera_input("对准目标后拍照")
    with right:
        if image is not None:
            st.image(image, caption="刚拍的照片", use_container_width=True)
        else:
            st.info("左侧拍照后，这里会显示预览。")

    if image is not None and st.button("➕ 加入待分析（回到第一个 Tab 分析）"):
        capture_api.add_image(image.getvalue())
        st.success("已加入服务端待分析图片。")
