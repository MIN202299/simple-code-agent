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
from .commands import run_command
from .config import (
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


def run_plain_turn(runner: asyncio.Runner, session: Session, console: Console, text: str) -> bool:
    """Run one turn in plain mode, reporting errors instead of raising."""
    try:
        runner.run(session.run_turn(text))
        return True
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。[/yellow]")
    except MaxTurnsExceeded:
        console.print(
            f"\n[red]已达到最大轮数 ({session.max_turns})，任务未完成。"
            "可用 --max-turns 调大。[/red]"
        )
    except (AgentsException, OpenAIError) as exc:
        console.print(f"\n[red]出错: {escape(f'{type(exc).__name__}: {exc}')}[/red]")
    return False


def plain_repl(runner: asyncio.Runner, session: Session, console: Console) -> None:
    """Line-oriented REPL, used when stdin or stdout is not a terminal."""
    console.print(
        f"[bold]Code Agent[/bold] v{__version__}  "
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
    if os.environ.get("CODE_AGENT_TRACING") != "1":
        set_tracing_disabled(True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-agent",
        description="基于 OpenAI Agents SDK 的命令行 code agent。",
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
    parser.add_argument("--config", help="配置文件路径（默认 ~/.code_agent/config.json）")
    parser.add_argument("--setup", action="store_true", help="交互式创建或修改配置文件后退出")
    parser.add_argument("-C", "--cwd", default=".", help="工作区目录（默认当前目录）")
    parser.add_argument(
        "-y", "--yes", action="store_true", help="自动批准写文件和执行命令，不再逐次确认"
    )
    parser.add_argument("--plain", action="store_true", help="不使用 TUI，改用逐行输出的简单模式")
    parser.add_argument("--no-mcp", action="store_true", help="不加载 MCP 服务器")
    parser.add_argument("--no-skills", action="store_true", help="不加载 skill")
    parser.add_argument("--max-turns", type=int, default=50, help="单个任务的最大模型轮数")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    if args.setup:
        return 0 if run_setup(config_path, console) else 1

    workspace = Path(args.cwd).expanduser().resolve()
    if not workspace.is_dir():
        console.print(f"[red]工作区不存在: {workspace}[/red]")
        return 2

    try:
        config = load_config(config_path)
        if not config.get("api_key") and not os.environ.get("OPENAI_API_KEY"):
            if not interactive:
                console.print(
                    f"[red]没有找到 API Key。请运行 code-agent --setup，或在 {config_path} "
                    "中设置 api_key，或设置环境变量 OPENAI_API_KEY。[/red]"
                )
                return 2
            console.print("[yellow]还没有配置 API Key，先完成配置。[/yellow]")
            if not run_setup(config_path, console):
                return 2
            config = load_config(config_path)
        settings = resolve_settings(
            config, model=args.model, base_url=args.base_url, api=args.api
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
            session = Session(
                workspace,
                model=settings.model,
                auto_approve=args.yes,
                max_turns=args.max_turns,
                ui=PlainUI(console),
                skills=skills,
                mcp_servers=servers,
            )
            if args.prompt:
                ok = run_plain_turn(runner, session, console, " ".join(args.prompt))
                return 0 if ok else 1
            if interactive and not args.plain:
                from .tui import Tui, history_file

                endpoint = settings.base_url or "OpenAI"
                subtitle = f"{endpoint} · {settings.api} · key {mask_key(settings.api_key)}"
                runner.run(Tui(session, subtitle=subtitle, history=history_file()).run())
            else:
                plain_repl(runner, session, console)
        finally:
            if specs:
                runner.run(manager.cleanup_all())
    return 0


if __name__ == "__main__":
    sys.exit(main())
