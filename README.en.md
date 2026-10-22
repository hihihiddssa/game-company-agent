# Multi-Agent Team Framework

[简体中文](README.md) | **English**

A small Python multi-agent framework built around learn-claude-code-style agent loops. **The entire implementation lives in `main.py`**, with detailed Chinese comments and 16 foldable sections for reading and experimentation.

A Lead agent can finish a task directly or create teammates as needed, choosing their names, roles and assignments at runtime. There is no preset roster or external team configuration JSON.

## What it does

- **Dynamic delegation:** each teammate runs in its own thread with separate messages and a model loop.
- **Shared tasks:** file-backed creation, dependencies, assignment, claiming and completion coordinated with file locks.
- **Messaging:** a JSONL MessageBus with thread notifications for results, approval and shutdown protocols.
- **Tools:** files, shell commands, task and team operations, with optional teammate tool restrictions.
- **Context compaction:** request-budget estimation, trimming of older tool output and rolling summaries when needed.
- **Coordination controls:** optional plan approval, Git worktrees, artifact checks and quality reports.

Persistent memory and MCP are not implemented. The current client uses DeepSeek through an Anthropic-compatible messages interface and the Anthropic Python SDK.

## Quick start

Requires Python 3.10+ and a working DeepSeek API key with available quota. Activate your preferred Python environment first; the project neither selects a hardcoded interpreter nor switches environments automatically.

Windows PowerShell:

```powershell
git clone https://github.com/hihihiddssa/multi-agent-team-framework.git
cd multi-agent-team-framework
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set your local `.env` values:

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key
MODEL_ID=deepseek-flash
```

```powershell
# Check startup settings after filling in .env; sends no API request
python main.py --check-config

# Start the interactive session; use q / exit or Ctrl+C to leave
python main.py

# Keep a new task's output separate
python main.py --workdir workspace/counter-demo
```

Model requests go to `https://api.deepseek.com/anthropic`. `--check-api` accesses the model list to verify access without generating text. The real `.env`, generated workspace and caches are ignored by Git.

In VS Code, use **Python: Select Interpreter** to select an environment with the dependencies installed. Run `python -c "import sys; print(sys.executable)"` to inspect your terminal's interpreter. An application `.env` file is separate from a Python virtual environment.

## How the loops work

```text
User message or team event
  → append to Lead messages
  → check/compact context
  → request the model with SYSTEM, messages and TOOLS
  → tool_use? execute tools and append tool_result, then request again
  → no tool_use? finish this turn and wait for input or team events
```

Tool calls in one model response execute sequentially. Teammate threads can run concurrently. Finishing a reply does not terminate the program or shut down teammates.

For tracked delegation:

```text
create_task → update_task dependencies if needed
            → assign_task(task_id, chosen_name)
            → spawn_teammate(name, role, prompt, task_id)
```

Spawning with an initial task automatically claims it when assignment and dependencies permit. Do not claim it again. Omit the spawn `task_id` for blocked tasks; an idle teammate scans for ready tasks assigned to its name. Teammates must claim a task before using workspace tools.

Each teammate has a similar loop and reports through MessageBus. `complete_task` updates task state; it does not immediately terminate the loop or thread. The teammate reports its result and becomes idle, waiting for messages or ready work. Lead uses `request_shutdown` to ask it to exit.

## Tools and source navigation

| Object | Purpose |
| --- | --- |
| `BASE_TOOLS` | `bash`, `read_file`, `write_file`, `edit_file`, `glob` |
| `TASK_TOOLS` | Task creation, dependencies, list/get, claim, completion and assignment |
| `TEAM_TOOLS` | Dynamic teammates, member list, messages, plan approval, shutdown and worktrees |
| `DELIVERY_TOOLS` | `accept_delivery` checks artifacts, existing tasks and an optional report |
| `TOOLS` | The four groups combined for the Lead model |
| `TOOL_HANDLERS` | Tool-name-to-Python-function execution mapping |
| `TEAMMATE_TOOLS` | A restricted teammate set including `submit_plan`, filtered by member permissions |

Teammates cannot create agents, create tasks or accept delivery. Their definitions reuse some Lead definitions, so `TEAMMATE_TOOLS` is not appended to `TOOLS` again.

Start reading `main()` → `agent_loop()` → `execute_tool()` → `TOOL_HANDLERS` → `run_*()`, then inspect `TeammateRuntime.work()` and `spawn_teammate_thread()`.

