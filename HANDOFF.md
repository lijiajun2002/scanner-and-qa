# Scanner & QA — 交接文档

> 状态：可用，但 **B 端往 IDE（VS Code）里的键盘输出仍不稳定（缩进问题）**。下一步就是专门优化这一块。

---

## 1. 项目目标

用摄像头/截屏采集"另一台设备（电脑 B）"的屏幕内容，交给视觉 LLM 分析，再把结果用**模拟真人键盘输入**的方式写回目标机器。

最初设想（鼠标快捷键 → 拍照 → 问 LLM → 浮窗显示）已演化为现在的 B/S 架构。

典型工作流（用于做题/写代码）：

1. 在 B 上工作，同时浏览器开着 S 的网页。
2. 按 `Ctrl+Shift+Alt+8` 给 B 截屏（可连按，多张追加）。
3. 按 `Ctrl+Shift+Alt+9` 分析：先走"题干抽离层"，再由主 LLM 作答；完成后会话清空。
4. 按 `Ctrl+Shift+Alt+0` 把最近回答**逐字输入到 B 当前焦点窗口**（VS Code）。
5. `Ctrl+Shift+Alt+-` 清空；`Ctrl+Shift+Alt++` 停止本次输入。

---

## 2. 架构

```
┌─────────────── 机器 B (Windows，自有) ───────────────┐        ┌──────────── S (Docker 主机) ────────────┐
│ b_client/capture_agent.py (pythonw / ScannerQA-Agent.exe) │        │ Docker 容器 scanner-and-qa               │
│  · 长轮询 S 的 /pending 领取任务                        │        │  · serve.py: 先起采集 API(8503) 再起 Streamlit(8501) │
│  · capture 任务：mss 静默抓主屏 → POST /frame            │  HTTP  │  · webapp/capture_api.py: 任务队列 + 动作 worker + 状态 │
│  · type 任务：SendInput 把文本注入焦点窗口 → POST /result│ <────> │  · streamlit_app.py: 网页 UI（8502 对外）               │
│  · 全局热键(pynput)：从 S 拉取组合并热注册               │        │  · app/llm.py: OpenAI 兼容视觉 LLM 客户端               │
└──────────────────────────────────────────────────────┘        └─────────────────────────────────────────┘
```

- **S 端无记忆**：图片/对话/回答只在内存；唯一落盘的是 `data/settings.json`（设置，含明文 API Key）。
- **采集 API 与 Streamlit 同进程**（`serve.py`），worker 线程随容器常驻，所以**不打开网页也能被 B 热键驱动**。
- 端口：`8502 → Streamlit UI`、`8503 → 采集/任务 API`。

---

## 3. 目录与关键文件

