from __future__ import annotations

import json
import os
from pathlib import Path

# 第三阶段（键盘序列规划）默认提示词：只决定"写作顺序/停顿/回看"，不复制正文，
# 因此不会改错代码；正文由 B 端按最近回答逐字输入。
DEFAULT_PLAN_PROMPT = """下面是一段代码，每行带编号。请给出一个"像真人写代码"的逐行书写顺序，
然后从断点/开头按这个顺序逐行输入。

只输出一个 JSON，不要任何解释或代码块标记：
{"order":[行号...], "pauses":[{"after":行号,"ms":毫秒}...], "revisit":[行号...]}

- order：1..N 的一个排列，表示先写哪一行、再写哪一行（可以乱序、可以先写后面再回头写前面）。
- pauses / revisit 可留空数组。"""

# 第三阶段（键盘流）：LLM 直接给"按键序列"，由引擎回放校验后执行。
DEFAULT_KEYSTREAM_PROMPT = """你是一个"人类键盘打字顺序规划器"。用户会给你一段纯代码（可能带 ``` 围栏，请忽略围栏本身）。
请输出一串"键盘按键"，在 VS Code 里依次按下后能得到与给定代码完全一致的文本。

只允许这些按键元素（不要用其他键）：
- 普通文本：直接照打的代码字符
- <Enter>：回车（VS Code 会自动缩进）
- <Tab>：Tab（增加一级缩进）
- <S-Tab>：Shift+Tab（减少一级缩进）
- <Up> / <Down>：上下移动光标

要点：VS Code 回车会自动继承上一行缩进；Python 里以 : 结尾的行，回车后还会多一级缩进。
请据此判断是否需要 <Tab>/<S-Tab>。按"真人先写框架和重点、再回头补细节"的顺序组织。

只输出一个 JSON，不要解释：{"keys": [ ... ]}

示例（目标代码）：
def add(a, b):
    c = a + b
    return c

示例输出：
{"keys": ["def add(a, b):", "<Enter>", "<Tab>", "c = a + b", "<Enter>", "<Tab>", "return c"]}
"""

# 代码题各语言的固定作答提示词（网页可编辑）
DEFAULT_LANGUAGE_PROMPTS = {
    "python": "用 Python 做这道题，只回答答案就可以，不要写注释。如果不是编程题，也只回答答案。",
    "java": "用 Java 做这道题，只回答答案就可以，不要写注释。如果不是编程题，也只回答答案。",
}

# 选择题：单层作答，仅在网页显示（不键盘输出、不追问）
DEFAULT_CHOICE_PROMPT = (
    "这是一道选择题。请直接给出正确选项（如 A/B/C/D），必要时用一句话说明理由；只回答答案。"
)

# 自动分流（第一层）：判断题型并分支。首行标记 [[CHOICE]] 或 [[CODE]]。
DEFAULT_ROUTE_PROMPT = """你会看到同一道题目的截图。先判断题型，再按题型处理，只输出结果。

第一行只输出 [[CHOICE]] 或 [[CODE]]（原样、不要多余字符）：
- [[CHOICE]]：这是选择题。从第二行开始，直接给出正确选项（如 A/B/C/D），必要时一句话说明。
- [[CODE]]：这是编程/代码题。从第二行开始，把截图内容整理成规范、完整、可直接作答的题干。

不要输出任何多余的解释或 Markdown 代码块。"""

DEFAULTS: dict = {
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "api_key": "",
    "prompt": "这张照片里有什么要素？",          # 通用兜底提示词（语言提示词缺失时用）
    "max_tokens": 1024,
    "b_api_token": "",
    "extract_prompt": "上述所有图片都是同一个题目的截图，请把它们的内容整理成规范、完整、可直接作答的题干。",
    "question_type": "auto",                    # auto / code / choice
    "code_language": "python",                  # python / java
    "language_prompts": dict(DEFAULT_LANGUAGE_PROMPTS),
    "choice_prompt": DEFAULT_CHOICE_PROMPT,
    "route_prompt": DEFAULT_ROUTE_PROMPT,
    "plan_enabled": False,
    "plan_method": "keys",                      # keys（LLM 键盘流）/ order（LLM 行序）
    "plan_prompt": DEFAULT_PLAN_PROMPT,
    "keystream_prompt": DEFAULT_KEYSTREAM_PROMPT,
    "type_interval_min_ms": 200,
    "type_interval_max_ms": 1000,
    "type_line_pause_min_ms": 1000,
    "type_line_pause_max_ms": 2000,
    "type_typo_rate_pct": 0.5,
    "type_typo_pause_min_ms": 100,
    "type_typo_pause_max_ms": 300,
    "type_enter_via_paste": False,
    "type_space_interval_ms": 15,
    "type_target_minutes": 10,
    "type_indent_mode": "human",
    "type_indent_style": "spaces",
    "type_tab_size": 4,
    "type_clean_invisibles": True,
    "type_dismiss_delay_ms": 80,
    "analyze_extract": True,
    "hotkeys": {
        "capture": "ctrl+shift+alt+8",
        "analyze": "ctrl+shift+alt+9",
        "type_answer": "ctrl+shift+alt+0",
        "clear": "ctrl+shift+alt+minus",
        "resume": "ctrl+shift+alt+plus",
    },
}


def _settings_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get("WEBAPP_SETTINGS", "./webapp_settings.json"))


def load_settings(path: Path | str | None = None) -> dict:
    target = _settings_path(path)
    data = dict(DEFAULTS)
    if target.exists():
        try:
            data.update(json.loads(target.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass

    # 环境变量优先，方便在容器里用 secret 注入而不落盘
    if os.environ.get("OPENAI_API_KEY"):
        data["api_key"] = os.environ["OPENAI_API_KEY"]
    if os.environ.get("OPENAI_BASE_URL"):
        data["base_url"] = os.environ["OPENAI_BASE_URL"]
    if os.environ.get("B_API_TOKEN"):
        data["b_api_token"] = os.environ["B_API_TOKEN"]
    return data


def save_settings(data: dict, path: Path | str | None = None) -> None:
    target = _settings_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
