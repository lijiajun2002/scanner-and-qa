from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.prompt import SYSTEM_PROMPT  # noqa: E402
from webapp import agentsvc, store  # noqa: E402
from webapp.tasks import manager  # noqa: E402

st.set_page_config(page_title="SQA 答题控制台", page_icon="🖥️", layout="wide")

settings = store.load_settings()
clients = store.load_clients()
TM = manager()

# =========================== 侧栏：设置 ===========================
with st.sidebar:
    st.header("⚙️ 设置")

    st.subheader("🤖 LLM")
    base_url = st.text_input("Base URL", value=settings["base_url"])
    api_key = st.text_input("API Key", value=settings["api_key"], type="password")
    model = st.text_input("模型", value=settings["model"])
    max_tokens = st.number_input("max_tokens", 256, 32768, int(settings["max_tokens"]), 256)

    st.subheader("🖥️ 截图来源")
    shot_source = st.selectbox(
        "来源", ["software", "video"],
        index=0 if settings["shot_source"] == "software" else 1,
        help="software=从 B 端 agent_server 截图（自动使用 B 的原生分辨率，坐标已归一化，无需设置采集分辨率）；"
             "video=本机采集卡 ffmpeg 抓帧（需与采集卡/HDMI 实际支持的 mode 一致）")
    video_source = st.text_input(
        "采集来源", value=settings["video_source"],
        help="仅 video 来源使用。Linux: v4l2:/dev/video0；Windows: dshow:video=xxx；或 rtsp://…")
    video_size = st.text_input(
        "采集分辨率", value=settings["video_size"],
        help="仅 video 来源使用。必须与采集卡支持、且 HDMI 实际输出的分辨率一致，"
             "例如 1920x1080。2K/4K 屏需先把输出设为采集卡支持的 mode，否则会黑屏或偏色。"
             "software 来源请忽略此项。")

    st.subheader("📝 系统提示词")
    system_prompt = st.text_area("agent 的系统提示词", value=settings.get("system_prompt") or SYSTEM_PROMPT,
                                 height=260)
    default_goal = st.text_area("默认任务（可选）", value=settings.get("default_goal", ""), height=70)

    if st.button("💾 保存设置", use_container_width=True):
        store.save_settings({
            "base_url": base_url, "api_key": api_key, "model": model,
            "max_tokens": int(max_tokens), "shot_source": shot_source,
            "video_source": video_source, "video_size": video_size,
            "system_prompt": system_prompt, "default_goal": default_goal,
        })
        st.success("已保存。")
        settings = store.load_settings()

st.title("🖥️ SQA 答题控制台")

tab_clients, tab_monitor, tab_manual = st.tabs(["🧩 客户端", "🤖 任务监控", "✍️ 人工托管"])