### S 端（容器）
- `serve.py`：入口。先 `capture_api.ensure_started()` 再跑 Streamlit CLI。
- `streamlit_app.py`：网页。三个 Tab：`📸 B 截屏分析`、`⌨️ 键盘输出`、`📷 浏览器拍照`。侧栏分区（LLM / 题干抽离 / B 通道 / 快捷键与行为）各自有保存按钮；键盘 Tab 有独立保存。
- `webapp/capture_api.py`：核心。
  - 任务队列 `_jobs`（capture / type）供 B 长轮询 `/pending`。
  - 动作队列 `_actions` + worker 线程：`capture / analyze / type_answer / ask / clear`。
  - 会话语义状态：`_images`、`_messages`、`_status`、`_extracted`、`_last_answer`。
  - `strip_code_fence()`：整段回答被 ``` 包裹时去掉首尾围栏。
  - HTTP：`GET /health`、`GET /pending?wait=N`、`GET /hotkeys`、`POST /frame`、`POST /result`、`POST /action`。
- `webapp/settings.py`：设置默认值与读写（含 `hotkeys`、各种 `type_*` 参数）。
- `app/llm.py`：`LLMClient`（`stream_answer_bytes` / `stream_chat(messages, images)`），图片压缩到最长边 1280。
- `app/` 其余（`capture.py`/`hotkey.py`/`ui.py`/`config.py`）与根 `main.py` 是最早的**桌面版**，当前 B/S 方案不用，可忽略。
- `Dockerfile` / `docker-compose.yml` / `requirements-web.txt`。

### B 端（Windows，`b_client/`）
- `capture_agent.py`：主力。截图（mss）+ 键盘注入（SendInput）+ 全局热键（pynput）。
- `config.example.json` → 复制为 `config.json`（填 `server_url`、`token`）。
- `capture_agent.spec`：PyInstaller 配置（单文件、无控制台）。
- 构建脚本：`build_exe.bat/ps1`（完整清理构建）、`rebuild.bat/ps1`（**增量重建，日常用**）、`run_source.bat`（直接跑源码最快）。
- 自启脚本：`install_autostart.bat/ps1`、`uninstall_autostart.bat`。
- `requirements.txt`：`mss`、`pillow`、`requests`、`pynput`。

---

## 4. 协议要点

- B → S 认证：请求头 `X-Auth-Token`，值 = S 端 `B_API_TOKEN`（也即 `settings.json.b_api_token`）。
- `GET /pending?wait=25` → `{"job": null}` 或 `{"job": {"type":"capture"}}` / `{"job":{"type":"type","text":...,"interval_ms":...,"start_delay_s":...,"humanize":...,"unicode_only":...,"dismiss_suggest":...,"paste_mode":...,"options":{...}}}`。
- `POST /frame`：原始 JPEG body，收到即追加进 `_images`（带 0.6s 冷却 + 内容哈希去重）。
- `POST /result`：`{"kind","ok","chars","skipped","error"}`。
- `POST /action`：B 热键投递 `{"action":"capture|analyze|type_answer|clear"}`；`stop` 只在 B 本地执行。
- `GET /hotkeys`：返回网页配置的热键组合。

---

## 5. 键盘输出（当前重点 & 现存问题）

### 已实现
`b_client/capture_agent.py` 的 `SendInputTyper`：

- 逐字注入：可用 `SendInput + KEYEVENTF_UNICODE`（支持中文/emoji），或整段/剪贴板粘贴。
- 拟人化：字符间隔随机、换行停顿、低频错字后 Backspace 纠正。
- `space_interval_ms`：空格与缩进走独立高速间隔。
- `unicode_only`：跳过非 BMP/代理字符。
- 预处理：EOL 归一、清理 NBSP/全角空格/零宽/智能引号、行首 Tab 按 tab stop 展开。
- 代码模式 `indent_mode`：
  - `vscode`（默认）：回车后**实测**编辑器自动缩进，再按目标缩进替换；
  - `target`：直接强制到目标缩进；
  - `none`：原样逐字。
- 实测手段：`Ctrl+L`（VS Code = Expand Line Selection）选中整行 → `Ctrl+C` → 读剪贴板得到该行空白 → 解析列数；`_force_indent()` 用 `Ctrl+L` 选中后用目标缩进替换。
- 缩进风格：`indent_style = spaces|tabs`，`tab_size`。
- 全局热键：组合由 S 的 `/hotkeys` 下发，B 启动时拉取、每 60s 同步并热注册；解析支持 `ctrl/shift/alt/win` + 字母/数字/`minus`/`plus`/功能键。

### 现存问题（下一步要解决）
- 用户实测：**在 VS Code 里缩进仍不对**。历史上出现过：
  - `target` 模式：换行处"来回跳跃"，最后只剩一个空格；
  - `vscode` 模式：干脆没有任何缩进；
  - 早前还出现过"换行符被换成 tab / 没有换行"（已试过用剪贴板换行，但 10ms 极速间隔下不可靠）。
- 目前的修复方向刚改为"`Ctrl+L` 选中行 + 实测/替换缩进"，**尚未在真实 VS Code 上验证**。
- 需重点确认的假设：
  1. VS Code 的 `Ctrl+L` 默认是 `expandLineSelection`，且只选中该行文本（不含换行）；
  2. 回车后光标位于自动缩进之后、该行为纯空白；
  3. `Ctrl+C` 复制到剪贴板的时序足够（当前 6×40ms 重试）；
  4. 目标文件被识别为 Python（否则冒号/括号的自动缩进模型不成立）。
- 调试线索：B 的日志 `%LOCALAPPDATA%\scanner-qa\agent.log` 会打印 `测得自动缩进 N 列 (repr=...)`——这是定位的关键。

### 更稳的兜底思路（供下一步参考）
- 用 `editor.autoIndent: none` + `editor.formatOnPaste: false` 配合，让实测值恒为 0，再用目标缩进补齐。
- 或彻底走"整段/逐行剪贴板粘贴"（缩进 100% 保真，但失去逐字节奏）。
- 或对每行：`Ctrl+L` 选中 → 直接输入「目标缩进 + 正文」替换整行（行级粘贴，保留行间节奏）。

---

## 6. 运行 / 构建

### S 端（Docker）
```bash
docker compose up -d --build
# UI:  http://<S-IP>:8502
# API: http://<S-IP>:8503
```
`docker-compose.yml` 用 `B_API_TOKEN` 环境变量注入 token；`data/` 挂载持久化设置。
注意：构建偶尔遇到 buildkit snapshot 报错，重跑 `docker compose build` 即可。

### B 端（Windows）
```powershell
# 首次
b_client\build_exe.bat            # 或 build_exe.ps1
# 日常增量（改了 capture_agent.py 后）
b_client\rebuild.bat              # 复用 .build-venv，不加 --clean，几秒
# 最快（构建机就是运行机时有 Python）
b_client\run_source.bat
```
产物：`b_client\dist\ScannerQA-Agent.exe` + 同目录 `config.json`。
**改了 B 端代码必须重建 exe 并替换 B 上的旧版**（S 端改动则重建 Docker）。

---

## 7. 当前运行环境/参数备注

- S 主机：Ubuntu 24.04，X11（`DISPLAY=:1`），Docker。
- 容器：`python:3.12-slim` + Streamlit 1.64（用到 `st.button(shortcut=...)`、`st.fragment(run_every=...)`、`st.iframe`）。
- B：Windows。
- 当前 `data/settings.json` 实际值（节选）：模型 `deepseek-v4.1-flash`、`max_tokens=1088`、`b_api_token=admin`、`prompt` 为"用python做这道题…只回答答案"、`type_enter_via_paste=true`、字符/换行间隔被压到 10ms（**这是压测值，会破坏剪贴板时序，应调回**）。
- 安全：`/pending`、`/frame`、`/action`、`/hotkeys` 都需 token；该通道能向 B 注入任意按键，**勿暴露公网**，仅限局域网。

---

## 8. 测试

仓库内无正式测试套件；开发期用 `/tmp/opencode/` 下的临时脚本验证（未入库），包括：
- `test_typer.py`：注入器纯函数与各种模式（打桩，Linux 可跑）。
- `test_hotkeys.py`：热键解析/映射/重映射。
- `test_orchestration.py`：服务端动作/抽离/清空/参数透传（内嵌 mock LLM）。
- `test_streamlit_*.py`：AppTest 验证 UI 与快捷键。

如需长期维护，建议把这些整理进 `tests/` 并接入 CI。

---

## 9. 下一步（明确）

**进一步优化 B 端在 IDE（VS Code）中的键盘输出，尤其是缩进。**

需要做的事：
1. 在真实 VS Code 上验证当前 `Ctrl+L` + 实测/替换方案；读取 `agent.log` 的 `测得自动缩进` 行。
2. 决定并实现更稳的缩进策略（见第 5 节"更稳的兜底思路"的三个方向），可能同时保留"逐字/逐行粘贴"备选。
3. 处理缩进之外仍可能影响 IDE 的因素：自动补全弹窗、括号/引号自动配对、`Tab` 语义、`autoIndent` 设置。
4. 把关键 VS Code 设置写进使用说明（`editor.autoIndent`、`formatOnPaste`、`insertSpaces`、`tabSize`）。
5. 建议补一套可离线运行的注入器测试，覆盖更真实的 VS Code 行为。

---

## 10. 已知取舍（供决策）

- **逐字模拟 vs 保真**：逐字更像真人但易被自动补全/自动缩进干扰；整段粘贴最保真但失去节奏。当前两者都支持，默认偏逐字 + 实测修正。
- **服务端单例会话**：多个浏览器窗口共享同一份图片/对话，`清空` 会影响所有窗口。
- **图片不落盘**，但**设置（含 API Key）明文落盘**在 `data/settings.json`（按用户要求保持现状）。
