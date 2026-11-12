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
    resolve_settings,
    run_setup,
)
from .mcp_config import MCPConfigError, MCPServerSpec, build_server, load_mcp_specs
from .session import PlainUI, Session
from .skills import discover_skills
from .store import SessionStore


def run_plain_turn(runner: asyncio.Runner, session: Session, console: Console, text: str) -> bool:
    """Run one turn in plain mode, reporting errors instead of raising."""
    try:
        runner.run(session.run_turn(text))
        return True
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。[/yellow]")
    except MaxTurnsExceeded:
        console.print(
            f"\n[red]已达到单次任务的最大轮数 ({session.max_turns})，任务未完成。[/red]\n"
            "[dim]已完成的步骤保留在上下文中，输入\"继续\"可接着执行；"
            "可用 --max-turns 或配置文件的 max_turns 调大上限。[/dim]"
        )
    except (AgentsException, OpenAIError) as exc:
        console.print(f"\n[red]出错: {escape(f'{type(exc).__name__}: {exc}')}[/red]")
    return False


def plain_repl(runner: asyncio.Runner, session: Session, console: Console) -> None:
    """Line-oriented REPL, used when stdin or stdout is not a terminal."""
    console.print(
        f"[bold]{APP_NAME}[/bold] v{__version__}  "
        f"[dim]模型: {escape(session.model_name)}  工作区: {session.context.workspace}[/dim]"
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
    console.print("[bold yellow]该项目的 .mcp.json 定义了以下 MCP 服务器：[/bold yellow]")
    for spec in project:
        console.print(f"  {escape(spec.name)}: {escape(spec.describe())}", highlight=False)
    try:
        answer = input("启动这些服务器？它们可以执行任意代码。 [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        answer = ""
    if answer in ("y", "yes", "是"):
        return specs
    return [spec for spec in specs if spec.source != "project"]


async def connect_mcp(manager: MCPServerManager, console: Console) -> list[MCPServer]:
    """Connect all servers, reporting the ones that fail instead of aborting."""
    await manager.connect_all()
    for server in manager.failed_servers:
        error = manager.errors.get(server)
        console.print(
            f"[red]MCP 服务器 {escape(server.name)} 连接失败: "
            f"{escape(f'{type(error).__name__}: {error}')}[/red]"
        )
    return list(manager.active_servers)


def apply_settings(settings: Settings) -> None:
    """Point the SDK at the configured endpoint."""
    client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)
    set_default_openai_client(client, use_for_tracing=False)
    if settings.api == "chat":
        set_default_openai_api("chat_completions")
    # Tracing uploads run data to OpenAI; keep it opt-in.
    if os.environ.get("NORI_TRACING") != "1":
        set_tracing_disabled(True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description=f"{APP_NAME} — {TAGLINE}。",
    )
    parser.add_argument("prompt", nargs="*", help="直接执行该任务后退出；省略则进入交互界面")
    parser.add_argument("-m", "--model", help="模型名（覆盖配置文件）")
    parser.add_argument("--base-url", help="API 地址（覆盖配置文件）")
    parser.add_argument(
        "--api",
        choices=("responses", "chat"),
        help="使用 Responses API 还是 Chat Completions API；"
        "默认：自定义 base url 用 chat，否则用 responses",
    )
    parser.add_argument("--config", help="配置文件路径（默认 ~/.nori/config.json）")
    parser.add_argument("--setup", action="store_true", help="交互式创建或修改配置文件后退出")
    parser.add_argument("-c", "--continue", dest="resume_latest", action="store_true",
                        help="继续这个工作区最近的一次会话")
    parser.add_argument("-r", "--resume", metavar="ID", help="恢复指定的会话（ID 可只写开头几位）")
    parser.add_argument("--list-sessions", action="store_true", help="列出这个工作区保存的会话后退出")
    parser.add_argument("--no-save", action="store_true", help="不保存本次会话")
    parser.add_argument("-C", "--cwd", default=".", help="工作区目录（默认当前目录）")
    parser.add_argument(
        "-y", "--yes", action="store_true", help="自动批准写文件和执行命令，不再逐次确认"
    )
    parser.add_argument("--plain", action="store_true", help="不使用 TUI，改用逐行输出的简单模式")
    parser.add_argument("--no-mcp", action="store_true", help="不加载 MCP 服务器")
    parser.add_argument("--no-skills", action="store_true", help="不加载 skill")
    parser.add_argument(
        "--max-turns",
        type=int,
        help="单个任务的最大模型轮数（TUI 默认不限；一次性执行和简单模式默认 200）",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        metavar="TOKENS",
        help="模型的上下文窗口大小（默认 128000，可在配置文件中设置）",
    )
    parser.add_argument(
        "--no-auto-compact", action="store_true", help="上下文快满时不自动压缩会话"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    migrated = migrate_legacy_dir()
    if migrated is not None:
        console.print(f"[dim]已把旧的配置目录 ~/{LEGACY_DIR_NAME} 迁移到 {migrated}[/dim]")

    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    if args.setup:
        return 0 if run_setup(config_path, console) else 1

    workspace = Path(args.cwd).expanduser().resolve()
    if not workspace.is_dir():
        console.print(f"[red]工作区不存在: {workspace}[/red]")
        return 2

    store = SessionStore()
    if args.list_sessions:
        records = store.list(workspace, limit=50)
        for record in records:
            console.print(f"{record.id}  {escape(describe_session(record))}", highlight=False)
        if not records:
            console.print("[dim]这个工作区还没有保存的会话。[/dim]")
        return 0
    resumed = None
    if args.resume:
        resumed = store.load(args.resume)
        if resumed is None:
            console.print(f"[red]找不到会话 {escape(args.resume)}。用 --list-sessions 查看。[/red]")
            return 2
    elif args.resume_latest:
        resumed = store.latest(workspace)
        if resumed is None:
            console.print("[dim]这个工作区没有可继续的会话，开始新会话。[/dim]")

    try:
        config = load_config(config_path)
        if not config.get("api_key") and not os.environ.get("OPENAI_API_KEY"):
            if not interactive:
                console.print(
                    f"[red]没有找到 API Key。请运行 {COMMAND} --setup，或在 {config_path} "
                    "中设置 api_key，或设置环境变量 OPENAI_API_KEY。[/red]"
                )
                return 2
            console.print("[yellow]还没有配置 API Key，先完成配置。[/yellow]")
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
        console.print(f"[red]配置有误: {escape(str(exc))}[/red]")
        return 2
    apply_settings(settings)

    skills = {} if args.no_skills else discover_skills(workspace)
    specs: list[MCPServerSpec] = []
    if not args.no_mcp:
        try:
            specs = select_mcp_specs(load_mcp_specs(workspace), args.yes, console)
        except MCPConfigError as exc:
            console.print(f"[red]MCP 配置有误，已跳过: {escape(str(exc))}[/red]")

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
                "接口": f"{settings.base_url or 'OpenAI'} · {settings.api}",
                "密钥": mask_key(settings.api_key),
                "配置": short_path(config_path),
            }
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
