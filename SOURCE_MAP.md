# 与 learn-claude-code s13 的对应关系

继续沿用原版模型工具循环。团队采用动态成员，通用提示词、工具权限上限和交付规则直接写在 main.py；模型供应商为 DeepSeek；DeepSeekClient 复用原版 Anthropic SDK 消息格式并关闭深度思考。

| 名称 | 原版行号 | 当前文件位置 | 说明 |
| --- | --- | --- | --- |
| `agent_loop` | 2887 | [main.py:3932](main.py#L3932) | 沿用原版执行结构 |
| `MessageBus` | 1171 | [main.py:1529](main.py#L1529) | 沿用原版执行结构 |
| `execute_tool` | 2870 | [main.py:3659](main.py#L3659) | 沿用原版执行结构 |
| `consume_lead_inbox` | 1451 | [main.py:1821](main.py#L1821) | 沿用原版执行结构 |
| `TeammateRuntime` | 1832 | [main.py:2776](main.py#L2776) | 扩展或修复 |
| `create_task` | 189 | [main.py:663](main.py#L663) | 沿用原版执行结构 |
| `update_task` | 227 | [main.py:701](main.py#L701) | 扩展或修复 |
| `claim_task` | 337 | [main.py:819](main.py#L819) | 扩展或修复 |
| `complete_task` | 367 | [main.py:857](main.py#L857) | 沿用原版执行结构 |
| `run_bash` | 977 | [main.py:2145](main.py#L2145) | 扩展或修复 |
| `DeepSeekClient` | — | [main.py:76](main.py#L76) | 新增 |
| `ContextManager` | — | [main.py:166](main.py#L166) | 新增：参考 s08 的分层压缩，接入 Lead 与队友 |
| `teammate_tools` | — | [main.py:2097](main.py#L2097) | 通用团队支持 |
| `build_prompt_sections` | — | [main.py:1454](main.py#L1454) | 通用团队支持 |
| `run_assign_task` | — | [main.py:2683](main.py#L2683) | 新增 |
| `run_accept_delivery` | — | [main.py:2716](main.py#L2716) | 新增 |
| `main` | — | [main.py:4011](main.py#L4011) | 新增 |

Shell 经过非交互与进程树超时修复。游戏职责、派发、验收和 DeepSeek 配置是本项目扩展。
原有中文注释保留；全部 Python 实现位于 main.py，按编号分区，函数位置见上表；根目录不附带框架 tests/。

压缩学习入口：`prepare()` → `_micro_compact()` → `_find_cut()` → `_summarize()`。

原 STUDIO_TOOLS 拆为通用 TASK_TOOLS 派发和 DELIVERY_TOOLS 交付；游戏只是用户可以提出的一种工作目标。


动态成员入口：`spawn_teammate_thread()` 接收模型自选身份，登记运行时 TEAM_MEMBERS，再创建 TeammateRuntime 和线程。不预设成员，也不指定固定成员必须参与交付。Lead 可直接完成任务并交付，无需创建任何子 Agent。

单文件工具入口：第 10 节实现 run_*，第 12 节直接定义分组 Schema、合并 TOOLS、建立 TOOL_HANDLERS 并进行分发；不再使用 initialize_tools 或运行时模块绑定。

直接设置入口：main.py 第 04 节的 TEAM_NAME、LEAD_PROMPT、TEAMMATE_PROMPT、TEAMMATE_ALLOWED_TOOLS、DELIVERY_REQUIRED_FILES 与 QUALITY_REPORT_PATH。外部团队 JSON 加载、校验和切换层已移除。

普通 Agent 输出不再受应用层低上限限制；DeepSeekClient 补齐 SDK 必填的提供方最大输出值。ContextManager.output_reserve 仅用于压缩预算估算，摘要仍使用独立的 summary_tokens。
