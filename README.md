# Multi-Agent Team Framework

**简体中文** | [English](README.en.md)

基于 learn-claude-code 综合运行框架思路的 Python Agent 项目。核心仍是 **模型 → 工具调用 → 工具结果 → 下一轮模型**，将任务、团队、记忆、技能、后台命令和调度整合在同一个 `main.py` 中。

当前主程序采用 s15 集成结构，持久记忆实现从 s09 合入。本仓库可以独立运行，不再依赖仓库外的课程目录。

## 当前能力

| 模块 | 实际行为 |
| --- | --- |
| Agent Loop | 动态组装系统提示词和工具池，执行工具并回填结果 |
| 任务与团队 | 文件任务板、依赖、认领与完成；支持常驻 teammate 和消息、计划审批、关闭协议 |
| 一次性子 Agent | `task` 工具执行聚焦任务，只返回最终摘要；与常驻 teammate 不同 |
| 持久 Memory | `.memory/` 中保存 YAML 元数据的 Markdown 记录，支持索引、召回、提取与合并 |
| Skills | 扫描 `skills/<目录>/SKILL.md`，先提供目录，再按需加载全文 |
| 上下文整理 | 大输出落盘、工具结果裁剪、旧消息归档、模型摘要与超长输入恢复 |
| 后台命令 | `bash(run_in_background=true)` 在线程中执行，结果以通知回传 |
| Cron | 五字段表达式、单次/重复任务、可持久化调度；需要本进程运行 |
| 重试恢复 | 限次退避、429/529 处理、可选备用模型、截断后的升级与续写 |
| MCP 教学接口 | **仅内置 `docs` / `deploy` 进程内模拟服务**，演示工具发现、名称空间与宿主权限策略 |

MCP 部分尚未实现连接真实远端或 stdio 服务的传输层；模拟部署工具不会实际部署服务。

## 安装与启动

需要 Python 3.10+。先激活你自己选择的 Python 环境；项目不固定解释器路径。

Windows PowerShell：

```powershell
git clone https://github.com/hihihiddssa/multi-agent-team-framework.git
cd multi-agent-team-framework
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

依赖包括 `anthropic`、`python-dotenv` 和 `PyYAML`。编辑 `.env`，例如使用 DeepSeek：

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key
MODEL_ID=deepseek-flash
```

只配置 `DEEPSEEK_API_KEY` 时，程序自动使用 `https://api.deepseek.com/anthropic`。也可以用其他 Anthropic 兼容端点：

```dotenv
ANTHROPIC_API_KEY=your_api_key
ANTHROPIC_BASE_URL=https://your-provider.example/anthropic
MODEL_ID=your_supported_model
# 可选：必须是同一端点支持的模型
# FALLBACK_MODEL_ID=your_fallback_model
```

两种密钥同时存在时，`ANTHROPIC_API_KEY` 优先。使用 Anthropic 官方服务时可省略 `ANTHROPIC_BASE_URL`，并设置它支持的模型名称。当前直接使用 SDK 参数，未沿用旧版强制关闭思考模式的包装器。

```powershell
python main.py
```

出现 `team >>` 后输入任务。输入 `q`、`exit` 或空行退出。在 VS Code 中使用 **Python: Select Interpreter** 选择环境。

**当前工作目录就是工作区。** 希望把产物隔离到子目录时：

```powershell
New-Item -ItemType Directory -Force workspace/demo | Out-Null
Set-Location workspace/demo
python ../../main.py
```

程序仍从脚本所在项目的 `.env` 加载配置；任务、记忆、Skills 与输出位置根据当前工作目录决定。本版本不提供旧版的 `--workdir`、`--env-file`、`--check-config` 或 `--check-api` 参数。

## 循环与协作

```text
用户输入 / 调度触发 / 团队反馈 / 后台结果
  → 注入当前消息
  → 整理上下文
  → 召回 Memory，刷新 Skills / MCP / 成员信息
  → 组装 system 和工具池
  → 请求模型（含恢复策略）
  → 执行 tool_use，回填 tool_result
  → 继续循环；无工具调用时提取长期记忆并结束本轮
```

后台事件通过异步事件循环触发 Lead 回合，使用锁与用户回合协调。单条模型回复中的普通工具调用顺序执行，后台命令和 teammate 可在独立线程中工作。

本版 `PROMPT_SECTIONS["teams"]` 要求 **先提出小团队方案并等待用户确认，再创建常驻成员**。这是提示词规则，不是额外的代码确认状态机。没有固定成员名单。任务关系通常为：

```text
create_task → 返回真实 ID → update_task 设置必要依赖
            → spawn_teammate(name, role, prompt, task_id)
```

初始任务在创建成员时自动认领；成员也可以从任务板领取就绪任务。本版没有旧的 `assign_task` / `accept_delivery` 工具，代码也不再使用 `Task.assignee`。任务完成、最终回复、线程退出是不同事件，Lead 通过 `request_shutdown` 协调关闭。

