"""Agent definition: system prompt and construction."""

from __future__ import annotations

import datetime
import platform
from pathlib import Path

from agents import Agent, Model

from .tools import ALL_TOOLS, AgentContext

PROJECT_DOC_NAMES = ("AGENTS.md", "CLAUDE.md")
MAX_PROJECT_DOC_CHARS = 20_000

SYSTEM_PROMPT = """\
You are a coding agent running in the user's terminal. You help with software \
engineering tasks: reading and explaining code, fixing bugs, adding features, \
refactoring, and running commands.

# How to work
- Explore before you change: use list_dir, glob_files, grep and read_file to \
understand the relevant code. Never guess at file contents.
- Read a file before editing it. Use edit_file for targeted changes and \
write_file only for new files or full rewrites.
- Follow the conventions already present in the project (style, libraries, structure).
- After changing code, verify it when you can: run the tests, the linter, or the program.
- Keep changes focused on what was asked. Do not commit or push unless asked to.
- Tool calls that modify files or run commands may need the user's approval. \
If one is rejected, do not retry it unchanged: adapt, or ask the user what they want.
- If a tool returns an error, read it and fix the cause instead of repeating the call.

# How to answer
- Be concise; this is a terminal. Plain text or light Markdown.
- Reply in the language the user writes in.
- When finished, say briefly what you changed and how you verified it. \
Be honest about anything that failed or that you did not check.

# Environment
- Workspace root: {workspace} (all tool paths are relative to it; files outside are off limits)
- Platform: {platform}
- Date: {date}
"""


def build_instructions(workspace: Path) -> str:
    text = SYSTEM_PROMPT.format(
        workspace=workspace,
        platform=f"{platform.system()} {platform.release()}",
        date=datetime.date.today().isoformat(),
    )
    for name in PROJECT_DOC_NAMES:
        doc = workspace / name
        if doc.is_file():
            content = doc.read_text(encoding="utf-8", errors="replace")[:MAX_PROJECT_DOC_CHARS]
            text += f"\n# Project instructions (from {name})\n{content}\n"
            break
    return text


def build_agent(workspace: Path, model: str | Model | None = None) -> Agent[AgentContext]:
    """Create the coding agent. `model=None` uses the SDK's default model."""
    return Agent[AgentContext](
        name="Code Agent",
        instructions=build_instructions(workspace),
        tools=list(ALL_TOOLS),
        model=model,
    )
