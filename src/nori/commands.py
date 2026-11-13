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
from .compact import COMPACT_AT
from .config import ConfigError, load_config, parse_token_count, save_config, update_config
from .i18n import LANGUAGES, get_language, set_language, t
from .permissions import MODES
from .pet import art as pet_art
from .render import format_tokens, shorten, tool_label
from .session import Session
from .skills import Skill
from .store import SessionRecord

Emit = Callable[[Any], None]


@dataclass(frozen=True)
class Command:
    name: str
    description: str  # English; translated when shown
    arg: str = ""  # argument hint, e.g. "[name]"; empty if the command takes none
    picker: bool = False  # without an argument, the TUI offers a list to choose from
    skill: bool = False

    @property
    def takes_arg(self) -> bool:
        return bool(self.arg)

    @property
    def usage(self) -> str:
        return f"/{self.name} {self.arg}".rstrip()


@dataclass(frozen=True)
class CommandResult:
    exit: bool = False
    prompt: str | None = None  # a message to send to the agent


@dataclass(frozen=True)
class Choice:
    """One row of a picker: selecting it runs `command`."""

    label: str
    description: str
    command: str
    current: bool = False


BUILTIN_COMMANDS = [
    Command("help", "Show help"),
    Command("model", "Switch the model", "[name]", picker=True),
    Command("clear", "Start a new conversation"),
    Command("compact", "Summarise the conversation to free up context", "[focus]"),
    Command("resume", "Resume an earlier session", "[number]", picker=True),
    Command("context", "Show or set the context window, e.g. /context 200k", "[size]"),
    Command("status", "Show model, endpoint and usage"),
    Command("skills", "List available skills"),
    Command("mcp", "List connected MCP servers and their tools"),
    Command("lang", "Switch the interface language", "[en|zh]", picker=True),
    Command("pet", "Meet your pet (name <name> · hide · show)", "[action]"),
    Command("mode", "Switch the permission mode", "[mode]", picker=True),
    Command("permissions", "Show or remove the allow rules of this project", "[remove N|clear]"),
    Command("undo", "Revert the file changes of the last turn"),
    Command("tasks", "List background commands"),
    Command("yes", "Toggle auto mode (approve everything)"),
    Command("exit", "Quit"),
]
ALIASES = {
    "quit": "exit",
    "new": "clear",
    "language": "lang",
    "models": "model",
    "rewind": "undo",
    "plan": "mode plan",
}
NO_RULES = 'No allow rules for this project. Choose "don\'t ask again" in an approval to add one.'
# Permission modes: English description, translated when shown.
MODE_DESCRIPTIONS = {
    "default": "ask before file changes and commands",
    "accept-edits": "apply file edits without asking; commands still ask",
    "plan": "read-only: research and propose a plan first",
    "auto": "approve everything without asking",
}


def all_commands(session: Session) -> list[Command]:
    """Built-in commands followed by one command per skill."""
    names = {command.name for command in BUILTIN_COMMANDS}
    skills = [
        Command(skill.name, skill.description, "[request]", skill=True)
        for skill in session.context.skills.values()
        if skill.name not in names
    ]
    return [*BUILTIN_COMMANDS, *skills]


def fuzzy_rank(query: str, text: str) -> int | None:
    """How well `query` matches `text`: lower is better, None is no match.

    A prefix beats a substring, which beats the letters merely appearing in order.
    """
    query, text = query.lower(), text.lower()
    if text.startswith(query):
        return 0
    if query in text:
        return 1
    position = 0
    for char in query:
        position = text.find(char, position) + 1
        if position == 0:
            return None
    return 2


def match_commands(session: Session, text: str) -> list[Command]:
    """Commands matching a partially typed `/name` (no arguments yet), best first."""
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    query = text[1:]
    ranked = []
    for order, command in enumerate(all_commands(session)):
        rank = fuzzy_rank(query, command.name)
        if rank is not None:
            ranked.append((rank, order, command))
    return [command for _rank, _order, command in sorted(ranked, key=lambda r: r[:2])]


def skill_message(skill: Skill, request: str) -> str:
    """The user message sent when a skill is invoked directly with `/<skill>`."""
    request = request or "Carry out this skill."
    return f"Use the following skill for this request.\n\n{skill.render()}\nRequest: {request}"


def _save(session: Session, **changes: Any) -> bool:
    """Write settings to the config file. Returns whether they were saved."""
    if session.config_path is None:
        return False
    try:
        update_config(session.config_path, **changes)
    except (ConfigError, OSError):
        return False
    return True


def _saved_note(saved: bool) -> str:
    return t(" (saved as the default)") if saved else t(" (this session only)")


