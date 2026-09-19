from __future__ import annotations

import os
import time

import streamlit as st

from app.config import Config
from app.llm import LLMClient
from webapp import capture_api
from webapp.settings import load_settings, save_settings

st.set_page_config(page_title="拍照问答", page_icon="📷", layout="wide")

settings = load_settings()

with st.sidebar:
    st.header("⚙️ 设置")
    base_url = st.text_input("Base URL", value=settings["base_url"])
    api_key = st.text_input("API Key", value=settings["api_key"], type="password")
    model = st.text_input("模型", value=settings["model"])
    prompt = st.text_area("提示词", value=settings["prompt"], height=100)
    max_tokens = st.number_input(
        "max_tokens", min_value=64, max_value=8192,
        value=int(settings["max_tokens"]), step=64,
    )

    st.divider()
    st.subheader("🖥️ B 截屏通道")
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
            "b_api_token": b_api_token,
        })
        st.success("已保存，下次打开自动读取。")

    if not api_key:
        st.warning("尚未填写 API Key，无法调用模型。")

capture_api.ensure_started(port=8503)
capture_api.set_token(b_api_token or os.environ.get("B_API_TOKEN", ""))

if "messages" not in st.session_state:
    st.session_state.messages = []
if "images" not in st.session_state:
    st.session_state.images = []
if "type_text" not in st.session_state:
    st.session_state.type_text = ""


def make_client() -> LLMClient:
    return LLMClient(Config(
        base_url=base_url.rstrip("/"),
        model=model,
        max_tokens=int(max_tokens),
        prompt=prompt,
        api_key=api_key,
    ))


def stream_reply(user_text: str) -> None:
    """把用户输入 + 已采集的所有图片发给 LLM，流式显示并写入会话内存。"""
    st.session_state.messages.append({"role": "user", "content": user_text})
    with st.chat_message("user"):
        st.markdown(user_text)
    with st.chat_message("assistant"):
        try:
            reply = st.write_stream(
                make_client().stream_chat(st.session_state.messages, st.session_state.images)
            )
        except Exception as exc:  # noqa: BLE001 - 把错误直接展示给用户
            reply = f"调用失败：{exc}"
            st.error(reply)
    st.session_state.messages.append({"role": "assistant", "content": reply})


def render_history() -> None:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])


def clear_session() -> None:
    st.session_state.messages = []
    st.session_state.images = []
    capture_api.clear_frame()


def do_capture(timeout: float = 20.0) -> bytes | None:
    since = time.time()
    capture_api.request_capture()
    deadline = time.time() + timeout
    while time.time() < deadline:
        data, ts = capture_api.get_latest_frame()
        if data and ts > since:
            return data
        time.sleep(0.3)
    return None


def fill_from_answer() -> None:
    messages = st.session_state.get("messages") or []
    if messages and messages[-1]["role"] == "assistant":
        st.session_state.type_text = messages[-1]["content"]


st.title("📷 拍照问答")

tab_b, tab_kb, tab_cam = st.tabs(["📸 B 截屏分析", "⌨️ 键盘输出", "📷 浏览器拍照"])

with tab_b:
    st.caption("可多次给 B 截屏攒图，再一次性发给 LLM 综合分析；也可针对这组图连续追问。")

    c1, c2, c3 = st.columns([2, 1, 1])
    capture_clicked = c1.button("📸 给 B 截屏（加入待分析）", type="primary", use_container_width=True)
    clear_clicked = c2.button("🧹 清空", use_container_width=True)
    remove_clicked = c3.button("↩️ 移除最后一张", use_container_width=True)

    if clear_clicked:
        clear_session()
        st.rerun()
    if remove_clicked and st.session_state.images:
        st.session_state.images.pop()
        st.session_state.messages = []
        st.rerun()

    if capture_clicked:
        if not api_key:
            st.error("请先在左侧填写 API Key。")
        else:
            with st.spinner("等待 B 截屏上传…（最多 20 秒）"):
                frame = do_capture()
            if frame is None:
                st.warning("未收到 B 的截图。请确认 B 客户端已运行，且 server_url 与 token 正确。")
            else:
                st.session_state.images.append(frame)
                st.session_state.messages = []
                st.rerun()

    images = st.session_state.images
    analyze_clicked = False
    if images:
        st.write(f"已采集 **{len(images)}** 张")
        columns = st.columns(min(len(images), 4))
        for index, img in enumerate(images):
            with columns[index % len(columns)]:
                st.image(img, caption=f"#{index + 1}", width=180)
        analyze_clicked = st.button(
            f"🔎 分析这 {len(images)} 张图", type="primary",
        )

    if not analyze_clicked:
        render_history()

    if analyze_clicked:
        if not api_key:
            st.error("请先在左侧填写 API Key。")
        else:
            st.session_state.messages = []
            stream_reply(prompt)

    if images and st.session_state.messages:
        follow_up = st.chat_input("针对这组图追问…")
        if follow_up:
            if not api_key:
                st.error("请先在左侧填写 API Key。")
            else:
                stream_reply(follow_up)

with tab_kb:
    st.caption("把文本逐字输入到 B 的当前焦点窗口（中文用 SendInput Unicode，不占用剪贴板）。")
    st.text_area("要输入到 B 的文本", key="type_text", height=160)

    humanize = st.checkbox(
        "拟人化输入：字符间隔 50–120ms 随机 + 换行后停顿 300–1000ms + 0.5% 错字纠正",
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

    c1, c2, c3 = st.columns([1, 1, 2])
    delay = c1.number_input("开始前延迟(s)", min_value=0, max_value=60, value=3)
    interval = c2.number_input(
        "字符间隔(ms)", min_value=0, max_value=1000, value=60, step=10,
        disabled=(humanize or paste_mode),
    )
    c3.button("⬆️ 用上方回答填充", on_click=fill_from_answer, use_container_width=True)

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

    if st.button("🚀 发送给 LLM", disabled=image is None):
        if not api_key:
            st.error("请先在左侧填写 API Key。")
        else:
            st.subheader("💬 回答")
            try:
                st.write_stream(make_client().stream_answer_bytes(image.getvalue(), prompt))
            except Exception as exc:  # noqa: BLE001
                st.error(f"调用失败：{exc}")
