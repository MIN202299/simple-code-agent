"""Tools the coding agent can call: file reading/editing, search and shell."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents import RunContextWrapper, function_tool
from agents.tool_context import ToolContext
from typing_extensions import TypedDict

from . import web
from .checkpoints import Checkpoints
from .permissions import Permissions
from .shelltasks import ShellTasks, kill_process
from .skills import Skill

MAX_OUTPUT_CHARS = 30_000
MAX_READ_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_SEARCH_RESULTS = 200
RIPGREP_TIMEOUT = 30
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


@dataclass
class AgentContext:
    """Per-session state shared with every tool call."""

    workspace: Path
    permissions: Permissions = None  # type: ignore[assignment]  # set in __post_init__
    skills: dict[str, Skill] = field(default_factory=dict)
    plan: list[dict[str, str]] = field(default_factory=list)
    checkpoints: Checkpoints = field(default_factory=Checkpoints)
    shell_tasks: ShellTasks = field(default_factory=ShellTasks)
    search_engine: str = "duckduckgo"
    # Set by the session: runs a sub-agent and returns its final report.
    spawn: Callable[[str, str, str], Awaitable[str]] | None = None
    # Set by the session: shows a plan to the user; returns the mode they chose, or "".
    present_plan: Callable[[str], Awaitable[str]] | None = None

    def __post_init__(self) -> None:
        if self.permissions is None:
            self.permissions = Permissions(self.workspace)

    @property
    def auto_approve(self) -> bool:
        return self.permissions.mode == "auto"

    @auto_approve.setter
    def auto_approve(self, value: bool) -> None:
        self.permissions.set_mode("auto" if value else "default")

    def readable_roots(self) -> list[Path]:
        """The workspace plus skill directories, which are readable but not writable."""
        return [self.workspace, *(skill.directory for skill in self.skills.values())]


Ctx = RunContextWrapper[AgentContext]


class ToolError(Exception):
    """An error whose message is reported back to the model."""


def _tool_error(_ctx: RunContextWrapper[Any], error: Exception) -> str:
    return f"Error: {error}"


def _resolve(ctx: Ctx, path: str, write: bool = False) -> Path:
    """Resolve `path` against the workspace and refuse to leave the allowed roots."""
    workspace = ctx.context.workspace
    resolved = (workspace / Path(path).expanduser()).resolve()
    roots = [workspace] if write else ctx.context.readable_roots()
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ToolError(f"path {path!r} is outside the workspace {workspace}")
    return resolved


def _rel(ctx: Ctx, path: Path) -> str:
    workspace = ctx.context.workspace
    if path == workspace or workspace in path.parents:
        return os.path.relpath(path, workspace)
    return str(path)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit // 2], text[-limit // 2 :]
    return f"{head}\n... [{len(text) - limit} characters truncated] ...\n{tail}"


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        raise ToolError(f"{path.name} looks like a binary file")
    return data.decode("utf-8", errors="replace")


def _walk(root: Path):
    """Yield files under `root`, skipping VCS/dependency directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _needs_approval(tool_name: str):
    async def check(ctx: Ctx, params: dict[str, Any], _call_id: str) -> bool:
        # "deny" also goes through the approval path, where the session refuses it.
        return ctx.context.permissions.decide(tool_name, params) != "allow"

    return check