def _help(session: Session, emit: Emit) -> None:
    rows = [(command.usage, t(command.description)) for command in BUILTIN_COMMANDS]
    rows.append(("/<skill> [request]", t("Use a skill directly")))
    width = max(len(usage) for usage, _ in rows) + 2
    for usage, description in rows:
        emit(Text.assemble("  ", (usage.ljust(width), "bold"), description))


def _skills(session: Session, emit: Emit) -> None:
    skills = session.context.skills
    if not skills:
        emit(f"[dim]{t('No skills found. Put them in .agents/skills/<name>/SKILL.md.')}[/dim]")
        return
    for skill in skills.values():
        emit(f"  [bold]/{escape(skill.name)}[/bold]  {escape(shorten(skill.description, 120))}")
        emit(f"    [dim]{escape(str(skill.directory))}[/dim]")


async def _mcp(session: Session, emit: Emit) -> None:
    if not session.mcp_servers:
        emit(f"[dim]{t('No MCP servers connected. Configure them in .mcp.json.')}[/dim]")
        return
    for server in session.mcp_servers:
        try:
            tools = await server.list_tools()
        except Exception as exc:  # a broken server must not take the UI down
            emit(f"  [bold]{escape(server.name)}[/bold]  [red]{escape(str(exc))}[/red]")
            continue
        count = t("{count} tools", count=len(tools))
        emit(f"  [bold]{escape(server.name)}[/bold]  [dim]{count}[/dim]")
        for tool in tools:
            description = (tool.description or "").strip().splitlines()
            summary = shorten(description[0], 100) if description else ""
            emit(f"    {escape(tool.name)}  [dim]{escape(summary)}[/dim]")


def extensions_summary(session: Session) -> str:
    return t(
        "{skills} skills · {servers} MCP servers",
        skills=len(session.context.skills),
        servers=len(session.mcp_servers),
    )


def _status(session: Session, emit: Emit) -> None:
    context = session.context
    on, off = t("on"), t("off")
    rows = [
        (t("Model"), session.model_name),
        *((t(label), value) for label, value in session.info.items()),
        (t("Workspace"), short_path(context.workspace)),
        (t("Language"), LANGUAGES[get_language()]),
        (t("Mode"), context.permissions.mode),
        (t("Extensions"), extensions_summary(session)),
        (
            t("Context"),
            f"{format_tokens(session.context_tokens)} / {format_tokens(session.context_window)}"
            f" tokens ({session.context_share:.0%}) · "
            + t("auto-compact {state}", state=on if session.auto_compact else off),
        ),
        (
            t("Total usage"),
            f"↑ {format_tokens(session.input_tokens)} ↓ {format_tokens(session.output_tokens)} tokens",
        ),
    ]
    width = max(cell_len(label) for label, _ in rows) + 2
    for label, value in rows:
        emit(Text.assemble("  ", (label, "dim"), " " * (width - cell_len(label)), value))


def _pet(session: Session, arg: str, emit: Emit) -> None:
    pet = session.pet
    if pet is None:
        emit(f"[dim]{t('The pet only lives in the interactive UI.')}[/dim]")
        return
    action, _, value = arg.partition(" ")
    if action == "name" and value.strip():
        pet.name = value.strip()[:20]
        pet.save()
    elif action in ("hide", "show"):
        pet.hidden = action == "hide"
        pet.save()
        message = t("{name} is hiding.", name=escape(pet.name))
        if not pet.hidden:
            message = t("{name} is back.", name=escape(pet.name))
        emit(f"[dim]{message}[/dim]")
        return
    lines = pet_art("happy")
    facts = [
        Text.assemble((pet.name, f"bold {ACCENT}"), f"  Lv.{pet.level} {pet.title}"),
        Text(
            t("XP {xp}/{next} · born {born}", xp=pet.xp, next=pet.next_level_xp, born=pet.born),
            style="dim",
        ),
        Text(
            t("Finished {tasks} tasks with you, using tools {tools} times", tasks=pet.tasks, tools=pet.tools),
            style="dim",
        ),
    ]
    for art_line, fact in zip(lines, facts):
        emit(Text.assemble("  ", (art_line, ACCENT), "  ", fact))


def describe_session(record: SessionRecord) -> str:
    when = record.updated.replace("T", " ")[:16]
    turns = t("{count} turns", count=record.turns)
    return f"{when} · {turns} · {record.title or t('(untitled)')}"


def _other_sessions(session: Session) -> list[SessionRecord]:
    if session.store is None:
        return []
    records = session.store.list(session.context.workspace)
    return [record for record in records if record.id != session.session_id]