# =========================== 客户端注册 ===========================
with tab_clients:
    st.caption("给每台主机 B 注册一个 token。地址填 B 的 `http://<局域网IP>:8600`（端口即安装时设的 Port）；"
               "token 由你自定义，写进该机器的 config.json 匹配即可。")

    edit_id = st.session_state.get("_edit_client")
    editing = next((c for c in clients if c["id"] == edit_id), None)
    with st.form("client_form"):
        c_name = st.text_input("名称", value=editing["name"] if editing else "")
        c_url = st.text_input(
            "B 端地址", value=editing["url"] if editing else "http://192.168.1.50:8600",
            help="填主机 B 的局域网地址 + agent_server 端口，例如 http://<B的IP>:8600；"
                 "端口即安装 B 端时 setup_server.ps1 里设的 Port（默认 8600）。")
        c_token = st.text_input(
            "token", value=editing["token"] if editing else "admin",
            help="与主机 B 的 config.json 里 token 一致（安装时输入的那个）。")
        submitted = st.form_submit_button("💾 注册 / 更新")
    if submitted:
        if not c_url.strip():
            st.error("地址不能为空")
        else:
            store.upsert_client(c_name.strip(), c_url.strip(), c_token.strip(),
                                client_id=editing["id"] if editing else None)
            st.session_state.pop("_edit_client", None)
            st.success("已保存。")
            st.rerun()

    st.divider()
    st.subheader(f"已注册主机 B（{len(clients)}）")
    if not clients:
        st.info("还没有客户端。")
    for c in clients:
        col = st.columns([3, 3, 3, 1, 1, 1])
        col[0].markdown(f"**{c['name']}**")
        col[1].code(c["url"], language=None)
        col[2].code(c["token"], language=None)
        if col[3].button("测活", key=f"ping_{c['id']}"):
            try:
                from sqa.backends import SoftwareBackend
                w, h = SoftwareBackend(c["url"], c.get("token", "")).size()
                st.success(f"{c['name']} 在线 {w}x{h}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"{c['name']} 不可达: {exc}")
        if col[4].button("编辑", key=f"edit_{c['id']}"):
            st.session_state["_edit_client"] = c["id"]
            st.rerun()
        if col[5].button("删除", key=f"del_{c['id']}"):
            store.delete_client(c["id"])
            st.rerun()


def _client_picker(key: str) -> dict | None:
    if not clients:
        st.info("请先在「🧩 客户端」注册主机 B。")
        return None
    labels = {c["id"]: f"{c['name']}  ({c['url']})" for c in clients}
    cid = st.selectbox("选择主机 B", list(labels), format_func=lambda x: labels[x], key=key)
    return next(c for c in clients if c["id"] == cid)


# =========================== 任务监控 ===========================
with tab_monitor:
    client = _client_picker("mon_client")
    if client:
        task = TM.status(client["id"])
        c1, c2, c3 = st.columns([2, 1, 1])
        status_txt = task.status if task else "idle"
        c1.markdown(f"**状态：** `{status_txt}`" + (f" · {task.error}" if task and task.error else ""))

        with c1:
            goal = st.text_input("任务目标（可选）", value=settings.get("default_goal", ""),
                                 key=f"goal_{client['id']}")
        controls = st.columns(4)
        if controls[0].button("▶️ 开始任务", type="primary", key=f"start_{client['id']}"):
            if settings.get("model") and settings.get("base_url"):
                TM.start(client, settings, goal)
                st.rerun()
            else:
                st.error("请先在侧栏配置 LLM。")
        if controls[1].button("⏹ 中止", key=f"stop_{client['id']}"):
            TM.stop(client["id"]); st.rerun()
        if controls[2].button("⏩ 恢复", key=f"resume_{client['id']}"):
            TM.start(client, settings, goal, resume=True); st.rerun()
        if controls[3].button("🔄 刷新", key=f"ref_{client['id']}"):
            st.rerun()

        colL, colR = st.columns([3, 2])
        with colL:
            st.subheader("实时画面")

            @st.fragment(run_every=2)
            def live():
                try:
                    st.image(agentsvc.screenshot(client, settings), use_container_width=True)
                    st.caption(f"自动刷新中 · {client['name']}")
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"截图失败：{exc}")
            live()
        with colR:
            with st.expander("运行日志", expanded=True):
                st.code(TM.log_tail(client["id"], 30) or "（无）", language=None)
            with st.expander("工具调用", expanded=False):
                st.code(TM.tool_calls_tail(client["id"], 15) or "（无）", language=None)

# =========================== 人工托管 ===========================
with tab_manual:
    st.caption("人工托管：你手动操作（点击下一题/运行代码等由你在 B 端进行），"
               "此页只提供 截屏 QA + 键盘输出。")
    client = _client_picker("manual_client")
    if client:
        colL, colR = st.columns([3, 2])
        with colL:
            st.subheader("实时画面")

            @st.fragment(run_every=3)
            def live_manual():
                try:
                    st.image(agentsvc.screenshot(client, settings), use_container_width=True)
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"截图失败：{exc}")
            live_manual()
        with colR:
            st.subheader("截屏 QA")
            question = st.text_area("问题", value="请分析当前屏幕，并给出这道题/这一步的答案（ASCII 代码优先）",
                                    height=90, key="qa_q")
            if st.button("🔎 截屏并询问", type="primary", key="qa_btn"):
                with st.spinner("询问中…"):
                    try:
                        st.session_state["qa_answer"] = agentsvc.qa(client, settings, question)
                    except Exception as exc:  # noqa: BLE001
                        st.session_state["qa_answer"] = f"失败：{exc}"
            ans = st.session_state.get("qa_answer", "")
            st.text_area("回答", value=ans, height=180)

            st.subheader("键盘输出")
            if st.button("⬆️ 用回答填充到输入框", key="fill_btn"):
                st.session_state["type_box"] = ans
                st.rerun()
            st.text_area("要输入的文本（仅 ASCII）", height=120, key="type_box")
            if st.button("⌨️ 发送到 B", key="type_btn"):
                res = agentsvc.type_text(client, settings, st.session_state.get("type_box", ""))
                st.success(res["content"])
            combo = st.text_input("按键/组合（enter / tab / ctrl+a / f5 …）", key="key_combo")
            if st.button("发送按键", key="key_btn") and combo.strip():
                res = agentsvc.key(client, settings, combo.strip())
                st.success(res["content"])
