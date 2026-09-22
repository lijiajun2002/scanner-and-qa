# Scanner & QA — 交接文档

> 状态：可用。B 端键盘输出的三种策略（机械粘贴 / 规则拟人 / LLM 乱序）都已真机验收；
> 支持题型自动分流、上下文保留、鼠标一动即停、断点续传。详见各节。

---

## 1. 项目目标与典型工作流

用摄像头/截屏采集"另一台设备（电脑 B）"的屏幕内容，交给视觉 LLM 分析，再把结果用**模拟真人键盘输入**写回 B。
用于做题/写代码，S 端（Docker 主机）做分析与编排，B 端（Windows）只负责截屏 + 注入按键。

典型工作流：

1. 在 B 上工作（打开 VS Code），浏览器开着 S 的网页（`http://<S-IP>:8502`）。
2. `Ctrl+Shift+Alt+8` 给 B 截屏（同一题可连按多张，追加到本轮）。
3. `Ctrl+Shift+Alt+9` 分析：第一层 LLM 判题型并分流（选择题直接答 / 代码题抽离题干后按语言作答）。
4. `Ctrl+Shift+Alt+0` 把最近回答**完整输出到 B 当前焦点窗口**（一次会话只做一次）。
5. 打字过程中**在 B 上移动鼠标**（>10px）即自动停止；停止后按 `Ctrl+Shift+Alt++` **从断点续传**。
6. `Ctrl+Shift+Alt+-` 清空。

---

## 2. 架构

```
┌─────────────── 机器 B (Windows) ─────────────────────┐        ┌──────────── S (Docker 主机) ──────────────────┐
│ b_client/capture_agent.py  →  ScannerQA-Agent.exe     │        │ 容器 scanner-and-qa                            │
│  · 长轮询 GET /pending 领任务（capture / type）        │        │  · serve.py：先起采集 API(8503)，再起 Streamlit │
│  · 截屏：mss 抓主屏 → POST /frame                      │  HTTP  │  · webapp/capture_api.py：任务/动作队列 + 编排   │
│  · 注入：SendInput 键盘注入到焦点窗口 → POST /result    │ <────> │  · streamlit_app.py：网页 UI（对外 8502）        │
│  · 全局热键(pynput)：从 S /hotkeys 拉取并热注册         │        │  · app/llm.py：OpenAI 兼容 LLM 客户端            │
│  · 控制线程：长轮询 GET /control（网页停止信号）        │        │  · 端口：8502 → UI，8503 → API                   │
└───────────────────────────────────────────────────────┘        └────────────────────────────────────────────────┘
```

- **S 端无记忆**：图片/对话/回答只在内存；唯一落盘的是 `data/settings.json`（设置，含明文 API Key）。
- **采集 API 与 Streamlit 同进程**（`serve.py`），action worker 常驻，所以**不打开网页也能被 B 热键驱动**。
- **打字时 B 主循环是同步的**（一次 type 任务可能跑 10–20 分钟，期间不轮询 `/pending`），所以
  **停止必须走独立控制通道 `GET /control`**；断点续传则靠新一轮 type 任务。

### 2.1 信息流

