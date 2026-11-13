"""Command-line entry point: picks the TUI, the plain REPL or one-shot mode."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
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
from .branding import (
    APP_NAME,
    COMMAND,
    LEGACY_DIR_NAME,
    TAGLINE,
    migrate_legacy_dir,
    short_path,
)
from .commands import describe_session, run_command
from .config import (
    UNATTENDED_MAX_TURNS,
    ConfigError,
    Settings,
    default_config_path,
    load_config,
    mask_key,
    parse_token_count,
    resolve_language,
    resolve_settings,
    run_setup,
)
from .i18n import normalize_language, set_language, t
from .mcp_config import MCPConfigError, MCPServerSpec, build_server, load_mcp_specs
from .session import PlainUI, Session
from .skills import discover_skills
from .store import SessionStore


MAX_TURNS_REACHED = "Reached the maximum of {limit} turns for one task; it is not finished."
MAX_TURNS_HINT = (
    'The finished steps are kept in context: say "continue" to carry on. '
    "Raise the limit with --max-turns or max_turns in the config file."
)
BAD_SIZE = "Cannot understand the size {value}; use a value like 128000, 200k or 1m."
NO_API_KEY = (
    "No API key found. Run {command} --setup, set api_key in {path}, "
    "or set the OPENAI_API_KEY environment variable."
)
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


def detect_language(argv: list[str] | None) -> str:
    """Find the interface language before the real parser (and its help) is built."""
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--lang")
    early.add_argument("--config")
    known, _ = early.parse_known_args(argv)
    path = Path(known.config).expanduser() if known.config else default_config_path()
    try:
        config = load_config(path)
    except ConfigError:
        config = {}  # reported properly later, once the language is known
    return resolve_language(config, language=known.lang)


def _token_count(text: str) -> int:
    value = parse_token_count(text)
    if value is None:
        raise argparse.ArgumentTypeError(t(BAD_SIZE, value=repr(text)))
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description=f"{APP_NAME} — {t(TAGLINE)}",
    )
    parser.add_argument("prompt", nargs="*", help=t("run this task and exit; omit it to open the interactive UI"))
    parser.add_argument("-m", "--model", help=t("model name (overrides the config file)"))
    parser.add_argument("--base-url", help=t("API base URL (overrides the config file)"))
    parser.add_argument(
        "--api",
        choices=("responses", "chat"),
        help=t(
            "use the Responses or the Chat Completions API; "
            "default: chat for a custom base URL, otherwise responses"
        ),
    )
    parser.add_argument("--config", help=t("config file path (default: ~/.nori/config.json)"))
    parser.add_argument("--setup", action="store_true", help=t("create or edit the config file interactively, then exit"))
    parser.add_argument("-c", "--continue", dest="resume_latest", action="store_true",
                        help=t("continue the most recent session in this workspace"))
    parser.add_argument("-r", "--resume", metavar="ID", help=t("resume a session by ID (a prefix is enough)"))
    parser.add_argument("--list-sessions", action="store_true", help=t("list the saved sessions of this workspace and exit"))
    parser.add_argument("--no-save", action="store_true", help=t("do not save this session"))
    parser.add_argument("-C", "--cwd", default=".", help=t("workspace directory (default: current directory)"))
    parser.add_argument(
        "-y", "--yes", action="store_true", help=t("approve file writes and commands automatically")
    )
    parser.add_argument("--plain", action="store_true", help=t("use plain line-by-line output instead of the TUI"))
    parser.add_argument("--no-mcp", action="store_true", help=t("do not load MCP servers"))
    parser.add_argument("--no-skills", action="store_true", help=t("do not load skills"))
    parser.add_argument(
        "--max-turns",
        type=int,
        help=t("maximum model turns per task (TUI: unlimited; one-shot and plain mode: 200)"),
    )
    parser.add_argument(
        "--context-window",
        type=_token_count,
        metavar="TOKENS",
        help=t("context window of the model, e.g. 128000, 200k, 1m (default: 128000)"),
    )
    parser.add_argument(
        "--no-auto-compact", action="store_true", help=t("do not compact the conversation automatically when the context fills up")
    )
    parser.add_argument(
        "--lang", metavar="LANG", help=t("interface language: en or zh (default: en)")
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    set_language(detect_language(argv))
    args = build_parser().parse_args(argv)
    if args.lang and normalize_language(args.lang) is None:
        print(t("Unknown language {value}; use en or zh.", value=args.lang), file=sys.stderr)
        return 2
    console = Console()
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    migrated = migrate_legacy_dir()
    if migrated is not None:
        console.print(
            f"[dim]{t('Moved the old config directory ~/{old} to {new}', old=LEGACY_DIR_NAME, new=migrated)}[/dim]"
        )

    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    if args.setup:
        return 0 if run_setup(config_path, console) else 1

    workspace = Path(args.cwd).expanduser().resolve()
    if not workspace.is_dir():
        console.print(f"[red]{t('Workspace does not exist: {path}', path=workspace)}[/red]")
        return 2

    store = SessionStore()
    if args.list_sessions:
        records = store.list(workspace, limit=50)
        for record in records:
            console.print(f"{record.id}  {escape(describe_session(record))}", highlight=False)
        if not records:
            console.print(f"[dim]{t('No saved sessions for this workspace yet.')}[/dim]")
        return 0
    resumed = None
    if args.resume:
        resumed = store.load(args.resume)
        if resumed is None:
            message = t("Session {id} not found. Use --list-sessions to see them.", id=escape(args.resume))
            console.print(f"[red]{message}[/red]")
            return 2
    elif args.resume_latest:
        resumed = store.latest(workspace)
        if resumed is None:
            console.print(f"[dim]{t('No session to continue in this workspace; starting a new one.')}[/dim]")

    try:
        config = load_config(config_path)
        if not config.get("api_key") and not os.environ.get("OPENAI_API_KEY"):
            if not interactive:
                console.print(f"[red]{t(NO_API_KEY, command=COMMAND, path=config_path)}[/red]")
                return 2
            console.print(f"[yellow]{t('No API key configured yet; let us set it up.')}[/yellow]")
            if not run_setup(config_path, console):
                return 2
            config = load_config(config_path)
        settings = resolve_settings(
            config,
            model=args.model,
            base_url=args.base_url,
            api=args.api,
            max_turns=args.max_turns,
            context_window=args.context_window,
            auto_compact=False if args.no_auto_compact else None,
        )
    except ConfigError as exc:
        console.print(f"[red]{t('Invalid configuration: {error}', error=escape(str(exc)))}[/red]")
        return 2
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
        try:
            servers = runner.run(connect_mcp(manager, console)) if specs else []
            use_tui = interactive and not args.plain and not args.prompt
            unattended_turns = settings.max_turns or UNATTENDED_MAX_TURNS
            session = Session(
                workspace,
                model=settings.model,
                auto_approve=args.yes,
                max_turns=settings.max_turns if use_tui else unattended_turns,
                context_window=settings.context_window,
                auto_compact=settings.auto_compact,
                store=None if args.no_save else store,
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
            if specs:
                runner.run(manager.cleanup_all())
    return 0


if __name__ == "__main__":
    sys.exit(main())