Useful sections: 04 prompts/settings, 05 tasks, 08 messaging/protocols, 10 tool implementations, 11 teammate lifecycle, 12 schemas/handlers/dispatch, and 15 the Lead loop. The file header contains the full map; VS Code folds `# region` sections. [SOURCE_MAP.md](SOURCE_MAP.md) maps the implementation to the original teaching code.

## Change behavior and permissions

Edit section 04 of `main.py`, then restart:

| Setting | Purpose |
| --- | --- |
| `TEAM_NAME` | Display name |
| `LEAD_PROMPT` | Lead behavior, scope and delegation guidance |
| `TEAMMATE_PROMPT` | Shared teammate guidance; role/prompt at spawn time defines the actual assignment |
| `TEAMMATE_ALLOWED_TOOLS` | Upper bound for teammate workspace tools |
| `DELIVERY_REQUIRED_FILES` | Optional required artifacts; defaults to `[]` |
| `QUALITY_REPORT_PATH` | Optional quality report path; defaults to `None` |

The spawn `tools` argument can narrow permissions, never exceed the upper bound. `tools=[]` retains task, messaging and plan tools. This upper bound does not restrict Lead tools. The default prompts encourage minimal work and direct completion of simple tasks; no fixed planner/developer/tester workflow is required.

`accept_delivery` requires at least one real artifact. No teammate or task needs to be created, but any existing tasks must all be complete. When a quality report is required, it must reference a completed task owned by the actual checker, include `passed: true`, an empty `issues` list and successful command `checks`. The checker may be Lead or a dynamic teammate.

## Context and token usage

Configure these in `.env`; defaults:

| Variable | Default |
| --- | --- |
| `AGENT_MAX_OUTPUT_TOKENS` | `4096` |
| `CONTEXT_COMPRESSION_ENABLED` | `true` |
| `CONTEXT_MAX_TOKENS` | `48000` |
| `CONTEXT_TRIGGER_RATIO` | `0.8` |
| `CONTEXT_TARGET_RATIO` | `0.55` |
| `CONTEXT_KEEP_RECENT` | `8` |
| `CONTEXT_KEEP_TOOL_RESULTS` | `3` |
| `CONTEXT_SUMMARY_TOKENS` | `2000` |
| `CONTEXT_TOOL_RESULT_CHARS` | `2400` |

The budget includes system text, tool schemas, history, output reservation and protocol allowance. It is estimated from UTF-8 bytes, not an exact provider token count. Older tool output is trimmed first; model-generated rolling summaries are used if needed. Recent messages and complete tool-call/result pairs are preserved. Summaries stay in the owning agent's current process; failure leaves the original history intact.

The terminal logs each model request's duration, input, cache-read and output usage. Compaction limits request history size, **not total task tokens or the number of model requests**. Summarization also consumes API quota.

## Small collaboration exercise

After starting the program, send:

> Create two teammates to build a single-file index.html counter in collab-test/ under the workspace, with increment, decrement and reset buttons; one implements it and one independently reviews it, using task dependencies and assignment tools, send_message for the review result, artifact acceptance and teammate shutdown when done; do not install dependencies, add documentation or test frameworks, or poll status, and finish with the artifact location and actual inspection results.

Paths are relative to the selected workspace. If no browser was opened, the agents should not claim browser testing took place. The default prompts request Chinese responses; change the language guidance in `LEAD_PROMPT` for English conversations.

## Files, limits and next steps

```text
main.py          # Entire implementation, 16 foldable sections and Chinese comments
README.md        # Chinese documentation
README.en.md     # English documentation
SOURCE_MAP.md    # Mapping to the teaching code
.env.example     # Environment variable template
workspace/       # Created as needed; not committed
  .tasks/        # JSON task records
  .mailboxes/    # JSONL inboxes
  delivery.json  # Delivery record
```

- A working directory is not an OS sandbox. Shell access can run other commands. On Windows, the `bash` tool uses CMD syntax.
- Tasks and inboxes are persisted; chat history, summaries and teammate threads do not resume automatically after restart. Use separate workspaces for fresh tasks.
- Delivery checks validate files and report structure; they do not prove that reported commands ran or guarantee artifact quality.
- Persistent memory, MCP and hard per-task request/token budgets remain unimplemented.

## Attribution and license

Adapted from the **learn-claude-code s13** tool loops and team structure, with compaction ideas from s08. The original MIT license and copyright notice are preserved in [LICENSE](LICENSE).