```
① 截屏
  [热键 +8] 或 [网页「📸 截屏」] ──/action {capture}（或进程内 append_action）──▶ [S _actions]
   [S worker: request_capture] ─▶ [S _jobs {type:capture}]
   [B 长轮询 /pending] ◀── {type:capture} ── [S]
   [B] mss 抓屏 → JPEG ──POST /frame──▶ [S _store_frame：0.6s 冷却 + 哈希去重 → _images]
   说明：收到新截图 = 新一轮会话（`_begin_round_if_needed` 会清上下文/记录，除非本轮还没分析过=连拍追加）。

② 分析（题型自动分流；question_type = auto/code/choice/ask）
  [热键 +9] 或 [网页「🔎 分析」] ──▶ [S _actions] ──worker: _run_analyze──
   auto：第一层 LLM(route_prompt, 带图) → 首行 [[CHOICE]] / [[CODE]]
        ├─ [[CHOICE]] → 直接给选项 → _last_answer（单层，仅网页显示，不输出/不追问）
        └─ [[CODE]]   → 文本即题干 → 第二层 LLM(语言 prompt + 题干, 不带图) → _last_answer
   code（手动）：抽离(extract_prompt, 带图) → 第二层 LLM(语言 prompt + 题干) → _last_answer
   choice（手动）：单层 LLM(choice_prompt, 带图) → _last_answer（仅网页显示、不追问）
   ask（自由问答）：单层 LLM(prompt, 带图) → _last_answer（保留上下文、可追问）
   上下文 `_messages`/`_images` **固定保留**，直到"新截图 / 手动清空"。
   会话记录 `_transcript` 逐条追加（截图/题干/回答/追问/操作），网页对话式回看。

③ 键盘输出
  [热键 +0] 或 [网页「⌨️ 输入回答」] ──▶ [S _actions] ──worker: _run_type_answer──
   读 settings：plan_enabled / plan_method(keys|order) / type_target_minutes
   可选第三阶段：S 生成 keys（键盘流）与 order（行序）两份计划
   可选时长摊平：_human_timing(answer, target) 覆盖 interval/space/line_pause
   → request_type(text, options, plan, keys) ─▶ [S _jobs {type:type,...}]
  [B 长轮询 /pending] ◀── {type:type} ── [S]
  [B] SendInput 注入：keys 校验通过→type_keys；否则 plan→type_plan；再否则 type_text ──POST /result──▶ [S _last_result]

④ 停止 / 断点续传
  停止：[B 鼠标移动]（逐字输入时 >mouse_stop_px）→ typer.stop("mouse")
        [网页「⏹ 停止输入」] ──/action {stop}──▶ request_stop() ─▶ [S _stop_event]
        [B control-loop GET /control] ◀── {"stop":true} ── [S] → typer.stop("web")
        停止时：清掉当前半行，B 记录断点 typer.progress（已写完的行 + 当前行号），POST /result 回报
  续传：[热键 ++] ──/action {resume, done_lines, total_lines}──▶ [S _run_resume]
        S 对"剩余未写的行"重新生成行序计划 → request_type(plan, resume=True)
        [B] type_plan(resume=True)：不重建空行，从断点续写剩余行（键盘流中断会先补足空白行）

⑤ 其它
  [热键 +-] 或 [网页「🧹 清空」] ──▶ clear_all()
  [网页追问] append_action("ask")：仅网页；选择题不进入上下文所以不支持追问
```

**快捷键 vs 网页点击**：截屏/分析/输入回答/清空/断点续传 都走**同一个 `_actions` 队列 + 同一个 worker**
（网页进程内 `append_action()`；快捷键 `POST /action`）。**停止没有快捷键**（鼠标移动 + 网页按钮）。
`⌨️ 输入到 B`（键盘 Tab）是"输入任意文本框内容"的独立通道，不走第三阶段/时长摊平。

### 2.2 会话生命周期（单向）
- `Ctrl+Shift+Alt+8`（截屏）= 新一轮：`_begin_round_if_needed()` 清 S 上下文/记录；B 收到 capture 时 `clear_breakpoint()`。
- `Ctrl+Shift+Alt+9`（分析）→ 产生 `_last_answer`。
- `Ctrl+Shift+Alt+0`（输入回答）= 本轮只做一次的「从头完整输出」，并丢弃断点。
- 输出被鼠标/网页停止 → 产生**断点**（B 端保存，半行已清掉）。
- 之后按 `Ctrl+Shift+Alt++`（断点续传）：S 针对剩余行重新生成计划，B 从断点续写；可反复。
- **断点保存在 B 端**（`typer.progress`），S 不落库；续传时 B 把 `done_lines`/`total_lines` 临时带给 S。

