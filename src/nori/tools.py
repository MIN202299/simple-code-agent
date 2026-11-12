"""Tools the coding agent can call: file reading/editing, search and shell."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents import RunContextWrapper, function_tool
from typing_extensions import TypedDict

from .skills import Skill

MAX_OUTPUT_CHARS = 30_000
MAX_READ_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_SEARCH_RESULTS = 200
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


@dataclass
class AgentContext:
    """Per-session state shared with every tool call."""

    workspace: Path
    auto_approve: bool = False
    always_allowed: set[str] = field(default_factory=set)
    skills: dict[str, Skill] = field(default_factory=dict)
    plan: list[dict[str, str]] = field(default_factory=list)

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
    async def check(ctx: Ctx, _params: dict[str, Any], _call_id: str) -> bool:
        state = ctx.context
        return not (state.auto_approve or tool_name in state.always_allowed)

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


@function_tool(failure_error_function=_tool_error)
def grep(
    ctx: Ctx, pattern: str, path: str = ".", include: str = "", ignore_case: bool = False
) -> str:
    """Search file contents with a regular expression. Returns `file:line: text` matches.

    Args:
        pattern: Python regular expression to search for.
        path: File or directory to search, relative to the workspace root.
        include: Optional file name glob to filter by, e.g. "*.py".
        ignore_case: Match case-insensitively.
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}") from exc
    root = _resolve(ctx, path)
    if not root.exists():
        raise ToolError(f"{path} does not exist")
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
                if len(results) >= MAX_SEARCH_RESULTS:
                    results.append(f"... [stopped after {MAX_SEARCH_RESULTS} matches]")
                    return "\n".join(results)
    return "\n".join(results) if results else "No matches found."


@function_tool(failure_error_function=_tool_error, needs_approval=_needs_approval("run_shell"))
async def run_shell(ctx: Ctx, command: str, timeout: int = 120) -> str:
    """Run a shell command in the workspace root and return its combined stdout/stderr.

    Use it for git, running tests, builds and installs. Commands must be
    non-interactive. Do not use it to read or edit files; use the file tools.

    Args:
        command: The command line to run with the system shell.
        timeout: Seconds to wait before the command is killed.
    """
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=ctx.context.workspace,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name != "nt",
    )

    def kill() -> None:
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

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
]
