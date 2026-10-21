#!/usr/bin/env python3
"""
Game Company Agent — adapted directly from learn-claude-code s13.

运行：python main.py（默认读取本项目的 DeepSeek .env）
核心循环：client.messages.create -> tool_use -> execute_tool -> tool_result。
Lead 和每个队友都调用模型，不包含固定游戏模板或 DemoExecutor。

保留原版 Task 文件、MessageBus、持久队友、Hooks、计划审批、可选 worktree
以及 Windows msvcrt 文件锁和终端输入支持。游戏公司新增处标记为 [Game Studio]。
原项目 MIT 授权见 LICENSE。
"""

import argparse
import json
import os
import random
import re
import secrets
import select
import subprocess
import sys
import threading
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import readline

    readline.parse_and_bind("set bind-tty-special-chars off")
except ImportError:
    pass

import urllib.error
import urllib.request
from anthropic import Anthropic
from dotenv import load_dotenv

# -- DeepSeek API client --
# 直接复用原版 SDK 的消息格式，下面熟悉的 agent_loop 和队友 work() 保持原样。

class DeepSeekClient:
    """复用原 Anthropic SDK 的消息格式，让原来的 Agent Loop 保持不变。

    请求实际发给 DeepSeek，不使用 Anthropic 的模型或余额。
    Lead 和三个队友共用此客户端，各自维护独立的 messages 历史。
    """

    def __init__(self, api_key: str, timeout: float = 120):
        if not api_key:
            raise ValueError("Missing DEEPSEEK_API_KEY")
        self._api_key = api_key
        # 显式指定地址和认证，避免旧终端的 Anthropic 配置影响当前项目。
        self._client = Anthropic(
            api_key=api_key,
            base_url="https://api.deepseek.com/anthropic",
            timeout=timeout, max_retries=0,
        )
        self.messages = self

    def create(self, **kwargs):
        """原封不动传递 system、messages 和 tools，仅固定关闭深度思考。

        回复仍是原代码使用的 text/tool_use 内容块，无需协议转换。
        不额外排队限速；超时或余额不足直接报告，避免后台反复请求。
        """
        kwargs["thinking"] = {"type": "disabled"}
        try:
            return self._client.messages.create(**kwargs)
        except Exception as exc:
            raise RuntimeError(str(exc).replace(self._api_key, "[REDACTED]")) from None

    def check_model(self, model: str):
        """只读官方模型列表，检查密钥及所选模型；不生成文本。"""
        request = urllib.request.Request(
            "https://api.deepseek.com/models",
            headers={"Authorization": "Bearer " + self._api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                models = json.load(response).get("data", [])
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"DeepSeek model check failed: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(str(exc).replace(self._api_key, "[REDACTED]")) from None
        if not any(item.get("id") == model for item in models):
            raise RuntimeError("DeepSeek model is not available: " + model)
        return {"name": model}


# [Game Studio] 导入模块不会加载凭据或请求模型；CLI 启动时再配置。
PROJECT_ROOT = Path(__file__).resolve().parent
WORKDIR = Path(os.getenv("GAME_STUDIO_WORKDIR", PROJECT_ROOT / "workspace")).resolve()
client = None
MODEL = ""
STUDIO_ROLES = {
    "designer": "设计游戏规则、关卡和验收标准，写入 design/game_design.md；不修改游戏实现。",
    "programmer": "依据设计编写和修复可运行游戏，代码放 game/，提供运行入口与说明。",
    "tester": "编写并实际执行测试，报告 Bug 和复现步骤；不修复实现，不虚构测试结果。",
}

# -- Task System --

TASKS_DIR = WORKDIR / ".tasks"
TASKS_ROOT = TASKS_DIR.resolve()
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
task_lock = threading.RLock()
TASK_LOCK_PATH = TASKS_DIR / ".lock"
_task_store_state = threading.local()

# owner -> {"task_id": str, "cwd": Path}. A teammate gets one assignment at
# a time, and every filesystem tool resolves its cwd through this registry.
teammate_assignments: dict[str, dict[str, object]] = {}
assignment_versions: dict[str, int] = {}


@contextmanager
def task_store_lock():
    """用可重入线程锁和跨进程文件锁保护任务存储临界区。

    输入：
        无显式输入参数；所需状态由当前对象或模块保存。

    输出：
        生成器：进入上下文时执行 yield 之前的步骤，退出时执行后续清理逻辑。

    执行流程：
        1. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。

    注意：
        可能创建、改写或删除文件系统记录；实际路径和发生条件见实现中的 mkdir 调用。
        涉及文件句柄；读取、追加或覆盖模式以 open() 的参数为准。
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        原有实现说明（保留）：
            Serialize task mutations across threads and host processes.
        Windows 使用 msvcrt 字节锁，其他平台使用 fcntl.flock；仅最外层嵌套调用获取文件锁。
    """
    with task_lock:
        depth = getattr(_task_store_state, "depth", 0)
        if depth == 0:
            # 创建保存task的地方 之后保存eg: task_XXX1.json task_XXX2.json ...
            TASKS_DIR.mkdir(parents=True, exist_ok=True)
            
            handle = TASK_LOCK_PATH.open("a+b")
            try:
                if os.name == "nt":
                    # Windows locks a byte range; every process locks byte 0.
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                handle.close()
                raise
            _task_store_state.handle = handle
        _task_store_state.depth = depth + 1
        try:
            yield
        finally:
            _task_store_state.depth -= 1
            if _task_store_state.depth == 0:
                handle = _task_store_state.handle
                try:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
                    del _task_store_state.handle


def advance_assignment_version(owner: str):
    """推进指定成员的分配版本，区分旧任务与新任务的协议消息。

    输入：
        owner (str)：任务认领者或成员名称。

    输出：
        None：不返回业务结果；通过状态更新、事件、输出或异常体现执行结果。

    执行流程：
        1. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        原有实现说明（保留）：
            Invalidate old approvals without clearing an explicit plan requirement.
    """
    with task_lock:
        assignment_versions[owner] = assignment_versions.get(owner, 0) + 1
        gates = globals().get("plan_gates")
        request_ids = globals().get("plan_request_ids")
        team = globals().get("team_lock")
        if team is not None:
            team.acquire()
        try:
            if (
                isinstance(gates, dict)
                and owner in gates
                and gates[owner] != "not_required"
            ):
                gates[owner] = "required"
            if isinstance(request_ids, dict):
                request_ids.pop(owner, None)
        finally:
            if team is not None:
                team.release()


@dataclass#数据类装饰器 自动生成初始化
class Task:#创建的“任务”的数据结构
    id: str  # "taks_XXXX"的唯一标识符
    subject: str  # "开发贪吃蛇游戏"
    description: str  # "任务的详细描述"
    status: str  # pending | in_progress | completed
    owner: str | None  # "agent" | "designer" | "programmer" | "tester" | None
    blockedBy: list[str]  # 依赖的任务ID列表，当前任务在这些任务完成之前无法开始
    worktree: str | None = None  # 绑定的工作树名称，任务在特定的工作树中执行
    assignee: str | None = None  # [Game Studio] Lead 派发后，只有指定队友能领取。


def _task_path(task_id: str) -> Path:
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError(f"Invalid task ID: {task_id!r}")
    path = (TASKS_DIR / f"{task_id}.json").resolve()
    if not TASKS_ROOT.is_relative_to(WORKDIR.resolve()) or not path.is_relative_to(
        TASKS_ROOT
    ):
        raise ValueError(f"Invalid task ID: {task_id!r}")
    return path


def create_task(subject: str, description: str = "") -> Task:
    subject = subject.strip()
    if not subject:
        raise ValueError("Task subject cannot be empty")
    with task_store_lock():
        for _ in range(100):
            task = Task(
                id=f"task_{secrets.token_hex(4)}",
                subject=subject,
                description=description,
                status="pending",
                owner=None,
                blockedBy=[],
            )
            try:
                with _task_path(task.id).open("x", encoding="utf-8") as handle:
                    json.dump(asdict(task), handle, indent=2)
                return task
            except FileExistsError:
                continue
    raise RuntimeError("Could not allocate a unique task ID")


def _task_depends_on(task_id: str, target_id: str) -> bool:
    """Return whether task_id transitively depends on target_id."""
    pending = [task_id]
    visited = set()
    while pending:
        current = pending.pop()
        if current == target_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(load_task(current).blockedBy)
    return False


def update_task(task_id: str, addBlockedBy: list[str]) -> Task:
    """Add dependency edges after create_task has returned real task IDs."""
    if not isinstance(addBlockedBy, list):
        raise ValueError("addBlockedBy must be a list of task IDs")

    with task_store_lock():
        task = load_task(task_id)
        if (
            task.status != "pending"
            or task.owner is not None
            or task.assignee is not None
        ):
            raise ValueError(
                f"Task {task_id} dependencies can only be updated while "
                "pending and unowned"
            )

        dependencies = list(dict.fromkeys(addBlockedBy))
        for dependency in dependencies:
            if dependency == task_id:
                raise ValueError("Task cannot depend on itself")
            if not _task_path(dependency).is_file():
                raise ValueError(f"Dependency not found: {dependency}")
            if dependency not in task.blockedBy and _task_depends_on(
                dependency, task_id
            ):
                raise ValueError(
                    f"Dependency cycle detected: {task_id} -> {dependency}"
                )

        task.blockedBy.extend(
            dependency
            for dependency in dependencies
            if dependency not in task.blockedBy
        )
        save_task(task)
        return task


def save_task(task: Task):
    with task_store_lock():
        path = _task_path(task.id)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def load_task(task_id: str) -> Task:
    with task_lock:
        data = json.loads(_task_path(task_id).read_text(encoding="utf-8"))
        task = Task(**data)
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        if task.status not in {"pending", "in_progress", "completed"}:
            raise ValueError(f"Invalid task status: {task.status}")
        return task


def list_tasks() -> list[Task]:
    with task_lock:
        if not TASKS_DIR.exists():
            return []
        if not TASKS_ROOT.is_relative_to(WORKDIR.resolve()):
            raise ValueError("Tasks directory escapes workspace")
        return [load_task(path.stem) for path in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """Return full task details as JSON."""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """Check if all blockedBy dependencies are completed.
    Missing dependencies are treated as blocked."""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        try:
            dep_path = _task_path(dep_id)
        except ValueError:
            return False
        if not dep_path.exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def _owner_in_progress(owner: str) -> Task | None:
    return next(
        (
            task
            for task in list_tasks()
            if task.status == "in_progress" and task.owner == owner
        ),
        None,
    )


def _incomplete_dependencies(task: Task) -> list[str]:
    incomplete = []
    for dep_id in task.blockedBy:
        try:
            dep_path = _task_path(dep_id)
        except ValueError:
            incomplete.append(dep_id)
            continue
        if not dep_path.exists() or load_task(dep_id).status != "completed":
            incomplete.append(dep_id)
    return incomplete


def claim_task(task_id: str, owner: str = "agent") -> str:
    """Atomically claim one task and bind the owner's filesystem cwd."""
    with task_store_lock():
        task = load_task(task_id)
        # [Game Studio] 未派发的任务不能被队友提前领走；Lead 仍可自行领取任务。
        if owner != "agent" and task.assignee != owner:
            return f"Task {task_id} is assigned to {task.assignee!r}, not {owner}"
        if owner == "agent" and task.assignee is not None:
            return f"Task {task_id} is reserved for {task.assignee}"
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if task.owner:
            return f"Task {task_id} is already owned by {task.owner}"
        assignment = teammate_assignments.get(owner)
        if assignment:
            return (
                f"Owner {owner} must finish the current work turn for "
                f"{assignment['task_id']} before claiming another task"
            )
        current = _owner_in_progress(owner)
        if current:
            return (
                f"Owner {owner} must complete {current.id} before claiming another task"
            )
        if not can_start(task_id):
            return f"Blocked by: {_incomplete_dependencies(task)}"
        cwd, error = task_worktree_cwd(task)
        if error:
            return f"Cannot claim {task_id}: {error}"
        task.owner = owner
        task.status = "in_progress"
        save_task(task)
        teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        advance_assignment_version(owner)
    print(f"  [claim] {task.subject} -> in_progress (owner: {owner})")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str, owner: str = "agent") -> str:
    """Complete an assignment only when the caller owns it."""
    with task_store_lock():
        task = load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        if task.owner != owner:
            return (
                f"Task {task_id} is owned by {task.owner}, not {owner}; cannot complete"
            )
        gate = globals().get("plan_gates", {}).get(owner, "not_required")
        if gate in {"required", "pending", "rejected"}:
            return f"Task {task_id} cannot complete while plan status is {gate}"
        assignment = teammate_assignments.get(owner)
        if not assignment or assignment.get("task_id") != task.id:
            cwd, error = task_worktree_cwd(task)
            if error:
                return f"Task {task_id} cannot complete: {error}"
            teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        task.status = "completed"
        save_task(task)
        unblocked = [
            t.subject
            for t in list_tasks()
            if t.status == "pending" and t.blockedBy and can_start(t.id)
        ]
    print(f"  [complete] {task.subject}")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  [unblocked] {', '.join(unblocked)}")
    return msg


# -- Task-bound Worktrees --

WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_ROOT = WORKTREES_DIR.resolve()
VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_worktree_name(name: str) -> str | None:
    """校验工作树名称是否满足命名和安全约束。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。

    输出：
        str | None：无效时返回错误原因，通过时返回 None。

    执行流程：
        1. 检查条件 not isinstance(name, str) or not VALID_WORKTREE_NAME.fullmatch(name)，按结果处理对应分支。
        2. 条件满足时，返回 'worktree name must be 1-64 letters, digits, dots, underscores, or dashes, and start with a letter or digit'，结束本次调用。
        3. 检查条件 name in {'.', '..'} or '..' in name，按结果处理对应分支。
        4. 条件满足时，返回 "worktree name cannot contain '..'"，结束本次调用。
        5. 返回 None，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    if not isinstance(name, str) or not VALID_WORKTREE_NAME.fullmatch(name):
        return (
            "worktree name must be 1-64 letters, digits, dots, "
            "underscores, or dashes, and start with a letter or digit"
        )
    if name in {".", ".."} or ".." in name:
        return "worktree name cannot contain '..'"
    return None


def _worktree_path(name: str) -> Path:
    """根据名称生成并检查工作区内的 Git worktree 路径。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。

    输出：
        Path：解析、生成或写入后对应的路径对象。

    执行流程：
        1. 调用 (WORKTREES_DIR / name).resolve，将结果保存到 path。
        2. 检查条件 not WORKTREES_ROOT.is_relative_to(WORKDIR.resolve()) or not path.is_relative_to(WORKTREES_ROOT) or path == WORKTRE…，按结果处理对应分支。
        3. 条件满足时，抛出 ValueError(f'Worktree path escapes directory: {name!r}')。
        4. 返回 path，结束本次调用。

    注意：
        实现中会显式抛出：ValueError；触发条件见对应检查分支。
    """
    path = (WORKTREES_DIR / name).resolve()
    if (
        not WORKTREES_ROOT.is_relative_to(WORKDIR.resolve())
        or not path.is_relative_to(WORKTREES_ROOT)
        or path == WORKTREES_ROOT
    ):
        raise ValueError(f"Worktree path escapes directory: {name!r}")
    return path


def _worktree_branch(name: str) -> str:
    """将工作树名称映射为课程使用的 Git 分支名称。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 返回 f'wt/{name}'，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    return f"wt/{name}"


def _run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """在指定目录运行 Git 命令，并返回成功标志和输出文本。

    输入：
        args (list[str])：调用参数；其结构由当前工具、Git 操作或工作流约定。
        cwd (Path | None)：执行或路径解析目录；None 时按实现使用默认工作目录。 默认值：None。

    输出：
        tuple[bool, str]：包含多个结果字段的元组，各分量按实现顺序返回。

    执行流程：
        1. 调用 subprocess.run，将结果保存到 result。
        2. 遇到 (OSError, subprocess.TimeoutExpired) 时进入对应 except 分支。
        3. 调用 (result.stdout + result.stderr).strip，将结果保存到 output。
        4. 返回 (result.returncode == 0, output or '(no output)')，结束本次调用。

    注意：
        可能启动线程或子进程；结束与清理方式以本函数及调用方的约定为准。
        只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
        原有实现说明（保留）：
            Run Git without shell interpolation and preserve machine output.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or WORKDIR,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output or "(no output)"


def run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """在工作树相关互斥保护下调用 Git 操作。

    输入：
        args (list[str])：调用参数；其结构由当前工具、Git 操作或工作流约定。
        cwd (Path | None)：执行或路径解析目录；None 时按实现使用默认工作目录。 默认值：None。

    输出：
        tuple[bool, str]：包含多个结果字段的元组，各分量按实现顺序返回。

    执行流程：
        1. 调用 _run_git，将结果保存到 (ok, output)。
        2. 返回 (ok, output[:5000])，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        原有实现说明（保留）：
            Run Git and bound only the text returned to the model.
    """
    ok, output = _run_git(args, cwd)
    return ok, output[:5000]


def _registered_worktrees() -> tuple[dict[Path, dict[str, str]], str | None]:
    """解析 Git 已登记的工作树列表及其属性。

    输入：
        无显式输入参数；所需状态由当前对象或模块保存。

    输出：
        tuple[dict[Path, dict[str, str]], str | None]：包含多个结果字段的元组，各分量按实现顺序返回。

    执行流程：
        1. 调用 _run_git，将结果保存到 (ok, output)。
        2. 检查条件 not ok，按结果处理对应分支。
        3. 条件满足时，返回 ({}, f'cannot read Git worktree registry: {output}')，结束本次调用。
        4. 设置 entries，取值为 {}。
        5. 设置 current，取值为 {}。
        6. 遍历 output.splitlines() + ['']，逐项处理 line。
        7. 返回 (entries, None)，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
    """
    ok, output = _run_git(["worktree", "list", "--porcelain"])
    if not ok:
        return {}, f"cannot read Git worktree registry: {output}"
    entries: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in output.splitlines() + [""]:
        if not line:
            raw_path = current.get("worktree")
            if raw_path:
                entries[Path(raw_path).resolve()] = current
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return entries, None


def _registered_worktree(name: str) -> tuple[Path | None, str | None]:
    """查找并核验指定名称对应的已登记工作树。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。

    输出：
        tuple[Path | None, str | None]：包含多个结果字段的元组，各分量按实现顺序返回。

    执行流程：
        1. 调用 _worktree_path，将结果保存到 path。
        2. 遇到 ValueError 时进入对应 except 分支。
        3. 调用 _registered_worktrees，将结果保存到 (entries, error)。
        4. 检查条件 error，按结果处理对应分支。
        5. 条件满足时，返回 (None, error)，结束本次调用。
        6. 检查条件 path not in entries，按结果处理对应分支。
        7. 条件满足时，返回 (None, f"worktree '{name}' is not registered with Git")，结束本次调用。
        8. 检查条件 not path.is_dir()，按结果处理对应分支。
        9. 条件满足时，返回 (None, f"worktree '{name}' is missing at {path}")，结束本次调用。
        10. 设置 expected_branch，取值为 f'refs/heads/{_worktree_branch(name)}'。
        11. 检查条件 entries[path].get('branch') != expected_branch，按结果处理对应分支。
        12. 条件满足时，返回 (None, f"worktree '{name}' is not registered on expected branch '{_worktree_branch(name)}'")，结束本次调用。
        13. 返回 (path, None)，结束本次调用。

    注意：
        只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
    """
    try:
        path = _worktree_path(name)
    except ValueError as exc:
        return None, str(exc)
    entries, error = _registered_worktrees()
    if error:
        return None, error
    if path not in entries:
        return None, f"worktree '{name}' is not registered with Git"
    if not path.is_dir():
        return None, f"worktree '{name}' is missing at {path}"
    expected_branch = f"refs/heads/{_worktree_branch(name)}"
    if entries[path].get("branch") != expected_branch:
        return None, (
            f"worktree '{name}' is not registered on expected "
            f"branch '{_worktree_branch(name)}'"
        )
    return path, None


def task_worktree_cwd(task: Task) -> tuple[Path, str | None]:
    """根据任务绑定信息取得执行目录或工作树错误提示。

    输入：
        task (Task)：当前任务对象；具体字段由所属课程的数据类定义。

    输出：
        tuple[Path, str | None]：任务执行目录及可选的错误说明。

    执行流程：
        1. 检查条件 not task.worktree，按结果处理对应分支。
        2. 条件满足时，返回 (WORKDIR, None)，结束本次调用。
        3. 调用 _registered_worktree，将结果保存到 (path, error)。
        4. 返回 (path or WORKDIR, error)，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        原有实现说明（保留）：
            Resolve a task cwd, failing closed for broken worktree bindings.
    """
    if not task.worktree:
        return WORKDIR, None
    path, error = _registered_worktree(task.worktree)
    return (path or WORKDIR), error


def assignment_cwd(owner: str) -> Path:
    """解析指定成员当前任务应使用的执行目录。

    输入：
        owner (str)：任务认领者或成员名称。

    输出：
        Path：解析、生成或写入后对应的路径对象。

    执行流程：
        1. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        实现中会显式抛出：ValueError；触发条件见对应检查分支。
    """
    with task_lock:
        assignment = teammate_assignments.get(owner)
        task = _owner_in_progress(owner)
        if task and (not assignment or assignment.get("task_id") != task.id):
            cwd, error = task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            assignment = {"task_id": task.id, "cwd": cwd}
            teammate_assignments[owner] = assignment
        elif not assignment:
            return WORKDIR
        task = load_task(str(assignment["task_id"]))
        if task.status not in {"in_progress", "completed"} or task.owner != owner:
            raise ValueError(f"Assignment for {owner} is no longer active")
        cwd, error = task_worktree_cwd(task)
        if error:
            raise ValueError(error)
        if cwd.resolve() != Path(assignment["cwd"]).resolve():
            raise ValueError(f"Assignment cwd changed for task {task.id}")
        return cwd


def release_completed_assignment(owner: str) -> bool:
    """在当前任务已完成时释放成员的任务分配。

    输入：
        owner (str)：任务认领者或成员名称。

    输出：
        bool：按函数中的判断条件返回 True 或 False。
        固定返回示例：False；True。

    执行流程：
        1. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        原有实现说明（保留）：
            Release a completed cwd lease only at a model turn boundary.
    """
    with task_lock:
        assignment = teammate_assignments.get(owner)
        if not assignment:
            return False
        task = load_task(str(assignment["task_id"]))
        if task.status != "completed" or task.owner != owner:
            return False
        teammate_assignments.pop(owner, None)
        advance_assignment_version(owner)
        if owner in globals().get("plan_gates", {}):
            globals()["plan_gates"][owner] = "not_required"
        return True


def release_teammate_assignment(owner: str):
    """释放成员占用的任务分配并同步相关运行时状态。

    输入：
        owner (str)：任务认领者或成员名称。

    输出：
        None：不返回业务结果；通过状态更新、事件、输出或异常体现执行结果。

    执行流程：
        1. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        原有实现说明（保留）：
            Return abandoned teammate work to the task board on thread exit.
    """
    with task_lock:
        try:
            task = _owner_in_progress(owner)
            if task:
                task.status = "pending"
                task.owner = None
                save_task(task)
        finally:
            teammate_assignments.pop(owner, None)
            advance_assignment_version(owner)
            if owner in globals().get("plan_gates", {}):
                globals()["plan_gates"][owner] = "not_required"


def create_worktree(name: str, task_id: str) -> str:
    """为已认领任务创建或绑定独立 Git 工作树。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。
        task_id (str)：本次操作关联的任务 ID。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。
        固定返回示例：'Error: Working directory must be the root of a Git repository'。

    执行流程：
        1. 调用 validate_worktree_name，将结果保存到 error。
        2. 检查条件 error，按结果处理对应分支。
        3. 条件满足时，返回 f'Error: {error}'，结束本次调用。
        4. 调用 _worktree_path，将结果保存到 path。
        5. 调用 _task_path，将结果保存到 task_path。
        6. 遇到 ValueError 时进入对应 except 分支。
        7. 调用 _worktree_branch，将结果保存到 branch。
        8. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        9. 调用 print(f'  \\x1b[33m[worktree] created: {name} at {p…)。
        10. 返回 f"Worktree '{name}' created at {path} for task {task_id}"，结束本次调用。

    注意：
        可能创建、改写或删除文件系统记录；实际路径和发生条件见实现中的 mkdir 调用。
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        执行时会向终端打印状态、结果或调试信息。
        只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
        原有实现说明（保留）：
            Create and bind a dedicated worktree after all inputs validate.
    """
    error = validate_worktree_name(name)
    if error:
        return f"Error: {error}"
    try:
        path = _worktree_path(name)
        task_path = _task_path(task_id)
    except ValueError as exc:
        return f"Error: {exc}"
    branch = _worktree_branch(name)

    with task_lock:
        if not task_path.exists():
            return f"Error: Task {task_id} not found"
        task = load_task(task_id)
        if task.status != "pending" or task.owner is not None:
            return f"Error: Task {task_id} must be pending and unowned"
        if task.worktree:
            return f"Error: Task {task_id} already uses worktree '{task.worktree}'"
        if any(t.worktree == name for t in list_tasks() if t.id != task_id):
            return f"Error: Worktree '{name}' is already bound to another task"
        if path.exists():
            return f"Error: Worktree path already exists: {path}"

        ok, root = run_git(["rev-parse", "--show-toplevel"])
        if not ok or Path(root).resolve() != WORKDIR.resolve():
            return "Error: Working directory must be the root of a Git repository"
        ok, branch_check = run_git(["check-ref-format", "--branch", branch])
        if not ok:
            return f"Error: Invalid worktree branch '{branch}': {branch_check}"
        exists, _ = run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        if exists:
            return f"Error: Branch '{branch}' already exists"
        entries, registry_error = _registered_worktrees()
        if registry_error:
            return f"Error: {registry_error}"
        if path in entries:
            return f"Error: Worktree path is already registered: {path}"

        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        ok, result = run_git(["worktree", "add", "-b", branch, str(path), "HEAD"])
        if not ok:
            entries, registry_error = _registered_worktrees()
            branch_exists, _ = run_git(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
            )
            artifacts = []
            if path.exists():
                artifacts.append(f"checkout path '{path}'")
            if registry_error is None and path in entries:
                artifacts.append("registered Git worktree")
            if branch_exists:
                artifacts.append(f"branch '{branch}'")
            if artifacts:
                return (
                    "Partial operation: git worktree add reported an error "
                    f"after leaving {', '.join(artifacts)}. Task {task_id} "
                    "remains unbound and no Git data was deleted. Run "
                    f"`git worktree list`, inspect '{path}' and '{branch}', "
                    "then keep or remove those artifacts manually after "
                    f"preserving any work. Git error: {result}"
                )
            return f"Git error: {result}"

        try:
            task.worktree = name
            save_task(task)
        except Exception as exc:
            return (
                f"Partial success: Worktree '{name}' was created at "
                f"{path} on branch '{branch}', but task binding failed: "
                f"{exc}. Git data was retained for manual recovery."
            )

    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f"Worktree '{name}' created at {path} for task {task_id}"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """根据保留修改策略移除 Git 工作树并清理绑定关系。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。
        discard_changes (bool)：是否明确允许丢弃工作树中的修改。 默认值：False。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 调用 validate_worktree_name，将结果保存到 error。
        2. 检查条件 error，按结果处理对应分支。
        3. 条件满足时，返回 f'Error: {error}'，结束本次调用。
        4. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        5. 调用 print(f'  [worktree] removed: {name}; branch retai…)。
        6. 返回 f"Worktree '{name}' removed; branch '{_worktree_branch(name)}' retained"，结束本次调用。

    注意：
        可能删除工作树目录及关联分支；是否允许丢弃修改由 discard_changes 和实现中的检查决定。
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        执行时会向终端打印状态、结果或调试信息。
        只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
        原有实现说明（保留）：
            Remove a registered checkout while always retaining its branch.
    """
    error = validate_worktree_name(name)
    if error:
        return f"Error: {error}"

    with task_lock:
        path, error = _registered_worktree(name)
        if error:
            return f"Error: {error}"
        bound = [task for task in list_tasks() if task.worktree == name]
        if not bound:
            return f"Error: Worktree '{name}' is not bound to a task"
        active = [task for task in bound if task.status != "completed"]
        if active:
            return (
                f"Error: Worktree '{name}' is bound to active task "
                f"{active[0].id}; complete it before removal"
            )
        leased = [
            owner
            for owner, assignment in teammate_assignments.items()
            if Path(assignment["cwd"]).resolve() == path.resolve()
        ]
        if leased:
            return (
                f"Error: Worktree '{name}' is still in use by "
                f"{', '.join(sorted(leased))}; wait for the turn to end"
            )
        ok, status = run_git(["status", "--porcelain", "--ignored"], cwd=path)
        if not ok:
            return f"Error: Cannot verify worktree '{name}' status: {status}"
        if status != "(no output)" and not discard_changes:
            changed = len([line for line in status.splitlines() if line.strip()])
            return (
                f"Error: Worktree '{name}' has {changed} uncommitted "
                "change(s); preserve or discard them manually"
            )

        args = ["worktree", "remove"]
        if discard_changes:
            args.append("--force")
        args.append(str(path))
        ok, result = run_git(args)
        if not ok:
            return f"Git error: {result}"

        try:
            for task in bound:
                task.worktree = None
                save_task(task)
        except Exception as exc:
            return (
                f"Partial success: Worktree '{name}' was removed and "
                f"branch '{_worktree_branch(name)}' retained, but task "
                f"unbinding failed: {exc}. Manual recovery is required."
            )

    print(f"  [worktree] removed: {name}; branch retained")
    return f"Worktree '{name}' removed; branch '{_worktree_branch(name)}' retained"


# -- System Prompt --

ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
# [Game Studio] 分工和任务图由模型决定，Python 不预建固定开发流水线。
PROMPT_SECTIONS = {
    "identity": f"你是游戏公司的 Lead Agent。使用工具推进工作，使用中文与用户交流。环境：{ENVIRONMENT_PROMPT}。",
    "tasks": (
        "先理解需求与验收标准，存在关键歧义时向用户澄清。Create all task nodes first; "
        "use runtime-generated IDs with update_task to add dependencies. "
        "只有 Lead 能维护任务图。所有节点和依赖建立完毕后，再用 assign_task 派发。"
        "已派发的任务不能再改依赖；需要变更时创建新任务。不要在同一批工具调用里引用尚未返回的任务ID。"
    ),
    "teams": (
        "用户已授权一个由 designer、programmer、tester 组成的团队，无需重复确认建队。"
        "使用 spawn_teammate 创建这三个队友，name 和 role 使用相同的英文角色名。"
        "队友可不带初始任务启动；收到 ready 的派发任务后会自动领取。每个角色最多一个持久队友。"
        "设计师负责规则与关卡；程序员依据设计写游戏；测试员独立测试并报告 Bug。"
        "根据需求安排依赖与并行度。只有所有前置任务完成后才能开始后续任务。"
        "每次 spawn 后不要轮询等待；安排好团队后结束当前轮次，运行时会通过收件箱事件再次唤醒你。"
        "result、idle_notification、error 分别是工作结果、空闲状态和错误，不能混同。"
        "测试不通过时创建 programmer 修复任务和依赖它的 tester 复测任务，最多三轮后向用户说明阻塞。"
        "计划审批和 request_shutdown 沿用原版协议。结束后 request_shutdown 关闭所有队友。"
        "worktree 只是可选工作目录，不是沙箱；默认所有任务使用共享工作区，按文件职责避免冲突。"
    ),
    "delivery": (
        "优先构建可本地运行的浏览器游戏，除非用户指定其他引擎。不要复制固定的演示游戏。"
        "所有产物写在工作区：design/ 设计、game/ 游戏、tests/ 测试、reports/ 报告。"
        "程序员写 game/README.md，明确启动命令和操作方式。"
        "测试员实际执行测试，写 reports/qa.json，结构为 "
        '{"task_id":"测试任务ID","passed":true,"bugs":[],"checks":'
        '[{"name":"检查名","command":"实际执行命令","returncode":0,"output":"执行结果"}],'
        '"manual_playtest":"not_run 或实际体验说明"}。'
        "QA 即使发现 Bug 也应完成报告任务，并通过结果明确测试失败；完成任务不等于通过测试。"
        "只有完整任务图全部完成、报告通过且游戏入口存在时，调用 accept_game，之后向用户交付路径。"
        "未进行浏览器或人工体验必须如实说明，不得把文件存在等同于实际可玩性验证。"
    ),
    "workspace": f"Working directory: {WORKDIR}",
}

SYSTEM = "\n\n".join(PROMPT_SECTIONS.values())


# -- Base Tools --


def safe_path(p: str, cwd: Path | None = None) -> Path:
    base = (cwd or WORKDIR).resolve()
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, cwd: Path | None = None, timeout: float = 120) -> str:
    """执行非交互 Shell 命令，返回输出；超时结束整个子进程树。

    command 是模型提供的命令；cwd 默认 WORKDIR；timeout 默认 120 秒。
    模型仍通过原来的 bash 工具调用此函数，Agent Loop 的协议不变。
    stdin 使用 DEVNULL，子程序不能偷偷读取用户正在给 Lead 输入的文字。
    npm 默认不自动安装缺失的包：缺依赖时返回错误；模型若决定安装须明确使用 --yes。
    每等待 15 秒打印进度，避免命令正在执行却没有任何可见状态。
    Windows 超时时使用 taskkill /T 结束整棵命令树，不能只杀 cmd.exe：
    npm 等子进程可能继续持有输出管道，让 communicate 再次无限等待。
    """
    if timeout <= 0:
        return "Error: command timeout must be positive"
    started = time.monotonic()
    tool_env = dict(os.environ)
    tool_env.setdefault("npm_config_yes", "false")
    print(f"  [shell] start (limit {timeout:g}s): {command[:160]}", flush=True)
    process = None
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd or WORKDIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=tool_env,
            start_new_session=(os.name != "nt"),
        )
        deadline = started + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                # 多次 communicate 会继续收集同一进程的输出，不会重新执行命令。
                stdout, stderr = process.communicate(timeout=min(15, remaining))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
                print(f"  [shell] still running: {time.monotonic() - started:.0f}s", flush=True)
        output = (stdout + stderr).strip()
        output = output[:50000] if output else "(no output)"
        print(f"  [shell] finished: exit={process.returncode}, {time.monotonic() - started:.1f}s", flush=True)
        if process.returncode:
            return f"Error: command exited with status {process.returncode}\n{output}"
        return output
    except subprocess.TimeoutExpired:
        # 显式清理后，再有界收尾；不使用 subprocess.run 的无界 Windows 管道等待。
        if process is not None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
            except (OSError, subprocess.TimeoutExpired):
                # 父进程可能已经结束；仍尝试结束当前句柄，不再无限等待输出。
                if process.poll() is None:
                    process.kill()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                print("  [shell] a descendant still holds the output pipe; stopped waiting", flush=True)
        print(f"  [shell] timeout after {timeout:g}s; process-tree cleanup attempted", flush=True)
        return (
            f"Error: Timeout ({timeout:g}s). Process-tree cleanup attempted. "
            "Commands must be non-interactive. Check missing dependencies or report a testing blocker."
        )
    except OSError as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def run_read(path: str, limit: int | None = None, cwd: Path | None = None) -> str:
    try:
        lines = safe_path(path, cwd).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path | None = None) -> str:
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str, cwd: Path | None = None) -> str:
    try:
        target = safe_path(path, cwd)
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count != 1:
            return f"Error: Expected 1 occurrence, found {count}"
        target.write_text(content.replace(old_text, new_text), encoding="utf-8")
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_glob(pattern: str, cwd: Path | None = None) -> str:
    try:
        base = (cwd or WORKDIR).resolve()
        matches = [
            str(path.relative_to(base))
            for path in sorted(base.glob(pattern))
            if path.resolve().is_relative_to(base)
        ]
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) or "No files found"
    except Exception as exc:
        return f"Error: {exc}"


def _agent_cwd() -> tuple[Path | None, str | None]:
    try:
        return assignment_cwd("agent"), None
    except (FileNotFoundError, ValueError) as exc:
        return None, f"Error: Invalid task assignment: {exc}"


def run_agent_bash(command: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_bash(command, cwd)


def run_agent_read(path: str, limit: int | None = None) -> str:
    cwd, error = _agent_cwd()
    return error or run_read(path, limit, cwd)


def run_agent_write(path: str, content: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_write(path, content, cwd)


def run_agent_edit(path: str, old_text: str, new_text: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_edit(path, old_text, new_text, cwd)


def run_agent_glob(pattern: str) -> str:
    cwd, error = _agent_cwd()
    return error or run_glob(pattern, cwd)


# -- Task Tools --


def run_create_task(subject: str, description: str = "") -> str:
    task = create_task(subject, description)
    print(f"  \033[34m[create] {task.subject}\033[0m")
    return f"Created {task.id}: {task.subject}"


def run_update_task(task_id: str, addBlockedBy: list[str]) -> str:
    try:
        task = update_task(task_id, addBlockedBy)
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"
    dependencies = ", ".join(task.blockedBy) or "(none)"
    print(f"  \033[34m[update] {task.subject} blockedBy: {dependencies}\033[0m")
    return f"Updated {task.id} blockedBy: {dependencies}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}.get(
            t.status, "[?]"
        )
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        worktree = f" (worktree: {t.worktree})" if t.worktree else ""
        worktree += f" (assignee: {t.assignee})" if t.assignee else " (not dispatched)"
        lines.append(
            f"  {icon} {t.id}: {t.subject} [{t.status}]{owner}{deps}{worktree}"
        )
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    try:
        return claim_task(task_id, owner="agent")
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_complete_task(task_id: str) -> str:
    try:
        return complete_task(task_id, owner="agent")
    
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


# -- MessageBus and Team Protocols --


MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_ROOT = MAILBOX_DIR.resolve()
VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESERVED_TEAMMATE_NAMES = {"lead", "agent"}


def is_valid_agent_name(name: str) -> bool:
    """检查 Agent 名称是否满足邮箱与成员命名约束。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。

    输出：
        bool：名称通过成员命名校验时返回 True，否则返回 False。

    执行流程：
        1. 返回 bool(VALID_AGENT_NAME.fullmatch(name))，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    return bool(VALID_AGENT_NAME.fullmatch(name))


class MessageBus:
    """Thread-safe file mailboxes with destructive reads."""

    def __init__(self):
        """初始化文件邮箱消息总线的互斥锁与等待通知机制。

        输入：
            无显式输入参数；所需状态由当前对象或模块保存。

        输出：
            None：不返回业务结果；通过状态更新、事件、输出或异常体现执行结果。

        执行流程：
            1. 调用 threading.RLock，将结果保存到 self._lock。
            2. 调用 threading.Condition，将结果保存到 self._changed。

        注意：
            会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        """
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    def _path(self, agent: str) -> Path:
        """校验成员名称并生成对应邮箱文件的路径。

        输入：
            agent (str)：要访问邮箱的成员名称。

        输出：
            Path：解析、生成或写入后对应的路径对象。

        执行流程：
            1. 检查条件 not is_valid_agent_name(agent)，按结果处理对应分支。
            2. 条件满足时，抛出 ValueError(f'Invalid mailbox recipient: {agent!r}')。
            3. 调用 (MAILBOX_DIR / f'{agent}.jsonl').resolve，将结果保存到 path。
            4. 检查条件 not path.is_relative_to(MAILBOX_ROOT)，按结果处理对应分支。
            5. 条件满足时，抛出 ValueError(f'Mailbox path escapes directory: {agent!r}')。
            6. 返回 path，结束本次调用。

        注意：
            实现中会显式抛出：ValueError；触发条件见对应检查分支。
        """
        if not is_valid_agent_name(agent):
            raise ValueError(f"Invalid mailbox recipient: {agent!r}")
        path = (MAILBOX_DIR / f"{agent}.jsonl").resolve()
        if not path.is_relative_to(MAILBOX_ROOT):
            raise ValueError(f"Mailbox path escapes directory: {agent!r}")
        return path

    def _read_unlocked(self, agent: str) -> list[dict]:
        """读取并领取指定成员的邮箱消息，供持锁调用者使用。

        输入：
            agent (str)：要访问邮箱的成员名称。

        输出：
            list[dict]：按实现收集或生成的结果列表。

        执行流程：
            1. 调用 self._path，将结果保存到 inbox。
            2. 检查条件 not inbox.exists()，按结果处理对应分支。
            3. 条件满足时，返回 []，结束本次调用。
            4. 设置 msgs，取值为 [json.loads(line) for line in inbox.read_text(encoding='utf-8').splitlines() if line.strip()]。
            5. 调用 inbox.unlink()。
            6. 返回 msgs，结束本次调用。

        注意：
            可能创建、改写或删除文件系统记录；实际路径和发生条件见实现中的 unlink 调用。
        """
        inbox = self._path(agent)
        if not inbox.exists():
            return []
        msgs = [
            json.loads(line)
            for line in inbox.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        inbox.unlink()
        return msgs

    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        msg_type: str = "message",
        metadata: dict | None = None,
    ):
        """把带发送方、接收方和类型信息的消息写入目标邮箱。

        输入：
            from_agent (str)：消息发送方名称。
            to_agent (str)：消息接收方名称。
            content (str)：要写入文件、发送给成员或解析的内容。
            msg_type (str)：邮箱消息的协议类型。 默认值：'message'。
            metadata (dict | None)：附加的请求关联或身份信息。 默认值：None。

        输出：
            None：不返回业务结果；通过状态更新、事件、输出或异常体现执行结果。

        执行流程：
            1. 设置 msg，取值为 {'from': from_agent, 'to': to_agent, 'content': content, 'type': msg_type, 'ts': time.time(), 'meta…。
            2. 进入 self._changed 上下文，执行受保护的操作并在退出时释放上下文资源。
            3. 调用 print(f'  [bus] {from_agent} -> {to_agent}: ({msg_…)。

        注意：
            可能创建、改写或删除文件系统记录；实际路径和发生条件见实现中的 mkdir 调用。
            涉及文件句柄；读取、追加或覆盖模式以 open() 的参数为准。
            会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
            执行时会向终端打印状态、结果或调试信息。
        """
        msg = {
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "ts": time.time(),
            "metadata": metadata or {},
        }
        with self._changed:
            MAILBOX_DIR.mkdir(parents=True, exist_ok=True)
            with self._path(to_agent).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(msg, ensure_ascii=True) + "\n")
            self._changed.notify_all()
        print(f"  [bus] {from_agent} -> {to_agent}: ({msg_type}) {content[:50]}")

    def read_inbox(self, agent: str) -> list[dict]:
        """在邮箱互斥保护下读取并领取指定成员消息。

        输入：
            agent (str)：要访问邮箱的成员名称。

        输出：
            list[dict]：按实现收集或生成的结果列表。

        执行流程：
            1. 进入 self._lock 上下文，执行受保护的操作并在退出时释放上下文资源。
            2. 返回 self._read_unlocked(agent)，结束本次调用。

        注意：
            本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        """
        with self._lock:
            return self._read_unlocked(agent)

    def peek(self, agent: str) -> bool:
        """检查指定邮箱是否包含尚未领取的消息。

        输入：
            agent (str)：要访问邮箱的成员名称。

        输出：
            bool：按函数中的判断条件返回 True 或 False。

        执行流程：
            1. 进入 self._lock 上下文，执行受保护的操作并在退出时释放上下文资源。
            2. 返回 inbox.exists() and inbox.stat().st_size > 0，结束本次调用。

        注意：
            本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        """
        with self._lock:
            inbox = self._path(agent)
            return inbox.exists() and inbox.stat().st_size > 0

    def wait_for_messages(self, agent: str, timeout: float | None = None) -> list[dict]:
        """等待邮箱通知，领取消息或在超时后返回。

        输入：
            agent (str)：要访问邮箱的成员名称。
            timeout (float | None)：等待超时秒数；None 时按实现持续等待。 默认值：None。

        输出：
            list[dict]：按实现收集或生成的结果列表。

        执行流程：
            1. 设置 deadline，取值为 None if timeout is None else time.monotonic() + timeout。
            2. 进入 self._changed 上下文，执行受保护的操作并在退出时释放上下文资源。
            3. 在 not self.peek(agent) 成立时循环，按 break、返回或异常条件结束。
            4. 返回 self._read_unlocked(agent)，结束本次调用。

        注意：
            本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
            原有实现说明（保留）：
                Block until the agent has messages or timeout expires.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(agent)


BUS = MessageBus()

# working | waiting_approval | idle | stopping
active_teammates: dict[str, str] = {}
plan_gates: dict[str, str] = {}
plan_request_ids: dict[str, str] = {}
team_lock = threading.RLock()


@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    work_version: int | None = None
    task_id: str | None = None
    created_at: float = field(default_factory=time.time)


pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    """生成团队协议请求的唯一关联 ID。

    输入：
        无显式输入参数；所需状态由当前对象或模块保存。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 在 True 成立时循环，按 break、返回或异常条件结束。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    while True:
        request_id = f"req_{random.randint(0, 999999):06d}"
        if request_id not in pending_requests:
            return request_id


def match_response(
    response_type: str, request_id: str, approve: bool, from_agent: str, to_agent: str
) -> bool:
    """核对协议响应的类型、请求 ID、双方身份及审批结果。

    输入：
        response_type (str)：预期或实际的协议响应类型。
        request_id (str)：用于关联请求和响应的协议 ID。
        approve (bool)：是否同意当前计划或协议请求。
        from_agent (str)：消息发送方名称。
        to_agent (str)：消息接收方名称。

    输出：
        bool：按函数中的判断条件返回 True 或 False。
        固定返回示例：False；True。

    执行流程：
        1. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        2. 调用 print(f'  [protocol] {request_id} -> {state.status…)。
        3. 返回 True，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        执行时会向终端打印状态、结果或调试信息。
        原有实现说明（保留）：
            Match one protocol response to one pending request.
    """
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            print(f"  [protocol] unknown request_id: {request_id}")
            return False
        expected = {
            "shutdown": "shutdown_response",
            "plan_approval": "plan_approval_response",
        }[state.type]
        if response_type != expected:
            print(f"  [protocol] expected {expected}, got {response_type}")
            return False
        if from_agent != state.target or to_agent != state.sender:
            print(f"  [protocol] {request_id} responder mismatch")
            return False
        if state.status != "pending":
            print(f"  [protocol] {request_id} already {state.status}")
            return False
        state.status = "approved" if approve else "rejected"
    print(f"  [protocol] {request_id} -> {state.status}")
    return True


def consume_lead_inbox() -> list[dict]:
    """领取主 Agent 邮箱消息，并按实现处理协议状态。

    输入：
        无显式输入参数；所需状态由当前对象或模块保存。

    输出：
        list[dict]：按实现收集或生成的结果列表。

    执行流程：
        1. 调用 BUS.read_inbox，将结果保存到 msgs。
        2. 遍历 msgs，逐项处理 msg。
        3. 返回 msgs，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        原有实现说明（保留）：
            Consume Lead events and update protocol state before model delivery.
    """
    msgs = BUS.read_inbox("lead")
    for msg in msgs:
        metadata = msg.get("metadata", {})
        request_id = metadata.get("request_id", "")
        if request_id and msg.get("type", "").endswith("_response"):
            match_response(
                msg["type"],
                request_id,
                metadata.get("approve", False),
                msg.get("from", ""),
                msg.get("to", ""),
            )
    return msgs


def format_team_events(msgs: list[dict]) -> str:
    """将团队消息整理为可注入模型上下文的事件文本。

    输入：
        msgs (list[dict])：待格式化的团队消息列表。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 设置 lines，取值为 []。
        2. 遍历 msgs，逐项处理 msg。
        3. 返回 '[Team events]\\n' + '\\n'.join(lines)，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
    """
    lines = []
    for msg in msgs:
        metadata = msg.get("metadata", {})
        request_id = metadata.get("request_id")
        suffix = f" request_id={request_id}" if request_id else ""
        lines.append(f"[{msg['type']}{suffix}] {msg['from']}: {msg['content']}")
    return "[Team events]\n" + "\n".join(lines)


def _last_assistant_text(content) -> str:
    """从助手内容块中拼接普通文本，忽略非文本内容。

    输入：
        content：要写入文件、发送给成员或解析的内容。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。
        固定返回示例：''。

    执行流程：
        1. 遍历 content，逐项处理 block。
        2. 返回 ''，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", "")).strip()
    return ""


def current_work_identity(owner: str) -> tuple[int, str | None]:
    """读取成员当前的分配版本与任务 ID，用于识别过期消息。

    输入：
        owner (str)：任务认领者或成员名称。

    输出：
        tuple[int, str | None]：当前分配版本与可选任务 ID。

    执行流程：
        1. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        2. 返回 (assignment_versions.get(owner, 0), task_id)，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    with task_lock:
        assignment = teammate_assignments.get(owner)
        task_id = str(assignment["task_id"]) if assignment else None
        return assignment_versions.get(owner, 0), task_id


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """为成员提交计划建立待审批请求并通知主 Agent。

    输入：
        from_name (str)：当前发送消息或提交计划的成员名称。
        plan (str)：成员提交的计划文本。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。
        固定返回示例：'A plan is already waiting for review.'。

    执行流程：
        1. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        2. 调用 BUS.send(from_name, 'lead', plan)。
        3. 返回 f"Plan submitted ({request_id}). Wait for Lead's decision."，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
    """
    with task_lock:
        assignment = teammate_assignments.get(from_name)
        task_id = str(assignment["task_id"]) if assignment else None
        work_version = assignment_versions.get(from_name, 0)
        with team_lock:
            if plan_gates.get(from_name) == "pending":
                return "A plan is already waiting for review."
            request_id = new_request_id()
            pending_requests[request_id] = ProtocolState(
                request_id=request_id,
                type="plan_approval",
                sender=from_name,
                target="lead",
                status="pending",
                payload=plan,
                work_version=work_version,
                task_id=task_id,
            )
            plan_gates[from_name] = "pending"
            plan_request_ids[from_name] = request_id
            active_teammates[from_name] = "waiting_approval"
    BUS.send(
        from_name, "lead", plan, "plan_approval_request", {"request_id": request_id}
    )
    return f"Plan submitted ({request_id}). Wait for Lead's decision."


def _run_teammate_tool(name: str, block, handlers: dict) -> str:
    """校验成员执行约束后，调用相应的工具处理函数。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。
        block：模型返回的工具调用块，包含工具名称与输入参数。
        handlers (dict)：工具名称到处理函数的映射。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 调用 plan_gates.get，将结果保存到 gate。
        2. 检查条件 block.name in {'bash', 'write_file', 'edit_file'}，按结果处理对应分支。
        3. 调用 handlers.get，将结果保存到 handler。
        4. 检查条件 not handler，按结果处理对应分支。
        5. 条件满足时，返回 f'Unknown tool: {block.name}'，结束本次调用。
        6. 调用 trigger_hooks('PreToolUse', block)。
        7. 调用 str，将结果保存到 output。
        8. 遇到 Exception 时进入对应 except 分支。
        9. 调用 trigger_hooks('PostToolUse', block, output)。
        10. 返回 output，结束本次调用。

    注意：
        只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
    """
    gate = plan_gates.get(name, "not_required")
    if block.name in {"bash", "write_file", "edit_file"}:
        if gate != "approved":
            if gate != "not_required":
                return (
                    f"Blocked: plan status is {gate}. Submit or revise the "
                    "plan and wait for approval before changing the workspace."
                )
        blocked = check_permission(block, prompt_user=False)
        if blocked:
            return blocked
    handler = handlers.get(block.name)
    if not handler:
        return f"Unknown tool: {block.name}"
    trigger_hooks("PreToolUse", block, skip_permission=True)
    try:
        output = str(handler(**block.input))
    except Exception as exc:
        output = f"Error: {type(exc).__name__}: {exc}"
    trigger_hooks("PostToolUse", block, output)
    return output


def apply_plan_response(name: str, msg: dict) -> tuple[bool, str]:
    """核验计划审批响应，并更新成员的审批状态。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。
        msg (dict)：一条团队协议消息。

    输出：
        tuple[bool, str]：是否应用响应，以及处理结果说明。

    执行流程：
        1. 调用 msg.get，将结果保存到 metadata。
        2. 调用 metadata.get，将结果保存到 request_id。
        3. 调用 current_work_identity，将结果保存到 (work_version, task_id)。
        4. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        5. 返回 (True, f"[Plan {outcome}] {msg['content']}")，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        原有实现说明（保留）：
            Apply only the Lead response for this teammate's current plan.
    """
    metadata = msg.get("metadata", {})
    request_id = metadata.get("request_id", "")
    work_version, task_id = current_work_identity(name)
    with team_lock:
        state = pending_requests.get(request_id)
        expected_id = plan_request_ids.get(name)
        valid = (
            msg.get("from") == "lead"
            and msg.get("to") == name
            and request_id == expected_id
            and state is not None
            and state.type == "plan_approval"
            and state.sender == name
            and state.target == "lead"
            and state.work_version == work_version
            and state.task_id == task_id
            and state.status in {"approved", "rejected"}
            and metadata.get("approve", False) == (state.status == "approved")
        )
        if not valid:
            return False, "[Ignored plan response: request mismatch]"
        plan_gates[name] = state.status
        active_teammates[name] = "working"
        plan_request_ids.pop(name, None)
        outcome = state.status
    return True, f"[Plan {outcome}] {msg['content']}"


def apply_shutdown_request(name: str, msg: dict) -> tuple[bool, str]:
    """处理关闭请求，并返回是否退出及相关说明。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。
        msg (dict)：一条团队协议消息。

    输出：
        tuple[bool, str]：是否满足关闭条件，以及处理说明。

    执行流程：
        1. 调用 msg.get('metadata', {}).get，将结果保存到 request_id。
        2. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        3. 返回 (True, request_id)，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        原有实现说明（保留）：
            Accept only a pending shutdown request sent by Lead to this teammate.
    """
    request_id = msg.get("metadata", {}).get("request_id", "")
    with team_lock:
        state = pending_requests.get(request_id)
        valid = (
            msg.get("from") == "lead"
            and msg.get("to") == name
            and state is not None
            and state.type == "shutdown"
            and state.sender == "lead"
            and state.target == name
            and state.status == "pending"
            and active_teammates.get(name) != "stopping"
        )
        if not valid:
            return False, "[Ignored shutdown request: request mismatch]"
        active_teammates[name] = "stopping"
    return True, request_id


def _teammate_send_message(from_name: str, to: str, content: str) -> str:
    """以指定成员身份向另一个 Agent 发送普通消息。

    输入：
        from_name (str)：当前发送消息或提交计划的成员名称。
        to (str)：消息接收方名称。
        content (str)：要写入文件、发送给成员或解析的内容。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        2. 调用 BUS.send(from_name, to, content)。
        3. 返回 f'Sent to {to}'，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    with team_lock:
        if to != "lead" and to not in active_teammates:
            return f"Agent '{to}' is not active"
    BUS.send(from_name, to, content)
    return f"Sent to {to}"


# -- Idle Task Discovery --

IDLE_SCAN_INTERVAL = 2.0


def scan_unclaimed_tasks() -> list[Task]:
    """筛选尚未认领且依赖已满足的任务。

    输入：
        无显式输入参数；所需状态由当前对象或模块保存。

    输出：
        list[Task]：按实现收集或生成的结果列表。

    执行流程：
        1. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        2. 遍历 list_tasks()，逐项处理 task。
        3. 返回 ready，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        原有实现说明（保留）：
            Return ready tasks whose optional worktree binding is usable.
    """
    with task_lock:
        ready = []
        for task in list_tasks():
            if (
                task.status != "pending"
                or task.owner is not None
                or not can_start(task.id)
            ):
                continue
            _, error = task_worktree_cwd(task)
            if not error:
                ready.append(task)
        return ready


def claim_next_task(name: str) -> Task | None:
    """为指定成员寻找并认领下一个可执行任务。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。

    输出：
        Task | None：由当前分支产生的结果；含 None 的联合类型表示该分支可不返回对象。

    执行流程：
        1. 进入 task_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        2. 遍历 scan_unclaimed_tasks()，逐项处理 task。
        3. 返回 None，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        原有实现说明（保留）：
            Claim the first still-available task, never a second assignment.
    """
    with task_lock:
        if teammate_assignments.get(name) or _owner_in_progress(name):
            return None
    for task in scan_unclaimed_tasks():
        if task.assignee != name:  # [Game Studio] 防止设计师抢走编程或测试任务。
            continue
        result = claim_task(task.id, owner=name)
        if result.startswith("Claimed "):
            return load_task(task.id)
    return None


# -- Teammate Runtime --


class TeammateRuntime:
    """One persistent teammate with separate messages and WORK/IDLE phases."""

    def __init__(
        self, name: str, role: str, prompt: str, task_id: str | None, require_plan: bool
    ):
        """保存成员身份、初始任务和计划审批状态，准备成员运行环境。

        输入：
            name (str)：当前操作涉及的成员、服务、工具或工作流名称。
            role (str)：新成员的角色描述。
            prompt (str)：交给模型或定时任务执行的提示文本。
            task_id (str | None)：本次操作关联的任务 ID。
            require_plan (bool)：是否要求成员先提交计划并等待审批。

        输出：
            None：不返回业务结果；通过状态更新、事件、输出或异常体现执行结果。

        执行流程：
            1. 设置 self.name，取值为 name。
            2. 设置 self.system，取值为 f"You are '{name}', a {role}. Use tools to complete the assigned Task, then call complete_task and …。
            3. 设置 self.messages，取值为 [{'role': 'user', 'content': prompt}]。
            4. 检查条件 task_id，按结果处理对应分支。
            5. 检查条件 require_plan，按结果处理对应分支。
            6. 设置 self.handlers，取值为 {'bash': self.bash, 'read_file': self.read, 'write_file': self.write, 'edit_file': self.edit, 'glob…。

        注意：
            会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        """
        self.name = name
        self.system = (
            f"You are '{name}', a {role}. Use tools to complete the assigned "
            "Task, then call complete_task and report a concise result. "
            "If the first user message contains [Assigned task], that Task is "
            "already claimed; do not call claim_task for it again. "
            "When asked for a plan, call submit_plan and wait for approval "
            "before bash or file changes. File and shell tools use the Task's "
            "working directory; that directory is not a sandbox. The runtime "
            "delivers your final text to Lead. Use send_message only for "
            "intermediate coordination, and address the coordinator as 'lead'."
        )
        # [Game Studio] 身份/上下文仍由原版每个 TeammateRuntime 独立保存。
        self.system += (
            "\n游戏公司职责："
            + STUDIO_ROLES[name]
            + "\n只处理 assignee 等于自己名字的任务。所有产物限制在当前工作区。"
            + PROMPT_SECTIONS["delivery"]
        )
        self.messages = [{"role": "user", "content": prompt}]
        if task_id:
            task = load_task(task_id)
            cwd = assignment_cwd(name)
            self.messages[0]["content"] += (
                f"\n\n[Assigned task {task.id}] {task.subject}\n"
                f"{task.description}\nWork directory: {cwd}"
            )
        if require_plan:
            self.messages[0]["content"] += (
                "\n\n[Plan required] Submit a plan and wait for Lead approval "
                "before changing files or using bash."
            )
        self.handlers = {
            "bash": self.bash,
            "read_file": self.read,
            "write_file": self.write,
            "edit_file": self.edit,
            "glob": self.glob,
            "send_message": lambda to, content: _teammate_send_message(
                name, to, content
            ),
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
            "list_tasks": run_list_tasks,
            "claim_task": self.claim,
            "complete_task": self.complete,
        }

    def current_cwd(self) -> tuple[Path | None, str | None]:
        """取得当前成员任务绑定的执行目录及可能的错误。

        输入：
            无显式输入参数；所需状态由当前对象或模块保存。

        输出：
            tuple[Path | None, str | None]：当前执行目录与可选的错误原因。

        执行流程：
            1. 检查条件 self.name not in teammate_assignments，按结果处理对应分支。
            2. 条件满足时，返回 (None, 'Error: Claim a Task before using workspace tools.')，结束本次调用。
            3. 返回 (assignment_cwd(self.name), None)，结束本次调用。
            4. 遇到 (FileNotFoundError, ValueError) 时进入对应 except 分支。

        注意：
            只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
        """
        if self.name not in teammate_assignments:
            return None, "Error: Claim a Task before using workspace tools."
        try:
            return assignment_cwd(self.name), None
        except (FileNotFoundError, ValueError) as exc:
            return None, f"Error: Invalid task assignment: {exc}"

    def bash(self, command: str) -> str:
        """在成员当前任务目录中执行 shell 命令。

        输入：
            command (str)：交给当前平台 shell 执行的命令文本。

        输出：
            str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

        执行流程：
            1. 调用 self.current_cwd，将结果保存到 (cwd, error)。
            2. 返回 error or run_bash(command, cwd=cwd)，结束本次调用。

        注意：
            本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        """
        cwd, error = self.current_cwd()
        return error or run_bash(command, cwd=cwd)

    def read(self, path: str, limit: int | None = None) -> str:
        """在成员当前任务目录中读取文本文件。

        输入：
            path (str)：目标文件或输出路径。
            limit (int | None)：最多读取的行数；None 表示不额外限制。 默认值：None。

        输出：
            str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

        执行流程：
            1. 调用 self.current_cwd，将结果保存到 (cwd, error)。
            2. 返回 error or run_read(path, limit=limit, cwd=cwd)，结束本次调用。

        注意：
            本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        """
        cwd, error = self.current_cwd()
        return error or run_read(path, limit=limit, cwd=cwd)

    def write(self, path: str, content: str) -> str:
        """在成员当前任务目录中写入文本文件。

        输入：
            path (str)：目标文件或输出路径。
            content (str)：要写入文件、发送给成员或解析的内容。

        输出：
            str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

        执行流程：
            1. 调用 self.current_cwd，将结果保存到 (cwd, error)。
            2. 返回 error or run_write(path, content, cwd=cwd)，结束本次调用。

        注意：
            本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        """
        cwd, error = self.current_cwd()
        return error or run_write(path, content, cwd=cwd)

    def edit(self, path: str, old_text: str, new_text: str) -> str:
        """在成员当前任务目录中替换文件的首个指定文本片段。

        输入：
            path (str)：目标文件或输出路径。
            old_text (str)：需要查找的原始文本。
            new_text (str)：用来替换首个匹配的新文本。

        输出：
            str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

        执行流程：
            1. 调用 self.current_cwd，将结果保存到 (cwd, error)。
            2. 返回 error or run_edit(path, old_text, new_text, cwd=cwd)，结束本次调用。

        注意：
            本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        """
        cwd, error = self.current_cwd()
        return error or run_edit(path, old_text, new_text, cwd=cwd)

    def glob(self, pattern: str) -> str:
        """在成员当前任务目录中搜索路径。

        输入：
            pattern (str)：路径匹配模式，支持当前实现的 glob 语法。

        输出：
            str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

        执行流程：
            1. 调用 self.current_cwd，将结果保存到 (cwd, error)。
            2. 返回 error or run_glob(pattern, cwd=cwd)，结束本次调用。

        注意：
            本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
        """
        cwd, error = self.current_cwd()
        return error or run_glob(pattern, cwd=cwd)

    def claim(self, task_id: str) -> str:
        """以当前成员身份认领任务，并同步分配状态。

        输入：
            task_id (str)：本次操作关联的任务 ID。

        输出：
            str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

        执行流程：
            1. 返回 claim_task(task_id, owner=self.name)，结束本次调用。
            2. 遇到 ValueError、FileNotFoundError 时进入对应 except 分支。

        注意：
            只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
        """
        try:
            return claim_task(task_id, owner=self.name)
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: Task {task_id} not found"

    def complete(self, task_id: str) -> str:
        """以当前成员身份完成任务，并同步成员运行状态。

        输入：
            task_id (str)：本次操作关联的任务 ID。

        输出：
            str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

        执行流程：
            1. 返回 complete_task(task_id, owner=self.name)，结束本次调用。
            2. 遇到 ValueError、FileNotFoundError 时进入对应 except 分支。

        注意：
            只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
        """
        try:
            return complete_task(task_id, owner=self.name)
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: Task {task_id} not found"

    def handle_inbox(self, inbox: list[dict]) -> bool:
        """处理成员收到的消息、计划审批与关闭协议。

        输入：
            inbox (list[dict])：已经领取的邮箱消息列表。

        输出：
            bool：按函数中的判断条件返回 True 或 False。
            固定返回示例：True；False。

        执行流程：
            1. 设置 work_messages，取值为 []。
            2. 遍历 inbox，逐项处理 msg。
            3. 检查条件 work_messages，按结果处理对应分支。
            4. 返回 False，结束本次调用。

        注意：
            会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
            原有实现说明（保留）：
                Append work messages and return True for a valid shutdown.
        """
        work_messages = []
        for msg in inbox:
            msg_type = msg.get("type", "message")
            if msg_type == "shutdown_request":
                accepted, notice = apply_shutdown_request(self.name, msg)
                if not accepted:
                    work_messages.append(notice)
                    continue
                BUS.send(
                    self.name,
                    "lead",
                    "Shutdown acknowledged.",
                    "shutdown_response",
                    {"request_id": notice, "approve": True},
                )
                return True
            if msg_type == "plan_approval_response":
                _, notice = apply_plan_response(self.name, msg)
                work_messages.append(notice)
                continue
            if msg_type == "plan_request":
                work_messages.append(f"[Plan required] {msg['content']}")
                continue
            work_messages.append(f"[Message from {msg['from']}] {msg['content']}")
        if work_messages:
            self.messages.append({"role": "user", "content": "\n".join(work_messages)})
        return False

    def work(self) -> str:
        """执行成员当前工作回合，在模型与工具之间循环推进任务。

        输入：
            无显式输入参数；所需状态由当前对象或模块保存。

        输出：
            str：成员当前工作回合的结果或停止原因。

        执行流程：
            1. 检查条件 self.handle_inbox(BUS.read_inbox(self.name))，按结果处理对应分支。
            2. 条件满足时，返回 'stop'，结束本次调用。
            3. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
            4. 调用 client.messages.create，将结果保存到 response。
            5. 遇到 Exception 时进入对应 except 分支。
            6. 调用 self.messages.append({'role': 'assistant', 'content': response.co…)。
            7. 设置 tool_calls，取值为 [block for block in response.content if block.type == 'tool_use']。
            8. 检查条件 tool_calls，按结果处理对应分支。
            9. 调用 _last_assistant_text，将结果保存到 summary。
            10. 调用 plan_gates.get，将结果保存到 gate。
            11. 检查条件 gate != 'pending' and summary，按结果处理对应分支。
            12. 检查条件 gate == 'pending'，按结果处理对应分支。
            13. 返回 'idle'，结束本次调用。

        注意：
            会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
            会发起模型 API 请求，响应速度、额度和鉴权受当前服务配置影响。
            只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
            原有实现说明（保留）：
                Run one model turn. Return continue, idle, or stop.
        """
        if self.handle_inbox(BUS.read_inbox(self.name)):
            return "stop"
        with team_lock:
            active_teammates[self.name] = "working"
        try:
            response = client.messages.create(
                model=MODEL,
                system=self.system,
                messages=self.messages,
                # 只能使用teammate工具集，不能使用其他工具
                tools=TEAMMATE_TOOLS,
                max_tokens=8000,
            )
        except Exception as exc:
            BUS.send(self.name, "lead", f"{type(exc).__name__}: {exc}", "error")
            return "stop"

        self.messages.append({"role": "assistant", "content": response.content})
        tool_calls = [block for block in response.content if block.type == "tool_use"]
        if tool_calls:
            results = []
            for block in tool_calls:
                output = _run_teammate_tool(self.name, block, self.handlers)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
            self.messages.append({"role": "user", "content": results})
            return "continue"

        # 如果agent_loop没有工具调用，则处理总结和门禁
        summary = _last_assistant_text(response.content)
        gate = plan_gates.get(self.name, "not_required")
        if gate != "pending" and summary:
            BUS.send(self.name, "lead", summary, "result")
        """eg:
        BUS.send(
            "alice",
            "lead",
            "前端页面已完成，后端接口也已实现，正在进行集成测试。",
            "result",
        )
        然后messageBus会把消息保存到.mailbox/lead.jsonl
        """
        if gate == "pending":
            with team_lock:
                active_teammates[self.name] = "waiting_approval"
        else:
            release_completed_assignment(self.name)
            # 不需要等待计划审批的正常流程 Alice接下来还会继续工作，Bob也会继续工作
            with team_lock:
                active_teammates[self.name] = "idle"
            BUS.send(self.name, "lead", "Waiting for more work.", "idle_notification")
        return "idle"

    def wait_for_work(self) -> bool:
        """在成员空闲时查找新任务或等待邮箱通知。

        输入：
            无显式输入参数；所需状态由当前对象或模块保存。

        输出：
            bool：成员是否继续工作，或根据关闭条件结束等待。

        执行流程：
            1. 在 True 成立时循环，按 break、返回或异常条件结束。

        注意：
            会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
            执行时会向终端打印状态、结果或调试信息。
            原有实现说明（保留）：
                Wait for a message or atomically claim the next ready Task.
        """
        while True:
            inbox = BUS.wait_for_messages(self.name, IDLE_SCAN_INTERVAL)
            if inbox:
                before = len(self.messages)
                if self.handle_inbox(inbox):
                    return False
                if len(self.messages) > before:
                    return True
                continue

            task = claim_next_task(self.name)
            if not task:
                continue
            cwd = assignment_cwd(self.name)
            self.messages.append(
                {
                    "role": "user",
                    "content": (
                        f"[Auto-claimed task {task.id}] {task.subject}\n"
                        f"{task.description}\nWork directory: {cwd}"
                    ),
                }
            )
            print(f"  [idle] {self.name} claimed {task.id}: {task.subject}")
            return True

    def run(self):
        """执行成员生命周期，在工作回合、空闲等待与关闭之间切换。

        输入：
            无显式输入参数；所需状态由当前对象或模块保存。

        输出：
            None：不返回业务结果；通过状态更新、事件、输出或异常体现执行结果。

        执行流程：
            1. 设置 state，取值为 'continue'。
            2. 在 state != 'stop' 成立时循环，按 break、返回或异常条件结束。
            3. 遇到 Exception 时进入对应 except 分支。f
            4. 无论正常结束或发生异常，都执行 finally 中的清理步骤。

        注意：
            会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
            执行时会向终端打印状态、结果或调试信息。
            只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
        """
        try:
            state = "continue"
            while state != "stop":
                if state == "idle" and not self.wait_for_work():
                    break
                # 假设是alice，那么就是alice.work() 进入alice的agent_loop
                state = self.work()
        except Exception as exc:
            try:
                BUS.send(self.name, "lead", f"{type(exc).__name__}: {exc}", "error")
            except Exception:
                pass
        finally:
            try:
                release_teammate_assignment(self.name)
            except Exception as exc:
                try:
                    BUS.send(
                        self.name,
                        "lead",
                        f"Assignment cleanup failed: {type(exc).__name__}: {exc}",
                        "error",
                    )
                except Exception:
                    pass
            with team_lock:
                active_teammates.pop(self.name, None)
                plan_gates.pop(self.name, None)
                plan_request_ids.pop(self.name, None)
                teammate_threads.pop(self.name, None)
            print(f"  [teammate] {self.name} finished")


teammate_threads: dict[str, threading.Thread] = {}


def spawn_teammate_thread(
    name: str,
    role: str,
    prompt: str,
    task_id: str | None = None,
    require_plan: bool = False,
) -> str:
    """校验成员参数、登记运行状态并启动成员工作线程。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。
        role (str)：新成员的角色描述。
        prompt (str)：交给模型或定时任务执行的提示文本。
        task_id (str | None)：本次操作关联的任务 ID。 默认值：None。
        require_plan (bool)：是否要求成员先提交计划并等待审批。 默认值：False。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。
        固定返回示例：'Invalid teammate name: use 1-64 letters, digits, underscores, or dashes'。

    执行流程：
        1. 检查条件 not is_valid_agent_name(name)，按结果处理对应分支。
        2. 条件满足时，返回 'Invalid teammate name: use 1-64 letters, digits, underscores, or dashes'，结束本次调用。
        3. 检查条件 name.lower() in RESERVED_TEAMMATE_NAMES，按结果处理对应分支。
        4. 条件满足时，返回 f"Invalid teammate name: '{name}' is reserved by the runtime"，结束本次调用。
        5. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        6. 检查条件 task_id，按结果处理对应分支。
        7. 调用 TeammateRuntime，将结果保存到 runtime。
        8. 调用 threading.Thread，将结果保存到 thread。
        9. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        10. 调用 thread.start()。
        11. 调用 print(f'  [teammate] {name} spawned as {role}')。
        12. 设置 assigned，取值为 f' for {task_id}' if task_id else ' without an initial Task'。
        13. 返回 f"Teammate '{name}' spawned as {role}{assigned}. End this turn; the runtime will deliver its events."，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
        可能启动线程或子进程；结束与清理方式以本函数及调用方的约定为准。
        执行时会向终端打印状态、结果或调试信息。
        只处理 except 中列出的异常；异常结果、重试或回退逻辑由对应分支决定。
        原有实现说明（保留）：
            Claim an initial Task, then start one persistent teammate.
    """
    # [Game Studio] 固定三个角色；工作如何拆解仍完全由模型决定。
    if name not in STUDIO_ROLES or role != name:
        return "Use name=role=designer, programmer, or tester."
    if not is_valid_agent_name(name):
        return "Invalid teammate name: use 1-64 letters, digits, underscores, or dashes"
    if name.lower() in RESERVED_TEAMMATE_NAMES:
        return f"Invalid teammate name: '{name}' is reserved by the runtime"
    with team_lock:
        if any(existing.casefold() == name.casefold() for existing in active_teammates):
            return f"Teammate '{name}' already exists"
        active_teammates[name] = "working"
        plan_gates[name] = "required" if require_plan else "not_required"
        assignment_versions[name] = 0

    if task_id:
        try:
            # 尝试认领任务 把状态pending->in_progress
            claimed = claim_task(task_id, owner=name)
        except (FileNotFoundError, ValueError) as exc:
            claimed = f"Error: {exc}"
        if not claimed.startswith("Claimed "):
            with team_lock:
                active_teammates.pop(name, None)
                plan_gates.pop(name, None)
                assignment_versions.pop(name, None)
            return f"Cannot spawn teammate '{name}': {claimed}"
    # 创建成员运行环境
    runtime = TeammateRuntime(name, role, prompt, task_id, require_plan)
    '''每创建一个团队成员 就会创建独立的teammateruntime对象 保存如下信息
    self.name = name
    self.system = "you are..."
    self.messages = [{"role": "user", "content": prompt}]
    '''
    # 启动成员线程 thread.start()启动成员循环
    thread = threading.Thread(target=runtime.run, daemon=True)
    '''成员创建后 假设三个成员都创建了
    avtive_teammates = {
        "designer": "working", 处于工作状态
        "programmer": "working", 处于工作状态
        "tester": "idle" 处于空闲状态
    }  
    '''
    with team_lock:
        teammate_threads[name] = thread
    thread.start()
    print(f"  [teammate] {name} spawned as {role}")
    assigned = f" for {task_id}" if task_id else " without an initial Task"
    return (
        f"Teammate '{name}' spawned as {role}{assigned}. "
        "End this turn; the runtime will deliver its events."
    )


# -- Lead Team Tools --


def run_spawn_teammate(
    name: str,
    role: str,
    prompt: str,
    task_id: str | None = None,
    require_plan: bool = False,
) -> str:
    """将模型工具参数转交给成员线程创建逻辑。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。
        role (str)：新成员的角色描述。
        prompt (str)：交给模型或定时任务执行的提示文本。
        task_id (str | None)：本次操作关联的任务 ID。 默认值：None。
        require_plan (bool)：是否要求成员先提交计划并等待审批。 默认值：False。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 返回 spawn_teammate_thread(name, role, prompt, task_id, require_plan)，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    return spawn_teammate_thread(name, role, prompt, task_id, require_plan)


def run_list_teammates() -> str:
    """将团队成员及其当前运行状态格式化为文本。

    输入：
        无显式输入参数；所需状态由当前对象或模块保存。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。
        固定返回示例：'No active teammates.'。

    执行流程：
        1. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        2. 返回 '\\n'.join((f'{name}: {status}' for name, status in sorted(active_teammates.items())))，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    with team_lock:
        if not active_teammates:
            return "No active teammates."
        return "\n".join(
            f"{name}: {status}" for name, status in sorted(active_teammates.items())
        )


def run_send_message(to: str, content: str) -> str:
    """以主 Agent 身份向指定成员发送消息。

    输入：
        to (str)：消息接收方名称。
        content (str)：要写入文件、发送给成员或解析的内容。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 检查条件 to not in active_teammates，按结果处理对应分支。
        2. 条件满足时，返回 f"Teammate '{to}' is not active"，结束本次调用。
        3. 调用 BUS.send('lead', to, content)。
        4. 返回 f'Sent to {to}'，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    if to not in active_teammates:
        return f"Teammate '{to}' is not active"
    BUS.send("lead", to, content)
    '''比如
    BUS.send(
        from_agent="lead",
        to="alice",
        content="游戏规则已完成 请查看game_design.md",
    )
    '''
    return f"Sent to {to}"


def run_request_shutdown(teammate: str) -> str:
    """创建关联请求并向成员发送关闭请求。

    输入：
        teammate (str)：目标团队成员的名称。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 检查条件 teammate not in active_teammates，按结果处理对应分支。
        2. 条件满足时，返回 f"Teammate '{teammate}' is not active"，结束本次调用。
        3. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        4. 调用 BUS.send('lead', teammate, 'Finish the current step and shut down.')。
        5. 返回 f'Shutdown requested from {teammate} ({request_id})'，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
    """
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        request_id = new_request_id()
        pending_requests[request_id] = ProtocolState(
            request_id=request_id,
            type="shutdown",
            sender="lead",
            target=teammate,
            status="pending",
            payload="",
        )
    BUS.send(
        "lead",
        teammate,
        "Finish the current step and shut down.",
        "shutdown_request",
        {"request_id": request_id},
    )
    return f"Shutdown requested from {teammate} ({request_id})"


def run_request_plan(teammate: str, task: str) -> str:
    """向指定成员发起与当前任务关联的计划请求。

    输入：
        teammate (str)：目标团队成员的名称。
        task (str)：当前任务对象；具体字段由所属课程的数据类定义。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 检查条件 teammate not in active_teammates，按结果处理对应分支。
        2. 条件满足时，返回 f"Teammate '{teammate}' is not active"，结束本次调用。
        3. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        4. 调用 BUS.send('lead', teammate, task)。
        5. 返回 f'Plan requested from {teammate}'，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
    """
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        plan_gates[teammate] = "required"
    BUS.send("lead", teammate, task, "plan_request")
    return f"Plan requested from {teammate}"


def run_review_plan(request_id: str, approve: bool, feedback: str = "") -> str:
    """核验待审批计划，并发送通过或拒绝的反馈。

    输入：
        request_id (str)：用于关联请求和响应的协议 ID。
        approve (bool)：是否同意当前计划或协议请求。
        feedback (str)：对计划审批或拒绝原因的补充文本。 默认值：''。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 调用 pending_requests.get，将结果保存到 state。
        2. 检查条件 not state，按结果处理对应分支。
        3. 条件满足时，返回 f'Request {request_id} not found'，结束本次调用。
        4. 调用 current_work_identity，将结果保存到 (work_version, task_id)。
        5. 进入 team_lock 上下文，执行受保护的操作并在退出时释放上下文资源。
        6. 设置 content，取值为 feedback or ('Plan approved.' if approve else 'Revise the plan and submit it again.')。
        7. 调用 BUS.send('lead', state.sender, content)。
        8. 返回 f'Plan {state.status} ({request_id})'，结束本次调用。

    注意：
        会按分支更新对象、集合或共享状态；调用时需注意当前任务和线程上下文。
    """
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    work_version, task_id = current_work_identity(state.sender)
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        if state.type != "plan_approval":
            return f"Request {request_id} is not a plan"
        if state.status != "pending":
            return f"Request {request_id} already {state.status}"
        if state.work_version != work_version or state.task_id != task_id:
            return f"Request {request_id} belongs to an earlier assignment"
        if plan_request_ids.get(state.sender) != request_id:
            return f"Request {request_id} is not the current plan"
        state.status = "approved" if approve else "rejected"
    content = feedback or (
        "Plan approved." if approve else "Revise the plan and submit it again."
    )
    BUS.send(
        "lead",
        state.sender,
        content,
        "plan_approval_response",
        {"request_id": request_id, "approve": approve},
    )
    return f"Plan {state.status} ({request_id})"


def run_create_worktree(name: str, task_id: str) -> str:
    """以工具接口调用工作树创建与任务绑定逻辑。

    输入：
        name (str)：当前操作涉及的成员、服务、工具或工作流名称。
        task_id (str)：本次操作关联的任务 ID。

    输出：
        str：文本结果，可能是正常输出、状态摘要或当前实现返回的错误提示。

    执行流程：
        1. 返回 create_worktree(name, task_id)，结束本次调用。

    注意：
        本层未显式执行文件写入或启动线程；被调用函数的行为和异常仍遵循其各自约定。
    """
    return create_worktree(name, task_id)


# -- [Game Studio] Dispatch and Acceptance --


def run_assign_task(task_id: str, assignee: str) -> str:
    """建完依赖后才发布任务，避免空闲队友在连边之前抢先开工。"""
    if assignee not in STUDIO_ROLES:
        return "Error: assignee must be designer, programmer, or tester"
    try:
        with task_store_lock():
            task = load_task(task_id)
            if task.status != "pending" or task.owner:
                return "Error: task must be pending and unowned"
            if task.assignee is not None and task.assignee != assignee:
                return "Error: task is already dispatched; create a replacement task instead"
            task.assignee = assignee
            save_task(task)
        # 通知只唤醒队友，任务板才是所有权与依赖的事实来源。
        with team_lock:
            active = assignee in active_teammates
        if active:
            BUS.send(
                "lead",
                assignee,
                f"Task dispatched: {task.id}. Claim it only when ready.",
            )
        return f"Assigned {task.id} to {assignee}"
    except (ValueError, FileNotFoundError) as exc:
        return f"Error: {exc}"


def run_accept_game(entrypoint: str, report_path: str = "reports/qa.json") -> str:
    """验收只检查任务和证据，不替模型实现游戏，也不替测试员执行测试。"""
    try:
        with task_store_lock():
            tasks = list_tasks()
            if not tasks or any(t.status != "completed" for t in tasks):
                return "Error: all tasks must be completed before acceptance"
            if not all(any(t.assignee == role for t in tasks) for role in STUDIO_ROLES):
                return "Error: require completed design, programming and testing tasks"
            entry = safe_path(entrypoint)
            if not entry.is_file():
                return "Error: game entrypoint does not exist"
            report = json.loads(safe_path(report_path).read_text(encoding="utf-8"))
            qa = next((t for t in tasks if t.id == report.get("task_id")), None)
            if qa is None or qa.owner != "tester" or qa.assignee != "tester":
                return "Error: report must reference a completed task owned by tester"
            if not report.get("passed") is True or report.get("bugs") != []:
                return "Error: QA has not passed or has unresolved bugs"
            checks = report.get("checks")
            if (
                not isinstance(checks, list)
                or not checks
                or any(
                    not isinstance(c, dict)
                    or not isinstance(c.get("command"), str)
                    or not c["command"].strip()
                    or type(c.get("returncode")) is not int
                    or c["returncode"] != 0
                    or not isinstance(c.get("output"), str)
                    for c in checks
                )
            ):
                return "Error: require actual successful check commands and outputs"
            delivery = {
                "status": "accepted",
                "entrypoint": str(entry),
                "report": report_path,
                "qa_task_id": qa.id,
                "manual_playtest": report.get("manual_playtest", "not_run"),
                "scope": "QA report checks; browser/manual play only if explicitly reported",
            }
            safe_path("delivery.json").write_text(
                json.dumps(delivery, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return json.dumps(delivery, ensure_ascii=False)
    except (ValueError, OSError, TypeError, AttributeError) as exc:
        return f"Error: {exc}"


# -- Tool Definitions --

BASE_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "Find files by glob pattern; ** matches recursively.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
]
# 任务管理工具集合：创建任务+更新任务+列出任务+获取任务+认领任务+完成任务
TASK_TOOLS = [
    # 创建任务并返回其运行时生成的 ID
    {
        "name": "create_task",
        "description": "Create a task and return its runtime-generated ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["subject"],
            "additionalProperties": False,
        },
    },
    # 更新任务，添加依赖关系
    {
        "name": "update_task",
        "description": "Add dependencies using IDs returned by create_task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"},
                "addBlockedBy": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"},
                    "minItems": 1,
                },
            },
            "required": ["task_id", "addBlockedBy"],
            "additionalProperties": False,
        },
    },
    # 列出共享任务
    {
        "name": "list_tasks",
        "description": "List shared tasks.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # 获取单个任务
    {
        "name": "get_task",
        "description": "Get one task by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    # 认领任务
    {
        "name": "claim_task",
        "description": "Claim a ready task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    # 标记任务完成
    {
        "name": "complete_task",
        "description": "Complete an owned task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]
# 团队成员可以使用的工具集合：基础工具+发送消息+提交计划+列出任务+认领任务+完成任务
TEAMMATE_TOOLS = [
    *BASE_TOOLS,
    # 发送消息给团队成员或 Lead
    {
        "name": "send_message",
        "description": "Send an intermediate message to 'lead' or an active teammate.",
        "input_schema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "content": {"type": "string"}},
            "required": ["to", "content"],
        },
    },
    # 请求计划审批
    {
        "name": "submit_plan",
        "description": "Submit a work plan for Lead approval.",
        "input_schema": {
            "type": "object",
            "properties": {"plan": {"type": "string"}},
            "required": ["plan"],
        },
    },
    # 列出任务、认领任务、完成任务
    next(tool for tool in TASK_TOOLS if tool["name"] == "list_tasks"),
    next(tool for tool in TASK_TOOLS if tool["name"] == "claim_task"),
    next(tool for tool in TASK_TOOLS if tool["name"] == "complete_task"),
]

# 团队工具集合：创建成员、列出成员、发送消息、请求关闭、请求计划、审批计划、创建工作树
TEAM_TOOLS = [
    # 创建团队成员
    {
        "name": "spawn_teammate",
        "description": "Spawn a persistent teammate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                "role": {"type": "string"},
                "prompt": {"type": "string"},
                "task_id": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"},
                "require_plan": {"type": "boolean"},
            },
            "required": ["name", "role", "prompt"],
        },
    },
    # 列出团队成员
    {
        "name": "list_teammates",
        "description": "List active teammates.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_message",
        "description": "Message a teammate.",
        "input_schema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "content": {"type": "string"}},
            "required": ["to", "content"],
        },
    },
    # 提交计划
    {
        "name": "request_shutdown",
        "description": "Ask a teammate to shut down.",
        "input_schema": {
            "type": "object",
            "properties": {"teammate": {"type": "string"}},
            "required": ["teammate"],
        },
    },
    # 请求计划
    {
        "name": "request_plan",
        "description": "Require a teammate plan before workspace changes.",
        "input_schema": {
            "type": "object",
            "properties": {"teammate": {"type": "string"}, "task": {"type": "string"}},
            "required": ["teammate", "task"],
        },
    },
    # 审批计划
    {
        "name": "review_plan",
        "description": "Approve or reject a plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "approve": {"type": "boolean"},
                "feedback": {"type": "string"},
            },
            "required": ["request_id", "approve"],
        },
    },
    # 创建工作树
    {
        "name": "create_worktree",
        "description": "Create and bind a task worktree.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^(?!.*\\.\\.)[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
                    "maxLength": 64,
                },
                "task_id": {"type": "string"},
            },
            "required": ["name", "task_id"],
            "additionalProperties": False,
        },
    },
]
# 三种工具集合：基础工具、任务工具、团队工具
# [Game Studio] 保留原工具定义格式，只向 Lead 增加派发与验收工具。
STUDIO_TOOLS = [
    {
        "name": "assign_task",
        "description": "Dispatch a task only after the full dependency graph is built.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "assignee": {"type": "string", "enum": list(STUDIO_ROLES)},
            },
            "required": ["task_id", "assignee"],
            "additionalProperties": False,
        },
    },
    {
        "name": "accept_game",
        "description": "Validate completed tasks, QA evidence and the game entrypoint before delivery.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entrypoint": {"type": "string"},
                "report_path": {"type": "string"},
            },
            "required": ["entrypoint"],
            "additionalProperties": False,
        },
    },
]
TOOLS = [*BASE_TOOLS, *TASK_TOOLS, *TEAM_TOOLS, *STUDIO_TOOLS]

