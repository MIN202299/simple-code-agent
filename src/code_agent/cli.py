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
    AgentsException,
    MaxTurnsExceeded,
    Model,
    Runner,
    ToolApprovalItem,
    set_default_openai_api,
    set_tracing_disabled,
)
from openai import OpenAIError
from rich.console import Console
from rich.markup import escape

from . import __version__
from .agent import build_agent
from .tools import AgentContext

# Approver: given a pending tool call, return "yes", "always" or "no".
Approver = Callable[[ToolApprovalItem], str]

HELP = """\
命令 / Commands:
  /help            显示帮助
  /clear           清空对话上下文
  /model [name]    查看或切换模型
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


def format_output(output: Any, max_lines: int = 6) -> str:
    lines = str(output).rstrip().splitlines() or ["(no output)"]
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
    ) -> None:
        self.context = AgentContext(workspace=workspace, auto_approve=auto_approve)
        self.model = model
        self.agent = build_agent(workspace, model)
        self.max_turns = max_turns
        self.console = console or Console()
        self.approver = approver or self._ask_approval
        self.history: list[Any] = []

    def set_model(self, model: str) -> None:
        self.model = model
        self.agent = build_agent(self.context.workspace, model)

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
                    call = format_call(name, getattr(raw, "arguments", None))
                    console.print(f"[bold cyan]●[/bold cyan] {escape(call)}", highlight=False)
                elif item.type == "tool_call_output_item":
                    console.print(f"[dim]{escape(format_output(item.output))}[/dim]")
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


def _handle_command(session: Session, line: str) -> bool:
    """Handle a slash command. Returns False when the REPL should exit."""
    console = session.console
    command, _, arg = line.partition(" ")
    arg = arg.strip()
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
    console.print("[dim]输入任务开始，/help 查看命令，Ctrl-D 退出。[/dim]")
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
            if not _handle_command(session, line):
                break
            continue
        _run(runner, session, line)


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

    session = Session(
        workspace, model=args.model, auto_approve=args.yes, max_turns=args.max_turns,
        console=console,
    )
    with asyncio.Runner() as runner:
        if args.prompt:
            return 0 if _run(runner, session, " ".join(args.prompt)) else 1
        repl(runner, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