### 2.3 题型 / 语言 / 输出策略（相互正交）
- **题型** `question_type = auto | code | choice | ask`（侧栏「📚 题型与语言」，默认 auto）。
- **语言** `code_language = python | java`，每种一个作答提示词 `language_prompts[lang]`（网页可编辑，代码题第二层用）。
- **提示词归属**：`route_prompt`=自动分流；`language_prompts`=代码题按语言；`choice_prompt`=选择题；
  `extract_prompt`=仅手动"代码题"抽离；`prompt`=自由问答 + 语言提示词缺失兜底。
- **输出策略**（键盘 Tab，与题型/语言正交）：`paste_mode`（机械整段粘贴）/ `indent_mode=human`（规则拟人）/
  `plan_enabled` + `plan_method`（LLM 键盘流 / LLM 行序）。代码题 = 语言(2) × 策略(3)；选择题 = 1。
- **目标总时长** `type_target_minutes > 0` 时服务端按答案长度**覆盖** interval/space/line_pause（网页这三项此时灰掉）；
  填 `0` 才用手动值。
- **会话记录** `_transcript`：`capture`（新截图）与 `清空` 重置；上下文固定保留。

---

## 3. 目录与关键文件

### S 端（容器）
- `serve.py`：入口。先 `capture_api.ensure_started()`，再跑 Streamlit CLI。
- `streamlit_app.py`：网页。Tab：`📸 B 截屏分析`（含会话记录）、`⌨️ 键盘输出`、`📷 浏览器拍照`。
  侧栏分区：LLM / 题型与语言 / B 通道 / 快捷键；键盘 Tab 有独立保存。`live_b_tab()` 用 `_transcript` 对话式回看。
- `webapp/capture_api.py`：核心。
  - 队列：`_jobs`（capture/type）供 B 轮询；`_actions` + worker（capture/analyze/type_answer/resume/stop/ask/clear）。
  - 控制通道：`request_stop()` + `_stop_event`；`GET /control?wait=N`。
  - 会话状态：`_images`、`_messages`、`_status`、`_extracted`、`_last_answer`、`_transcript`、`_round_analyzed`。
  - 题型：`_run_analyze`、`_route_and_extract`、`_run_code`、`_run_choice`、`_run_free_ask`。
  - 提示词：`_generate_plan`（行序）、`_generate_keys`（键盘流）、`_ask_plan_json`/`_ask_keystream`（带重试+原始输出记录）。
  - 时长：`_human_timing`、`_apply_plan_pauses`；续传：`_run_resume`、`_generate_plan_for_lines`。
  - HTTP：`GET /health`、`GET /pending`、`GET /control`、`GET /hotkeys`、`POST /frame`、`POST /result`、`POST /action`。
- `webapp/settings.py`：默认值与读写（`hotkeys`；`question_type`/`code_language`/`language_prompts`/`choice_prompt`/
  `route_prompt`/`extract_prompt`；`plan_enabled`/`plan_method`/`plan_prompt`/`keystream_prompt`；各种 `type_*`）。
- `app/llm.py`：`LLMClient`（`stream_chat(messages, images)`），图片压到最长边 1280。
- `app/` 其余与根 `main.py` 是最早的**桌面版**，当前不用，可忽略。
- `Dockerfile` / `docker-compose.yml` / `requirements-web.txt`。

### B 端（Windows，`b_client/`）
- `capture_agent.py`：主力。截图（mss）+ 键盘注入（SendInput）+ 全局热键（pynput）+ 控制线程。
  - `type_text`（机械/规则拟人）、`type_plan`（行序计划，支持 `resume`）、`type_keys`（键盘流，执行前回放校验）。
  - `_KeyMirror`/`simulate_keys`/`keys_valid`：模拟 VS Code 语义，用于键盘流校验与进度。
  - `_sleep`（可中断）、`_arm_mouse_guard`/`_mouse_moved`（鼠标守卫）、`_clear_current_line`（安全清半行）、
    `_ensure_blank_lines`（键盘流续传补行）、`typer.progress`（断点，B 端）。
  - `start_control_loop()`：长轮询 `GET /control`。
