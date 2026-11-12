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

from .agent import build_agent, build_subagent
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
from .pet import Pet
from .render import call_title, parse_args, render_call, render_output
from .skills import Skill
from .store import TITLE_CHARS, SessionRecord, SessionStore, new_session_id
from .tools import AgentContext

INTERRUPT_NOTE = "[Request interrupted by user]"
SUBAGENT_MAX_TURNS = 100
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

    def tool_progress(self, call_id: str, text: str) -> None:
        """What a long-running tool call (a sub-agent) is doing right now."""

    def show_history(self, items: list[Any]) -> None:
        """Display a restored conversation."""

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

    def tool_progress(self, call_id: str, text: str) -> None:
        self.console.print(f"  [dim]│ {escape(text)}[/dim]", highlight=False)

    def show_history(self, items: list[Any]) -> None:
        turns = sum(1 for item in items if isinstance(item, dict) and item.get("role") == "user")
        self.console.print(f"[dim]已恢复会话（{turns} 轮对话）。[/dim]")

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
        max_turns: int | None = None,
        ui: UI | None = None,
        skills: dict[str, Skill] | None = None,
        mcp_servers: list[MCPServer] | None = None,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        auto_compact: bool = True,
        store: SessionStore | None = None,
    ) -> None:
        self.context = AgentContext(
            workspace=workspace, auto_approve=auto_approve, skills=dict(skills or {})
        )
        self.context.spawn = self.run_subagent
        self.store = store  # None: conversations are not saved
        self.session_id = new_session_id()
        self._created = ""
        self._approval_lock = asyncio.Lock()  # sub-agents run in parallel; ask one at a time
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
        """Start a new conversation. The old one stays on disk and can be resumed."""
        self.history = []
        self.context_tokens = 0
        self._compact_floor = 0
        self.session_id = new_session_id()
        self._created = ""

    # ----------------------------------------------------------- persistence

    def _title(self) -> str:
        for item in self.history:
            if isinstance(item, dict) and item.get("role") == "user":
                text = item.get("content")
                if isinstance(text, str) and not text.startswith(("[The earlier part", "[Request")):
                    return " ".join(text.split())[:TITLE_CHARS]
        return ""

    def save(self) -> None:
        """Persist the conversation so it can be resumed."""
        if self.store is None or not self.history:
            return
        record = SessionRecord(
            id=self.session_id,
            workspace=str(self.context.workspace),
            title=self._title(),
            model=self.model_name,
            history=self.history,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            context_tokens=self.context_tokens,
        )
        if self._created:
            record.created = self._created
        else:
            self._created = record.created
        self.store.save(record)

    def resume(self, record: SessionRecord) -> None:
        """Continue a saved conversation."""
        self.session_id = record.id
        self._created = record.created
        self.history = list(record.history)
        self.input_tokens = record.input_tokens
        self.output_tokens = record.output_tokens
        self.context_tokens = record.context_tokens or estimate_tokens(self.history)
        self._compact_floor = 0
        self._usage_reported = False

    # ------------------------------------------------------------- approvals

    async def _decide(self, result: Any) -> Any:
        """Resolve the pending tool approvals of a paused run; returns its state."""
        state = result.to_state()
        for item in result.interruptions:
            name = item.tool_name or ""
            async with self._approval_lock:
                # The SDK also pauses when it had to normalise the arguments (e.g. the
                # model left out a defaulted one), so honour the session policy here too.
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
        return state

    # ------------------------------------------------------------ sub-agents

    async def run_subagent(self, call_id: str, agent_type: str, prompt: str) -> str:
        """Run a sub-agent in a fresh context and return its final report."""
        agent = build_subagent(self.context.workspace, agent_type, self.model, self.mcp_servers)
        run_input: Any = prompt
        tool_calls = 0
        while True:
            result = Runner.run_streamed(
                agent, run_input, context=self.context, max_turns=SUBAGENT_MAX_TURNS
            )
            try:
                async for event in result.stream_events():
                    if event.type != "run_item_stream_event":
                        continue
                    if event.item.type == "tool_call_item":
                        raw = event.item.raw_item
                        name = getattr(raw, "name", None) or "tool"
                        title = call_title(name, parse_args(getattr(raw, "arguments", None)))
                        tool_calls += 1
                        self.ui.tool_progress(call_id, f"{title} · 第 {tool_calls} 次工具调用")
                if not result.interruptions:
                    break
                run_input = await self._decide(result)
            except asyncio.CancelledError:
                result.cancel()
                raise
            except MaxTurnsExceeded:
                return (
                    f"Error: the sub-agent did not finish within {SUBAGENT_MAX_TURNS} turns. "
                    "Split the task into smaller parts."
                )
        usage = result.context_wrapper.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        return str(result.final_output or "(the sub-agent returned no report)")

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
        self.save()
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
                state = await self._decide(result)
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
        self.save()
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
        self.save()

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