def _resume(session: Session, arg: str, emit: Emit) -> None:
    if session.store is None:
        emit(f"[dim]{t('Saving sessions is turned off.')}[/dim]")
        return
    records = _other_sessions(session)
    if not arg:
        if not records:
            emit(f"[dim]{t('No sessions to resume in this workspace yet.')}[/dim]")
            return
        for number, record in enumerate(records, 1):
            emit(Text.assemble("  ", (f"{number:>2}. ", "bold"), describe_session(record)))
        emit(f"[dim]  {t('Type /resume <number> to resume one.')}[/dim]")
        return
    record = None
    if arg.isdigit() and 1 <= int(arg) <= len(records):
        record = records[int(arg) - 1]
    elif not arg.isdigit():
        record = session.store.load(arg)
    if record is None:
        emit(f"[red]{t('Session {id} not found.', id=escape(arg))}[/red]")
        return
    session.resume(record)
    session.ui.show_history(session.history)


def _save_window(session: Session, window: int) -> bool:
    """Save the window where it will be found again: on the model if it has an entry."""
    if session.config_path is None:
        return False
    try:
        config = load_config(session.config_path)
        for entry in config.get("models", []):
            if entry["name"] == session.model and "context_window" in entry:
                entry["context_window"] = window
                break
        else:
            config["context_window"] = window
        save_config(session.config_path, config)
    except (ConfigError, OSError):
        return False
    return True


def _set_window(session: Session, window: int) -> None:
    """Apply a window to the session: to the current model's entry if it has one."""
    session.context_window = window
    for entry in session.models:
        if entry.get("name") == session.model and "context_window" in entry:
            entry["context_window"] = window
            return
    session.default_context_window = window


def _context(session: Session, arg: str, emit: Emit) -> None:
    if arg:
        window = parse_token_count(arg)
        if window is None:
            message = t(
                "Cannot understand the size {value}; use a value like 128000, 200k or 1m.",
                value=escape(arg),
            )
            emit(f"[red]{message}[/red]")
            return
        _set_window(session, window)
        note = _saved_note(_save_window(session, window))
        emit(f"[dim]{t('Context window set to {size} tokens', size=f'{window:,}')}{note}[/dim]")
    threshold = int(session.context_window * COMPACT_AT)
    policy = t("auto-compact is off")
    if session.auto_compact:
        policy = t("auto-compacts at {size}", size=f"{threshold:,}")
    usage = t(
        "Window {window} tokens · {used} used ({share})",
        window=f"{session.context_window:,}",
        used=f"{session.context_tokens:,}",
        share=f"{session.context_share:.0%}",
    )
    emit(f"[dim]{usage} · {policy}[/dim]")


async def _compact(session: Session, instructions: str, emit: Emit) -> None:
    if not session.history:
        emit(f"[dim]{t('Nothing to compact yet.')}[/dim]")
        return
    try:
        result = await session.compact(instructions)
    except (AgentsException, OpenAIError) as exc:
        error = escape(f"{type(exc).__name__}: {exc}")
        emit(f"[red]{t('Compaction failed: {error}', error=error)}[/red]")
        return
    if result is None:
        emit(f"[dim]{t('The conversation is too short to compact.')}[/dim]")


async def _model(session: Session, arg: str, emit: Emit) -> None:
    if arg:
        session.set_model(arg)
        note = _saved_note(_save(session, model=arg))
        emit(f"[dim]{t('Model set to {model}', model=escape(arg))}{note}[/dim]")
        window = t("Context window: {size} tokens", size=f"{session.context_window:,}")
        emit(f"[dim]{window}[/dim]")
        return
    emit(f"[dim]{t('Current model: {model}', model=escape(session.model_name))}[/dim]")
    names = [name for name in await session.available_models() if name != session.model]
    if names:
        emit(f"[dim]{t('Available: {models}', models=escape(', '.join(names[:30])))}[/dim]")
    emit(f"[dim]{t('Type /model <name> to switch.')}[/dim]")


def _lang(session: Session, arg: str, emit: Emit) -> None:
    if arg:
        if not set_language(arg):
            emit(f"[red]{t('Unknown language {value}; use en or zh.', value=escape(arg))}[/red]")
            return
        note = _saved_note(_save(session, language=get_language()))
        emit(f"[dim]{t('Language set to {language}', language=LANGUAGES[get_language()])}{note}[/dim]")
        return
    emit(f"[dim]{t('Language: {language}', language=LANGUAGES[get_language()])}[/dim]")
    emit(f"[dim]{t('Type /lang en or /lang zh to switch.')}[/dim]")


def _mode(session: Session, arg: str, emit: Emit) -> None:
    permissions = session.context.permissions
    if arg:
        if not permissions.set_mode(arg.lower()):
            message = t("Unknown mode {value}; use one of: {modes}", value=escape(arg), modes=", ".join(MODES))
            emit(f"[red]{message}[/red]")
            return
    description = t(MODE_DESCRIPTIONS[permissions.mode])
    emit(f"[dim]{t('Mode: {mode}', mode=permissions.mode)} — {description}[/dim]")


