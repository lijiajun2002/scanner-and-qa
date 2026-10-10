# SQA Runbook

## 架构
```
Controller（主机 A，Docker）
  streamlit_app.py   ← 前端：设置 / 客户端注册 / 任务监控 / 人工托管
  run_agent.py       ← 自写 agent 循环（OpenAI 兼容 tool-calling + 看图）
  sqa/               ← 工具层：screenshot / type / key / move / click / scroll / wait …
                       backends: software(走 B) | video(采集卡) | composite
        │ HTTP
        ▼
Target（主机 B，Windows）
  SQA-Agent-Server.exe  ← 截图(mss) + 键鼠(SendInput) + 2 个热键
```
- **Controller** 通过 HTTP 操作 **Target**；Target 无需联网其它服务，只需 Controller 能访问它。

---

## 一、Controller（主机 A）：Docker

```bash
docker compose up -d --build
# 打开 http://<主机A>:8501
```
- `network_mode: host`，可直接访问局域网里的主机 B 和 LLM 端点。
- 挂载 `./data`（设置 + 客户端注册表）与 `./runs`（运行记录）。
- LLM 可先在网页侧栏配置，或用环境变量：`OPENAI_BASE_URL / OPENAI_API_KEY`。

## 二、Target（主机 B）：一键安装

**方式 1（双击，推荐）**：把 `b_client/install.cmd` 发给对方，双击运行。它会：
1. 让你输入 token（与前端一致）和端口；
2. 从 GitHub Release 拉取 `SQA-Agent-Server.exe` + `setup_server.ps1`；
3. 自动安装：复制到 `C:\ProgramData\SQA`、写 `config.json`、加防火墙规则、加 Defender 排除、
   注册登录自启任务 `SQAServer` 并启动；最后打印本机 IP。

> 私有仓库：先设环境变量 `SQA_GH_TOKEN`（有 read 权限的 PAT），再双击。
> 需要在仓库打 tag 触发 CI，生成带 exe 的 Release（见 `.github/workflows/build-agent-server.yml`）。

**方式 2（离线）**：把 `SQA-Agent-Server.exe` + `setup_server.ps1` 放同一目录，双击 `setup_server.ps1` 右键“以管理员身份运行”（脚本会自我提权）。

**Device Guard / WDAC**：若组织策略拦截未签名 exe，改源码方式：
装 Python → `pip install -r b_client/requirements.txt` → 双击 `b_client/run_server.bat`（用 venv 的 python 跑 `agent_server.py`）。

## 三、前端使用

**① 客户端注册**（🧩 页）
- 填 名称 / 地址(`http://<B-IP>:8600`) / token，点“注册/更新”。
- token 由你自定义；把它填进主机 B 的 `C:\ProgramData\SQA\config.json` 的 `token` 即匹配。
- 列表里可“测活”（读分辨率）、“编辑”、“删除”。

**② 任务监控**（🤖 页）
- 选主机 B → 看实时画面（自动刷新）+ 运行日志 + 工具调用。
- 控制：**开始任务**（可填任务目标）· **中止** · **恢复**（从上次 handoff/截图续跑）。

**③ 人工托管**（✍️ 页）
- 与“开始任务”是独立分支：agent 不介入，你手动操作（点击下一题/运行代码等你自己做）。
- 退化为：**截屏 QA**（截当前屏问 LLM）+ **键盘输出**（文本/按键发到 B，仅 ASCII）。
- “用回答填充到输入框”可把 QA 答案一键转成待输入文本。

## 四、给一台新主机 B 的完整工作流

1. 主机 B：双击 `install.cmd` → 输入 token/端口 → 记下脚本打印的 IP。
2. Controller：`docker compose up -d --build` → 打开 `http://<A>:8501`。
3. 前端 🧩：注册该 B（名称/IP/token，token 与安装时一致）。
4. 前端侧栏：填 LLM（Base URL / Key / 模型）与系统提示词，保存。
5. 在主机 B 上进入做题系统、登录、进入答题界面。
6. 前端 🤖：选该 B → 填任务目标（可选）→ **开始任务**；需要时“中止/恢复”。
7. 想自己接管时，用 ✍️ 人工托管：截屏 QA + 键盘输出。
8. 运行记录在 Controller 的 `runs/<client_id>/<时间>/`：`agent.log`、`tool_calls.jsonl`、`shots/`、`notes.md`、`tool_issues.md`。

## 五、命令行 / MCP（可选）
- CLI：`python -m sqa.cli screenshot|type "..."|key enter|click x y|scroll dy|size`
- MCP（stdio）：`python -m sqa.mcp_server`，把同一套工具暴露给 MCP 客户端。
- 相关环境变量：`SQA_TARGET_URL`、`SQA_TARGET_TOKEN`、`SQA_SHOT_SOURCE`(software|video)、
  `SQA_VIDEO_SOURCE`、`SQA_VIDEO_SIZE`、`SQA_LLM_BASE_URL/API_KEY/MODEL`、`SQA_GOAL`、`SQA_SYSTEM_PROMPT`。

## 六、Target 热键（只有两个）
- `Ctrl+Shift+Alt+8`：提示 agent 去截屏。
- `Ctrl+Shift+Alt+0`：停止正在进行的打字。

## 七、已知限制
- `type_text` 仅 ASCII（中文会被剔除）。
- 采集卡（video 后端）需要主机 B 的 HDMI 真正输出到采集卡；否则黑屏。
- Target 进程已做 DPI 感知，保证坐标与截图分辨率一致（缩放 125%/150% 下也正确）。
