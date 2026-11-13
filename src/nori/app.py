"""The running application: connects everything and starts the chosen front end.

Kept apart from `cli.py` so that `--help`, `--version` and `--setup` do not pay
for importing the agent SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from agents import (
    AgentsException,
    MaxTurnsExceeded,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from agents.mcp import MCPServer, MCPServerManager
from openai import AsyncOpenAI, OpenAIError
from rich.console import Console
from rich.markup import escape

from . import __version__
from .branding import APP_NAME, short_path
from .commands import run_command
from .config import UNATTENDED_MAX_TURNS, Settings, mask_key
from .i18n import t
from .mcp_config import MCPConfigError, MCPServerSpec, build_server, load_mcp_specs
from .permissions import rules_path
from .session import MAX_TURNS_HINT, MAX_TURNS_REACHED, PlainUI, Session
from .skills import discover_skills
from .store import SessionRecord, SessionStore

YES_ANSWERS = ("y", "yes", "是")
MODEL_LIST_TIMEOUT = 8.0


def run_plain_turn(runner: asyncio.Runner, session: Session, console: Console, text: str) -> bool:
    """Run one turn in plain mode, reporting errors instead of raising."""
    try:
        runner.run(session.run_turn(text))
        return True
    except KeyboardInterrupt:
        console.print(f"\n[yellow]{t('Interrupted.')}[/yellow]")
    except MaxTurnsExceeded:
        console.print(
            f"\n[red]{t(MAX_TURNS_REACHED, limit=session.max_turns)}[/red]\n"
            f"[dim]{t(MAX_TURNS_HINT)}[/dim]"
        )
    except (AgentsException, OpenAIError) as exc:
        error = escape(f"{type(exc).__name__}: {exc}")
        console.print(f"\n[red]{t('Error: {error}', error=error)}[/red]")
    return False


def plain_repl(runner: asyncio.Runner, session: Session, console: Console) -> None:
    """Line-oriented REPL, used when stdin or stdout is not a terminal."""
    console.print(
        f"[bold]{APP_NAME}[/bold] v{__version__}  "
        f"[dim]{t('Model')}: {escape(session.model_name)}  "
        f"{t('Workspace')}: {session.context.workspace}[/dim]"
    )
    while True:
        try:
            line = input("> ").strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            console.print()
            break
        if not line:
            continue
        if line.startswith("!") and line[1:].strip():
            try:
                output, code = runner.run(session.user_shell(line[1:].strip()))
            except KeyboardInterrupt:
                console.print(f"\n[yellow]{t('Interrupted.')}[/yellow]")
                continue
            console.print(f"{output}\n[exit code: {code}]".lstrip(), markup=False, highlight=False)
            continue
        if not line.startswith("/"):
            run_plain_turn(runner, session, console, line)
            continue
        result = runner.run(run_command(session, line, console.print))
        if result.exit:
            break
        if result.prompt:
            run_plain_turn(runner, session, console, result.prompt)


def select_mcp_specs(
    specs: list[MCPServerSpec], auto_approve: bool, console: Console
) -> list[MCPServerSpec]:
    """Drop project-defined servers the user does not trust.

    A project's `.mcp.json` can start arbitrary commands, so servers from it are
    only started after the user agrees (or with `-y`).
    """
    project = [spec for spec in specs if spec.source == "project"]
    if not project or auto_approve:
        return specs
    console.print(
        f"[bold yellow]{t('This project defines these MCP servers in .mcp.json:')}[/bold yellow]"
    )
    for spec in project:
        console.print(f"  {escape(spec.name)}: {escape(spec.describe())}", highlight=False)
    try:
        answer = input(t("Start these servers? They can run arbitrary code. [y/N]: ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        answer = ""
    if answer in YES_ANSWERS:
        return specs
    return [spec for spec in specs if spec.source != "project"]


async def connect_mcp(manager: MCPServerManager, console: Console) -> list[MCPServer]:
    """Connect all servers, reporting the ones that fail instead of aborting."""
    await manager.connect_all()
    for server in manager.failed_servers:
        error = manager.errors.get(server)
        detail = escape(f"{type(error).__name__}: {error}")
        message = t(
            "MCP server {name} failed to connect: {error}", name=escape(server.name), error=detail
        )
        console.print(f"[red]{message}[/red]")
    return list(manager.active_servers)


def apply_settings(settings: Settings) -> AsyncOpenAI:
    """Point the SDK at the configured endpoint. Returns the client in use."""
    client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)
    set_default_openai_client(client, use_for_tracing=False)
    if settings.api == "chat":
        set_default_openai_api("chat_completions")
    # Tracing uploads run data to OpenAI; keep it opt-in.
    if os.environ.get("NORI_TRACING") != "1":
        set_tracing_disabled(True)
    return client


def model_lister(client: AsyncOpenAI):
    """A function listing the models the endpoint offers, for the /model picker."""

    async def fetch() -> list[str]:
        page = await client.models.list(timeout=MODEL_LIST_TIMEOUT)
        return [model.id for model in page.data]

    return fetch


def run(
    args: argparse.Namespace,
    settings: Settings,
    config_path: Path,
    workspace: Path,
    store: SessionStore,
    resumed: SessionRecord | None,
    console: Console,
    interactive: bool,
) -> int:
    """Start a session in the mode the arguments ask for. Returns the exit code."""
    client = apply_settings(settings)

    skills = {} if args.no_skills else discover_skills(workspace)
    specs: list[MCPServerSpec] = []
    if not args.no_mcp:
        try:
            specs = select_mcp_specs(load_mcp_specs(workspace), args.yes, console)
        except MCPConfigError as exc:
            console.print(
                f"[red]{t('Invalid MCP configuration, skipped: {error}', error=escape(str(exc)))}[/red]"
            )

    with asyncio.Runner() as runner:
        # connect_in_parallel gives every server its own long-lived task, which keeps
        # connect and cleanup on the same task even though each turn is a new one.
        manager = MCPServerManager(
            [build_server(spec, workspace) for spec in specs], connect_in_parallel=True
        )
        session: Session | None = None
        try:
            servers = runner.run(connect_mcp(manager, console)) if specs else []
            use_tui = interactive and not args.plain and not args.prompt
            unattended_turns = settings.max_turns or UNATTENDED_MAX_TURNS
            session = Session(
                workspace,
                model=settings.model,
                mode="auto" if args.yes else (args.mode or settings.mode),
                permissions_path=rules_path(),
                max_turns=settings.max_turns if use_tui else unattended_turns,
                context_window=settings.context_window,
                auto_compact=settings.auto_compact,
                store=None if args.no_save else store,
                retries=settings.retries,
                ui=PlainUI(console),
                skills=skills,
                mcp_servers=servers,
            )
            session.info = {
                "Endpoint": f"{settings.base_url or 'OpenAI'} · {settings.api}",
                "API key": mask_key(settings.api_key),
                "Config": short_path(config_path),
            }
            session.config_path = config_path
            session.context.search_engine = settings.search_engine
            session.models = [dict(entry) for entry in settings.models]
            session.default_context_window = settings.default_context_window
            session.fetch_models = model_lister(client)
            if resumed is not None:
                session.resume(resumed)
            if args.prompt:
                ok = run_plain_turn(runner, session, console, " ".join(args.prompt))
                return 0 if ok else 1
            if use_tui:
                from .pet import load_pet
                from .tui import Tui, history_file

                tui = Tui(session, pet=load_pet(), history=history_file())
                runner.run(tui.run(show_resumed=resumed is not None))
            else:
                if resumed is not None:
                    session.ui.show_history(session.history)
                plain_repl(runner, session, console)
        finally:
            if session is not None:
                session.close()
            if specs:
                runner.run(manager.cleanup_all())
    return 0

