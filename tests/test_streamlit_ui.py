"""网页（Streamlit）UI 冒烟：渲染无异常 + 侧栏/键盘选项 + 按钮投递动作。

用 streamlit.testing.v1.AppTest，同进程执行。运行：python tests/test_streamlit_ui.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sqa_ui_"))
SETTINGS = _TMP / "settings.json"
SETTINGS.write_text(json.dumps({
    "base_url": "http://127.0.0.1:9/v1", "model": "m", "api_key": "k",
    "prompt": "自由问答提示词", "max_tokens": 64, "b_api_token": "tok",
    "extract_prompt": "抽离提示词", "type_target_minutes": 0,
}, ensure_ascii=False), encoding="utf-8")
os.environ["WEBAPP_SETTINGS"] = str(SETTINGS)

from streamlit.testing.v1 import AppTest  # noqa: E402
from webapp import capture_api  # noqa: E402


def drain_jobs():
    out = []
    try:
        while True:
            out.append(capture_api._jobs.get_nowait())
    except Exception:
        pass
    return out


def drain_actions():
    out = []
    try:
        while True:
            out.append(capture_api._actions.get_nowait())
    except Exception:
        pass
    return out


def test_render_and_widgets():
    at = AppTest.from_file(str(_ROOT / "streamlit_app.py"), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    assert len(at.tabs) == 3
    btns = [b.label for b in at.button]
    for want in ("截屏", "分析", "输入回答", "停止输入", "断点续传", "清空",
                 "保存键盘参数", "保存题型与语言", "保存 LLM 设置", "保存快捷键"):
        assert any(want in b for b in btns), (want, btns)
    cbs = [c.label for c in at.checkbox]
    for want in ("拟人化", "纯 Unicode", "整段粘贴"):
        assert any(want in c for c in cbs), (want, cbs)
    sels = [s.label for s in at.selectbox]
    for want in ("题型", "缩进策略", "第三阶段方式"):
        assert any(want in s for s in sels), (want, sels)
    nums = [n.label for n in at.number_input]
    assert any("目标总时长" in n for n in nums), nums
    print("[ok] 渲染无异常 + 关键控件齐全")


def test_capture_button_enqueues_job():
    drain_jobs()
    at = AppTest.from_file(str(_ROOT / "streamlit_app.py"), default_timeout=30)
    at.run()
    next(b for b in at.button if "截屏" in b.label).click().run()
    jobs = drain_jobs()
    assert any(j.get("type") == "capture" for j in jobs), jobs
    print("[ok] 「截屏」按钮投递 capture 任务")


def test_analyze_without_image():
    at = AppTest.from_file(str(_ROOT / "streamlit_app.py"), default_timeout=30)
    at.run()
    capture_api.clear_all()
    next(b for b in at.button if "分析" in b.label).click().run()
    # 无图片 -> 异步动作会置状态；轮询一下
    import time
    for _ in range(40):
        if "没有可分析的图片" in capture_api.get_state()["status"]:
            break
        time.sleep(0.05)
    assert "没有可分析的图片" in capture_api.get_state()["status"], capture_api.get_state()["status"]
    print("[ok] 「分析」无图片时给出提示")


def test_clear_button():
    at = AppTest.from_file(str(_ROOT / "streamlit_app.py"), default_timeout=30)
    at.run()
    capture_api._last_answer = "x"
    next(b for b in at.button if "清空" in b.label).click().run()
    import time
    for _ in range(40):
        if capture_api.get_state()["last_answer"] == "":
            break
        time.sleep(0.05)
    assert capture_api.get_state()["last_answer"] == ""
    print("[ok] 「清空」按钮生效")


def _run_all():
    drain_actions()
    test_render_and_widgets()
    test_capture_button_enqueues_job()
    test_analyze_without_image()
    test_clear_button()
    print("STREAMLIT UI OK")


if __name__ == "__main__":
    _run_all()
