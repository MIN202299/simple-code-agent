"""Slash commands, shared by the TUI and the plain REPL."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.markup import escape
from rich.text import Text

from .render import shorten
from .session import Session
from .skills import Skill

Emit = Callable[[Any], None]


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    takes_arg: bool = False


@dataclass(frozen=True)
class CommandResult:
    exit: bool = False
    prompt: str | None = None  # a message to send to the agent


BUILTIN_COMMANDS = [
    Command("help", "显示帮助"),
    Command("clear", "清空对话上下文"),
    Command("model", "查看或切换模型", takes_arg=True),
    Command("skills", "列出可用的 skill"),
    Command("mcp", "列出已连接的 MCP 服务器及其工具"),
    Command("yes", "开关自动批准（跳过写文件、执行命令前的确认）"),
    Command("exit", "退出"),
]


def all_commands(session: Session) -> list[Command]:
    """Built-in commands followed by one command per skill."""
    names = {command.name for command in BUILTIN_COMMANDS}
    skills = [
        Command(skill.name, skill.description, takes_arg=True)
        for skill in session.context.skills.values()
        if skill.name not in names
    ]
    return [*BUILTIN_COMMANDS, *skills]


def match_commands(session: Session, text: str) -> list[Command]:
    """Commands completing a partially typed `/name` (no arguments yet)."""
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    prefix = text[1:].lower()
    return [command for command in all_commands(session) if command.name.startswith(prefix)]


def skill_message(skill: Skill, request: str) -> str:
    """The user message sent when a skill is invoked directly with `/<skill>`."""
    request = request or "Carry out this skill."
    return f"Use the following skill for this request.\n\n{skill.render()}\nRequest: {request}"


def _help(session: Session, emit: Emit) -> None:
    rows = [
        (f"/{command.name}" + (" [name]" if command.takes_arg else ""), command.description)
        for command in BUILTIN_COMMANDS
    ]
    rows.append(("/<skill> [text]", "直接使用某个 skill"))
    for usage, description in rows:
        emit(Text.assemble("  ", (usage.ljust(18), "bold"), description))


def _skills(session: Session, emit: Emit) -> None:
    skills = session.context.skills
    if not skills:
        emit("[dim]没有找到 skill。可放在 .agents/skills/<name>/SKILL.md。[/dim]")
        return
    for skill in skills.values():
        emit(f"  [bold]/{escape(skill.name)}[/bold]  {escape(shorten(skill.description, 120))}")
        emit(f"    [dim]{escape(str(skill.directory))}[/dim]")


async def _mcp(session: Session, emit: Emit) -> None:
    if not session.mcp_servers:
        emit("[dim]没有已连接的 MCP 服务器。可在 .mcp.json 中配置。[/dim]")
        return
    for server in session.mcp_servers:
        try:
            tools = await server.list_tools()
        except Exception as exc:  # a broken server must not take the UI down
            emit(f"  [bold]{escape(server.name)}[/bold]  [red]{escape(str(exc))}[/red]")
            continue
        emit(f"  [bold]{escape(server.name)}[/bold]  [dim]{len(tools)} 个工具[/dim]")
        for tool in tools:
            description = (tool.description or "").strip().splitlines()
            summary = shorten(description[0], 100) if description else ""
            emit(f"    {escape(tool.name)}  [dim]{escape(summary)}[/dim]")


async def run_command(session: Session, line: str, emit: Emit) -> CommandResult:
    """Run a slash command, writing its output through `emit`."""
    command, _, arg = line.partition(" ")
    arg = arg.strip()
    name = command[1:].lower()
    if name in ("exit", "quit"):
        return CommandResult(exit=True)
    if name == "help":
        _help(session, emit)
    elif name == "clear":
        session.clear()
        emit("[dim]上下文已清空。[/dim]")
    elif name == "model":
        if arg:
            session.set_model(arg)
        emit(f"[dim]当前模型: {escape(session.model_name)}[/dim]")
    elif name == "yes":
        session.context.auto_approve = not session.context.auto_approve
        emit(f"[dim]自动批准已{'开启' if session.context.auto_approve else '关闭'}。[/dim]")
    elif name == "skills":
        _skills(session, emit)
    elif name == "mcp":
        await _mcp(session, emit)
    elif name in session.context.skills:
        return CommandResult(prompt=skill_message(session.context.skills[name], arg))
    else:
        emit(f"[red]未知命令 {escape(command)}，输入 /help 查看帮助。[/red]")
    return CommandResult()