- `config.example.json` → `config.json`（填 `server_url`、`token`）。
- `capture_agent.spec`；构建脚本 `build_exe.*`（全量）/`rebuild.*`（增量）/`run_source.bat`（跑源码）；
  自启 `install_autostart.*`；`requirements.txt`：`mss`/`pillow`/`requests`/`pynput`。

---

## 4. 协议要点

- 认证：请求头 `X-Auth-Token` = S 的 `B_API_TOKEN`（= `settings.json.b_api_token`）。
- `GET /pending?wait=25` → `{"job": null}`；或
  `{"job":{"type":"capture"}}` /
  `{"job":{"type":"type","text","interval_ms","start_delay_s","humanize","unicode_only","dismiss_suggest","paste_mode","options","plan","keys","resume"}}`。
- `POST /frame`：原始 JPEG body → `_store_frame`（0.6s 冷却 + 哈希去重）。
- `POST /result`：`{"kind","ok","chars","skipped","error","progress"}`（`error` 在停止时为 `mouse`/`web`）。
- `POST /action`：`{"action":"capture|analyze|type_answer|resume|stop|ask|clear", ...}`。
- `GET /control?wait=N` → `{"stop": bool}`（B 长轮询消费网页停止信号）。
- `GET /hotkeys` → `{"hotkeys": {...}}`。

---

## 5. B 端键盘输出实现

`SendInputTyper`：

- 逐字注入：`SendInput + KEYEVENTF_UNICODE`（中文/emoji 可用）；`paste_mode` 整段粘贴。
- 拟人化：字符间隔随机、换行停顿、低频错字 + Backspace 纠正；`_sleep` 分块可中断。
- 预处理：EOL 归一、清理 NBSP/全角空格/零宽/智能引号、行首 Tab 按 tab stop 展开。
- 注入限制：控制字符 `\t`/`\n` 走 Unicode 会被 VS Code 丢弃 → 换行必须 `VK_RETURN`；Tab 用 `VK_TAB`。

**三选一输出策略**
1. `paste_mode`（机械）：整段剪贴板粘贴（最保真，丢失节奏）。
2. `indent_mode=human`（规则拟人）：逐行对齐；回车后 `Home`×2 + `Ctrl+[`（outdentLines）×N 把行缩进清 0，
   再逐字输入「目标缩进 + 正文」。**无选区（不变蓝）**，不读剪贴板测量。
   - 注意：行首缩进用**整段粘贴**（spaces/tabs 都一样），瞬间到位、避免"逐个空格挪过去"，也避开 VS Code 智能 Tab。
3. 第三阶段（LLM 计划，`plan_enabled`）：
   - **键盘流 `keys`（默认）**：LLM 给按键序列，合法元素 `普通文本 / <Enter> / <Tab> / <S-Tab> / <Up> / <Down>`。
     B 端 `type_keys` 执行前用 `_KeyMirror` 回放 `simulate_keys(keys)==目标` 校验，**不一致则抛错**。
     缩进由 VS Code 回车自动缩进完成（不再从行首打空格）。`keystream_autoindent`（默认 True）控制假设。
   - **行序 `order`**：LLM 只给 `{"order":1..N 的排列,"pauses","revisit"}`；B 先建 N 行空行，按 order 逐行填。
     正文逐行取自回答、不依赖光标算术 → **乱序也保证正确**。
   - **兜底链**：keys 校验失败 → order 计划 → 再失败 → human 逐字。

