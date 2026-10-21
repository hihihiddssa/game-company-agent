# Game Company Agent — DeepSeek

核心代码仍集中在 main.py，保留原版 s13 Agent Loop、持久队友、文件任务板和 MessageBus。

使用便宜的 `deepseek-flash`，关闭深度思考。DeepSeek 原生支持 Anthropic 消息格式，因此直接复用旧环境中的 SDK，无需转换工具和历史消息。

```powershell
cd D:\project\game-company-agent
.\run.ps1
```

已运行的程序需要退出并重新启动。run.ps1 复用 learn-claude-code 的虚拟环境，读取本项目 .env。

```dotenv
DEEPSEEK_API_KEY=你的DeepSeek密钥
MODEL_ID=deepseek-flash
```

密钥只保存到本地 .env，不进入源码或下载包。

```powershell
.\run.ps1 --check-config  # 只检查配置
.\run.ps1 --check-api     # 只读取模型列表，不生成文本
```

DeepSeekClient.create 仅固定关闭思考模式，再调用原来的 client.messages.create。
Lead 和三个队友各自保存消息历史。不会再按 Google 免费额度排队，也不会自动切换模型。

## 任务与产物

Lead 先 `create_task`，用真实 ID `update_task` 连边，完成建图后 `assign_task` 派发，再启动或唤醒相应队友。队友仅能认领 assignee 属于自己的、依赖已经完成的任务。

```text
workspace/
  .tasks/            文件任务板
  .mailboxes/        JSONL 收件箱，领取即消费
  design/            设计方案
  game/              实际游戏及运行说明
  tests/             tester 按需为具体游戏生成的检查文件
  reports/qa.json    测试报告与 Bug
  delivery.json      Lead 验收通过时生成
```

框架自身的 tests/ 不再附带。tester 游戏测试角色仍保留；任务 completed 与 QA passed 是两回事。`accept_game` 检查入口、任务和报告结构，不代替真实浏览器体验测试。

`--workdir workspace\my-game` 可为新游戏指定独立目录。现阶段不提供崩溃自动恢复或持久化聊天历史；旧任务文件仍在，但重启后队友线程和对话上下文需由 Lead 重新组织。

Shell 命令默认非交互；npm 缺包时不会偷偷等安装确认。执行期间每 15 秒打印进度，超时会尝试结束整个命令进程树。计划审批与可选 Git worktree 沿用原版，工作目录检查不是 OS 沙箱。

DeepSeek 官方参考：[价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[Anthropic 兼容接口](https://api-docs.deepseek.com/guides/anthropic_api/)。

## 待实现功能

以下功能尚未实现，后续有空时逐步补充：

- [ ] 上下文压缩：控制长对话与工具输出的上下文占用，并保留关键任务信息。
- [ ] Memory：持久保存项目知识、用户偏好与关键决策，支持后续会话读取。
- [ ] MCP：连接 MCP Server，把外部工具接入现有 Agent Loop。

当前 context_hook / summary_hook 是已有 Hook 扩展点，不代表上述上下文压缩或持久记忆已经完成。

## 在其他电脑上运行

要求 Python 3.10 或以上。安装依赖，将 .env.example 复制为 .env 并填写自己的密钥：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env，填写 DEEPSEEK_API_KEY
.\.venv\Scripts\python.exe main.py
```

仓库不包含真实 .env、运行 workspace 或本地归档 test；工作目录由程序按需创建。
项目附带的 run.ps1 与 .vscode 配置使用开发者本机的旧虚拟环境路径，在其他电脑上应按上面的命令运行或调整解释器路径。