# 工具名称到处理函数的映射表，供 execute_tool 调用
TOOL_HANDLERS = {
    # STUDIO_TOOLS
    # 工作室工具：派发任务与验收游戏
    "assign_task": run_assign_task,# 派发任务给指定成员 TASK中会更新assignee字段 只有指定的成员才能认领任务
    "accept_game": run_accept_game,#
    # BASE_TOOLS
    # 基础工具：命令执行、文件读写与文件匹配
    "bash": run_agent_bash,
    "read_file": run_agent_read,
    "write_file": run_agent_write,
    "edit_file": run_agent_edit,
    "glob": run_agent_glob,
    # TASK_TOOLS
    # 任务管理工具集合：创建任务+更新任务+列出任务+获取任务+认领任务+完成任务
    "create_task": run_create_task,# 创建任务 写入任务是什么 描述是什么等
    "update_task": run_update_task,# 创建任务依赖
    "list_tasks": run_list_tasks,# 让主Agent列出所有任务的状态 让LLM了解任务状态
    "get_task": run_get_task,# 获取单个任务的详细信息 让LLM了解任务状态
    "claim_task": run_claim_task,# 团队成员认领任务 任务的owner字段会变成认领成员的名字 status会变成in_progress
    "complete_task": run_complete_task,# 完成任务后 任务的status会变成completed
    # TEAM_TOOLS
    # 团队工具集合：创建成员+列出成员+发送消息+请求关闭+请求计划+审批计划+创建工作树
    "spawn_teammate": run_spawn_teammate,#可以创建子线程成员 让LLM可以创建子线程成员来协作完成任务 子线程成员会有自己的agent_loop和工具调用能力和单独的上下文
    "list_teammates": run_list_teammates,
    "send_message": run_send_message,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan,
    "review_plan": run_review_plan,
    "create_worktree": run_create_worktree,
}