def _permissions(session: Session, arg: str, emit: Emit) -> None:
    permissions = session.context.permissions
    action, _, value = arg.partition(" ")
    if action == "clear":
        permissions.clear_rules()
    elif action == "remove":
        removed = permissions.remove_rule(int(value) - 1) if value.strip().isdigit() else None
        if removed is None:
            emit(f"[red]{t('No rule with that number.')}[/red]")
            return
    if not permissions.rules:
        emit(f"[dim]{t(NO_RULES)}[/dim]")
        return
    emit(f"[dim]{t('Allowed without asking in this project:')}[/dim]")
    for number, rule in enumerate(permissions.rules, 1):
        tool, _, detail = rule.partition(":")
        emit(Text.assemble("  ", (f"{number:>2}. ", "bold"), tool_label(tool), f"  {detail}" if detail else ""))
    emit(f"[dim]  {t('Remove one with /permissions remove <number>, or all with /permissions clear.')}[/dim]")


def _undo(session: Session, emit: Emit) -> None:
    result = session.undo()
    if result is None:
        emit(f"[dim]{t('Nothing to undo: no file changes were made through the edit tools.')}[/dim]")
        return
    label, restored, deleted = result
    emit(f"[dim]{t('Reverted the file changes of: {turn}', turn=escape(label or '…'))}[/dim]")
    for name in restored:
        emit(f"  [dim]{t('restored')}[/dim]  {escape(name)}")
    for name in deleted:
        emit(f"  [dim]{t('deleted')}[/dim]  {escape(name)}")
    emit(f"[dim]  {t('Changes made by shell commands are not tracked and were not reverted.')}[/dim]")


def _tasks(session: Session, emit: Emit) -> None:
    tasks = session.context.shell_tasks.tasks
    if not tasks:
        emit(f"[dim]{t('No background commands.')}[/dim]")
        return
    for task in tasks.values():
        state = t("running") if task.running else t("exited ({code})", code=task.process.returncode)
        emit(Text.assemble("  ", (task.id, "bold"), f"  {state}  ", (shorten(task.command, 80), "dim")))


async def picker_choices(session: Session, name: str) -> tuple[str, list[Choice]] | None:
    """The list a command offers when run without an argument (None: no picker)."""
    name = ALIASES.get(name, name).partition(" ")[0]
    if name == "model":
        choices = []
        for model in await session.available_models():
            window = next(
                (m.get("context_window") for m in session.models if m["name"] == model), None
            )
            detail = t("{size} context", size=format_tokens(window)) if window else ""
            choices.append(Choice(model, detail, f"/model {model}", current=model == session.model))
        return t("Select a model"), choices
    if name == "resume":
        choices = [
            Choice(record.title or t("(untitled)"), describe_session(record).rsplit(" · ", 1)[0],
                   f"/resume {record.id}")
            for record in _other_sessions(session)
        ]  # fmt: skip
        return t("Resume a session"), choices
    if name == "mode":
        current = session.context.permissions.mode
        choices = [
            Choice(mode, t(MODE_DESCRIPTIONS[mode]), f"/mode {mode}", current=mode == current)
            for mode in MODES
        ]
        return t("Select a mode"), choices
    if name == "lang":
        choices = [
            Choice(label, code, f"/lang {code}", current=code == get_language())
            for code, label in LANGUAGES.items()
        ]
        return t("Select a language"), choices
    return None


async def run_command(session: Session, line: str, emit: Emit) -> CommandResult:
    """Run a slash command, writing its output through `emit`."""
    command, _, arg = line.partition(" ")
    arg = arg.strip()
    name = command[1:].lower()
    if name not in session.context.skills and name in ALIASES:
        name, _, alias_arg = ALIASES[name].partition(" ")
        arg = arg or alias_arg
    if name == "exit":
        return CommandResult(exit=True)
    if name == "help":
        _help(session, emit)
    elif name == "clear":
        session.clear()
        emit(f"[dim]{t('Started a new conversation.')}[/dim]")
    elif name == "resume":
        _resume(session, arg, emit)
    elif name == "context":
        _context(session, arg, emit)
    elif name == "compact":
        await _compact(session, arg, emit)
    elif name == "model":
        await _model(session, arg, emit)
    elif name == "lang":
        _lang(session, arg, emit)
    elif name == "yes":
        session.context.auto_approve = not session.context.auto_approve
        state = t("on") if session.context.auto_approve else t("off")
        emit(f"[dim]{t('Auto-approve is {state}.', state=state)}[/dim]")
    elif name == "mode":
        _mode(session, arg, emit)
    elif name == "permissions":
        _permissions(session, arg, emit)
    elif name == "undo":
        _undo(session, emit)
    elif name == "tasks":
        _tasks(session, emit)
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
        message = t("Unknown command {command}; type /help for help.", command=escape(command))
        emit(f"[red]{message}[/red]")
    return CommandResult()
