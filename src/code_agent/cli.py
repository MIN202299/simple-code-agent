"""Command-line interface: interactive REPL and one-shot mode."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agents import (
    Agent,
    AgentsException,
    MaxTurnsExceeded,
    Model,
    Runner,
    ToolApprovalItem,
    set_default_openai_api,
    set_tracing_disabled,
)
from agents.mcp import MCPServer, MCPServerManager
from openai import OpenAIError
from rich.console import Console
from rich.markup import escape

from . import __version__
from .agent import build_agent
from .mcp_config import MCPConfigError, MCPServerSpec, build_server, load_mcp_specs
from .skills import Skill, discover_skills
from .tools import AgentContext

# Approver: given a pending tool call, return "yes", "always" or "no".
Approver = Callable[[ToolApprovalItem], str]

HELP = """\
命令 / Commands:
  /help            显示帮助
  /clear           清空对话上下文
  /model [name]    查看或切换模型
  /skills          列出可用的 skill
  /<skill> [要求]  直接使用某个 skill
  /mcp             列出已连接的 MCP 服务器及其工具
  /yes             开关自动批准（跳过写文件、执行命令前的确认）
  /exit            退出（也可以按 Ctrl-D）
运行中按 Ctrl-C 可中断当前任务。"""


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"arguments": raw}
    return parsed if isinstance(parsed, dict) else {"arguments": parsed}


def _short(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_call(name: str, raw_args: Any) -> str:
    """One-line summary of a tool call, e.g. `read_file(path=src/a.py)`."""
    args = _parse_args(raw_args)
    return f"{name}({', '.join(f'{k}={_short(v)}' for k, v in args.items())})"


def _output_text(output: Any) -> str:
    """Flatten tool output; MCP tools return content blocks rather than a string."""
    if isinstance(output, dict) and isinstance(output.get("text"), str):
        return output["text"]
    if isinstance(output, list):
        return "\n".join(_output_text(part) for part in output)
    return str(output)


def format_output(output: Any, max_lines: int = 6) -> str:
    lines = _output_text(output).rstrip().splitlines() or ["(no output)"]
    shown = [_short(line, 160) for line in lines[:max_lines]]
    if len(lines) > max_lines:
        shown.append(f"… (+{len(lines) - max_lines} 行)")
    return "\n".join(f"  ⎿ {line}" if i == 0 else f"    {line}" for i, line in enumerate(shown))


class Session:
    """Holds the conversation and runs turns against the agent."""

    def __init__(
        self,
        workspace: Path,
        model: str | Model | None = None,
        auto_approve: bool = False,
        max_turns: int = 50,
        console: Console | None = None,
        approver: Approver | None = None,
        skills: dict[str, Skill] | None = None,
        mcp_servers: list[MCPServer] | None = None,
    ) -> None:
        self.context = AgentContext(
            workspace=workspace, auto_approve=auto_approve, skills=dict(skills or {})
        )
        self.model = model
        self.mcp_servers = list(mcp_servers or [])
        self.agent = self._build_agent()
        self.max_turns = max_turns
        self.console = console or Console()
        self.approver = approver or self._ask_approval
        self.history: list[Any] = []

    def _build_agent(self) -> Agent[AgentContext]:
        return build_agent(
            self.context.workspace, self.model, self.context.skills, self.mcp_servers
        )

    def set_model(self, model: str) -> None:
        self.model = model
        self.agent = self._build_agent()

    def clear(self) -> None:
        self.history = []

    async def run_turn(self, text: str) -> str | None:
        """Run one user turn to completion, pausing for tool approvals.

        Returns the agent's final answer. The conversation history is only
        updated when the turn finishes, so a failed or interrupted turn is dropped.
        """
        run_input: Any = [*self.history, {"role": "user", "content": text}]
        while True:
            result = Runner.run_streamed(
                self.agent, run_input, context=self.context, max_turns=self.max_turns
            )
            try:
                await self._render(result)
            except asyncio.CancelledError:
                result.cancel()
                raise
            if not result.interruptions:
                break
            state = result.to_state()
            for item in result.interruptions:
                name = item.tool_name or ""
                # The SDK also pauses when it had to normalise the arguments (e.g. the
                # model left out a defaulted one), so honour the session policy here too.
                if self.context.auto_approve or name in self.context.always_allowed:
                    decision = "yes"
                else:
                    decision = self.approver(item)
                if decision == "always":
                    self.context.always_allowed.add(name)
                if decision in ("yes", "always"):
                    state.approve(item)
                else:
                    state.reject(item, rejection_message="The user rejected this tool call.")
            run_input = state
        self.history = result.to_input_list()
        usage = result.context_wrapper.usage
        self.console.print(
            f"[dim]tokens: {usage.input_tokens} in / {usage.output_tokens} out[/dim]"
        )
        return result.final_output

    async def _render(self, result: Any) -> None:
        """Print streamed text, tool calls and tool results as they arrive."""
        console = self.console
        in_text = False
        plan_calls: set[str] = set()
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                if event.data.type == "response.output_text.delta":
                    console.print(event.data.delta, end="", markup=False, highlight=False)
                    in_text = True
            elif event.type == "run_item_stream_event":
                if in_text:
                    console.print()
                    in_text = False
                item = event.item
                if item.type == "tool_call_item":
                    raw = item.raw_item
                    name = getattr(raw, "name", None) or "tool"
                    if name == "update_plan":
                        # The checklist itself is printed when the output arrives.
                        plan_calls.add(getattr(raw, "call_id", ""))
                        console.print("[bold cyan]●[/bold cyan] 计划", highlight=False)
                        continue
                    call = format_call(name, getattr(raw, "arguments", None))
                    console.print(f"[bold cyan]●[/bold cyan] {escape(call)}", highlight=False)
                elif item.type == "tool_call_output_item":
                    raw = item.raw_item
                    call_id = raw.get("call_id") if isinstance(raw, dict) else None
                    max_lines = 50 if call_id in plan_calls else 6
                    console.print(f"[dim]{escape(format_output(item.output, max_lines))}[/dim]")
        if in_text:
            console.print()

    def _ask_approval(self, item: ToolApprovalItem) -> str:
        console = self.console
        args = _parse_args(item.arguments)
        console.print(f"\n[bold yellow]需要确认[/bold yellow] {escape(item.tool_name or 'tool')}")
        for key, value in args.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            lines = text.splitlines() or [""]
            if len(lines) > 20:
                lines = [*lines[:20], f"… (+{len(lines) - 20} 行)"]
            if len(lines) == 1:
                console.print(f"  [bold]{escape(key)}[/bold]: {escape(lines[0])}", highlight=False)
            else:
                console.print(f"  [bold]{escape(key)}[/bold]:")
                for line in lines:
                    console.print(f"    {escape(line)}", highlight=False)
        try:
            answer = input("允许执行？ [y] 是 / [a] 本次会话总是允许该工具 / [n] 否: ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return "no"
        answer = answer.strip().lower()
        if answer in ("a", "always"):
            return "always"
        return "yes" if answer in ("y", "yes", "是") else "no"


def _run(runner: asyncio.Runner, session: Session, text: str) -> bool:
    """Run one turn, reporting errors instead of raising. Returns success."""
    console = session.console
    try:
        runner.run(session.run_turn(text))
        return True
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断，本轮对话未计入上下文。[/yellow]")
    except MaxTurnsExceeded:
        console.print(
            f"\n[red]已达到最大轮数 ({session.max_turns})，任务未完成。"
            "可用 --max-turns 调大。[/red]"
        )
    except (AgentsException, OpenAIError) as exc:
        console.print(f"\n[red]出错: {escape(f'{type(exc).__name__}: {exc}')}[/red]")
    return False


def skill_message(skill: Skill, request: str) -> str:
    """The user message sent when a skill is invoked directly with `/<skill>`."""
    request = request or "Carry out this skill."
    return f"Use the following skill for this request.\n\n{skill.render()}\nRequest: {request}"


def _print_skills(session: Session) -> None:
    console = session.console
    skills = session.context.skills
    if not skills:
        console.print("[dim]没有找到 skill。可放在 .agents/skills/<name>/SKILL.md。[/dim]")
        return
    for skill in skills.values():
        console.print(f"  [bold]/{escape(skill.name)}[/bold]  {escape(skill.description)}")
        console.print(f"    [dim]{escape(str(skill.directory))}[/dim]", highlight=False)


async def _print_mcp(session: Session) -> None:
    console = session.console
    if not session.mcp_servers:
        console.print("[dim]没有已连接的 MCP 服务器。可在 .mcp.json 中配置。[/dim]")
        return
    for server in session.mcp_servers:
        try:
            tools = await server.list_tools()
        except Exception as exc:  # a broken server must not take the REPL down
            console.print(f"  [bold]{escape(server.name)}[/bold]  [red]{escape(str(exc))}[/red]")
            continue
        console.print(f"  [bold]{escape(server.name)}[/bold]  [dim]{len(tools)} 个工具[/dim]")
        for tool in tools:
            description = (tool.description or "").strip().splitlines()
            summary = _short(description[0], 100) if description else ""
            console.print(f"    {escape(tool.name)}  [dim]{escape(summary)}[/dim]", highlight=False)


def _handle_command(runner: asyncio.Runner, session: Session, line: str) -> bool:
    """Handle a slash command. Returns False when the REPL should exit."""
    console = session.console
    command, _, arg = line.partition(" ")
    arg = arg.strip()
    skill = session.context.skills.get(command[1:].lower())
    if command in ("/exit", "/quit"):
        return False
    if command == "/help":
        console.print(HELP)
    elif command == "/clear":
        session.clear()
        console.print("[dim]上下文已清空。[/dim]")
    elif command == "/model":
        if arg:
            session.set_model(arg)
        console.print(f"[dim]当前模型: {session.model or '(SDK 默认)'}[/dim]")
    elif command == "/yes":
        session.context.auto_approve = not session.context.auto_approve
        state = "开启" if session.context.auto_approve else "关闭"
        console.print(f"[dim]自动批准已{state}。[/dim]")
    elif command == "/skills":
        _print_skills(session)
    elif command == "/mcp":
        runner.run(_print_mcp(session))
    elif skill is not None:
        _run(runner, session, skill_message(skill, arg))
    else:
        console.print(f"[red]未知命令 {escape(command)}，输入 /help 查看帮助。[/red]")
    return True


def _make_reader() -> Callable[[], str]:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return lambda: input("> ")
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    history_file = Path.home() / ".code_agent_history"
    prompt_session: PromptSession[str] = PromptSession(history=FileHistory(str(history_file)))
    return lambda: prompt_session.prompt("> ")


def repl(runner: asyncio.Runner, session: Session) -> None:
    console = session.console
    console.print(
        f"[bold]Code Agent[/bold] v{__version__}  "
        f"[dim]模型: {session.model or '(SDK 默认)'}  工作区: {session.context.workspace}[/dim]"
    )
    console.print(
        f"[dim]skills: {len(session.context.skills)}  "
        f"MCP 服务器: {len(session.mcp_servers)}  "
        "输入任务开始，/help 查看命令，Ctrl-D 退出。[/dim]"
    )
    read = _make_reader()
    while True:
        try:
            line = read().strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            console.print()
            break
        if not line:
            continue
        if line.startswith("/"):
            if not _handle_command(runner, session, line):
                break
            continue
        _run(runner, session, line)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="code-agent",
        description="基于 OpenAI Agents SDK 的命令行 code agent。",
    )
    parser.add_argument("prompt", nargs="*", help="直接执行该任务后退出；省略则进入交互模式")
    parser.add_argument(
        "-m",
        "--model",
        default=os.environ.get("CODE_AGENT_MODEL"),
        help="模型名（默认读取 CODE_AGENT_MODEL，否则用 SDK 默认模型）",
    )
    parser.add_argument("-C", "--cwd", default=".", help="工作区目录（默认当前目录）")
    parser.add_argument(
        "-y", "--yes", action="store_true", help="自动批准写文件和执行命令，不再逐次确认"
    )
    parser.add_argument(
        "--api",
        choices=("responses", "chat"),
        default=os.environ.get("CODE_AGENT_API", "responses"),
        help="使用 Responses API 还是 Chat Completions API（第三方兼容服务通常选 chat）",
    )
    parser.add_argument("--no-mcp", action="store_true", help="不加载 MCP 服务器")
    parser.add_argument("--no-skills", action="store_true", help="不加载 skill")
    parser.add_argument("--max-turns", type=int, default=50, help="单个任务的最大模型轮数")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    console = Console()
    workspace = Path(args.cwd).expanduser().resolve()
    if not workspace.is_dir():
        console.print(f"[red]工作区不存在: {workspace}[/red]")
        return 2
    if not os.environ.get("OPENAI_API_KEY"):
        console.print("[red]请先设置环境变量 OPENAI_API_KEY。[/red]")
        return 2

    if args.api == "chat":
        set_default_openai_api("chat_completions")
    # Tracing uploads run data to OpenAI; keep it opt-in.
    if os.environ.get("CODE_AGENT_TRACING") != "1":
        set_tracing_disabled(True)

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
                model=args.model,
                auto_approve=args.yes,
                max_turns=args.max_turns,
                console=console,
                skills=skills,
                mcp_servers=servers,
            )
            if args.prompt:
                return 0 if _run(runner, session, " ".join(args.prompt)) else 1
            repl(runner, session)
        finally:
            if specs:
                runner.run(manager.cleanup_all())
    return 0


if __name__ == "__main__":
    sys.exit(main())