# -- Hooks and Permission Checks --

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args, skip_permission: bool = False):
    for callback in HOOKS[event]:
        if skip_permission and callback is permission_hook:
            continue
        result = callback(*args)
        if result is not None:
            return result
    return None


def check_permission(block, prompt_user: bool = True) -> str | None:
    if block.name == "bash":
        command = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                return f"Permission denied by deny list: {pattern}"
        if contains_destructive_command(command) or any(
            keyword in command for keyword in DESTRUCTIVE
        ):
            if not prompt_user:
                return "Permission required: ask Lead to run this command."
            print(f"\n[permission] {block.name}({block.input})")
            if input("Allow? [y/N] ").strip().lower() not in {"y", "yes"}:
                return "Permission denied by user"

    if block.name in {"read_file", "write_file", "edit_file"}:
        raw_path = block.input.get("path", "")
        if not (WORKDIR / raw_path).resolve().is_relative_to(WORKDIR.resolve()):
            if not prompt_user:
                return "Permission required: path is outside the workspace."
            print(f"\n[permission] {block.name}({block.input})")
            if input("Allow? [y/N] ").strip().lower() not in {"y", "yes"}:
                return "Permission denied by user"
    return None


def permission_hook(block):
    return check_permission(block, prompt_user=True)


def log_hook(block):
    preview = str(list(block.input.values())[:2])[:60]
    print(f"[hook] {block.name}({preview})")


