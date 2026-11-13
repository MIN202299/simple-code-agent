"""Command-line entry point: picks the TUI, the plain REPL or one-shot mode."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from . import __version__
from .branding import APP_NAME, COMMAND, LEGACY_DIR_NAME, TAGLINE, migrate_legacy_dir
from .config import (
    ConfigError,
    default_config_path,
    load_config,
    parse_token_count,
    resolve_language,
    resolve_settings,
    run_setup,
)
from .i18n import normalize_language, set_language, t
from .permissions import MODES
from .store import SessionStore

BAD_SIZE = "Cannot understand the size {value}; use a value like 128000, 200k or 1m."
NO_API_KEY = (
    "No API key found. Run {command} --setup, set api_key in {path}, "
    "or set the OPENAI_API_KEY environment variable."
)


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
    parser.add_argument(
        "--mode",
        choices=MODES,
        help=t("permission mode: default, accept-edits, plan or auto (default: default)"),
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
        from .commands import describe_session

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
    from .app import run  # imported late: this is what pulls in the agent SDK

    return run(args, settings, config_path, workspace, store, resumed, console, interactive)


if __name__ == "__main__":
    sys.exit(main())