@function_tool(failure_error_function=_tool_error)
def read_file(ctx: Ctx, path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> str:
    """Read a text file. Output lines are prefixed with their line number.

    Args:
        path: File path, relative to the workspace root.
        offset: 1-based line number to start reading from.
        limit: Maximum number of lines to return.
    """
    file = _resolve(ctx, path)
    if not file.is_file():
        raise ToolError(f"{path} is not a file")
    lines = _read_text(file).splitlines()
    if not lines:
        return "(empty file)"
    start = max(offset, 1) - 1
    chunk = lines[start : start + max(limit, 1)]
    if not chunk:
        raise ToolError(f"offset {offset} is past the end of the file ({len(lines)} lines)")
    out = "\n".join(f"{start + i + 1:>6}\t{line[:MAX_LINE_CHARS]}" for i, line in enumerate(chunk))
    remaining = len(lines) - (start + len(chunk))
    if remaining > 0:
        out += f"\n... [{remaining} more lines; continue with offset={start + len(chunk) + 1}]"
    return _truncate(out)


@function_tool(failure_error_function=_tool_error, needs_approval=_needs_approval("write_file"))
def write_file(ctx: Ctx, path: str, content: str) -> str:
    """Create a file or overwrite it entirely. Parent directories are created as needed.

    Prefer edit_file for changing part of an existing file.

    Args:
        path: File path, relative to the workspace root.
        content: The complete new content of the file.
    """
    file = _resolve(ctx, path, write=True)
    existed = file.exists()
    ctx.context.checkpoints.record(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    verb = "Overwrote" if existed else "Created"
    return f"{verb} {_rel(ctx, file)} ({len(content.splitlines())} lines)"


@function_tool(failure_error_function=_tool_error, needs_approval=_needs_approval("edit_file"))
def edit_file(
    ctx: Ctx, path: str, old_string: str, new_string: str, replace_all: bool = False
) -> str:
    """Replace an exact string in an existing file.

    `old_string` must match the file exactly, including whitespace and indentation,
    and must be unique unless `replace_all` is true. Read the file first.

    Args:
        path: File path, relative to the workspace root.
        old_string: The exact text to replace.
        new_string: The text to replace it with.
        replace_all: Replace every occurrence instead of requiring a unique match.
    """
    file = _resolve(ctx, path, write=True)
    if not file.is_file():
        raise ToolError(f"{path} is not a file")
    if not old_string:
        raise ToolError("old_string must not be empty")
    if old_string == new_string:
        raise ToolError("old_string and new_string are identical")
    text = _read_text(file)
    count = text.count(old_string)
    if count == 0:
        raise ToolError("old_string was not found in the file")
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string occurs {count} times; add surrounding context to make it "
            "unique or set replace_all=true"
        )
    ctx.context.checkpoints.record(file)
    file.write_text(text.replace(old_string, new_string), encoding="utf-8")
    return f"Edited {_rel(ctx, file)} ({count} replacement{'s' if count > 1 else ''})"


@function_tool(failure_error_function=_tool_error)
def list_dir(ctx: Ctx, path: str = ".") -> str:
    """List the entries of a directory. Directories end with a slash.

    Args:
        path: Directory path, relative to the workspace root.
    """
    directory = _resolve(ctx, path)
    if not directory.is_dir():
        raise ToolError(f"{path} is not a directory")
    entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    if not entries:
        return "(empty directory)"
    return _truncate("\n".join(f"{p.name}/" if p.is_dir() else p.name for p in entries))


@function_tool(failure_error_function=_tool_error)
def glob_files(ctx: Ctx, pattern: str, path: str = ".") -> str:
    """Find files by glob pattern, e.g. "**/*.py" or "src/**/test_*.ts".

    Args:
        pattern: Glob pattern, matched relative to `path`.
        path: Directory to search in, relative to the workspace root.
    """
    root = _resolve(ctx, path)
    if not root.is_dir():
        raise ToolError(f"{path} is not a directory")
    matches = sorted(
        _rel(ctx, p)
        for p in root.glob(pattern)
        if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts)
    )
    if not matches:
        return "No files matched."
    out = "\n".join(matches[:MAX_SEARCH_RESULTS])
    if len(matches) > MAX_SEARCH_RESULTS:
        out += f"\n... [{len(matches) - MAX_SEARCH_RESULTS} more files not shown]"
    return out


def _ripgrep(
    ctx: Ctx, root: Path, pattern: str, include: str, ignore_case: bool
) -> list[str] | None:
    """Search with ripgrep. Returns None if it is unavailable or rejects the pattern."""
    executable = shutil.which("rg")
    if executable is None:
        return None
    command = [
        executable, "--line-number", "--with-filename", "--no-heading", "--color", "never",
        "--hidden", "--sort", "path", "--max-columns", "1000", "--max-count", "200",
    ]  # fmt: skip
    for directory in sorted(SKIP_DIRS):
        command += ["--glob", f"!{directory}"]
    if include:
        command += ["--glob", include]
    if ignore_case:
        command.append("--ignore-case")
    command += ["--regexp", pattern, "--", str(root)]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=RIPGREP_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode not in (0, 1):  # 2: bad pattern or another error; let Python try
        return None
    results = []
    for line in done.stdout.splitlines():
        file, _, rest = line.partition(":")
        number, _, text = rest.partition(":")
        if not number.isdigit():
            continue
        results.append(f"{_rel(ctx, Path(file))}:{number}: {text.strip()[:300]}")
    return results


