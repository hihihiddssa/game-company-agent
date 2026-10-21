# 与 learn-claude-code s13 的对应关系

继续沿用原版模型工具循环。供应商已换为 DeepSeek；DeepSeekClient 复用原版 Anthropic SDK 消息格式并关闭深度思考。

| 名称 | 原版行号 | main.py 行号 | 说明 |
| --- | --- | --- | --- |
| `agent_loop` | 2887 | 3415 | 沿用原版执行结构 |
| `MessageBus` | 1171 | 1338 | 沿用原版执行结构 |
| `execute_tool` | 2870 | 3301 | 沿用原版执行结构 |
| `consume_lead_inbox` | 1451 | 1630 | 沿用原版执行结构 |
| `TeammateRuntime` | 1832 | 2020 | 扩展或修复 |
| `create_task` | 189 | 246 | 沿用原版执行结构 |
| `update_task` | 227 | 284 | 扩展或修复 |
| `claim_task` | 337 | 402 | 扩展或修复 |
| `complete_task` | 367 | 440 | 沿用原版执行结构 |
| `run_bash` | 977 | 1080 | 扩展或修复 |
| `DeepSeekClient` | — | 49 | 新增 |
| `run_assign_task` | — | 2829 | 新增 |
| `run_accept_game` | — | 2856 | 新增 |
| `main` | — | 3485 | 新增 |

Shell 经过非交互与进程树超时修复。游戏职责、派发、验收和 DeepSeek 配置是本项目扩展。
原用户注释和函数位置保留；根目录不附带框架 tests/。
