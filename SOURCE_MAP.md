# 与 learn-claude-code s13 的对应关系

继续沿用原版模型工具循环。团队已改为 JSON 可配置框架；模型供应商为 DeepSeek；DeepSeekClient 复用原版 Anthropic SDK 消息格式并关闭深度思考。

| 名称 | 原版行号 | main.py 行号 | 说明 |
| --- | --- | --- | --- |
| `agent_loop` | 2887 | 3817 | 沿用原版执行结构 |
| `MessageBus` | 1171 | 1741 | 沿用原版执行结构 |
| `execute_tool` | 2870 | 3703 | 沿用原版执行结构 |
| `consume_lead_inbox` | 1451 | 2033 | 沿用原版执行结构 |
| `TeammateRuntime` | 1832 | 2423 | 扩展或修复 |
| `create_task` | 189 | 645 | 沿用原版执行结构 |
| `update_task` | 227 | 683 | 扩展或修复 |
| `claim_task` | 337 | 801 | 扩展或修复 |
| `complete_task` | 367 | 839 | 沿用原版执行结构 |
| `run_bash` | 977 | 1483 | 扩展或修复 |
| `DeepSeekClient` | — | 50 | 新增 |
| `ContextManager` | — | 104 | 新增：参考 s08 的分层压缩，接入 Lead 与队友 |
| `load_team_config` | — | 463 | 新增通用团队配置 |
| `configure_team` | — | 478 | 新增通用团队配置 |
| `teammate_tools` | — | 498 | 新增通用团队配置 |
| `build_prompt_sections` | — | 1430 | 新增通用团队配置 |
| `run_assign_task` | — | 3242 | 新增 |
| `run_accept_delivery` | — | 3269 | 新增 |
| `main` | — | 3895 | 新增 |

Shell 经过非交互与进程树超时修复。游戏职责、派发、验收和 DeepSeek 配置是本项目扩展。
原用户注释和函数位置保留；根目录不附带框架 tests/。

压缩学习入口：`prepare()` → `_micro_compact()` → `_find_cut()` → `_summarize()`。

原 STUDIO_TOOLS 拆为通用 TASK_TOOLS 派发和 DELIVERY_TOOLS 交付；游戏只是 examples/game-team.json。
