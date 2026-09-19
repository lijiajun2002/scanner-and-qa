from __future__ import annotations

import base64
import io
import os

import streamlit as st

from webapp import capture_api
from webapp.settings import load_settings, save_settings


def html_widget(src: str, height: int) -> None:
    """兼容新旧 Streamlit：优先 st.iframe，旧版回退 components.v1.html。"""
    if hasattr(st, "iframe"):
        st.iframe(src, height=height)
    else:  # pragma: no cover
        from streamlit.components.v1 import html as st_html

        st_html(src, height=height)

st.set_page_config(page_title="拍照问答", page_icon="📷", layout="wide")

settings = load_settings()

with st.sidebar:
    st.header("⚙️ 设置")
    base_url = st.text_input("Base URL", value=settings["base_url"])
    api_key = st.text_input("API Key", value=settings["api_key"], type="password")
    model = st.text_input("模型", value=settings["model"])
    prompt = st.text_area("提示词", value=settings["prompt"], height=100)
    max_tokens = st.number_input(
        "max_tokens", min_value=64, max_value=32768,
        value=int(settings["max_tokens"]), step=64,
        help="单次回答的最大长度；回答被截断时调大。范围 64–32768。",
    )

    st.divider()
    st.subheader("🧩 题干抽离层")
    extract_prompt = st.text_area(
        "抽离提示词（固定）", value=settings.get("extract_prompt", ""), height=80
    )
    st.caption("勾选 Tab 里的「先做题干抽离」后会先用这段提示词把多张图整理成规范题干。")

    st.divider()
    st.subheader("🖥️ B 通道")
    b_api_token = st.text_input(
        "B_API_TOKEN", value=settings.get("b_api_token", ""), type="password"
    )
    st.caption("B 客户端需用相同 token 访问本机 8503 端口")

    if st.button("💾 保存设置", use_container_width=True):
        save_settings({
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "prompt": prompt,
            "max_tokens": int(max_tokens),
            "extract_prompt": extract_prompt,
            "b_api_token": b_api_token,
        })
        st.success("已保存，下次打开自动读取。")

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


@st.fragment(run_every=1.5)
def live_b_tab() -> None:
    """轮询服务端状态并渲染（B 端热键的效果也会在这里体现）。"""
    state = capture_api.get_state()
    st.caption(f"状态：{state['status']}")

    images = state["images"]
    if images:
        st.write(f"已采集 **{len(images)}** 张")
        columns = st.columns(min(len(images), 4))
        for index, img in enumerate(images):
            with columns[index % len(columns)]:
                st.image(img, caption=f"#{index + 1}", width=180)

    if state["extracted"]:
        with st.expander("题干（抽离结果）", expanded=True):
            st.markdown(state["extracted"])

    for message in state["messages"]:
        with st.chat_message(message["role"]):
            st.markdown(message["content"] or "…")

    if state["last_answer"]:
        st.markdown("**最近回答**（Ctrl+Shift+L 可逐字输入到 B）")
        st.markdown(state["last_answer"])


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
        "快捷键：`Ctrl+Shift+Alt+8` 截屏（可连按追加）· `+9` 分析（固定走题干抽离，完成即清空会话，无追问）"
        "· `+0` 把最近回答逐字输入到 B · `+-` 清空对话（B 端全局）· `++` 停止本次输入（B 端全局）"
    )

    extract = st.checkbox(
        "先做题干抽离（先用固定提示词整理成规范题干；之后全程不再传图片）", value=True
    )
    capture_api.set_extract_enabled(extract)

    c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
    c1.button("📸 截屏  Ctrl+Shift+Alt+8", shortcut="Ctrl+Shift+Alt+8", type="primary",
              on_click=capture_api.append_action, args=("capture",), use_container_width=True)
    c2.button("🔎 分析  Ctrl+Shift+Alt+9", shortcut="Ctrl+Shift+Alt+9",
              on_click=capture_api.append_action, args=("analyze", {"extract": extract}),
              use_container_width=True)
    c3.button("⌨️ 输入回答  Ctrl+Shift+Alt+0", shortcut="Ctrl+Shift+Alt+0",
              on_click=capture_api.append_action, args=("type_answer",),
              use_container_width=True)
    c4.button("🧹 清空", on_click=capture_api.append_action, args=("clear",),
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

    humanize = st.checkbox(
        "拟人化输入：字符间隔 200–1000ms 随机 + 换行后停顿 1000–2000ms + 0.5% 错字纠正",
        value=True,
    )
    unicode_only = st.checkbox(
        "纯 Unicode 输出：跳过非 Unicode 字符（emoji、代理区等）", value=False
    )
    paste_mode = st.checkbox(
        "整段粘贴模式：一次性粘贴，绕过 IDE 自动补全/自动配对（会覆盖 B 的剪贴板）",
        value=False,
    )
    dismiss_suggest = st.checkbox(
        "IDE 兼容：回车/制表前先按 Esc 关掉自动补全弹窗", value=True, disabled=paste_mode
    )

    k1, k2, k3 = st.columns([1, 1, 2])
    delay = k1.number_input("开始前延迟(s)", min_value=0, max_value=60, value=3)
    interval = k2.number_input(
        "字符间隔(ms)", min_value=0, max_value=1000, value=60, step=10,
        disabled=(humanize or paste_mode),
    )
    k3.button("⬆️ 用最近回答填充", on_click=fill_from_answer, use_container_width=True)

    if st.button("⌨️ 输入到 B", type="primary"):
        text = (st.session_state.get("type_text") or "").strip()
        if not text:
            st.warning("文本为空。")
        else:
            capture_api.request_type(
                text, int(interval), float(delay),
                humanize=humanize, unicode_only=unicode_only,
                dismiss_suggest=dismiss_suggest, paste_mode=paste_mode,
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