def large_output_hook(block, output):
    if len(str(output)) > 100000:
        print(f"[hook] Large output from {block.name}: {len(str(output))} chars")


def context_hook(query: str):
    print(f"[hook] UserPromptSubmit: working in {WORKDIR}")


def summary_hook(messages: list):
    tool_count = sum(
        1
        for message in messages
        for block in (
            message.get("content") if isinstance(message.get("content"), list) else []
        )
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    print(f"[hook] Stop: session used {tool_count} tool calls")


register_hook("UserPromptSubmit", context_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# 执行工具调用，返回结果或错误信息
def execute_tool(block) -> str:
    # 1.执行前检查
    # 工具调用前触发 PreToolUse 钩子，检查权限和日志
    # 如果钩子返回非空结果，说明工具调用被阻止，直接返回阻止信息
    # 什么情况下会阻止工具调用？例如 bash 命令包含危险操作，或者文件路径不在工作目录内
    blocked = trigger_hooks("PreToolUse", block)
    if blocked:
        return str(blocked)

    # 2.找到对应的处理函数
    # 传入block 从block.name就能获取工具名称，从TOOL_HANDLERS中获取对应的映射的处理函数
    handler = TOOL_HANDLERS.get(block.name)
    # 如果没有找到对应的处理函数，说明工具名称未知，返回错误信息
    if not handler:
        return f"Unknown tool: {block.name}"

    # 3.执行工具调用
    # 执行工具调用，捕获异常并返回错误信息
    try:
        # 执行工具调用，传入block.input作为参数，获取返回结果
        output = str(handler(**block.input))
    except Exception as exc:
        output = f"Error: {type(exc).__name__}: {exc}"

    # 4.处理结果
    # 工具调用后触发 PostToolUse 钩子，可以用于日志记录、输出检查等
    trigger_hooks("PostToolUse", block, output)

    return output

def print_last_assistant_message(history: list):
    if not history:
        return
    for block in history[-1].get("content", []):
        if getattr(block, "type", None) == "text":
            print(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            print(block.get("text", ""))


_cli_pending_input: list[str] = []


def wait_for_cli_event() -> tuple[str, str | None]:
    """等待命令行输入或团队事件，并返回事件类型与内容。

    输入：
        无显式输入参数；所需状态由当前对象或模块保存。

    输出：
        tuple[str, str | None]：事件类型及可选的用户输入或事件内容。

    执行流程：
        1. 设置 prompt_visible，取值为 False。
        2. 在 True 成立时循环，按 break、返回或异常条件结束。

    注意：
        执行时会向终端打印状态、结果或调试信息。
    """
    prompt_visible = False
    while True:
        # 检查lead邮箱是否有新消息，如果有则返回"wake"事件
        if BUS.peek("lead"):
            if prompt_visible:
                print()
            return "wake", None
        if not prompt_visible:
            pending = "".join(_cli_pending_input) if os.name == "nt" else ""
            print("studio >> " + pending, end="", flush=True)
            prompt_visible = True

        # 读取终端可读取的输入 如果用户没有输入 那么继续检查邮箱是否有新消息
        if os.name == "nt":
            if not sys.stdin.isatty():
                line = sys.stdin.readline()
                if line == "":
                    return "quit", None
                return "user", line.rstrip("\r\n")
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                msvcrt.getwch()  # Consume an extended key's second code unit.
                continue
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\x1a" and not _cli_pending_input:
                return "quit", None
            if char in ("\r", "\n"):
                print()
                line = "".join(_cli_pending_input)
                _cli_pending_input.clear()
                return "user", line
            if char == "\b":
                if _cli_pending_input:
                    _cli_pending_input.pop()
                    print("\b \b", end="", flush=True)
            elif char.isprintable():
                _cli_pending_input.append(char)
                print(char, end="", flush=True)
            continue

        readable, _, _ = select.select([sys.stdin], [], [], 0.25)
        if readable:
            line = sys.stdin.readline()
            if line == "":
                return "quit", None
            return "user", line.rstrip("\n")


# -- Agent Loop --


def agent_loop(messages: list):
    while True:
        #1.尝试给LLM发送消息 获得响应,如果出现异常，捕获异常并将错误信息添加到消息中，然后释放已完成的任务并触发停止钩子
        try:
            
            response = client.messages.create(
                model=MODEL,
                system=SYSTEM,
                messages=messages,
                # base_tools,task_tools,team_tools,teammate_tools
                tools=TOOLS,
                max_tokens=8000,
            )
        except Exception as exc:
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": f"[Error] {type(exc).__name__}: {exc}",
                        }
                    ],
                }
            )
            #**释放已完成的任务
            release_completed_assignment("agent")
            #**触发停止钩子
            trigger_hooks("Stop", messages)
            return

        #2.将LLM的响应内容添加到消息中
        messages.append({"role": "assistant", "content": response.content})
        
        
        #3.检查是否有工具调用，如果有则执行工具调用，没有则结束循环
        #从LLM中提取工具调用块
        tool_calls = [block for block in response.content if block.type == "tool_use"]
        #如果没有工具调用，则释放已完成的任务并触发停止钩子，然后返回
        if not tool_calls:
            release_completed_assignment("agent")
            trigger_hooks("Stop", messages)
            return

        #4.执行工具调用，并将结果添加到消息中
        results = []
        # 实际执行工具调用，并将结果添加到消息中
        for block in tool_calls:
            print(f"> {block.name}")
            #执行工具调用，获取输出
            output = execute_tool(block)
            #打印输出的前300个字符
            print(output[:300])
            #将工具调用结果添加到结果列表中
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )
        #多个工具调用的结果会被合并为一个消息块，添加到消息中
        messages.append({"role": "user", "content": results})