**停止与断点续传**
- 鼠标守卫：逐字输入时采样 `GetCursorPos`，位移 >`mouse_stop_px`（默认 10）→ `stop("mouse")`；滚轮不改坐标，翻页不影响。
- 清半行：`Home`×2 → `Shift+End` 选中 → `Delete`（**不用退格 N 次**：自动配对 overtype 会让逻辑字数≠实际字数，多退格会吃掉上一行）。
- 续传：S 用 B 上报的 `total_lines` 算剩余行、order 只留剩余行并去重；**续传计划无效绝不回退成整段重打**。
  键盘流中断后 B 先 `_ensure_blank_lines` 补足空白行，再按行序续写。

**历史坑（已修）**
- 旧「`Ctrl+L` 选中行 + `Ctrl+C` 读剪贴板实测缩进」：`Ctrl+L`（`expandLineSelection`）选中范围含行尾换行、空行 `Ctrl+C` 是 no-op，导致 `None/脏值`，替换后串行/丢行/无缩进 → 已彻底移除。
- 全部键盘流的「停止/续传」都建立在可中断 sleep 上。

**建议的 VS Code 设置**：`editor.insertSpaces`/`editor.tabSize` 与页面一致；键盘流模式保持 `autoIndent` 默认（若设 `none`，把 `keystream_autoindent` 设为 False 并让提示词显式用 `<Tab>`）。

---

## 6. 运行 / 构建 / 部署

### S 端（Docker）
```bash
docker compose up -d --build      # 改了 S 端代码后必须重建
# UI: http://<S-IP>:8502   API: http://<S-IP>:8503/health
```
- token 用环境变量 `B_API_TOKEN` 注入；`data/` 挂载持久化 `settings.json`。
- `data/settings.json` 为容器内 root 所有，宿主机免 sudo 改不了；可在容器内改：
  `docker exec -i scanner-and-qa python - <<'PY' ... PY`。
- 构建偶发 buildkit snapshot 报错，重跑 `docker compose build` 即可。

### B 端（Windows）
```powershell
b_client\build_exe.bat     # 全量
b_client\rebuild.bat       # 增量（日常）
b_client\run_source.bat    # 跑源码最快
```
产物 `b_client\dist\ScannerQA-Agent.exe`（+ 同目录 `config.json`）。**改了 B 端代码必须重建 exe 并替换 B 上的旧版。**

**B 机器当前部署细节（本机实测环境）**
- 路径：`C:\Users\me\Downloads\b_client`；exe 在 `dist\`，`dist\config.json` 的 `server_url=http://192.168.1.137:8503`、`token=test-token`。
- agent 通过**计划任务在交互会话 1** 启动（自启脚本已装）；启动新 exe：
  ```powershell
  $p = New-ScheduledTaskPrincipal -UserId (whoami) -LogonType Interactive -RunLevel Limited
  $a = New-ScheduledTaskAction -Execute "C:\Users\me\Downloads\b_client\dist\ScannerQA-Agent.exe" -WorkingDirectory "...\dist"
  $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName qaStartAgent -Action $a -Principal $p -Settings $s -Force
  Start-ScheduledTask qaStartAgent; Start-Sleep 5; schtasks /delete /tn qaStartAgent /f
  ```
  （`schtasks` 默认"电池不启动"，务必加 `-AllowStartIfOnBatteries`，否则任务不跑。）
- **PyInstaller 构建坑**：`uv` 默认管理的 Python 3.14 在这台机器上损坏（`os error 448 不受信任的装入点`）。
  重建环境要用 3.12：
  ```powershell
  C:\Users\me\.local\bin\uv.exe venv --python 3.12 .build-venv
  C:\Users\me\.local\bin\uv.exe pip install --python .build-venv\Scripts\python.exe -r requirements.txt pyinstaller
  .build-venv\Scripts\python.exe -m PyInstaller --noconfirm --clean capture_agent.spec
  ```
- 已为免密运维把本机运维公钥加进 `C:\ProgramData\ssh\administrators_authorized_keys`（`me` 是管理员，Windows SSH 走这个文件，不是 `~/.ssh/authorized_keys`）。
- 已通过 SYSTEM 计划任务关闭自动锁屏（`InactivityTimeoutSecs=0`、`NoLockScreen=1`）、屏保、动态锁，并把 AC/DC 的关屏/睡眠/休眠设为从不（否则锁屏后无法注入）。