def _python_grep(
    ctx: Ctx, root: Path, pattern: str, include: str, ignore_case: bool
) -> list[str]:
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}") from exc
    files = [root] if root.is_file() else _walk(root)
    results: list[str] = []
    for file in files:
        if include and not fnmatch.fnmatch(file.name, include):
            continue
        try:
            text = _read_text(file)
        except (ToolError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                results.append(f"{_rel(ctx, file)}:{number}: {line.strip()[:300]}")
                if len(results) > MAX_SEARCH_RESULTS:
                    return results
    return results


@function_tool(failure_error_function=_tool_error)
def grep(
    ctx: Ctx, pattern: str, path: str = ".", include: str = "", ignore_case: bool = False
) -> str:
    """Search file contents with a regular expression. Returns `file:line: text` matches.

    Args:
        pattern: Regular expression to search for.
        path: File or directory to search, relative to the workspace root.
        include: Optional file name glob to filter by, e.g. "*.py".
        ignore_case: Match case-insensitively.
    """
    root = _resolve(ctx, path)
    if not root.exists():
        raise ToolError(f"{path} does not exist")
    # ripgrep is far faster on large trees (and skips files ignored by git);
    # the pure-Python search is the fallback when it is not installed.
    results = _ripgrep(ctx, root, pattern, include, ignore_case)
    if results is None:
        results = _python_grep(ctx, root, pattern, include, ignore_case)
    if len(results) > MAX_SEARCH_RESULTS:
        results = [*results[:MAX_SEARCH_RESULTS], f"... [stopped after {MAX_SEARCH_RESULTS} matches]"]
    return "\n".join(results) if results else "No matches found."


@function_tool(failure_error_function=_tool_error, needs_approval=_needs_approval("run_shell"))
async def run_shell(ctx: Ctx, command: str, timeout: int = 120, background: bool = False) -> str:
    """Run a shell command in the workspace root and return its combined stdout/stderr.

    Use it for git, running tests, builds and installs. Commands must be
    non-interactive. Do not use it to read or edit files; use the file tools.

    Set background=true for commands that keep running (a dev server, a watcher)
    or take very long: the call returns at once with a task id, and you read the
    output later with shell_output and stop it with shell_kill.

    Args:
        command: The command line to run with the system shell.
        timeout: Seconds to wait before the command is killed (foreground only).
        background: Start the command and return immediately.
    """
    if background:
        try:
            task = await ctx.context.shell_tasks.start(command, ctx.context.workspace)
        except RuntimeError as exc:
            raise ToolError(str(exc)) from exc
        return (
            f"Started in the background as {task.id}. Use shell_output(task_id=\"{task.id}\") "
            "to read its output and shell_kill to stop it."
        )
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=ctx.context.workspace,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name != "nt",
    )

    def kill() -> None:
        kill_process(proc)

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=max(timeout, 1))
    except asyncio.TimeoutError:
        kill()
        await proc.wait()
        raise ToolError(f"command timed out after {timeout}s and was killed") from None
    except asyncio.CancelledError:
        kill()
        raise
    output = stdout.decode("utf-8", errors="replace").rstrip()
    return _truncate(f"{output}\n[exit code: {proc.returncode}]".lstrip())


@function_tool(failure_error_function=_tool_error)
def load_skill(ctx: Ctx, name: str) -> str:
    """Load the full instructions of a skill listed in the system prompt.

    Args:
        name: The skill name exactly as listed.
    """
    skill = ctx.context.skills.get(name.strip().lower())
    if skill is None:
        available = ", ".join(ctx.context.skills) or "(none)"
        raise ToolError(f"unknown skill {name!r}; available skills: {available}")
    return skill.render()


class PlanStep(TypedDict):
    step: str
    """Short description of the step."""
    status: str
    """One of: pending, in_progress, completed."""


PLAN_MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


@function_tool(failure_error_function=_tool_error)
def update_plan(ctx: Ctx, steps: list[PlanStep]) -> str:
    """Record or update the task plan shown to the user as a checklist.

    Use it for tasks with three or more steps: send the whole list every time,
    with at most one step in_progress, and update it as steps are completed.

    Args:
        steps: The complete plan, in order.
    """
    plan = []
    for item in steps:
        status = item.get("status", "pending")
        if status not in PLAN_MARKS:
            raise ToolError(f"invalid status {status!r}; use pending, in_progress or completed")
        plan.append({"step": str(item.get("step", "")).strip(), "status": status})
    ctx.context.plan = plan
    if not plan:
        return "Plan cleared."
    return "\n".join(f"{PLAN_MARKS[p['status']]} {p['step']}" for p in plan)


@function_tool(failure_error_function=_tool_error)
async def shell_output(ctx: Ctx, task_id: str, wait: int = 0) -> str:
    """Read the new output of a background command and whether it is still running.

    Args:
        task_id: The id returned by run_shell with background=true, e.g. "bg1".
        wait: Seconds to wait for the command to finish or produce output first (max 60).
    """
    tasks = ctx.context.shell_tasks
    task = tasks.get(task_id)
    if task is None:
        known = ", ".join(tasks.tasks) or "(none)"
        raise ToolError(f"unknown task {task_id!r}; background tasks: {known}")
    deadline = time.monotonic() + min(max(wait, 0), 60)
    while task.running and task.read_to >= len(task.output) and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    if not task.running and task.reader is not None:
        await asyncio.wait({task.reader}, timeout=1)  # let the last output arrive
    output = task.take_new_output().rstrip()
    return _truncate(f"{output or '(no new output)'}\n[{task.id}: {task.status}]")


