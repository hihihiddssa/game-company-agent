# Multi-Agent Team Framework

[简体中文](README.md) | **English**

A Python agent project built around an integrated learn-claude-code-style runtime: **model → tool calls → tool results → next model turn**. Tasks, teams, memory, skills, background shell work and scheduling live in a single `main.py`.

The current host follows the s15 integrated structure, with the required s09 memory implementation inlined. A clone no longer needs a sibling course checkout.

## Current capabilities

| Module | Implemented behavior |
| --- | --- |
| Agent loop | Rebuilds the system prompt and tool pool, executes tools and feeds back results |
| Tasks and teams | File-backed dependencies, claiming and completion; persistent teammates, messages, approval and shutdown protocols |
| Focused subagent | The `task` tool completes a focused assignment and returns a final summary |
| Persistent memory | Markdown records with YAML metadata, an index, recall, extraction and consolidation |
| Skills | Discovers `skills/<directory>/SKILL.md`; exposes a catalog and loads full content on demand |
| Context handling | Persists large output, trims results, archives history, summarizes and recovers from oversized prompts |
| Background work | Runs `bash(run_in_background=true)` in a thread and returns notifications |
| Cron | Five-field schedules, one-shot/recurring jobs and durable records; requires the process to be running |
| Recovery | Bounded backoff, 429/529 handling, optional fallback model and output escalation/continuation |
| Teaching MCP interface | **Only in-process `docs` / `deploy` mocks**, demonstrating discovery, namespacing and host authorization |

There is no real remote or stdio MCP transport yet. Mock deployment tools do not deploy a service.

## Setup and startup

Requires Python 3.10+. Activate your preferred environment; no interpreter path is hardcoded.

Windows PowerShell:

```powershell
git clone https://github.com/hihihiddssa/multi-agent-team-framework.git
cd multi-agent-team-framework
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Dependencies are `anthropic`, `python-dotenv` and `PyYAML`. For DeepSeek, edit `.env`:

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key
MODEL_ID=deepseek-flash
```

When only `DEEPSEEK_API_KEY` is set, the program selects `https://api.deepseek.com/anthropic`. Alternatively, configure an Anthropic-compatible endpoint:

```dotenv
ANTHROPIC_API_KEY=your_api_key
ANTHROPIC_BASE_URL=https://your-provider.example/anthropic
MODEL_ID=your_supported_model
# Optional; must be supported by the same endpoint:
# FALLBACK_MODEL_ID=your_fallback_model
```

`ANTHROPIC_API_KEY` takes precedence when both keys are set. For Anthropic's official service, omit the base URL and choose a supported model. This revision uses SDK request parameters directly; the previous wrapper that forced thinking off is no longer present.

```powershell
python main.py
```

Enter a task at `team >>`. Use `q`, `exit` or an empty line to quit. In VS Code, select your environment with **Python: Select Interpreter**.

**The current working directory is the workspace.** To isolate a run:

```powershell
New-Item -ItemType Directory -Force workspace/demo | Out-Null
Set-Location workspace/demo
python ../../main.py
```

The script still loads the project's `.env`; task, memory, skill and output paths follow the working directory. The old `--workdir`, `--env-file`, `--check-config` and `--check-api` arguments are not provided by this revision.

## Loop and collaboration

```text
User input / scheduled work / team events / background results
  → inject messages
  → prepare context
  → recall memory and refresh skills, MCP and team context
  → assemble system prompt and tool pool
  → call the model with recovery
  → execute tool_use and append tool_result
  → repeat; extract persistent memories and finish when no tool calls remain
```

An asynchronous event loop triggers Lead turns for background events, coordinated with user turns through a lock. Ordinary tool calls in one response run sequentially; background commands and persistent teammates can run in separate threads.

The current team prompt requires **proposing a small team and waiting for user confirmation before spawning persistent teammates**. This is a prompt rule, not a separate confirmation state machine. No roster is predefined. Typical task setup:

```text
create_task → runtime-generated IDs → update_task dependencies
            → spawn_teammate(name, role, prompt, task_id)
```

Spawning claims a ready initial task automatically; teammates can also claim ready work from the board. The previous `assign_task`, `accept_delivery` and `Task.assignee` interfaces are absent. Task completion, final replies and thread shutdown remain separate events; Lead uses `request_shutdown` to coordinate exit.

## Tool pool

`BUILTIN_TOOLS` declares schemas and `BUILTIN_HANDLERS` maps functions. `assemble_tool_pool()` merges connected mock MCP tools on every turn. The loop handles `compact` specially.

