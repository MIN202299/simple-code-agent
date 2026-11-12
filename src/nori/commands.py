"""Slash commands, shared by the TUI and the plain REPL."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agents import AgentsException
from openai import OpenAIError
from rich.cells import cell_len
from rich.markup import escape
from rich.text import Text

from .branding import ACCENT, short_path
from .pet import art as pet_art
from .render import format_tokens, shorten
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
    Command("compact", "把对话压缩成摘要以释放上下文（可附带保留重点的说明）", takes_arg=True),
    Command("model", "查看或切换模型", takes_arg=True),
    Command("skills", "列出可用的 skill"),
    Command("mcp", "列出已连接的 MCP 服务器及其工具"),
    Command("status", "查看模型、接口和用量"),
    Command("pet", "看看你的宠物（/pet name <名字> 改名，/pet hide|show 隐藏或显示）", takes_arg=True),
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


def _status(session: Session, emit: Emit) -> None:
    context = session.context
    rows = [
        ("模型", session.model_name),
        *session.info.items(),
        ("工作区", short_path(context.workspace)),
        ("自动批准", "开启" if context.auto_approve else "关闭"),
        ("扩展", f"{len(context.skills)} 个 skill · {len(session.mcp_servers)} 个 MCP 服务器"),
        (
            "上下文",
            f"{format_tokens(session.context_tokens)} / {format_tokens(session.context_window)}"
            f" tokens ({session.context_share:.0%})"
            f" · 自动压缩{'开启' if session.auto_compact else '关闭'}",
        ),
        (
            "累计用量",
            f"↑ {format_tokens(session.input_tokens)} ↓ {format_tokens(session.output_tokens)} tokens",
        ),
    ]
    for label, value in rows:
        emit(Text.assemble("  ", (f"{label}  ", "dim"), " " * (8 - cell_len(label)), value))


def _pet(session: Session, arg: str, emit: Emit) -> None:
    pet = session.pet
    if pet is None:
        emit("[dim]宠物只在交互界面里出现。[/dim]")
        return
    action, _, value = arg.partition(" ")
    if action == "name" and value.strip():
        pet.name = value.strip()[:20]
        pet.save()
    elif action in ("hide", "show"):
        pet.hidden = action == "hide"
        pet.save()
        emit(f"[dim]{escape(pet.name)} {'躲起来了' if pet.hidden else '回来了'}。[/dim]")
        return
    lines = pet_art("happy")
    facts = [
        Text.assemble((pet.name, f"bold {ACCENT}"), f"  Lv.{pet.level} {pet.title}"),
        Text(f"经验 {pet.xp}/{pet.next_level_xp} · 生日 {pet.born}", style="dim"),
        Text(f"陪你完成了 {pet.tasks} 个任务，用了 {pet.tools} 次工具", style="dim"),
    ]
    for art_line, fact in zip(lines, facts):
        emit(Text.assemble("  ", (art_line, ACCENT), "  ", fact))


async def _compact(session: Session, instructions: str, emit: Emit) -> None:
    if not session.history:
        emit("[dim]还没有对话内容，无需压缩。[/dim]")
        return
    try:
        result = await session.compact(instructions)
    except (AgentsException, OpenAIError) as exc:
        emit(f"[red]压缩失败: {escape(f'{type(exc).__name__}: {exc}')}[/red]")
        return
    if result is None:
        emit("[dim]对话太短，无需压缩。[/dim]")


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
    elif name == "compact":
        await _compact(session, arg, emit)
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
    elif name == "status":
        _status(session, emit)
    elif name == "pet":
        _pet(session, arg, emit)
    elif name in session.context.skills:
        return CommandResult(prompt=skill_message(session.context.skills[name], arg))
    else:
        emit(f"[red]未知命令 {escape(command)}，输入 /help 查看帮助。[/red]")
    return CommandResult()