@function_tool(failure_error_function=_tool_error)
async def shell_kill(ctx: Ctx, task_id: str) -> str:
    """Stop a background command.

    Args:
        task_id: The id returned by run_shell with background=true.
    """
    task = ctx.context.shell_tasks.get(task_id)
    if task is None:
        raise ToolError(f"unknown task {task_id!r}")
    if not task.running:
        return f"{task.id} had already {task.status}."
    await ctx.context.shell_tasks.kill(task)
    return f"Stopped {task.id}."


@function_tool(failure_error_function=_tool_error, needs_approval=_needs_approval("web_fetch"))
async def web_fetch(ctx: Ctx, url: str) -> str:
    """Fetch a web page or text file and return its content as readable text.

    Use it to read documentation, an issue, or a link the user gave you.

    Args:
        url: The http(s) URL to fetch.
    """
    try:
        return _truncate(await asyncio.to_thread(web.fetch, url))
    except web.WebError as exc:
        raise ToolError(str(exc)) from exc


@function_tool(failure_error_function=_tool_error)
async def web_search(ctx: Ctx, query: str) -> str:
    """Search the web. Returns titles, URLs and snippets; use web_fetch to read a result.

    Args:
        query: What to search for.
    """
    if not query.strip():
        raise ToolError("query must not be empty")
    try:
        results = await asyncio.to_thread(web.search, query, ctx.context.search_engine)
    except web.WebError as exc:
        raise ToolError(f"search failed ({exc})") from exc
    return "\n\n".join(f"{r.title}\n{r.url}\n{r.snippet}".rstrip() for r in results)


@function_tool(failure_error_function=_tool_error)
async def present_plan(ctx: Ctx, plan: str) -> str:
    """Show your plan to the user and ask for approval to start implementing it.

    Only used in plan mode, after the research is done. Write the plan in Markdown:
    what you will change, in which files, in what order, and how you will verify it.

    Args:
        plan: The complete plan.
    """
    context = ctx.context
    if context.permissions.mode != "plan":
        return "Plan mode is not on; you do not need approval for a plan. Proceed."
    if not plan.strip():
        raise ToolError("plan must not be empty")
    if context.present_plan is None:
        raise ToolError("there is no user to approve the plan here")
    chosen = await context.present_plan(plan)
    if not chosen:
        return (
            "The user did not approve the plan. Stay in plan mode: do not change anything. "
            "Ask what they want different, or revise the plan and present it again."
        )
    context.permissions.set_mode(chosen)
    return "The user approved the plan. Plan mode is off; start implementing it now."


AGENT_TYPES = ("general", "explore")


@function_tool(failure_error_function=_tool_error)
async def spawn_agent(
    ctx: ToolContext[AgentContext], description: str, prompt: str, agent_type: str = "general"
) -> str:
    """Delegate a task to a sub-agent that works in its own, separate context.

    Use it for broad searches and investigations whose intermediate output you
    do not need, and for independent subtasks; call it several times in one
    response to run sub-agents in parallel. The sub-agent sees nothing of this
    conversation, so the prompt must be self-contained: state the goal, what is
    already known, and exactly what to report back. Only its final report is
    returned to you; the user does not see it unless you relay it.

    Args:
        description: Three to five words describing the task, shown to the user.
        prompt: Complete, self-contained instructions for the sub-agent.
        agent_type: "general" has every tool and can change files and run commands;
            "explore" can only read and search, and is the right choice for research.
    """
    if agent_type not in AGENT_TYPES:
        raise ToolError(f"unknown agent_type {agent_type!r}; use one of {', '.join(AGENT_TYPES)}")
    if not prompt.strip():
        raise ToolError("prompt must not be empty")
    if ctx.context.spawn is None:
        raise ToolError("sub-agents are not available here")
    return _truncate(await ctx.context.spawn(ctx.tool_call_id, agent_type, prompt))


READ_ONLY_TOOLS = [read_file, list_dir, glob_files, grep, web_search, web_fetch]
# What a sub-agent may use: everything except planning and spawning further agents.
SUBAGENT_TOOLS = {
    "general": [
        read_file, write_file, edit_file, list_dir, glob_files, grep,
        run_shell, shell_output, shell_kill, web_search, web_fetch, load_skill,
    ],  # fmt: skip
    "explore": READ_ONLY_TOOLS,
}

ALL_TOOLS = [
    read_file,
    write_file,
    edit_file,
    list_dir,
    glob_files,
    grep,
    run_shell,
    load_skill,
    update_plan,
    spawn_agent,
    shell_output,
    shell_kill,
    web_search,
    web_fetch,
    present_plan,
]