---

## 7. 当前运行环境 / 参数备注

- S：Ubuntu 24.04，Docker；容器 `python:3.12-slim` + Streamlit。
- B：Windows（`me` 为管理员；agent 在交互会话 1 常驻）。
- LLM：`data/settings.json` 里 `model=deepseek-v4.1-flash`，走 OpenAI 兼容端点（`base_url` 指向 B 上的 3000 端口代理）。
  注意该模型会先花 token 做 **reasoning**；`max_tokens` 太小会只出 reasoning、正文为空，建议 ≥8000。
- `type_target_minutes` 默认 10（LeetCode 小题约 10 分钟；大题可填 20）。
- 安全：`/pending`、`/frame`、`/action`、`/hotkeys`、`/control` 都需 token；该通道能向 B 注入任意按键，**勿暴露公网**。

---

## 8. 测试

仓库内 `tests/`（Linux 可跑，纯打桩 / 内嵌 mock，无需外网；可直接 `python tests/xxx.py` 或 pytest）：

- `tests/test_typer.py`：注入器纯函数、human/none 动作序列、plan 校验/子集、**键盘流回放校验/缩进继承**、
  **鼠标停止**、plan 执行导航。
- `tests/test_vscode_sim.py`：**VS Code 语义模拟器**（`Home`/`Ctrl+[`/Enter 自动缩进/Tab/S-Tab/Up/Down/括号配对），
  用真实文本跑注入器并逐行比对：human、plan(order) 乱序、**键盘流**、**停止→安全清半行→断点续传**、
  键盘流校验失败抛错。
- `tests/test_hotkeys.py`：热键解析/映射/重映射、缺修饰键不触发、char 回退、**resume 带断点进度**。
- `tests/test_orchestration.py`：内嵌 mock LLM，覆盖 **/frame 入库去重、题型自动分流（代码/选择）、手动自由问答、
  追问保留上下文、新截图开新会话、输入回答同时带 keys+plan、断点续传、/control 停止通道、plan/keys JSON 解析容错**。
- `tests/test_streamlit_ui.py`：AppTest 渲染无异常 + 关键控件齐全 + 按钮投递动作（截屏/分析/清空）。

> 注意：模拟器只证明"在建模的 VS Code 语义下正确"，真机行为以第 8 节下方的验收记录为准。

### 真机验收记录（已在 B 上做过，均 PASS）
- 规则拟人 `indent_style=spaces` / `tabs`：输出《接雨水》示例，`Ctrl+S` 后回读文件逐字符一致。
- 第三阶段行序 `plan(order)`：乱序计划在真实 VS Code 上输出正确。
- 停止 + 断点续传：逐字中移鼠标 → `原因=mouse`、半行清掉、上一行完好 → `++` 续写 → 文件一致。
- 网页停止控制通道：网页 `stop` → B 日志 `收到网页停止指令`。
- 键盘流 `keys`：`["def f(a):","<Enter>","b = a[0]","<Enter>","return b + 1"]` 真机输出与源码一致。
- 方法：计划任务在会话 1 打开新 VS Code 窗口 → 注入 → 保存 → 回读比对。

---

## 9. 已知取舍 / 下一步

- **逐字模拟 vs 保真**：`human`/`keys` 更像真人、保留节奏；`paste_mode` 最保真但无节奏。
- **服务端单例会话**：多窗口共享同一份图片/对话；`清空` 影响所有窗口。
- **图片不落盘**；**设置（含 API Key）明文落盘**在 `data/settings.json`（按用户要求）。
- 可继续做：给 `tests/` 接入 CI；第三阶段提示词继续在网页里迭代；
  如需更"无声"的缩进观感，可评估 `editor.autoIndent:"none"` + 键盘流显式 `<Tab>`。
