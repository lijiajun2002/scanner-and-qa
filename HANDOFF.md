# Scanner & QA — 交接文档

> 状态：可用。B 端键盘输出的缩进问题已修复（改为「行内容选中 + 目标缩进替换」的确定性方案，见第 5 节）。

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

## 5. 键盘输出（缩进方案）

### 实现
`b_client/capture_agent.py` 的 `SendInputTyper`：

- 逐字注入：`SendInput + KEYEVENTF_UNICODE`（支持中文/emoji）；整段走 `paste_mode` 粘贴。
- 拟人化：字符间隔随机、换行停顿、低频错字后 Backspace 纠正。
- `space_interval_ms`：空格与缩进走独立高速间隔。
- `unicode_only`：跳过非 BMP/代理字符。
- 预处理：EOL 归一、清理 NBSP/全角空格/零宽/智能引号、行首 Tab 按 tab stop 展开。
- 代码模式 `indent_mode`：
  - `human`（默认，页面选「拟人逐行」）：**逐行对齐** —— 回车后用 `Home`×2 + `Shift+End` 选中该行内容（此时只含编辑器自动缩进空白），再逐字输入「目标缩进 + 正文」替换选区。不读剪贴板、不测量、不删换行，且能保留逐字节奏。
  - `none`：原样逐字，不做缩进对齐（换行可选 `enter_via_paste`）。
- 首行不整行替换，直接在光标处输入，避免毁掉光标所在行已有内容。
- 缩进风格：`indent_style = spaces|tabs`，`tab_size`。
  - `spaces`：逐字输入空格（保留拟人节奏）。
  - `tabs`：把缩进串（Tab 字符）粘贴覆盖选区。**不能用 Tab 键**——VS Code 的 Tab 是智能命令：选中整行时是「缩进整行」，空行时会跳到语言推导缩进，无法精确到目标列（依据 `cursorTypeEditOperations.TabOperation`）。粘贴后需给 VS Code 足够处理时间（`0.12s` + `0.25s`）——真机实测 30ms 会被随后键入的正文抢先，导致缩进丢失。
  - 注：控制字符 `\t`/`\n` 不能走 Unicode 注入（真机实测会被 VS Code 丢弃），换行必须用 `VK_RETURN`。
- 全局热键：组合由 S 的 `/hotkeys` 下发，B 启动时拉取、每 60s 同步并热注册。

### 关键结论（历史 bug 根因）
旧方案用 `Ctrl+L` 选中行再 `Ctrl+C` 读剪贴板「实测」缩进。VS Code 的 `Ctrl+L`（`expandLineSelection`）选中范围是 `(N,1) → (N+1,1)`，**包含行尾换行**；且空行时 `Ctrl+C` 是 no-op，剪贴板会保留旧内容。于是实测恒为 `None/脏值`，而按目标缩进输入时又把换行一起替换掉 → 串行/丢行/无缩进。现已彻底移除对 `Ctrl+L`/`Ctrl+C`/剪贴板测量的依赖。

### 为什么 `Home`×2 + `Shift+End` 是可靠的（VS Code 源码依据）
- `Home` = `MoveOperations.moveToBeginningOfLine`：`firstNonBlank = getLineFirstNonWhitespaceColumn || minColumn`。回车后该行只有自动缩进空白，游标在行尾 ≠ 行首，故第一次 `Home` 到第 1 列，第二次仍在第 1 列。
- `Shift+End` = `moveToEndOfLine`（选择），选到行末但不含换行。
- 纯空白选区不会触发「输入括号/引号包裹选区」（`SurroundSelectionOperation._isSurroundSelectionType` 对 only-whitespace 返回 false），所以替换选区安全。

### 建议的 VS Code 设置
- `editor.insertSpaces` / `editor.tabSize` 与页面「缩进字符 / tab 宽度」保持一致（文件用 Tab 就选 tabs，用空格就选 spaces；混用会导致 Python `TabError`）。
- 无需关闭 `editor.autoIndent`（`human` 模式对自动缩进不敏感）。
- 建议关闭或保持默认的自动闭合括号/引号均可；如出现异常配对/补全，页面保持「IDE 兼容：回车/制表前先按 Esc」勾选。

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
- 当前 `data/settings.json` 实际值（节选）：模型 `deepseek-v4.1-flash`、`max_tokens=1088`、`prompt` 为"用python做这道题…只回答答案"、`type_enter_via_paste=true`、字符/换行间隔 50ms（页面可调）。注意：`human` 模式不使用剪贴板，`enter_via_paste` 只在 `indent_mode=none` 时生效；若把间隔压到 10ms 以下，可能影响 `paste_mode`/`none` 的剪贴板时序。
- 安全：`/pending`、`/frame`、`/action`、`/hotkeys` 都需 token；该通道能向 B 注入任意按键，**勿暴露公网**，仅限局域网。

---

## 8. 测试

- `tests/test_typer.py`：注入器纯函数与各模式的动作序列（打桩，Linux 可跑）：`python tests/test_typer.py`。
- `tests/test_vscode_sim.py`：**VS Code 编辑器语义模拟器**，按源码实现 `Home`/`Shift+End`/`Enter` 自动缩进/`Tab`/括号自动配对，用真实文本跑注入器并逐行比对。含空格/制表、有无自动缩进、追加/中段插入等场景：`python tests/test_vscode_sim.py`。
- 另有开发期 `/tmp/opencode/` 下的临时脚本（未入库）：`test_hotkeys.py`、`test_orchestration.py`、`test_streamlit_*.py`。
- 建议后续把这些也整理进 `tests/` 并接入 CI。注意：模拟器只能证明「在建模的 VS Code 语义下正确」，最终仍需在真实 Windows/VS Code 上验收。

### 真机验收记录（已做）
在 B（Windows，VS Code，交互会话）上用 `SendInput` 对真实 VS Code 输入《接雨水》示例并 `Ctrl+S` 后回读文件：
- `indent_style=spaces`：PASS
- `indent_style=tabs`：PASS（需上面的粘贴 settle 时间）
- 验证方法：`explorer.exe`/计划任务在会话 1 启动一个新 VS Code 窗口 → 输入 → 保存 → 读取文件比对。

---

## 9. 下一步（可选）

键盘缩进问题已按第 5 节方案修复。后续可选：

1. 在真实 VS Code 上验证 `human` 模式（建议先用 `run_source.bat` 免构建验证）；观察日志 `%LOCALAPPDATA%\scanner-qa\agent.log`。
2. 视需要补充「整段粘贴」与 `human` 模式的一键切换体验（页面已有缩进策略 + 整段粘贴选项）。
3. 处理缩进之外仍可能影响 IDE 的因素：自动补全弹窗、括号/引号自动配对。
4. 将 `/tmp/opencode` 的临时测试并入 `tests/` 并接入 CI。

---

## 10. 已知取舍（供决策）

- **逐字模拟 vs 保真**：`human` 模式逐字更像真人、保留行间节奏且缩进确定；`paste_mode` 整段粘贴最保真但失去节奏。
- **服务端单例会话**：多个浏览器窗口共享同一份图片/对话，`清空` 会影响所有窗口。
- **图片不落盘**，但**设置（含 API Key）明文落盘**在 `data/settings.json`（按用户要求保持现状）。
