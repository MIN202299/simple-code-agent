"""Agent definition: system prompt and construction."""

from __future__ import annotations

import datetime
import platform
from pathlib import Path

from agents import Agent, Model, ModelSettings, RunContextWrapper
from agents.mcp import MCPServer

from .branding import APP_NAME
from .skills import Skill, skills_prompt
from .tools import ALL_TOOLS, SUBAGENT_TOOLS, AgentContext

PROJECT_DOC_NAMES = ("AGENTS.md", "CLAUDE.md")
MAX_PROJECT_DOC_CHARS = 20_000

SYSTEM_PROMPT = """\
You are Nori, a coding agent running in the user's terminal. You help with software \
engineering tasks: reading and explaining code, fixing bugs, adding features, \
refactoring, and running commands.

# How to work
- Explore before you change: use list_dir, glob_files, grep and read_file to \
understand the relevant code. Never guess at file contents.
- Read a file before editing it. Use edit_file for targeted changes and \
write_file only for new files or full rewrites.
- Follow the conventions already present in the project (style, libraries, structure).
- After changing code, verify it when you can: run the tests, the linter, or the program.
- For tasks with three or more steps, keep a checklist with update_plan and update it as you go.
- Tools from MCP servers may also be available; use them when they fit the task.
- Use spawn_agent to delegate work that would flood your context (searching a large \
codebase, reading many files to answer one question) or that splits into independent \
parts; several calls in one response run in parallel. Do simple, targeted lookups yourself.
- Keep changes focused on what was asked. Do not commit or push unless asked to.
- Tool calls that modify files or run commands may need the user's approval. \
If one is rejected, do not retry it unchanged: adapt, or ask the user what they want.
- If a tool returns an error, read it and fix the cause instead of repeating the call.
- `@path` in a user message refers to that file or directory in the workspace.
- For commands that keep running (a dev server) use run_shell with background=true, \
then shell_output to check on them.
- Use web_search and web_fetch when you need information that is not in the workspace, \
such as current documentation or an error message you do not recognise.

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


def build_instructions(workspace: Path, skills: dict[str, Skill] | None = None) -> str:
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
    return text + skills_prompt(skills or {})


SUBAGENT_PROMPT = """\
You are a sub-agent of Nori, a coding agent. The main agent delegated one task to \
you; its instructions are the user message. You cannot ask questions, so make \
reasonable assumptions and state them.

- Work autonomously until the task is done, using your tools. Never guess at file contents.
- {abilities}
- Tool calls may need the user's approval. If one is rejected, do not retry it unchanged.
- Finish with a report for the main agent, which sees nothing else of your work: \
what you found or did, with exact file paths and line numbers, relevant snippets, \
and anything that failed or remains uncertain. Be complete but concise.

# Environment
- Workspace root: {workspace} (all tool paths are relative to it; files outside are off limits)
- Platform: {platform}
- Date: {date}
"""

SUBAGENT_ABILITIES = {
    "general": "You can read, search and modify files and run shell commands.",
    "explore": "You can only read and search. Do not attempt to change anything.",
}


def build_subagent(
    workspace: Path,
    agent_type: str,
    model: str | Model | None = None,
    mcp_servers: list[MCPServer] | None = None,
    model_settings: ModelSettings | None = None,
) -> Agent[AgentContext]:
    """Create a sub-agent of the given type with a fresh context."""
    instructions = SUBAGENT_PROMPT.format(
        abilities=SUBAGENT_ABILITIES[agent_type],
        workspace=workspace,
        platform=f"{platform.system()} {platform.release()}",
        date=datetime.date.today().isoformat(),
    )
    return Agent[AgentContext](
        name=f"{APP_NAME} sub-agent ({agent_type})",
        instructions=with_mode(instructions),
        tools=list(SUBAGENT_TOOLS[agent_type]),
        mcp_servers=list(mcp_servers or []) if agent_type == "general" else [],
        model=model,
        model_settings=model_settings or ModelSettings(),
    )


PLAN_MODE_PROMPT = """
# Plan mode is ON
The user wants a plan before anything is changed. Until they approve one:
- Do NOT create or edit files, and do not run commands that change anything. \
File edits will be refused.
- Investigate with the read-only tools (read_file, grep, glob_files, list_dir, web tools, \
explore sub-agents). Read-only shell commands such as `git log` are fine.
- When you understand the task, call present_plan with a concrete plan: what you will \
change, in which files, in what order, and how you will verify it. Ask the user instead \
if a decision is theirs to make.
- Only start implementing after present_plan reports that the user approved.
"""

MODE_PROMPTS = {
    "plan": PLAN_MODE_PROMPT,
    "accept-edits": "\n# Mode\nFile edits are applied without asking the user; commands still need approval.\n",
    "auto": "\n# Mode\nYour tool calls run without asking the user. Be careful with destructive commands.\n",
}


def with_mode(base: str):
    """Instructions that follow the permission mode, which can change during a session."""

    def instructions(context: RunContextWrapper[AgentContext], _agent: Agent[AgentContext]) -> str:
        return base + MODE_PROMPTS.get(context.context.permissions.mode, "")

    return instructions


def build_agent(
    workspace: Path,
    model: str | Model | None = None,
    skills: dict[str, Skill] | None = None,
    mcp_servers: list[MCPServer] | None = None,
    model_settings: ModelSettings | None = None,
) -> Agent[AgentContext]:
    """Create the coding agent. `model=None` uses the SDK's default model."""
    return Agent[AgentContext](
        name=APP_NAME,
        instructions=with_mode(build_instructions(workspace, skills)),
        tools=list(ALL_TOOLS),
        mcp_servers=list(mcp_servers or []),
        model=model,
        model_settings=model_settings or ModelSettings(),
    )
