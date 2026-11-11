"""Formatting of the transcript: tool calls, tool results and assistant text.

Everything here returns rich renderables so the plain REPL and the TUI share
one look.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

BULLET = "●"
MAX_OUTPUT_LINES = 6
MAX_DETAIL_LINES = 24
MAX_LINE_CHARS = 160

# Tool name -> (display title, argument shown in the parentheses).
TOOL_TITLES = {
    "read_file": ("Read", "path"),
    "write_file": ("Write", "path"),
    "edit_file": ("Update", "path"),
    "list_dir": ("List", "path"),
    "glob_files": ("Glob", "pattern"),
    "grep": ("Grep", "pattern"),
    "run_shell": ("Bash", "command"),
    "load_skill": ("Skill", "name"),
}
PLAN_TITLE = "Update Todos"


def parse_args(raw: Any) -> dict[str, Any]:
    """Tool arguments arrive as a JSON string; never fail on malformed ones."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"arguments": raw}
    return parsed if isinstance(parsed, dict) else {"arguments": parsed}


def shorten(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def output_text(output: Any) -> str:
    """Flatten tool output; MCP tools return content blocks rather than a string."""
    if isinstance(output, dict) and isinstance(output.get("text"), str):
        return output["text"]
    if isinstance(output, list):
        return "\n".join(output_text(part) for part in output)
    return str(output)


def tool_label(name: str) -> str:
    """Short human name of a tool, e.g. `Bash` for `run_shell`."""
    if name == "update_plan":
        return PLAN_TITLE
    return TOOL_TITLES.get(name, (name, ""))[0]


def call_title(name: str, args: dict[str, Any]) -> str:
    """One-line summary of a tool call, e.g. `Read(src/a.py)` or `Bash(pytest -q)`."""
    if name == "update_plan":
        return PLAN_TITLE
    if name in TOOL_TITLES:
        title, key = TOOL_TITLES[name]
        value = args.get(key)
        if value is None:
            value = "." if key == "path" else ""
        return f"{title}({shorten(str(value).strip().splitlines()[0] if value else '', 100)})"
    return f"{name}({', '.join(f'{k}={shorten(v, 60)}' for k, v in args.items())})"


def _clip(lines: list[str], limit: int) -> tuple[list[str], int]:
    return lines[:limit], max(len(lines) - limit, 0)


def _detail(lines: list[Text], hidden: int) -> Text:
    """Indented block shown under a tool call."""
    out = Text()
    for line in lines:
        out.append("     ")
        out.append_text(line)
        out.append("\n")
    if hidden:
        out.append(f"     … +{hidden} 行\n", style="dim")
    out.rstrip()
    return out


def _diff_lines(old: str, new: str) -> list[Text]:
    lines = []
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2)
    for line in list(diff)[2:]:  # skip the ---/+++ header
        if line.startswith("@@"):
            continue
        style = "green" if line.startswith("+") else "red" if line.startswith("-") else "dim"
        lines.append(Text(line[:MAX_LINE_CHARS], style=style))
    return lines


def render_call(name: str, args: dict[str, Any]) -> RenderableType:
    """The `● Title(args)` line, plus a preview of what a mutating call will do."""
    head = Text()
    head.append(f"{BULLET} ", style="bold cyan")
    head.append(call_title(name, args), style="bold")
    detail: list[Text] = []
    if name == "edit_file":
        detail = _diff_lines(str(args.get("old_string", "")), str(args.get("new_string", "")))
    elif name == "write_file":
        detail = [
            Text("+" + line[:MAX_LINE_CHARS], style="green")
            for line in str(args.get("content", "")).splitlines()
        ]
    elif name == "run_shell":
        command = str(args.get("command", ""))
        if "\n" in command.strip() or len(command) > 100:
            detail = [Text(line[:MAX_LINE_CHARS], style="dim") for line in command.splitlines()]
    if not detail:
        return head
    shown, hidden = _clip(detail, MAX_DETAIL_LINES)
    return Group(head, _detail(shown, hidden))


def _result(lines: list[Text], hidden: int = 0) -> Text:
    """The `⎿` block under a tool call."""
    out = Text()
    for index, line in enumerate(lines):
        out.append("  ⎿  " if index == 0 else "     ", style="dim")
        out.append_text(line)
        out.append("\n")
    if hidden:
        out.append(f"     … +{hidden} 行\n", style="dim")
    out.rstrip()
    return out


def render_plan(output: str) -> Text:
    lines = []
    for line in output.splitlines():
        mark, step = line[:3], line[4:]
        if mark == "[x]":
            lines.append(Text(f"☒ {step}", style="dim strike"))
        elif mark == "[~]":
            lines.append(Text(f"☐ {step}", style="bold"))
        else:
            lines.append(Text(f"☐ {step}"))
    return _result(lines or [Text(output, style="dim")])


def render_output(name: str, args: dict[str, Any], output: Any) -> RenderableType:
    """The result of a tool call, summarised to a few lines."""
    text = output_text(output).rstrip()
    if text.startswith("Error:") or "rejected this tool call" in text:
        return _result([Text(line[:MAX_LINE_CHARS], style="red") for line in text.splitlines()[:4]])
    if name == "update_plan" and text.startswith("["):
        return render_plan(text)
    if name == "read_file":
        count = sum(1 for line in text.splitlines() if "\t" in line)
        return _result([Text(f"Read {count} lines", style="dim")])
    if name == "load_skill":
        return _result([Text("Loaded skill", style="dim")])
    lines = text.splitlines() or ["(no output)"]
    shown, hidden = _clip(lines, MAX_OUTPUT_LINES)
    return _result([Text(line[:MAX_LINE_CHARS], style="dim") for line in shown], hidden)


def render_user(text: str) -> Text:
    out = Text(style="dim")
    for index, line in enumerate(text.splitlines() or [""]):
        out.append("> " if index == 0 else "  ")
        out.append(line + "\n")
    out.rstrip()
    return out


def render_assistant(markdown: str, first: bool = True) -> RenderableType:
    """A block of assistant text as Markdown, with a bullet on the first block."""
    grid = Table.grid(padding=0)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text(BULLET if first else " "), Markdown(markdown, code_theme="ansi_dark"))
    return grid


def split_blocks(text: str) -> tuple[list[str], str]:
    """Split streamed Markdown into finished blocks and the unfinished remainder.

    Blocks end at a blank line outside a code fence. A blank line followed by an
    indented line does not end the block, because that is a continuation of a
    list item and would render as a code block on its own.
    """
    *complete, partial = text.split("\n")
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    pending_blank = 0
    for line in complete:
        stripped = line.strip()
        if not in_fence and not stripped:
            if current:
                pending_blank += 1
            continue
        if pending_blank:
            if line[:1] in (" ", "\t"):
                current.extend([""] * pending_blank)
            else:
                blocks.append("\n".join(current))
                current = []
            pending_blank = 0
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
        current.append(line)
    remainder = "\n".join([*current, *([""] * pending_blank), partial])
    return blocks, remainder