# -- [Game Studio] CLI configuration; the conversation still calls agent_loop --


def main() -> int:
    # Windows 重定向输出时可能使用西文编码；统一 UTF-8，避免中文提示导致启动失败。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    global client, MODEL, WORKDIR, TASKS_DIR, TASKS_ROOT, TASK_LOCK_PATH
    global MAILBOX_DIR, MAILBOX_ROOT, WORKTREES_DIR, WORKTREES_ROOT, SYSTEM
    parser = argparse.ArgumentParser(
        description="Game Company: original s13 agent loop"
    )
    parser.add_argument("request", nargs="?", help="可选的第一条需求；之后仍可继续对话")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument(
        "--workdir", type=Path, help="游戏产物目录；默认项目下的 workspace"
    )
    parser.add_argument(
        "--check-config", action="store_true", help="仅检查配置，不发送模型请求"
    )
    parser.add_argument("--check-api", action="store_true", help="只读取 DeepSeek 模型列表验证接口，不生成文本")
    args = parser.parse_args()
    # 切换供应商后，以明确选择的 .env 为准，防止编辑器旧终端遗留旧 MODEL_ID。
    load_dotenv(dotenv_path=args.env_file, override=True)
    MODEL = os.getenv("MODEL_ID", "deepseek-flash").strip()
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    if not MODEL or not deepseek_key:
        print("Missing MODEL_ID or DEEPSEEK_API_KEY in the selected .env file.", file=sys.stderr)
        return 2
    if args.check_config:
        print("DeepSeek configuration present. No API request sent; connectivity is unverified.")
        return 0
    if args.check_api:
        try:
            details = DeepSeekClient(deepseek_key).check_model(MODEL)
            print("DeepSeek authentication/model access OK: " + details.get("name", MODEL))
            return 0
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    WORKDIR = (
        args.workdir
        or Path(os.getenv("GAME_STUDIO_WORKDIR", PROJECT_ROOT / "workspace"))
    ).resolve()
    if WORKDIR == PROJECT_ROOT or PROJECT_ROOT.is_relative_to(WORKDIR):
        parser.error("工作区应使用独立目录，例如项目下的 workspace，不可包含程序源代码")
    WORKDIR.mkdir(parents=True, exist_ok=True)
    TASKS_DIR = WORKDIR / ".tasks"
    TASKS_ROOT = TASKS_DIR.resolve()
    TASK_LOCK_PATH = TASKS_DIR / ".lock"
    MAILBOX_DIR = WORKDIR / ".mailboxes"
    MAILBOX_ROOT = MAILBOX_DIR.resolve()
    WORKTREES_DIR = WORKDIR / ".worktrees"
    WORKTREES_ROOT = WORKTREES_DIR.resolve()
    PROMPT_SECTIONS["workspace"] = f"Working directory: {WORKDIR}"
    SYSTEM = "\n\n".join(PROMPT_SECTIONS.values())
    client = DeepSeekClient(api_key=deepseek_key, timeout=120)
    print("Game Company — original s13 agent loop / DeepSeek Flash (non-thinking)")
    print(f"Workspace: {WORKDIR}")
    print("输入需求并回车发送，q 退出。团队事件会自动唤醒 Lead。\n")
    history = []
    first_request = args.request
    try:
        while True:
            if first_request is not None:
                kind, payload = "user", first_request
                first_request = None
            else:
                kind, payload = wait_for_cli_event()
            if kind == "quit":
                break
            if kind == "user":
                if payload is None or payload.strip().lower() in {"q", "exit"}:
                    break
                if not payload.strip():
                    continue
                trigger_hooks("UserPromptSubmit", payload)
                history.append({"role": "user", "content": payload})
            else:
                inbox = consume_lead_inbox()
                if not inbox:
                    continue
                history.append({"role": "user", "content": format_team_events(inbox)})
                print(f"[wake: {len(inbox)} team event(s) -> new turn]")
            agent_loop(history)
            print_last_assistant_message(history)
            print()
    except (KeyboardInterrupt, EOFError):
        print("\nStopping studio...")
    finally:
        # 不再调用模型；沿用原版关机握手，请求所有持久队友退出。
        with team_lock:
            # agent信息 谁在运行？ 保存在active_teammates这个变量中
            names = list(active_teammates)
            # 获取所有活跃的队友线程
            threads = list(teammate_threads.values())
        for name in names:
            run_request_shutdown(name)
        deadline = time.monotonic() + 3
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(t.is_alive() for t in threads):
            print(
                "Some teammates are still in a model/tool call; unfinished tasks were not accepted."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
