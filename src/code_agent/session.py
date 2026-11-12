"""The conversation: runs turns against the agent and reports progress to a UI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from agents import Agent, MaxTurnsExceeded, Model, Runner, ToolApprovalItem
from agents.mcp import MCPServer
from rich.console import Console
from rich.markup import escape

from .agent import build_agent
from .config import DEFAULT_MAX_TURNS
from .render import parse_args, render_call, render_output
from .skills import Skill
from .tools import AgentContext

INTERRUPT_NOTE = "[Request interrupted by user]"
REJECTION_MESSAGE = "The user rejected this tool call."


class UI(Protocol):
    """What a front end must implement to display a turn."""

    def text_delta(self, delta: str) -> None:
        """A chunk of streamed assistant text."""

    def text_done(self) -> None:
        """The current assistant message is complete."""

    def tool_call(self, name: str, args: dict[str, Any]) -> None:
        """The agent called a tool."""

    def tool_output(self, name: str, args: dict[str, Any], output: Any) -> None:
        """A tool call finished."""

    async def approve(self, item: ToolApprovalItem) -> str:
        """Ask the user about a tool call. Returns "yes", "always" or "no"."""

    def turn_done(self, input_tokens: int, output_tokens: int) -> None:
        """The turn finished; token counts are for this turn."""


# Sync approver used by the plain UI and tests.
Approver = Callable[[ToolApprovalItem], str]


class PlainUI:
    """Line-oriented output for one-shot runs and non-interactive terminals."""

    def __init__(self, console: Console | None = None, approver: Approver | None = None) -> None:
        self.console = console or Console()
        self.approver = approver or self._ask
        self._in_text = False

    def text_delta(self, delta: str) -> None:
        self.console.print(delta, end="", markup=False, highlight=False)
        self._in_text = True

    def text_done(self) -> None:
        if self._in_text:
            self.console.print()
            self._in_text = False

    def tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.console.print(render_call(name, args), highlight=False)

    def tool_output(self, name: str, args: dict[str, Any], output: Any) -> None:
        self.console.print(render_output(name, args, output), highlight=False)

    async def approve(self, item: ToolApprovalItem) -> str:
        return self.approver(item)

    def turn_done(self, input_tokens: int, output_tokens: int) -> None:
        self.console.print(f"[dim]tokens: {input_tokens} in / {output_tokens} out[/dim]")

    def _ask(self, item: ToolApprovalItem) -> str:
        name = escape(item.tool_name or "tool")
        self.console.print(f"\n[bold yellow]需要确认[/bold yellow] {name}")
        try:
            answer = input("允许执行？ [y] 是 / [a] 本次会话总是允许该工具 / [n] 否: ")
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return "no"
        answer = answer.strip().lower()
        if answer in ("a", "always"):
            return "always"
        return "yes" if answer in ("y", "yes", "是") else "no"


def _item_type(item: Any) -> str | None:
    return item.get("type") if isinstance(item, dict) else getattr(item, "type", None)


def _call_id(item: Any) -> str | None:
    return item.get("call_id") if isinstance(item, dict) else getattr(item, "call_id", None)


def complete_history(items: list[Any]) -> list[Any]:
    """Drop tool calls that never got a result, so the history is valid to resend.

    An interrupted turn can end with a call whose output is missing; the API
    rejects that. Reasoning items directly before a dropped call go with it,
    since they may not be sent without the item they belong to.
    """
    answered = {_call_id(item) for item in items if _item_type(item) == "function_call_output"}
    kept: list[Any] = []
    for item in items:
        if _item_type(item) == "function_call" and _call_id(item) not in answered:
            while kept and _item_type(kept[-1]) == "reasoning":
                kept.pop()
            continue
        kept.append(item)
    while kept and _item_type(kept[-1]) == "reasoning":
        kept.pop()
    return kept


class Session:
    """Holds the conversation and runs turns against the agent."""

    def __init__(
        self,
        workspace: Path,
        model: str | Model | None = None,
        auto_approve: bool = False,
        max_turns: int = DEFAULT_MAX_TURNS,
        ui: UI | None = None,
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
        self.ui: UI = ui or PlainUI()
        self.history: list[Any] = []
        self.input_tokens = 0
        self.output_tokens = 0

    def _build_agent(self) -> Agent[AgentContext]:
        return build_agent(
            self.context.workspace, self.model, self.context.skills, self.mcp_servers
        )

    @property
    def model_name(self) -> str:
        if isinstance(self.model, str):
            return self.model
        return "(SDK 默认)" if self.model is None else type(self.model).__name__

    def set_model(self, model: str) -> None:
        self.model = model
        self.agent = self._build_agent()

    def clear(self) -> None:
        self.history = []

    async def run_turn(self, text: str) -> str | None:
        """Run one user turn to completion, pausing for tool approvals.

        Returns the agent's final answer. A turn that fails leaves the history
        untouched; a turn that is cancelled or runs out of model turns keeps the
        work finished so far, so the user can ask to continue.
        """
        run_input: Any = [*self.history, {"role": "user", "content": text}]
        calls: dict[str, tuple[str, dict[str, Any]]] = {}
        while True:
            result = Runner.run_streamed(
                self.agent, run_input, context=self.context, max_turns=self.max_turns
            )
            try:
                await self._stream(result, calls)
                if not result.interruptions:
                    break
                state = result.to_state()
                for item in result.interruptions:
                    name = item.tool_name or ""
                    # The SDK also pauses when it had to normalise the arguments (e.g.
                    # the model left out a defaulted one), so honour the policy here too.
                    if self.context.auto_approve or name in self.context.always_allowed:
                        decision = "yes"
                    else:
                        decision = await self.ui.approve(item)
                    if decision == "always":
                        self.context.always_allowed.add(name)
                    if decision in ("yes", "always"):
                        state.approve(item)
                    else:
                        state.reject(item, rejection_message=REJECTION_MESSAGE)
            except asyncio.CancelledError:
                result.cancel()
                self._keep_partial(result, INTERRUPT_NOTE)
                raise
            except MaxTurnsExceeded:
                self._keep_partial(result)
                raise
            run_input = state
        self.history = result.to_input_list()
        usage = result.context_wrapper.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.ui.turn_done(usage.input_tokens, usage.output_tokens)
        return result.final_output

    def _keep_partial(self, result: Any, note: str | None = None) -> None:
        """Keep what an unfinished turn already did, optionally followed by a note."""
        try:
            items = complete_history(result.to_input_list())
        except Exception:  # never let bookkeeping mask the original error
            return
        self.history = [*items, *([{"role": "user", "content": note}] if note else [])]

    async def _stream(self, result: Any, calls: dict[str, tuple[str, dict[str, Any]]]) -> None:
        """Forward streamed text, tool calls and tool results to the UI."""
        ui = self.ui
        try:
            async for event in result.stream_events():
                if event.type == "raw_response_event":
                    if event.data.type == "response.output_text.delta":
                        ui.text_delta(event.data.delta)
                elif event.type == "run_item_stream_event":
                    ui.text_done()
                    item = event.item
                    raw = item.raw_item
                    if item.type == "tool_call_item":
                        name = getattr(raw, "name", None) or "tool"
                        args = parse_args(getattr(raw, "arguments", None))
                        calls[getattr(raw, "call_id", "")] = (name, args)
                        ui.tool_call(name, args)
                    elif item.type == "tool_call_output_item":
                        name, args = calls.get(_call_id(raw) or "", ("tool", {}))
                        ui.tool_output(name, args, item.output)
        finally:
            ui.text_done()