## 工具池

`BUILTIN_TOOLS` 声明参数，`BUILTIN_HANDLERS` 映射 Python 函数；每轮由 `assemble_tool_pool()` 合并已连接的模拟 MCP 工具。`compact` 由主循环单独处理。

| 类别 | 工具 |
| --- | --- |
| 文件与命令 | `bash`、`read_file`、`write_file`、`edit_file`、`glob` |
| 会话辅助 | `todo_write`、`task`、`load_skill`、`compact` |
| 任务 | `create_task`、`update_task`、`list_tasks`、`get_task`、`claim_task`、`complete_task` |
| 常驻团队 | `spawn_teammate`、`list_teammates`、`send_message`、`request_shutdown`、`request_plan`、`review_plan` |
| 调度与目录 | `schedule_cron`、`list_crons`、`cancel_cron`、`create_worktree` |
| 模拟 MCP | `connect_mcp`，连接后加入 `mcp__docs__*` / `mcp__deploy__*` |

一次性 `task` 子 Agent 只有五种基础工具；常驻 teammate 的工具集合在创建逻辑中单独定义。

主线程执行 Shell 前会询问 `Allow? [y/N]`；异步回合不能弹出这种交互确认，因此会拒绝需要确认的 Shell 或 MCP 操作。Shell 命令有 120 秒超时，Windows 下使用 CMD 语法。工作区与权限检查不是 OS 沙箱。

## 课程模块与资源

已完整带上课程的 `agent-builder`、`code-review`、`mcp-builder`、`pdf` 四个技能，以及 agent-builder 的参考文件和初始化脚本。按需加载时会附带技能目录，便于定位相对引用；修改技能后重启即可。技能文档里的额外工具依赖按需安装。

s09 Memory 已内联到 `main.py` 并接入召回、提取、合并；MCP 保留本地 s15 的模拟接口与动态工具池。数据流和测试提示词见 [模块接入说明](docs/EXTENSIONS.md)。没有复制课程中的运行状态或私人记忆记录。

## 记忆、上下文与用量

Memory 索引位于 `.memory/MEMORY.md`，记录类型包括 user、feedback、project、reference。召回会选择相关记录，模型选择失败时可按关键词降级；回合结束会尝试提取持久信息，过滤临时范围和重复内容，达到阈值时可合并记录。这些步骤可能额外调用模型，并不是免费缓存。

本版主要参数直接定义在 `main.py` 顶部：

| 常量 | 当前值 / 用途 |
| --- | --- |
| `DEFAULT_MAX_TOKENS` | `8000`，普通请求输出额度 |
| `ESCALATED_MAX_TOKENS` | `16000`，截断后的升级额度 |
| `MAX_RETRIES` | `3`，错误重试次数 |
| `MAX_RECOVERY_RETRIES` | `2`，截断续写恢复次数 |
| `CONTEXT_LIMIT` | `50000`，序列化消息的估算长度，**不是精确 Token 数** |
| `KEEP_RECENT_TOOL_RESULTS` | `3`，保留近期工具结果 |
| `PERSIST_THRESHOLD` | `30000`，长工具输出落盘阈值 |

本次文档按你新代码中的 8000/16000 配置编写；旧版 `AGENT_MAX_OUTPUT_TOKENS` 和 `CONTEXT_*` 环境变量不再控制这些常量。重试与续写次数限制也不等于整个任务的总调用或费用上限。

大输出保存至 `.task_outputs/tool-results/`，历史归档保存至 `.transcripts/`。上下文还会进入模型摘要或提示过长时的恢复路径；保留当前请求并把摘要作为参考状态。

## 运行文件与阅读入口

```text
main.py                 # 综合运行时，含内联 Memory
README.md / README.en.md
SOURCE_MAP.md           # 当前模块与函数位置
requirements.txt
.env.example
skills/<name>/SKILL.md  # 自带课程四个技能，扫描实际工作区
.memory/               # 持久记忆与索引
.tasks/ / .mailboxes/   # 任务板与邮箱
.task_outputs/         # 大工具输出
.transcripts/          # 历史归档
.scheduled_tasks.json  # 持久调度记录
```

运行状态、密钥和缓存已加入 `.gitignore`。聊天历史、运行线程不会整体自动恢复；持久记忆和耐久调度是独立的恢复机制。长期调度仍依赖 Python 进程运行，模拟 MCP 不等于生产级集成。

建议先读 `agent_loop()` → `call_llm()` → `assemble_system_prompt()` / `assemble_tool_pool()`，再读 Memory、`start_background_task()`、`spawn_teammate_thread()` 与调度器。准确位置见 [SOURCE_MAP.md](SOURCE_MAP.md)。

## 来源与许可

主程序基于 **learn-claude-code s15 Integrated Harness**，记忆实现来自 s09；保留原有注释和 MIT 版权声明，详见 [LICENSE](LICENSE)。
