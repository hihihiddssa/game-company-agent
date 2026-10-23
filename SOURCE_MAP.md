# 当前综合运行时阅读导航

主程序来自 learn-claude-code s15 的综合结构，记忆存储、召回、提取与合并来自 s09，并已合入 main.py，不依赖仓库外文件。工具定义、函数与状态仍保持显式，用户修改的核心循环和恢复逻辑保留。

| 入口 | 当前位置 | 用途 |
| --- | --- | --- |
| `agent_loop` | [main.py:3941](main.py#L3941) | 主循环：调度、上下文、模型、工具与恢复 |
| `call_llm` | [main.py:3919](main.py#L3919) | 动态系统提示词与错误重试 |
| `assemble_system_prompt` | [main.py:1382](main.py#L1382) | Skills / Memory / MCP 背景组装 |
| `assemble_tool_pool` | [main.py:3422](main.py#L3422) | 内置工具与模拟 MCP 合并 |
| `write_memory_file` | [main.py:206](main.py#L206) | 持久记忆文件与索引 |
| `load_memories` | [main.py:398](main.py#L398) | 相关记忆召回 |
| `extract_memories` | [main.py:447](main.py#L447) | 回合结束提取持久信息 |
| `consolidate_memories` | [main.py:524](main.py#L524) | 记忆整理合并 |
| `scan_skills` | [main.py:1266](main.py#L1266) | 本地 Skill 目录扫描 |
| `create_task` | [main.py:743](main.py#L743) | 共享任务创建 |
| `update_task` | [main.py:781](main.py#L781) | 任务依赖维护 |
| `claim_task` | [main.py:889](main.py#L889) | 认领与目录绑定 |
| `complete_task` | [main.py:919](main.py#L919) | 任务完成 |
| `MessageBus` | [main.py:1683](main.py#L1683) | JSONL 邮箱与线程通知 |
| `spawn_teammate_thread` | [main.py:1946](main.py#L1946) | 常驻成员线程与工作/空闲状态 |
| `spawn_subagent` | [main.py:2498](main.py#L2498) | 一次性聚焦子 Agent |
| `start_background_task` | [main.py:2942](main.py#L2942) | 后台 Shell 与结果通知 |
| `schedule_job` | [main.py:3159](main.py#L3159) | Cron 注册与持久化 |
| `start_runtime_services` | [main.py:3275](main.py#L3275) | 启动调度服务 |
| `MCPClient` | [main.py:3305](main.py#L3305) | 进程内模拟工具服务 |
| `connect_mcp` | [main.py:3407](main.py#L3407) | 模拟发现和连接 |
| `prepare_context` | [main.py:3842](main.py#L3842) | 多级上下文整理 |
| `compact_history` | [main.py:2761](main.py#L2761) | 归档与摘要 |
| `RecoveryState` | [main.py:2800](main.py#L2800) | 重试及输出恢复状态 |
| `async_event_loop` | [main.py:4162](main.py#L4162) | 后台事件唤醒 Lead |

与上一版的重要区别：

- BUILTIN_TOOLS / BUILTIN_HANDLERS 与动态 assemble_tool_pool 替代旧的分组 TOOLS。
- 不再有 DeepSeekClient、ContextManager、TeammateRuntime 类，也没有 assign_task / accept_delivery 工具。
- Memory 已实际接入；MCP 仍是 docs/deploy 的进程内模拟，不包含远端传输。
- 工作区使用 Path.cwd()；启动入口在文件末尾的 __main__ 分支，不提供旧的 CLI 参数。
- 输出使用 DEFAULT_MAX_TOKENS=8000、ESCALATED_MAX_TOKENS=16000；上下文使用序列化消息长度估算。
- .env 使用 Anthropic SDK 配置，并兼容原有 DEEPSEEK_API_KEY 快捷方式。

当前目录中的 .memory、.tasks、.mailboxes、.transcripts、.task_outputs 和 .scheduled_tasks.* 是运行状态，不上传 Git。

课程 Skills 资源位于 `skills/`；模块数据流与测试提示词见 [docs/EXTENSIONS.md](docs/EXTENSIONS.md)。
