"""The conversation: runs turns against the agent and reports progress to a UI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from agents import Agent, MaxTurnsExceeded, Model, RunConfig, Runner, ToolApprovalItem
from agents.run_config import CallModelData, ModelInputData
from agents.mcp import MCPServer
from rich.console import Console
from rich.markup import escape

from .agent import build_agent
from .compact import (
    COMPACT_AT,
    DEFAULT_CONTEXT_WINDOW,
    KEEP_RECENT_MAX,
    KEEP_RECENT_SHARE,
    estimate_tokens,
    find_cut,
    summarise,
    summary_item,
)
from .config import DEFAULT_MAX_TURNS
from .pet import Pet
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

    def tool_call(self, call_id: str, name: str, args: dict[str, Any]) -> None:
        """The agent called a tool."""

    def tool_output(self, call_id: str, name: str, args: dict[str, Any], output: Any) -> None:
        """A tool call finished."""

    def usage(self, input_tokens: int, output_tokens: int) -> None:
        """Token counts of one model response; the input side is the context size."""

    def compaction(self, stage: str, before: int = 0, after: int = 0) -> None:
        """Context compaction progress: stage is "start", "done" or "failed"."""

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

    def tool_call(self, call_id: str, name: str, args: dict[str, Any]) -> None:
        self.console.print(render_call(name, args), highlight=False)

    def tool_output(self, call_id: str, name: str, args: dict[str, Any], output: Any) -> None:
        self.console.print(render_output(name, args, output), highlight=False)

    def usage(self, input_tokens: int, output_tokens: int) -> None:
        pass

    def compaction(self, stage: str, before: int = 0, after: int = 0) -> None:
        if stage == "done":
            self.console.print(f"[dim]已压缩上下文：约 {before:,} → {after:,} tokens[/dim]")
        elif stage == "failed":
            self.console.print("[yellow]压缩上下文失败，继续使用完整上下文。[/yellow]")

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
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        auto_compact: bool = True,
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
        self.context_tokens = 0  # size of the conversation as last seen by the model
        self.context_window = context_window
        self.auto_compact = auto_compact
        # During a turn: how many leading items of the run's input have been replaced
        # by a summary, and that summary. The SDK keeps the full list; we swap the
        # prefix out on every model call and once more when the turn ends.
        self._compacted: tuple[int, dict[str, Any]] | None = None
        self._compact_floor = 0  # context size right after the last compaction
        self._usage_reported = False
        self.info: dict[str, str] = {}  # endpoint details shown by /status
        self.pet: Pet | None = None

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
        self.context_tokens = 0
        self._compact_floor = 0

    # ------------------------------------------------------------ compaction

    @property
    def context_share(self) -> float:
        """How full the context window is, from 0 to 1 (or more)."""
        return self.context_tokens / self.context_window if self.context_window else 0.0

    def _collapse(self, items: list[Any]) -> list[Any]:
        """Apply the summary made during this turn to the run's full item list."""
        if self._compacted is None or len(items) < self._compacted[0]:
            return items
        count, summary = self._compacted
        return [summary, *items[count:]]

    async def _summarise_prefix(
        self, items: list[Any], keep_tokens: int, instructions: str = ""
    ) -> tuple[int, dict[str, Any]] | None:
        """Summarise the older part of `items`. Returns (items replaced, summary)."""
        cut = find_cut(items, keep_tokens)
        if cut < 2:  # nothing worth summarising
            return None
        text, used_in, used_out = await summarise(
            self.model, items[:cut], self.context_window, instructions
        )
        self.input_tokens += used_in
        self.output_tokens += used_out
        if not text:
            return None
        return cut, summary_item(text)

    async def compact(self, instructions: str = "") -> tuple[int, int] | None:
        """Summarise the whole conversation now. Returns (before, after) token estimates."""
        before = max(self.context_tokens, estimate_tokens(self.history))
        self.ui.compaction("start", before)
        try:
            compacted = await self._summarise_prefix(self.history, 0, instructions)
        except BaseException:
            self.ui.compaction("failed", before)
            raise
        if compacted is None:
            return None
        cut, summary = compacted
        self.history = [summary, *self.history[cut:]]
        self.context_tokens = self._compact_floor = estimate_tokens(self.history)
        self.ui.compaction("done", before, self.context_tokens)
        return before, self.context_tokens

    async def _filter_model_input(self, data: CallModelData[AgentContext]) -> ModelInputData:
        """Called by the SDK before every model call: compact when the window fills up."""
        model_data = data.model_data
        items = self._collapse(list(model_data.input))
        estimate = estimate_tokens(items)
        tokens = max(self.context_tokens, estimate)
        if not self._usage_reported:
            self.context_tokens = estimate  # the provider sends no usage: show our estimate
        due = tokens >= self.context_window * COMPACT_AT and tokens > self._compact_floor * 1.25
        if self.auto_compact and due:
            keep = min(KEEP_RECENT_MAX, int(self.context_window * KEEP_RECENT_SHARE))
            self.ui.compaction("start", tokens)
            try:
                compacted = await self._summarise_prefix(items, keep)
            except Exception:  # a failed summary must not fail the turn
                compacted = None
                self.ui.compaction("failed", tokens)
            if compacted is not None:
                cut, summary = compacted
                # `cut` counts items of the collapsed list; convert it to the full list,
                # where an earlier summary stands for `replaced` items instead of one.
                replaced = self._compacted[0] - 1 if self._compacted else 0
                self._compacted = (replaced + cut, summary)
                items = [summary, *items[cut:]]
                self.context_tokens = self._compact_floor = estimate_tokens(items)
                self.ui.compaction("done", tokens, self.context_tokens)
        return ModelInputData(input=items, instructions=model_data.instructions)

    async def run_turn(self, text: str) -> str | None:
        """Run one user turn to completion, pausing for tool approvals.

        Returns the agent's final answer. A turn that fails leaves the history
        untouched; a turn that is cancelled or runs out of model turns keeps the
        work finished so far, so the user can ask to continue.
        """
        run_input: Any = [*self.history, {"role": "user", "content": text}]
        calls: dict[str, tuple[str, dict[str, Any]]] = {}
        self._compacted = None
        run_config = RunConfig(call_model_input_filter=self._filter_model_input)
        while True:
            result = Runner.run_streamed(
                self.agent,
                run_input,
                context=self.context,
                max_turns=self.max_turns,
                run_config=run_config,
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
        self.history = self._collapse(result.to_input_list())
        self._compacted = None
        usage = result.context_wrapper.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.ui.turn_done(usage.input_tokens, usage.output_tokens)
        return result.final_output

    def _keep_partial(self, result: Any, note: str | None = None) -> None:
        """Keep what an unfinished turn already did, optionally followed by a note."""
        try:
            items = complete_history(self._collapse(result.to_input_list()))
        except Exception:  # never let bookkeeping mask the original error
            return
        self._compacted = None
        self.history = [*items, *([{"role": "user", "content": note}] if note else [])]

    async def _stream(self, result: Any, calls: dict[str, tuple[str, dict[str, Any]]]) -> None:
        """Forward streamed text, tool calls and tool results to the UI."""
        ui = self.ui
        try:
            async for event in result.stream_events():
                if event.type == "raw_response_event":
                    if event.data.type == "response.output_text.delta":
                        ui.text_delta(event.data.delta)
                    elif event.data.type == "response.completed":
                        usage = getattr(event.data.response, "usage", None)
                        if usage is not None and usage.input_tokens:
                            self._usage_reported = True
                            self.context_tokens = usage.input_tokens + usage.output_tokens
                            ui.usage(usage.input_tokens, usage.output_tokens)
                elif event.type == "run_item_stream_event":
                    ui.text_done()
                    item = event.item
                    raw = item.raw_item
                    if item.type == "tool_call_item":
                        name = getattr(raw, "name", None) or "tool"
                        args = parse_args(getattr(raw, "arguments", None))
                        call_id = getattr(raw, "call_id", "") or ""
                        calls[call_id] = (name, args)
                        ui.tool_call(call_id, name, args)
                    elif item.type == "tool_call_output_item":
                        call_id = _call_id(raw) or ""
                        name, args = calls.get(call_id, ("tool", {}))
                        ui.tool_output(call_id, name, args, item.output)
        finally:
            ui.text_done()
