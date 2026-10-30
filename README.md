# MarkBot 🦞

一个**可自托管（self-hosted）的全功能个人 AI 助手框架**：将 LLM、聊天渠道、工具执行与长期记忆组装成单个进程即可运行的智能体（Agent）。MarkBot 擅长复杂任务规划与软件开发，支持多模型自动容错、多渠道接入、向量记忆、技能扩展、定时任务与全自动任务流水线。

核心设计取向：**单进程部署、零外部基础设施依赖（SQLite 向量库 + 文件存储）、一人可私有运维**——能力上是全功能的，部署上是轻量的。

- **协议**：AGPL-3.0
- **运行时**：Python ≥ 3.11（Web 控制台为 React + Vite，微信桥接需要 Node.js ≥ 18）
- **当前版本**：`20261030`

---

## 目录

- [功能总览](#功能总览)
- [架构一览](#架构一览)
- [部署指南](#部署指南)
- [配置指南](#配置指南)
- [CLI 命令速查](#cli-命令速查)
- [会话内斜杠命令](#会话内斜杠命令)
- [权限模式与安全](#权限模式与安全)
- [开发指南](#开发指南)

---

## 功能总览

### 多模型接入与自动容错

- **28 个内置 Provider**：Anthropic、OpenAI、Azure OpenAI、DeepSeek、Gemini、Moonshot(Kimi)、智谱 GLM、通义 DashScope、MiniMax、Mistral、阶跃星辰、xAI、NVIDIA NIM、Groq、HuggingFace，以及 OpenRouter / AiHubMix / 硅基流动 / 火山引擎(含 Coding Plan) / BytePlus(含 Coding Plan) 等网关服务。
- **OAuth 登录**：原生支持 OpenAI Codex 与 GitHub Copilot（`markbot provider login`）。
- **本地模型**：vLLM、Ollama、OVMS，以及任意 OpenAI 兼容端点（`custom`）。
- **模型链自动降级**：`model_chain` 按优先级排列多个模型，超时 / 限流 / 过载时自动切换下一个；内置熔断器（连续 6 次失败熔断，60 秒冷却）。
- **Prompt Caching**：Anthropic 与 OpenRouter 支持前缀缓存，缓存策略跨 Provider 统一管理。
- **Mixture-of-Agents（MoA）**：`/moa` 命令让多个低成本模型并行独立作答，再由聚合模型综合成最终答案；顾问模型可显式指定，也可从各 Provider 的 `default_aux_model` 自动挑选。
- **辅助模型路由**：`agents.defaults.auxiliary` 可按任务名（如 `web_extract`）固定辅助模型，未配置的任务走自动解析。
- **Provider 插件**：向数据目录的 `providers/` 放入 `.py` 并调用 `register_provider_spec()`，即可新增或覆盖内置 Provider，无需改动源码（默认不加载，需在 `security` 中显式 opt-in）。

### 多渠道消息接入

- 内置 **钉钉、飞书/Lark、QQ 机器人、微信、Email(IMAP+SMTP)** 五类渠道，插件可自动发现注册。
- 渠道健康检查、断线自动重连、异常自动重启；发送重试与死信记录。
- 微信扫码登录（`markbot channels login weixin`）；邮件渠道带 DKIM/SPF 校验与附件限制。
- 语音消息转写（Groq Whisper）。

### 工具系统（40+ 内置工具）

- **文件与搜索**：read / write / edit / list / delete（回收站式安全删除 + 自动备份）、glob、grep。
- **执行**：Shell 命令（超时 / 正则白黑名单 / 危险命令拦截）、沙箱化 Python 代码执行（内存与依赖限制）。
- **Web**：搜索（Brave / Tavily / DuckDuckGo / SearXNG / Jina）、网页抓取与正文抽取，支持 HTTP/SOCKS5 代理。
- **桌面与浏览器自动化**：
  - `computer_use` 跨平台桌面控制——截图 + 元素编号定位(SOM)、鼠标键盘操作；macOS 可用 cua-driver 后台驱动，Linux 支持 AT-SPI 无障碍栈，Windows 使用 pyautogui；
  - `browser` 基于 Playwright 的浏览器控制（导航 / 快照 / 点击 / 输入等 10 个工具），域名黑白名单与会话隔离。
- **规划与协作**：think（分析/规划/反思）、todo 任务跟踪、子代理（spawn/check/list，能力令牌委派）、ask_user_question、message（主动给渠道用户发消息）、explore 深度代码探索、上下文目录检索（catalog/search/load）。
- **MCP**：Model Context Protocol 客户端，支持 stdio / SSE / streamable HTTP，工具自动注册。

### 记忆系统

- **混合语义检索**：向量 + 关键词双路召回，RRF 融合排序。分层 Embedder 自动降级：OpenAI 兼容 API → 本地 sentence-transformers → 零依赖哈希。
- **向量库可插拔**：默认 SQLite（零额外依赖），可切换 ChromaDB；支持第三方记忆 Provider 插件（entry points / 命名约定发现）。
- **四级渐进式上下文压缩**：Context Collapse → Micro-Compact → Auto-Compaction → History Snip，按需逐级升级；超长工具输出自动落盘并保留预览。
- **Dream 服务**：按 cron 定时用 AI 整理记忆——向量去重（余弦合并）、重要性时间衰减、高频记忆自动晋升 `MEMORY.md`。
- **每日日志**：`memory/daily/*.md` 按天归档；API Key、JWT、连接串等敏感信息在摘要前自动脱敏。

### 技能（Skills）系统

- 内置技能：weather、summarize、surprise-me、tmux、github、clawhub（技能市场）、skill-creator、memory、cron。
- 加载前安全扫描、护栏策略、沙箱执行；使用计数与生命周期管理（30 天未用转 stale，90 天归档）。
- CuratorService 后台巡检 + SkillImprover 质量评估与 LLM 改进建议。

### 自动化

- **Cron 定时任务**：cron 表达式调度提醒与周期任务，失败重试 + 死信保留。
- **Heartbeat 心跳**：网关模式下定期巡检工作区触发器（默认每 30 分钟）。
- **Autopilot 自动任务流水线**：Intake → Score → Accept → Execute → Verify → Repair，验证失败自动修复，全程留痕 journal。

### 安全与权限

- 5 种权限模式（default / plan / accept_edits / auto / bypass_permissions），3 种运行档案（coding / assistant / unattended）。
- **特权命令鉴权**：`/mode`、`/profile`、`/restart` 等进程级操作按 `security.adminIds` 校验发送者；`/stop`、`/clear`、`/new` 只在群聊中要求管理员（1:1 会话里操作者只会影响自己）。群聊默认不具备特权，单管理员部署可回退到 `allowFrom` 唯一匹配。
- 文件系统与 Shell 默认限制在工作区内；SSRF 防护（云元数据地址硬黑名单 + 私网段拦截）；工具循环 guardrails 防止死循环烧钱。
- 会话预算控制（USD 上限 + 告警阈值 + 自定义单价）与 token 用量实时统计。

### Web 控制台

自托管单用户聊天与管理界面（React SPA + FastAPI WebSocket），十一个页面：

| 页面 | 功能 |
|------|------|
| Chat | 对话（流式输出、工具调用可视化、Markdown 渲染） |
| Sessions | 历史会话检索、重命名、导出与批量删除 |
| Models | 模型与提供商管理 |
| Channels | 渠道状态与启停 |
| MCP | MCP 服务器管理 |
| Skills | 技能浏览与管理 |
| Cron | 定时任务管理 |
| Config | 配置文件在线编辑（JSON 编辑器） |
| Keys | 环境变量与密钥查看（敏感值单独分组） |
| Logs | 运行日志查看 |
| System | 系统 / 运行状态 |

界面为响应式布局：桌面端为可折叠侧边栏，移动端切换为抽屉式导航；`Ctrl/⌘ + K` 打开命令面板，`Ctrl/⌘ + B` 折叠侧边栏，支持亮/暗主题。

### 可观测性

- Token / 成本实时统计（含缓存命中 token），按模型累计费用。
- 全量 LLM 请求-响应交互日志（增量去重），日志自动脱敏。
- `markbot doctor` 环境体检与常见问题自动修复。

---

## 架构一览

```
渠道 (钉钉/飞书/QQ/微信/Email)
        │  publish_inbound
        ▼
   Message Bus ──────────► Agent Loop
                            │  Pipeline 中间件 (QuestionResponse / MemoryLifecycle)
                            │  Context Builder → 记忆系统 → 四级压缩
                            │  Tool Registry (40+ 工具) / 子代理 / MCP
                            │  Guardrails + 权限检查 + 预算控制
                            ▼
                      Fallback Manager（模型链熔断降级）
                            ▼
                    Providers (28 家 / OAuth / 本地模型)
        ┌───────────────────┘
        ▼
   Channel Manager（健康检查 / 自动重启）──► 渠道回发消息

后台服务：CronService · Heartbeat · Dream · Autopilot · Curator/SkillImprover
```

---

## 部署指南

### 环境要求

| 组件 | 要求 | 说明 |
|------|------|------|
| Python | ≥ 3.11 | 必需 |
| Node.js | ≥ 18 | 仅微信渠道扫码登录的桥接服务需要 |
| Chromium | — | 浏览器自动化需要（`playwright install chromium`） |
| 系统包（Linux 桌面控制） | python3-tk、python3-xlib、scrot、at-spi2-core、python3-pyatspi、wmctrl/xdotool | 仅 `computer_use` 需要 |

### 安装

```bash
git clone https://github.com/mickletang/markbot.git
cd markbot
pip install -e .
```

可选扩展（按需安装）：

```bash
pip install -e ".[web]"               # Web 控制台后端 (FastAPI + uvicorn)
pip install -e ".[chroma]"            # ChromaDB 向量记忆后端
pip install -e ".[local-embeddings]"  # 本地 sentence-transformers 向量化（离线记忆）
pip install -e ".[desktop]"           # 桌面控制 computer_use (pyautogui/Pillow)
pip install -e ".[desktop-linux]"     # 同上，Linux 别名（另需 apt 装 python3-pyatspi）
pip install -e ".[desktop-macos]"     # 同上，macOS 别名
pip install -e ".[desktop-windows]"   # 同上，Windows 别名
pip install -e ".[weixin]"            # 微信二维码登录 (qrcode/pycryptodome)
pip install -e ".[langsmith]"         # LangSmith 追踪
pip install -e ".[dev]"               # 开发依赖 (pytest/ruff)

# 浏览器自动化（Playwright 独立安装）
pip install playwright && playwright install chromium
```

> macOS 桌面控制如需后台静默操作（不抢焦点），另行安装 cua-driver：
> `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"`，
> 并在「系统设置 → 隐私与安全性」中授予辅助功能与屏幕录制权限。

### 初始化（onboard）

```bash
markbot onboard              # 已有配置则询问覆盖/保留，否则写入默认配置
markbot onboard --guided     # 分步引导式初始化（首次使用推荐）
markbot onboard --wizard     # 菜单驱动的交互向导
markbot onboard --defaults   # 全默认值非交互初始化（脚本/Docker 场景）

markbot onboard --workspace /srv/markbot --config /etc/markbot/config.json
```

onboard 会创建配置文件与工作区目录，并把内置模板（SOUL.md、MEMORY.md 等）同步到工作区。

### 三种运行模式

三种模式读取同一份配置文件，共享同一工作区（技能、记忆通用），可随时切换或并用。

#### 1. 交互式 CLI（REPL）—— 本地开发 / 临时会话

```bash
markbot agent                          # 默认工作区 ~/.markbot/workspace
markbot agent --workspace /path/to/ws  # 指定工作区
markbot agent --config /path/config.json
markbot agent -m "你好"                # 单次执行（无 TTY 也可用）
```

Rich REPL 流式渲染回复；`Ctrl+C` 或 `/stop` 中断正在进行的迭代。

#### 2. Gateway 网关 —— 生产常驻服务（挂载聊天渠道）

```bash
markbot gateway start                 # 守护进程方式启动（默认）
markbot gateway start --foreground    # 前台运行（配合 systemd）
markbot gateway start --port 18790 --workspace /srv/markbot
markbot gateway status                # 健康状态 + 运行时长
markbot gateway restart
markbot gateway stop                  # (--force 强杀)
```

- PID 文件位于 `~/.markbot/gateway/`；守护日志 `~/.markbot/gateway/gateway.log`，Agent 事件日志 `~/.markbot/logs/agent.log`。
- 启动时预检端口占用。相关配置键：`gateway.host`（默认 `127.0.0.1`）、`gateway.port`（默认 `18790`）、`gateway.heartbeat.*`、`channels.<name>.enabled`。
- 网关同时承载 Cron、Heartbeat、Dream、Autopilot 等后台服务。

#### 3. Web 控制台 —— 浏览器访问

```bash
pip install -e ".[web]"
markbot web                           # 默认 http://127.0.0.1:9120
markbot web --host 0.0.0.0 --port 8080
markbot web --config /etc/markbot/config.json --workspace /srv/markbot
```

- 认证：启动时生成会话 token，CLI 打印带 `?token=…` 的完整 URL；后续请求也可携带 `x-markbot-session-token` 请求头。token 在进程生命周期内有效，重启轮换。
- Web 复用网关的工具栈。**Gateway + Web 可并存**：先启动 gateway，再在另一端口运行 `markbot web`。

### 生产部署（systemd / Docker）

```ini
# /etc/systemd/system/markbot.service
[Unit]
Description=MarkBot Gateway
After=network-online.target

[Service]
ExecStart=/usr/local/bin/markbot gateway start --foreground --config /etc/markbot/config.json --workspace /var/lib/markbot
Restart=on-failure
User=markbot
WorkingDirectory=/var/lib/markbot

[Install]
WantedBy=multi-user.target
```

```dockerfile
# 最小 Dockerfile
FROM python:3.12-slim
RUN pip install markbot[web,chroma]
COPY config.json /etc/markbot/config.json
EXPOSE 9120
CMD ["markbot", "web", "--host", "0.0.0.0", "--port", "9120", "--config", "/etc/markbot/config.json"]
```

---

## 配置指南

### 配置文件位置与规则

- 默认路径：`~/.markbot/config.json`，可用 `--config` 指定其他文件，`--workspace` 覆盖工作区。
- 键名同时接受 **camelCase 与 snake_case**（如 `apiKey` 与 `api_key` 等价）。
- API Key 直接以字面量写入配置文件，**不做 `${VAR}` 环境变量插值**。请勿将 config.json 提交进版本库。
- 运行时数据（logs、gateway PID、代码执行临时目录等）存放在配置文件同级目录下。

### 完整配置示例

```json
{
  "agents": {
    "defaults": {
      "model_chain": [
        "anthropic/claude-sonnet-4-5",
        "deepseek/deepseek-chat"
      ],
      "max_tokens": 8192,
      "context_window_tokens": 65536,
      "temperature": 0.1,
      "timezone": "Asia/Shanghai",
      "profile": "coding",
      "defaultPermissionMode": "auto",
      "workspace": "~/.markbot/workspace",
      "auxiliaryVision": {
        "forceTextOnly": false,
        "provider": "openai",
        "model": "gpt-4o-mini"
      },
      "moa": {
        "advisors": ["deepseek/deepseek-chat"],
        "numAdvisors": 3,
        "aggregator": ""
      }
    }
  },
  "providers": {
    "anthropic": {
      "apiKey": "sk-ant-xxx",
      "models": [
        {
          "id": "claude-sonnet-4-5",
          "name": "claude-sonnet-4-5",
          "max_tokens": 8192,
          "context_window": 200000,
          "capabilities": ["text", "image"]
        }
      ]
    },
    "deepseek": {
      "apiKey": "sk-xxx",
      "models": [
        { "id": "deepseek-chat", "name": "deepseek-chat", "capabilities": [] }
      ]
    }
  },
  "channels": {
    "dingtalk": { "enabled": true, "client_id": "...", "client_secret": "..." }
  },
  "gateway": {
    "host": "127.0.0.1",
    "port": 18790,
    "heartbeat": { "enabled": true, "interval_s": 1800, "keep_recent_messages": 8 }
  },
  "security": {
    "adminIds": ["feishu:ou_xxxxxxxx"],
    "allowPrivateChatFallback": true
  },
  "tools": {
    "restrictToWorkspace": true,
    "web": {
      "search": { "provider": "brave", "apiKey": "", "max_results": 5 }
    },
    "exec": {
      "enable": true,
      "timeout": 60,
      "restrictToWorkspace": true
    },
    "filesystem": {
      "backup_dir": "~/.markbot/.markbot_backups",
      "safeDelete": true
    },
    "code_execution": { "enable": true, "timeout": 60, "maxMemoryMb": 256 },
    "memory": {
      "longTermEnabled": true,
      "vectorBackend": "sqlite",
      "embeddingBackend": "openai",
      "dreamCron": "0 23 * * *"
    },
    "computer_use": { "enable": false },
    "browser": { "enable": true, "headless": true },
    "mcp_servers": {}
  },
  "compaction": {
    "threshold_ratio": 0.85,
    "collapse_tool_result_chars": 4000
  },
  "budget": {
    "enabled": true,
    "maxBudgetUsd": null,
    "warnThresholdUsd": 0.5
  },
  "display": { "language": "zh" }
}
```

### 各模块配置详解

#### agents.defaults —— Agent 默认行为

| 键 | 默认值 | 说明 |
|----|--------|------|
| `workspace` | `~/.markbot/workspace` | Agent 工作区目录 |
| `model_chain` | `[]` | 模型链，格式 `"provider/model"`，按优先级自动降级 |
| `max_tokens` | `8192` | 单次输出上限 |
| `context_window_tokens` | `65536` | 上下文窗口（影响压缩触发时机） |
| `temperature` | `0.1` | 温度 |
| `max_tool_iterations` | `40` | 单回合最大工具迭代次数 |
| `reasoning_effort` | null | `low / medium / high`，开启思考模式 |
| `timezone` | `UTC` | IANA 时区（影响 cron 与日志时间） |
| `profile` | `coding` | 运行档案：`coding`(开发) / `assistant`(助理，需确认) / `unattended`(无人值守) |
| `defaultPermissionMode` | 继承 profile | 启动时的权限模式，见[权限模式与安全](#权限模式与安全) |
| `auxiliaryVision.forceTextOnly` | `false` | 强制所有多模态结果降级为纯文本 |
| `auxiliaryVision.provider/model` | 空 | 辅助识图模型：主模型不支持图片时由它描述截图 |
| `auxiliary` | `{}` | 按任务名覆盖辅助模型，如 `{"web_extract": {"provider": "deepseek", "model": "deepseek-chat"}}`；未配置的任务走自动解析 |
| `moa.advisors` | `[]` | MoA 顾问模型列表（`"provider/model"`）；留空则从各 Provider 的 `default_aux_model` 自动挑选 |
| `moa.numAdvisors` | `3` | 自动挑选顾问时的数量上限（1–6） |
| `moa.aggregator` | 空 | 聚合模型；留空则用主模型 `model_chain[0]` |

#### providers.<name> —— 模型提供商

每个 provider 支持：`apiKey`、`apiBase`（自定义端点）、`extraHeaders`、`models[]`。

模型条目字段：

| 字段 | 说明 |
|------|------|
| `id` | 模型引用 ID（`model_chain` 中使用） |
| `name` | 传给 API 的实际模型名 |
| `maxTokens` / `contextWindow` | 输出上限 / 上下文窗口 |
| `temperature` / `reasoningEffort` | 覆盖全局默认 |
| `capabilities` | 能力声明，可选 `text` `image` `image_edit` `image_generate` `video` `audio` `music` `embedding` `tool_use`；驱动能力感知路由（主模型无 `image` 能力时截图自动转辅助识图描述） |

内置 provider 标识（可直接作为键名）：`anthropic`、`openai`、`openai_codex`(OAuth)、`github_copilot`(OAuth)、`azure_openai`、`openrouter`、`deepseek`、`groq`、`zhipu`、`dashscope`、`gemini`、`moonshot`、`minimax`、`mistral`、`stepfun`、`xai`、`nvidia`、`huggingface`、`aihubmix`、`siliconflow`、`volcengine`、`volcengine_coding_plan`、`byteplus`、`byteplus_coding_plan`、`vllm`、`ollama`、`ovms`、`custom`（任意 OpenAI 兼容端点）。部分 provider 支持别名查找（如 `anthropic`=`claude`、`gemini`=`google`、`dashscope`=`alibaba`/`qwen-dashscope`）。

**Provider 插件**：在数据目录的 `providers/` 下（默认 `~/.markbot/providers/`）放置 `.py`，模块导入时调用 `register_provider_spec()` 即可注册自定义 Provider：

```python
# ~/.markbot/providers/mycloud.py
from markbot.providers.registry import ProviderSpec, register_provider_spec

register_provider_spec(ProviderSpec(
    name="mycloud",
    keywords=("mycloud",),
    env_key="MYCLOUD_API_KEY",
    display_name="MyCloud",
    default_api_base="https://api.mycloud.example/v1",
))
```

同名插件会覆盖内置 Provider（后写入者生效），新名字则追加在内置列表之后；发现是惰性的，插件加载失败不会影响启动。

**插件默认不加载，需要显式开启。** 该目录在 agent 自己的文件工具可写范围内，无条件导入其中的 `.py` 等于把「写一个文件」变成「在本进程内执行任意代码」，绕过权限确认与沙箱。两种开启方式（任选其一）：

```json
{
  "security": {
    "allow_user_provider_plugins": false,
    "user_provider_plugin_allowlist": ["mycloud.py"]
  }
}
```

- `user_provider_plugin_allowlist`（推荐）：只导入列出的文件（文件名或去掉 `.py` 的 stem 均可）。
- `allow_user_provider_plugins: true`：导入目录下全部非 `_` 开头的 `.py`。

被跳过的文件会在日志里逐个列出并提示如何开启，不会静默失效。此外，世界可写（`o+w`）的插件文件即使已开启也会被拒绝加载——任何人都能替换的文件无法证明「被批准的是这份代码」。

#### channels —— 渠道

全局键：`send_progress`(true，向渠道推送进度)、`send_tool_hints`(false)、`sendMaxRetries`(3)。各渠道以 `enabled: true` 开启：

| 渠道 | 关键配置 |
|------|----------|
| `dingtalk` | `client_id`、`client_secret`（钉钉开放平台应用凭证） |
| `feishu` | `app_id`、`app_secret`，可选 `verification_token` / `encrypt_key`、`domain`(`feishu`/`lark`)、群聊策略 `group_policy`、`done_emoji` 等 |
| `qq` | `app_id`、`secret`（QQ 开放平台机器人）、`msg_format` |
| `weixin` | 无需手工配置，运行 `markbot channels login weixin` 扫码登录后 token 自动保存 |
| `email` | 收信 `imap_host/port/username/password/mailbox/use_ssl`；发信 `smtp_host/port/username/password/use_tls`；`from_address`；**`consent_granted: true` 必填**（自动回信授权）；`verify_dkim` / `verify_spf`(默认 true)；`poll_interval_seconds` 等 |

#### tools.* —— 工具开关与安全

| 子键 | 要点 |
|------|------|
| `restrictToWorkspace` | 总开关（默认 `true`）：所有工具限制在工作区内访问 |
| `web.proxy` | HTTP/SOCKS5 代理，如 `http://127.0.0.1:7890` |
| `web.search` | `provider`: `brave/tavily/duckduckgo/searxng/jina`；`apiKey`；SearXNG 另填 `base_url`；`maxResults`(5) |
| `exec` | Shell：`enable`、`timeout`(60s)、`restrictToWorkspace`、`allowNestedShell`、`requireAllowlist` + `allowPatterns`/`denyPatterns` 正则白黑名单、`allowedInternalIps`（放行 SSRF 检查的内网 IP）。详见 [tools.exec —— Shell 安全防护粒度](#tools-exec--shell-安全防护粒度) |
| `filesystem` | `backupDir` 备份目录、`maxBackups`(50)、`safeDelete`(true=移入回收站而非直接删除) |
| `codeExecution` | 沙箱 Python：`enable`、`timeout`(5–300s)、`maxMemoryMb`(64–4096)、`allowedDependencies`(pip 包白名单) |
| `memory` | 见下方[记忆配置](#记忆配置) |
| `computerUse` | `enable`、`backend`(`cua/atspi/pyautogui/noop`)、`captureAfterActions`、`maxElements`、`blockedKeyCombos`、`blockedTypePatterns` |
| `browser` | `enable`、`backend`(`playwright/browserbase`)、`headless`、`recordSession`(.webm 录制)、`defaultTimeout`、`snapshotMaxChars`、`blockedDomains` / `allowedDomains`(glob) |
| `skills` | `minScore` 质量门槛（0 关闭）、`hideStale`(true 隐藏过期技能) |
| `mcpServers` | 见下方 [MCP 配置](#mcp-配置) |
| `guardrails` | 工具循环护栏阈值（重复失败告警/熔断、无进展检测、停滞上限等） |
| `delegation` | 子代理控制：`maxSpawnDepth`(1)、`maxConcurrentChildren`(3)、`maxChildrenPerSession`(8)、`blockedTools` 黑名单、`allowNestedSpawn`(false)、`forceAutoPermission`(true) |
| `outcomeGate` | 完成前验证门：`enabled`、`maxNudges`(2)、`surfaces`(默认 `cli`/`web`)、`requireVerificationForMutations`、`requireVerificationForSideEffects` |
| `cachePolicy` | 前缀缓存策略：`deferMutations`(true，把工具面/系统提示变更推迟到下一回合，保持字节稳定的缓存前缀) |
| `runtimeBudget` | 单回合硬顶：`maxInactivitySeconds`(引擎默认 1800s，0 关闭)、`maxTotalTokens`(引擎默认 3M) |

#### tools.exec —— Shell 安全防护粒度

`exec` 的防护按顺序分四层，任一层命中即拦截：

1. **内置危险命令黑名单**（`rm -rf`、`mkfs`、`shutdown`、`curl … | bash` 等）——始终生效，只能用 `denyPatterns` 追加，无法移除；
2. **嵌套 shell / 命令替换**（`$(...)`、反引号、`bash -c`、`python -c` 等）——由 `allowNestedShell` 控制；
3. **白名单**——`requireAllowlist` + `allowPatterns`。`&&`、`;`、`|` 会把命令拆成多段，**每一段都要单独命中白名单**（`ls && bash x.sh` 会被 `bash` 拦下）；`cd` 只接受一个字面路径参数，`cd`、`cd $VAR`、`cd ~`、`cd -` 一律拒绝；
4. **路径限制**——`restrictToWorkspace`，拦截工作区外的路径。

配置键（config.json 用驼峰，snake_case 亦可解析）：

| 键 | 含义 | 默认 |
|----|------|------|
| `enable` | 是否注册 `exec` 工具 | `true` |
| `timeout` | 单条命令超时（秒） | `60` |
| `restrictToWorkspace` | 拦截路径在工作区外的命令；**显式写 `false` 可关闭**，不写则跟随 profile 默认（`true`） | `true` |
| `allowNestedShell` | 放行 `$(...)`、反引号、`bash -c`、`python -c` 等嵌套解释器调用；**不控制 `&&` / `;` / `|` 这类普通拼接**（那由白名单逐段判定）。旧键名 `allowShellChaining` 仍可识别 | `false` |
| `requireAllowlist` | 仅允许 `allowPatterns` 命中的命令 | `false` |
| `allowPatterns` | 允许名单（正则） | `[]` |
| `denyPatterns` | 在内置黑名单之上追加（只能加，不能减） | `[]` |
| `pathAppend` | 追加到子进程 `PATH` 的目录 | `""` |
| `allowedInternalIps` | 放行 SSRF 检查的内网 IP / CIDR（**仅 IPv4**） | `[]` |
| `envAllowlist` | 非空时仅这些环境变量传入子进程（严格白名单模式） | `[]` |
| `extraEnvAllow` | 即使形似密钥也放行传递的环境变量名 | `[]` |

**最宽松**——面向受信 / 已隔离环境，几乎不做拦截：

```json
{
  "tools": {
    "restrictToWorkspace": false,
    "exec": {
      "enable": true,
      "timeout": 600,
      "restrictToWorkspace": false,
      "allowNestedShell": true,
      "requireAllowlist": false,
      "allowedInternalIps": ["0.0.0.0/0"]
    }
  }
}
```

> `allowedInternalIps: ["0.0.0.0/0"]` 等价于关闭所有 IPv4 的 SSRF 拦截，且不覆盖 IPv6 内网地址；如需放行请优先写具体地址，如 `["127.0.0.1", "10.10.1.90"]`。

**最收紧**——仅允许少数只读命令，黑名单 + 白名单 + 环境白名单三重加固：

```json
{
  "tools": {
    "restrictToWorkspace": true,
    "exec": {
      "enable": true,
      "timeout": 30,
      "restrictToWorkspace": true,
      "allowNestedShell": false,
      "requireAllowlist": true,
      "allowPatterns": ["^ls\\b", "^cat\\b", "^pwd\\b", "^git status\\b"],
      "denyPatterns": ["\\bcurl\\b", "\\bwget\\b", "\\bssh\\b"],
      "allowedInternalIps": [],
      "envAllowlist": ["PATH", "HOME", "LANG"]
    }
  }
}
```

> 内置危险命令黑名单与速率限制（30 条 / 60 秒）写死在代码中，`denyPatterns` 只能追加、无法移除；`allowPatterns` 只能在黑名单与注入检测之后再收窄，不能反向放行。

#### 记忆配置

长期向量记忆**默认开启且零配置**（自动选择最优 Embedder + SQLite 向量库）。常用调优项：

```json
{
  "tools": {
    "memory": {
      "longTermEnabled": true,
      "vectorBackend": "sqlite",
      "vectorMaxRecords": 50000,
      "vectorMaxScanRecords": 20000,
      "vectorMinScore": 0.15,
      "vectorMinContentChars": 12,
      "vectorTopKMultiplier": 2,
      "embeddingBackend": "openai",
      "embeddingApiKey": "",
      "embeddingBaseUrl": "",
      "embeddingModelName": "",
      "memorySummaryEnabled": true,
      "contextCompactEnabled": true,
      "memoryCompactThreshold": 0,
      "memoryCompactReserve": 10000,
      "memoryCompactRatio": 0.75,
      "memoryReserveRatio": 0.1,
      "forceMemorySearch": false,
      "forceMaxResults": 1,
      "forceMinScore": 0.3,
      "dreamCron": "0 23 * * *",
      "consolidationEnabled": true,
      "consolidationDedupThreshold": 0.95,
      "consolidationAgeDecayDays": 90,
      "consolidationPromoteAccess": 8,
      "dailyLogRetentionDays": 30,
      "autoSummaryToCurated": false
    }
  }
}
```

Embedding 后端按优先级自动选择：① OpenAI 兼容 API（设置 `embeddingApiKey` 后启用，质量最佳）；② 本地 sentence-transformers（安装 `.[local-embeddings]`，离线可用）；③ 零依赖哈希兜底。

几个不常用但影响较大的旋钮：

- `vectorMaxScanRecords`（20000）：单次 SQLite 向量查询载入内存的向量上限，语料更大请换 ChromaDB。
- `vectorTopKMultiplier`（2）：向量召回的过采样倍数，先多取再与关键词结果做 RRF 融合。
- `forceMemorySearch`（false）：每次 LLM 调用前自动注入记忆检索结果（`forceMaxResults` / `forceMinScore` 控制条数与阈值）。
- `memoryCompactThreshold`（0）：记忆压缩的 token 阈值，0 表示按上下文窗口的 75%（`memoryCompactRatio`）自动计算；`memoryCompactReserve` 为压缩后为新消息预留的 token。
- `consolidationAgeDecayDays`（90）：重要性的年龄衰减半衰期；`consolidationPromoteAccess`（8）为访问次数超过多少后可将高价值记忆晋升到 `MEMORY.md`。

外部记忆 Provider 插件：设置 `tools.memory.provider`（如 `"chroma"`）+ `providerConfig`；支持 entry points（`markbot.memory_providers`）、命名约定（`markbot_memory_*` 包）与手动注册三种发现方式。

#### MCP 配置

```json
{
  "tools": {
    "mcpServers": {
      "filesystem": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
        "env": {},
        "toolTimeout": 30,
        "enabledTools": ["*"],
        "enabled": true
      },
      "remote": {
        "type": "streamableHttp",
        "url": "https://example.com/mcp",
        "headers": {}
      }
    }
  }
}
```

`type` 支持 `stdio` / `sse` / `streamableHttp`（缺省自动探测）；`enabledTools` 控制注册哪些工具（`["*"]` 全部）。

#### security —— 特权命令鉴权

`/mode`、`/profile`、`/restart` 是**进程级**的：一次 `/mode auto` 会切换整个守护进程的工具权限并写回 `config.json`。因此这些命令不按渠道 `allowFrom` 放行，而是走独立的管理员校验。

`/stop`、`/clear`、`/new` 只影响单个会话，所以规则更窄：1:1 会话里发送者就是唯一受影响的人，不需要管理员；群聊里同样的命令会取消或抹掉其他参与者正在等待的工作，按进程级命令同等校验。群聊类型无法判断时按群聊处理（fail closed）。

| 键 | 默认 | 说明 |
|----|------|------|
| `adminIds` | `[]` | 允许执行特权命令的发送者 ID；支持 `feishu:ou_xxx` 这类渠道限定形式，也接受裸 ID。`"*"` 表示所有人（仅限单操作者部署） |
| `allowPrivateChatFallback` | `true` | `adminIds` 为空时，允许在私聊中把「`allowFrom` 恰好只有一个发送者」推断为管理员；设为 `false` 则要求每个渠道都显式配置 `adminIds` |

判定顺序：本地界面（CLI / Web / API）始终有权 → 群聊永不隐式提权 → 显式 `adminIds`（或渠道级 `adminFrom`）优先 → 单用户部署回退 → 其余一律拒绝，并在回复中提示应配置哪个键。

#### 其他模块

| 模块 | 要点 |
|------|------|
| `gateway.heartbeat` | 心跳巡检：`enabled`(true)、`intervalS`(1800)、`keepRecentMessages`(8，巡检时回看的历史消息条数) |
| `compaction` | 四级压缩参数：`thresholdRatio`(0.85 触发阈值)、`collapseToolResultChars`(4000)、`collapseHead/TailChars`(900/500)、`microCompactKeepTurns`(6)、`autoCompactKeepRecent`(5)、`snipKeepMessages`(10)、`toolOutputInlineChars`(16000，超出落盘)、`systemPromptTokenBudget`(16000) 等 |
| `budget` | 成本控制：`enabled`、`maxBudgetUsd`(会话 USD 上限，null 不限)、`warnThresholdUsd`(0.5 告警)、`customPricing`(按模型覆盖单价) |
| `reliability` | 后台任务可靠性：`cronMaxRetries`(2)、`cronRetryDelayS`(5)、`deadLetterKeep`(50)、`notifyOnFailure`(true) |
| `display.language` | 静态界面文案语言：`en` / `zh`（不影响 Agent 回复语言） |
| `ssrf` | 出站请求防护：`blockedHostnames`、`alwaysBlockedIps`(云元数据等)、`blockedNetworks`(私网段列表) |

### 环境变量

环境变量只调优运行行为，不用于配置文件或 API Key 的替换。

| 变量 | 效果 |
|------|------|
| `MARKBOT_COMPUTER_USE_BACKEND` | 强制桌面控制后端：`cua` / `atspi` / `pyautogui` / `noop` |
| `MARKBOT_CUA_DRIVER_CMD` | 指定已安装 cua-driver 二进制路径 |
| `MARKBOT_CUA_DRIVER_VERSION` | 固定 cua-driver 版本（默认 `0.5.0`） |
| `MARKBOT_VISION_FORCE_TEXT_ONLY` | `1` 强制多模态工具结果纯文本化（覆盖 config） |
| `MARKBOT_MAX_CONCURRENT_REQUESTS` | 单循环最大并发 LLM 请求数（默认 3） |
| `MARKBOT_PRODUCTION` | `1` 收紧 Shell 工具安全限制 |
| `MARKBOT_ARTIFACT_DIR` | 覆写工具输出落盘目录 |
| `MARKBOT_IMAGE_TOKEN_ESTIMATE` | 覆写单张图片的 token 预估成本 |
| `MARKBOT_LANGUAGE` | 覆写界面语言（`en` / `zh`），优先级高于 `display.language` |
| `MARKBOT_PROMPT_CACHE_DIR` | 覆写系统提示缓存目录（主要供测试重定向） |

此外，配置项也可通过 `MARKBOT_` 前缀 + 双下划线嵌套的环境变量覆盖（Pydantic Settings，如 `MARKBOT_AGENTS__DEFAULTS__TEMPERATURE=0.5`）。

---

## CLI 命令速查

```bash
# 初始化
markbot onboard [--wizard|--guided|--defaults]

# Agent
markbot agent                     # 交互 REPL（需要 TTY）
markbot agent -m "Hello!"         # 单次执行
markbot agent --workspace /path   # 指定工作区
markbot agent --config file.json  # 指定配置

# Gateway
markbot gateway start|stop|restart|status

# Web UI
markbot web [--host H] [--port P]

# 技能
markbot skills list               # 列出技能
markbot skills info <name>        # 技能详情
markbot skills scan <skill> <script>  # 安全扫描脚本

# Autopilot
markbot autopilot list            # 任务列表
markbot autopilot add "标题"      # 新建任务 (--body --source --labels)
markbot autopilot show <id>       # 任务详情
markbot autopilot tick            # 执行下一个排队任务
markbot autopilot reject <id>     # 拒绝任务 (--reason)
markbot autopilot requeue <id>    # 重新入队
markbot autopilot stats           # 统计
markbot autopilot journal         # 执行日志
markbot autopilot context         # 当前上下文

# 渠道 / 提供商
markbot channels status           # 渠道健康状态
markbot channels login weixin     # 微信扫码登录
markbot provider login openai-codex    # Codex OAuth 登录
markbot provider login github-copilot  # Copilot OAuth 登录

# 配置与诊断
markbot status                    # 进程 / 工作区 / 运行时状态
markbot config get|set|list|show  # 查看与修改配置
markbot config provider           # 交互式配置 LLM Provider 与模型
markbot config channel            # 交互式配置聊天渠道
markbot plugins list              # 已发现的渠道插件（内置 + 插件）
markbot doctor                    # 环境体检
markbot doctor fix                # 自动修复常见问题
markbot --version
```

---

## 会话内斜杠命令

**这些命令只在 markbot 会话内生效**——即 `markbot` 交互式终端、Web 控制台，以及 Feishu / QQ / 微信等渠道的对话框里输入的消息。它们不是 shell 命令：在系统终端敲 `/new` 不会有任何效果；反过来，在会话里输入 `markbot gateway` 也只会被当作一句话发给模型。shell 子命令见上一节。

命令名与参数之间用**空格**分隔（写 `/trace full`，不是 `/trace[full]`）。输入了不存在的命令会收到一条"未知命令"提示，不会被当成普通消息送去消耗一次模型调用。

| 类别 | 命令 | 说明 |
|------|------|------|
| 控制 | `/stop` | 取消本会话所有活动任务与子代理 |
| 控制 | `/steer <指令>`（别名 `/btw`） | 向运行中的 Agent 注入中途指令；没有任务在跑时直接拒绝 |
| 控制 | `/restart` | 重启 Agent 进程（需管理员） |
| 会话 | `/status` | 会话状态、token 用量与统计 |
| 会话 | `/trace [full]` | 查看上一回合的决策轨迹（加 `full` 输出完整时间线） |
| 会话 | `/compact` | 手动触发上下文压缩（保留窗口与自动压缩一致，取 `compaction.autoCompactKeepRecent`） |
| 会话 | `/summary`（别名 `/compact_str`） | 查看当前压缩摘要内容 |
| 会话 | `/new` | 把当前会话归档到长期记忆，然后开启新会话 |
| 会话 | `/clear` | 清空历史与压缩摘要（不写长期记忆） |
| 运行时 | `/mode [mode]` | 查看/切换权限模式（需管理员） |
| 运行时 | `/profile [name]` | 查看/切换运行档案（需管理员） |
| 通用 | `/moa [question]` | Mixture-of-Agents：多个顾问并行作答，再由聚合模型综合 |
| 通用 | `/help` | 显示可用命令（按类别分组） |

几点容易误解的地方：

- **`/new` 与 `/clear` 都是从空上下文开始**，区别只有一处：`/new` 会先把当前消息交给记忆管理器做长期记忆归档，`/clear` 不会。两者都会丢弃当前会话的压缩摘要——那份摘要属于正在结束的会话，不属于新会话。
- **群聊里的 `/stop`、`/clear`、`/new` 需要管理员身份**：它们会取消或抹掉其他参与者正在等待的工作。1:1 会话不受此限制，任何人都能停掉自己那个跑飞的任务。
- **`/moa` 的结果会写入会话历史**，可以直接接着追问；但它运行期间不接受 `/steer`（MoA 不消费中途指令）。

---

## 权限模式与安全

权限系统分两层：**静态能力**（config.json 的 `tools.<x>.enable`）决定工具是否注册可见；**运行时模式**（`/mode`）决定已注册工具是否免确认执行。`/mode` 的切换会持久化到 `agents.defaults.defaultPermissionMode`，重启后仍生效。

| 模式 | 行为 |
|------|------|
| `default` | 除只读工具外，全部需要确认 |
| `plan` | 只读模式，阻止一切变更操作 |
| `accept_edits` | 允许文件编辑，破坏性操作（exec / run_code / delete_file / 桌面点击）仍需确认 |
| `auto` | 所有工具免确认（**schema 默认已不是它**，需显式 `/mode auto`） |
| `bypass_permissions` | 绕过全部权限检查（仅调试用） |

确认 UI 是存在的：`PermissionApprover` 会通过当前渠道发一条带编号选项的消息（CLI 下走 stdin），等待用户在 `agents.defaults.permissionApprovalTimeoutS`（默认 60 秒）内回答。**超时一律拒绝**，不存在放行路径。没有渠道、没有 stdin、回答无法解析，也都是拒绝。

要点：

- Cron / Autopilot / Heartbeat 等无人值守路径**始终以 auto 模式**运行（`process_direct` 强制传入）。
- `/mode`、`/profile`、`/restart` 需要管理员身份；`/stop`、`/clear`、`/new` 仅在群聊中需要。配置见 [security](#security--特权命令鉴权)。
- 三种典型配置范式：
  - **安全聊天**（只读）：关闭 `exec` / `codeExecution` / `computerUse` / `browser`，配合 `/mode plan`；
  - **本地开发助手**：开启 exec + 代码执行但 `restrictToWorkspace: true`，配合 `/mode accept_edits`；
  - **完全桌面托管**：放开所有限制，配合 `/mode auto` 或 `bypass`。
- 默认值（`agents.defaults.profile` 未设置时）是 **`assistant` + `accept_edits`**：文件读写与编辑免确认，exec / 代码执行 / 删除 / 桌面操作需要确认；桌面控制默认关闭（`enableDesktop: false` 且 `disabledTools` 含 `computer_use`），需要时显式切到 `coding`。
- 其余安全默认值：`gateway.host` 默认仅监听 `127.0.0.1`；Shell 与文件工具默认限制在工作区内；删除默认进入回收站目录；`message` 工具的附件只能取自 workspace，且不能发往当前会话以外的 chat。

---

## 开发指南

### 目录结构

```
markbot/
├── agent/            # Agent 循环：iteration、压缩、上下文构建、成本跟踪、
│   │                 #   前缀缓存、pipeline 中间件、subagent、MCP 管理器、MoA
├── channels/         # 渠道：dingtalk / feishu / qq / weixin / email + 管理器
├── tools/            # 内置工具实现（文件系统/Shell/Web/记忆/browser/computer_use…）
├── skills/           # 技能框架（loader/scanner/guardrail/sandbox）+ 内置技能
├── providers/        # 28 家 LLM Provider + FallbackManager(熔断) + OAuth + 插件注册表
├── config/           # Pydantic 配置 schema / loader / 路径 / 运行档案
├── runtime/          # 声明式装配工厂（CLI/Gateway/Web 共用）
├── memory/           # 记忆系统：长期向量库、整合、每日日志、脱敏、插件发现
├── bus/              # 消息总线（inbound/outbound 队列 + 事件）
├── session/          # 会话持久化、完整性校验、任务跟踪
├── schedule/         # CronService / DreamService / HeartbeatService
├── autopilot/        # 自动任务流水线（Intake→Verify→Repair）
├── cli/              # Typer CLI：onboard/agent/gateway/web/skills/autopilot/doctor
├── web/              # FastAPI 服务端（WebSocket 聊天 + REST 路由）
├── types/            # 共享类型定义（权限、工具、协议）
├── utils/            # 通用工具函数
├── locales/          # i18n 文案（en / zh）
├── templates/        # 工作区模板（SOUL.md / MEMORY.md 等）
└── log/              # loguru 日志核心与敏感信息脱敏
web/                  # Web 控制台前端（React 19 + Vite + Tailwind 4）
tests/                # pytest 测试套件
```

### 常用开发命令

```bash
# 后端
pip install -e ".[dev]"
ruff check .                      # 代码检查
pytest                            # 运行测试
pytest --cov=markbot              # 带覆盖率

# 前端（Web 控制台）
cd web
npm install
npm run dev                       # Vite 开发服务器
npm run build                     # 构建产物输出到后端静态目录

# 调试运行（走真实 runtime 装配路径）
python -m markbot.cli.commands --help
markbot status                    # 进程 / 工作区 / 运行时状态
```

### 贡献流程

1. Fork 仓库并创建特性分支（`git checkout -b feature/amazing-feature`）
2. 提交改动（`git commit -m 'Add amazing feature'`）
3. 推送分支并发起 Pull Request

---

## License

本项目基于 **GNU Affero General Public License v3 (AGPL-3.0)** 开源。

---

**MarkBot** 🦞 — 面向开发者与效率用户的个人 AI 助手。




