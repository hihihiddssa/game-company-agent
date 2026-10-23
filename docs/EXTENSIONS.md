# MCP、Skills、Memory：课程实现与本项目接入

本项目以本机 `learn-claude-code/s15_integrated_harness/code.py` 为基础，复用其 Agent Loop；Memory 从 s09 内联合入，Skill 文件从课程根目录的 `skills/` 完整复制。所有运行时实现仍在 `main.py`。

## 三者怎样进入 Agent Loop

| 模块 | 数据来自哪里 | 模型怎样看到它 | 谁负责调用 |
| --- | --- | --- | --- |
| Skills | `skills/*/SKILL.md` | 名称、简介进入 system；全文作为 tool_result 进入 messages | 模型调用 `load_skill(name)` |
| Memory | `.memory/MEMORY.md` 与各条 Markdown 记录 | 索引及相关记忆正文进入 system | 宿主在 `update_context()` 自动召回，回合结束自动提取 |
| MCP | 当前为进程内 docs / deploy 模拟服务 | 工具 schema 加入 tools，调用结果进入 messages | 模型先 `connect_mcp(name)`，再调用发现的工具 |

因此，Memory 不是每轮把整个历史 JSON 塞进 messages；Skill 也不会把所有全文默认发给模型。MCP 则是扩充可调用工具。

## Skills：复制了什么，怎样使用

随仓库带上课程的四个技能：`agent-builder`、`code-review`、`mcp-builder`、`pdf`。`agent-builder` 下的 `references/` 与 `scripts/init_agent.py` 也完整保留。

`scan_skills()` 启动时扫描当前工作区；`list_skills()` 提供名称与描述；`load_skill()` 按名称返回全文并附带技能目录。相对引用以这个技能目录为基准，模型可继续用 `read_file` 读取资料，执行脚本则仍需普通 Shell 权限。

可以在项目目录启动后输入：

> 请调用 load_skill 加载 code-review，然后检查 main.py 中的 MCP 工具注册部分，只报告最重要的三个问题。

新增技能只需创建 `skills/你的技能/SKILL.md`，然后重启：

```markdown
---
name: your-skill
description: 什么时候应该使用这个技能
---

# 操作说明
写清楚步骤、约束和参考资料路径。
```

如果从 `workspace/demo` 启动，扫描的是 `workspace/demo/skills`。需要时复制项目自带的 `skills` 到该工作区；运行状态也随当前工作区隔离。

课程技能是说明文档，不是已经安装的服务。PDF 示例里的 PyMuPDF、Poppler，以及 MCP 示例里的 mcp SDK 属于按需安装的额外依赖；加载技能本身不需要安装或运行它们。

## Memory：保留 s09 的完整链路

1. `write_memory_file()` 写入带 YAML 元数据的 Markdown，重建索引。
2. `read_memory_index()` 读取目录；`load_memories()` 选择并读取相关记录。
3. `update_context()` 把索引和选中正文交给 `assemble_system_prompt()`。
4. 正常回合结束后，`remember_after_turn()` 调用 `extract_memories()` 提取长期信息；有新增记录时尝试 `consolidate_memories()`。

四类记录为 user、feedback、project、reference。临时要求与重复内容会被过滤。模型选择失败时召回可退回关键词匹配；提取与整理也可能失败，不能保证用户说一句话就一定形成记忆。

首次没有记录时不会预先生成虚假记忆。真正保存后才出现 `.memory/` 和索引，且它们不会上传 Git。召回、提取、合并可能额外调用模型。

可以测试“请记住：以后给我的代码解释使用中文”，正常完成一轮后检查 `.memory/`；下一次在同一工作区启动，再问“我偏好什么语言的代码解释？”。

## MCP：忠实保留课程的模拟边界

当前没有 stdio、HTTP、SSE 传输层，也没有 MCP 服务器配置文件。`MCPClient` 是进程内教学替身：`register()` 登记 schema 与处理函数，`call_tool()` 调用 Python handler。

可以输入：

> 请连接 docs，然后使用发现的 search 工具搜索 agent loop，并告诉我这是不是模拟结果。

流程为：`connect_mcp("docs")` → `mcp_clients` 登记 → 下一轮 `assemble_tool_pool()` 加入 `mcp__docs__search` / `mcp__docs__get_version` → 模型选择工具 → handler 返回模拟文本。

`deploy` 的 trigger / status 同样是模拟，不会部署服务。宿主 `MCP_HOST_POLICY` 决定是否需要确认，不相信服务描述中的“安全”声明；后台回合无法交互确认时会拒绝需授权操作。

后续接入真实 MCP 时，可从 `connect_mcp()` / `MCPClient` 增加传输适配、初始化、tools/list、tools/call、超时与关闭流程，继续复用现有 `assemble_tool_pool()`、命名冲突检查和权限策略。复制 `mcp-builder` 技能只提供编写服务的知识，不会自动完成这一步。

## English overview

The standalone host keeps the local course's s15 loop and inlines its s09 memory dependency. All four course skills, including agent-builder reference files and its script, are bundled unchanged. `load_skill` adds the skill's directory to resolve relative references.

Skill metadata enters the system prompt; full text is returned on demand as a tool result. Memory is recalled automatically into system context and extracted after a completed turn. Both selection and extraction may make model requests. Skill and memory directories follow the current working directory.

MCP retains the course's in-process docs/deploy demonstrations. It has no real stdio or network transport; `mcp-builder` is documentation, not a running MCP server. See the bilingual READMEs and `SOURCE_MAP.md` for setup and runtime entry points.