| Category | Tools |
| --- | --- |
| Files and shell | `bash`, `read_file`, `write_file`, `edit_file`, `glob` |
| Session helpers | `todo_write`, `task`, `load_skill`, `compact` |
| Tasks | `create_task`, `update_task`, `list_tasks`, `get_task`, `claim_task`, `complete_task` |
| Persistent teams | `spawn_teammate`, `list_teammates`, `send_message`, `request_shutdown`, `request_plan`, `review_plan` |
| Scheduling and directories | `schedule_cron`, `list_crons`, `cancel_cron`, `create_worktree` |
| Mock MCP | `connect_mcp`, then namespaced `mcp__docs__*` / `mcp__deploy__*` tools |

The focused `task` subagent gets five basic tools. Persistent teammates have their own tool set inside their creation logic.

Shell calls on the main thread ask `Allow? [y/N]`. Asynchronous turns cannot ask interactive questions and reject shell or MCP operations requiring confirmation. Shell calls have a 120-second timeout and use CMD syntax on Windows. These checks and a working directory are not an OS sandbox.

## Bundled Skills, Memory and MCP

The four course skills (`agent-builder`, `code-review`, `mcp-builder`, `pdf`) are included, together with the agent-builder reference files and initialization script. Full skill text is loaded on demand; the returned directory identifies the base for relative file references. Restart after editing skills. Optional tools shown inside skills need their own dependencies.

Memory from s09 is embedded in `main.py` and wired into recall, extraction and consolidation. MCP keeps the local s15 mocks and tool-pool integration. See [the integration guide](docs/EXTENSIONS.md) for data flow, examples and the boundary between demonstrations and real services. Runtime state and personal memories from the course are not copied.

## Memory, context and usage

The memory index is `.memory/MEMORY.md`; record types are user, feedback, project and reference. Recall selects relevant records and can fall back to keyword matching when model selection fails. Turn completion attempts durable extraction, rejects temporary/duplicate records and can consolidate records after a threshold. These steps can make additional paid model calls; they are not a free cache.

Current parameters are constants near the top of `main.py`:

| Constant | Current value / purpose |
| --- | --- |
| `DEFAULT_MAX_TOKENS` | `8000`, normal output allowance |
| `ESCALATED_MAX_TOKENS` | `16000`, retry allowance after truncation |
| `MAX_RETRIES` | `3`, error retry attempts |
| `MAX_RECOVERY_RETRIES` | `2`, truncation continuation recoveries |
| `CONTEXT_LIMIT` | `50000`, estimated serialized message length, **not exact tokens** |
| `KEEP_RECENT_TOOL_RESULTS` | `3`, recent results retained |
| `PERSIST_THRESHOLD` | `30000`, large output persistence threshold |

These docs reflect the 8000/16000 settings in the new code. The old `AGENT_MAX_OUTPUT_TOKENS` and `CONTEXT_*` environment settings no longer control these constants. Bounded retries and continuation recoveries are not a hard limit on an entire task's model calls or cost.

Large output goes to `.task_outputs/tool-results/`; archived history goes to `.transcripts/`. Context may be summarized or recovered after a prompt-too-long error, keeping the active request authoritative and summaries as reference state.

## Runtime files and navigation

```text
main.py                 # Integrated host with inlined memory
README.md / README.en.md
SOURCE_MAP.md           # Current function/module locations
requirements.txt
.env.example
skills/<name>/SKILL.md  # Four bundled course skills; scanned in the workspace
.memory/               # Persistent records and index
.tasks/ / .mailboxes/   # Task board and inboxes
.task_outputs/         # Large tool output
.transcripts/          # History archives
.scheduled_tasks.json  # Durable schedule records
```

Runtime state, secrets and caches are ignored by Git. Chat history and live threads do not resume as a complete session; persistent memory and durable jobs are separate recovery mechanisms. Scheduled work still requires the Python process, and mock MCP is not a production integration.

Read `agent_loop()` → `call_llm()` → `assemble_system_prompt()` / `assemble_tool_pool()`, then memory, `start_background_task()`, `spawn_teammate_thread()` and the scheduler. See [SOURCE_MAP.md](SOURCE_MAP.md) for exact positions.

## Attribution and license

The host is based on **learn-claude-code s15 Integrated Harness**, with memory from s09. Original comments and MIT copyright notices are retained; see [LICENSE](LICENSE).
