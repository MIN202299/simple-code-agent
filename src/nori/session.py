"""The conversation: runs turns against the agent and reports progress to a UI."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from agents import (
    Agent,
    MaxTurnsExceeded,
    Model,
    ModelRetrySettings,
    ModelSettings,
    RetryDecision,
    RetryPolicyContext,
    RunConfig,
    Runner,
    ToolApprovalItem,
)
from agents.run_config import CallModelData, ModelInputData
from agents.mcp import MCPServer
from rich.console import Console
from rich.markup import escape

from .agent import build_agent, build_subagent
from .attachments import find_images, user_content
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
from .i18n import t
from .permissions import PLAN_REJECTION, Permissions, rule_for
from .pet import Pet
from .render import call_title, parse_args, render_call, render_output
from .shelltasks import kill_process
from .skills import Skill
from .store import TITLE_CHARS, SessionRecord, SessionStore, new_session_id
from .tools import AgentContext

INTERRUPT_NOTE = "[Request interrupted by user]"
MAX_TURNS_REACHED = "Reached the maximum of {limit} turns for one task; it is not finished."
MAX_TURNS_HINT = (
    'The finished steps are kept in context: say "continue" to carry on. '
    "Raise the limit with --max-turns or max_turns in the config file."
)
COMPACTED = "Compacted context: about {before} → {after} tokens"
COMPACTION_FAILED = "Compacting the context failed; continuing with the full context."
SUBAGENT_MAX_TURNS = 100
DEFAULT_RETRIES = 4
USER_SHELL_CHARS = 20_000
# User-role items that are bookkeeping rather than something the user typed.
NOTE_PREFIXES = ("[The earlier part", "[Request", "[The user reverted", "[I ran this shell")
RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
RETRYING = "Request failed ({reason}); retrying ({attempt}/{limit})…"
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

    def retrying(self, attempt: int, limit: int, reason: str) -> None:
        """A model request failed with a transient error and is being retried."""

    def show_history(self, items: list[Any]) -> None:
        """Display a restored conversation."""

    async def approve(self, item: ToolApprovalItem, rule: str | None = None) -> str:
        """Ask the user about a tool call. Returns "yes", "always" or "no".

        `rule` is what "always" would allow from now on; None if no standing
        rule can be offered for this call.
        """

    async def approve_plan(self, plan: str) -> str:
        """Show a plan made in plan mode. Returns the mode to continue in, or "" to keep planning."""

    def notice(self, text: str) -> None:
        """A short informational line for the user."""

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
            message = t(COMPACTED, before=f"{before:,}", after=f"{after:,}")
            self.console.print(f"[dim]{message}[/dim]")
        elif stage == "failed":
            self.console.print(f"[yellow]{t(COMPACTION_FAILED)}[/yellow]")

    def tool_progress(self, call_id: str, text: str) -> None:
        self.console.print(f"  [dim]│ {escape(text)}[/dim]", highlight=False)

    def retrying(self, attempt: int, limit: int, reason: str) -> None:
        message = t(RETRYING, reason=escape(reason), attempt=attempt, limit=limit)
        self.console.print(f"[yellow]{message}[/yellow]", highlight=False)

    def show_history(self, items: list[Any]) -> None:
        turns = sum(1 for item in items if isinstance(item, dict) and item.get("role") == "user")
        self.console.print(f"[dim]{t('Session resumed ({turns} turns).', turns=turns)}[/dim]")

    async def approve(self, item: ToolApprovalItem, rule: str | None = None) -> str:
        return self.approver(item)

    async def approve_plan(self, plan: str) -> str:
        self.console.print(f"\n[bold yellow]{t('Plan')}[/bold yellow]")
        self.console.print(plan, markup=False, highlight=False)
        try:
            answer = input(t("Carry out this plan? [y] yes / [e] yes, and accept edits / [n] keep planning: "))
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return ""
        answer = answer.strip().lower()
        if answer in ("e", "edits"):
            return "accept-edits"
        return "default" if answer in ("y", "yes", "是") else ""

    def notice(self, text: str) -> None:
        self.console.print(f"[dim]{escape(text)}[/dim]", highlight=False)

    def turn_done(self, input_tokens: int, output_tokens: int) -> None:
        self.console.print(f"[dim]tokens: {input_tokens} in / {output_tokens} out[/dim]")

    def _ask(self, item: ToolApprovalItem) -> str:
        name = escape(item.tool_name or "tool")
        self.console.print(f"\n[bold yellow]{t('Approval needed')}[/bold yellow] {name}")
        try:
            answer = input(t("Allow? [y] yes / [a] always allow this tool in this session / [n] no: "))
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


class ContextGuard:
    """Keeps one run inside the context window by compacting when it fills up.

    The SDK keeps the run's full item list. The guard remembers how many leading
    items a summary stands for and swaps that prefix out before every model call
    (`filter`) and once more when the run's items are read back (`collapse`).
    The main conversation and every sub-agent have their own guard.
    """

    def __init__(self, session: Session, call_id: str | None = None) -> None:
        self.session = session
        self.call_id = call_id  # set for a sub-agent: progress goes to that tool call
        self.compacted: tuple[int, dict[str, Any]] | None = None
        self.floor = 0  # context size right after the last compaction
        self.reported = 0  # sub-agents: context size last reported by the provider

    @property
    def main(self) -> bool:
        return self.call_id is None

    def collapse(self, items: list[Any]) -> list[Any]:
        """Apply the summary made during this run to the run's full item list."""
        if self.compacted is None or len(items) < self.compacted[0]:
            return items
        count, summary = self.compacted
        return [summary, *items[count:]]

    async def filter(self, data: CallModelData[AgentContext]) -> ModelInputData:
        """Called by the SDK before every model call."""
        session, ui = self.session, self.session.ui
        model_data = data.model_data
        items = self.collapse(list(model_data.input))
        estimate = estimate_tokens(items)
        tokens = max(session.context_tokens if self.main else self.reported, estimate)
        if self.main and not session._usage_reported:
            session.context_tokens = estimate  # the provider sends no usage: show our estimate
        window = session.context_window
        if session.auto_compact and tokens >= window * COMPACT_AT and tokens > self.floor * 1.25:
            keep = min(KEEP_RECENT_MAX, int(window * KEEP_RECENT_SHARE))
            if self.main:
                ui.compaction("start", tokens)
            else:
                ui.tool_progress(self.call_id or "", t("compacting its context"))
            try:
                compacted = await session._summarise_prefix(items, keep)
            except Exception:  # a failed summary must not fail the run
                compacted = None
                if self.main:
                    ui.compaction("failed", tokens)
            if compacted is not None:
                cut, summary = compacted
                # `cut` counts items of the collapsed list; convert it to the full list,
                # where an earlier summary stands for `replaced` items instead of one.
                replaced = self.compacted[0] - 1 if self.compacted else 0
                self.compacted = (replaced + cut, summary)
                items = [summary, *items[cut:]]
                self.floor = self.reported = estimate_tokens(items)
                if self.main:
                    session.context_tokens = self.floor
                    ui.compaction("done", tokens, self.floor)
        return ModelInputData(input=items, instructions=model_data.instructions)


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
        retries: int = DEFAULT_RETRIES,
        mode: str = "default",
        permissions_path: Path | None = None,
    ) -> None:
        permissions = Permissions(
            workspace, "auto" if auto_approve else mode, path=permissions_path
        )
        self.context = AgentContext(
            workspace=workspace, permissions=permissions, skills=dict(skills or {})
        )
        self.context.spawn = self.run_subagent
        self.context.present_plan = self._present_plan
        self.store = store  # None: conversations are not saved
        self.session_id = new_session_id()
        self._created = ""
        self._approval_lock = asyncio.Lock()  # sub-agents run in parallel; ask one at a time
        self.model = model
        self.mcp_servers = list(mcp_servers or [])
        self.retries = retries  # retries of a model request after a transient error
        self.retry_delay = 1.0  # seconds before the first retry; doubles each time
        self.agent = self._build_agent()
        self.max_turns = max_turns
        self.ui: UI = ui or PlainUI()
        self.history: list[Any] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.context_tokens = 0  # size of the conversation as last seen by the model
        self.context_window = context_window
        # The window for models without one of their own; set by the CLI and /context.
        self.default_context_window = context_window
        self.auto_compact = auto_compact
        self._guard = ContextGuard(self)  # compaction state of the main conversation
        self._usage_reported = False
        self.info: dict[str, str] = {}  # endpoint details shown by /status
        self.pet: Pet | None = None
        self.config_path: Path | None = None  # where commands save settings
        # Models offered by /model: configured ones, plus whatever the endpoint lists.
        self.models: list[dict[str, Any]] = []
        self.fetch_models: Callable[[], Awaitable[list[str]]] | None = None
        self._fetched_models: list[str] | None = None

    def _model_settings(self) -> ModelSettings:
        if not self.retries:
            return ModelSettings()

        # A plain function rather than a bound method: the SDK deep-copies model
        # settings, and copying a method would try to copy the whole session.
        def policy(context: RetryPolicyContext) -> bool | RetryDecision:
            """Retry transient failures (rate limits, overload, network) with backoff."""
            error = context.normalized
            transient = (
                error.is_network_error or error.is_timeout or error.status_code in RETRY_STATUSES
            )
            # Once output has been streamed, a retry would show it twice.
            if not transient or context.response_started or error.is_abort:
                return False
            reason = f"HTTP {error.status_code}" if error.status_code else type(context.error).__name__
            self.ui.retrying(context.attempt, context.max_retries, reason)
            return RetryDecision(retry=True, delay=error.retry_after)

        retry = ModelRetrySettings(
            max_retries=self.retries,
            backoff={
                "initial_delay": self.retry_delay,
                "max_delay": 30.0,
                "multiplier": 2.0,
                "jitter": True,
            },
            policy=policy,
        )
        return ModelSettings(retry=retry)

    def _build_agent(self) -> Agent[AgentContext]:
        return build_agent(
            self.context.workspace,
            self.model,
            self.context.skills,
            self.mcp_servers,
            self._model_settings(),
        )

    @property
    def model_name(self) -> str:
        if isinstance(self.model, str):
            return self.model
        return t("(SDK default)") if self.model is None else type(self.model).__name__

    def set_model(self, model: str) -> None:
        """Switch models; a context window configured for the model comes with it."""
        self.model = model
        self.agent = self._build_agent()
        own = next((m.get("context_window") for m in self.models if m.get("name") == model), None)
        self.context_window = own or self.default_context_window

    async def available_models(self) -> list[str]:
        """Configured models first, then the ones the endpoint reports (fetched once)."""
        if self._fetched_models is None and self.fetch_models is not None:
            try:
                self._fetched_models = sorted(await self.fetch_models())
            except Exception:  # listing models is optional; many endpoints lack it
                self._fetched_models = []
        names = [entry["name"] for entry in self.models]
        if isinstance(self.model, str):
            names.append(self.model)
        names.extend(self._fetched_models or [])
        return list(dict.fromkeys(names))

    def clear(self) -> None:
        """Start a new conversation. The old one stays on disk and can be resumed."""
        self.history = []
        self.context_tokens = 0
        self._guard.floor = 0
        self.session_id = new_session_id()
        self._created = ""

    # ----------------------------------------------------------- persistence

    def _title(self) -> str:
        for item in self.history:
            if isinstance(item, dict) and item.get("role") == "user":
                text = item.get("content")
                if isinstance(text, list):  # a message with images: use its text part
                    text = next((p.get("text") for p in text if isinstance(p, dict) and p.get("text")), "")
                if isinstance(text, str) and text and not text.startswith(NOTE_PREFIXES):
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
        self._guard.floor = 0
        self._usage_reported = False

    # ------------------------------------------------------------- approvals

    async def _decide(self, result: Any) -> Any:
        """Resolve the pending tool approvals of a paused run; returns its state."""
        state = result.to_state()
        for item in result.interruptions:
            name = item.tool_name or ""
            args = parse_args(item.arguments)
            permissions = self.context.permissions
            rejection = REJECTION_MESSAGE
            async with self._approval_lock:
                # Decide again here rather than trusting why the SDK paused: it also
                # pauses when it had to normalise the arguments, and an earlier answer
                # in this batch may have changed the mode or added a rule.
                verdict = permissions.decide(name, args)
                if verdict == "deny":
                    rejection = PLAN_REJECTION
                elif verdict == "ask":
                    rule = rule_for(name, args)
                    answer = await self.ui.approve(item, rule)
                    if answer == "always" and rule:
                        permissions.add_rule(rule)
                    verdict = "allow" if answer in ("yes", "always") else "deny"
            if verdict == "allow":
                state.approve(item)
            else:
                state.reject(item, rejection_message=rejection)
        return state

    async def _present_plan(self, plan: str) -> str:
        """Ask the user to approve a plan made in plan mode."""
        async with self._approval_lock:
            return await self.ui.approve_plan(plan)

    @property
    def mode(self) -> str:
        return self.context.permissions.mode

    # ------------------------------------------------------- undo, shell, close

    def undo(self) -> tuple[str, list[str], list[str]] | None:
        """Revert the file changes of the last turn that made any.

        Returns (what that turn was, files restored, files deleted). The model is
        told, so it does not assume its changes are still in place.
        """
        result = self.context.checkpoints.undo()
        if result is None:
            return None
        turn, restored, deleted = result
        workspace = self.context.workspace
        names = lambda paths: [os.path.relpath(p, workspace) for p in paths]  # noqa: E731
        restored_names, deleted_names = names(restored), names(deleted)
        note = "[The user reverted the file changes of an earlier turn."
        if restored_names:
            note += f" Restored to their previous content: {', '.join(restored_names)}."
        if deleted_names:
            note += f" Deleted again (they had been created): {', '.join(deleted_names)}."
        self.history.append({"role": "user", "content": note + " Re-read files before relying on them.]"})
        self.save()
        return turn.label, restored_names, deleted_names

    async def user_shell(self, command: str, timeout: float = 300) -> tuple[str, int | None]:
        """Run a command the user typed with `!`, and add it and its output to the context."""
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=self.context.workspace,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        try:
            raw, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            output = raw.decode("utf-8", errors="replace").rstrip()
        except asyncio.TimeoutError:
            kill_process(process)
            output = f"(timed out after {int(timeout)}s and was stopped)"
        except asyncio.CancelledError:
            kill_process(process)
            raise
        code = process.returncode
        shown = output if len(output) <= USER_SHELL_CHARS else output[:USER_SHELL_CHARS] + "\n…[truncated]"
        self.history.append(
            {
                "role": "user",
                "content": f"[I ran this shell command myself]\n$ {command}\n{shown}\n[exit code: {code}]",
            }
        )
        self.save()
        return output, code

    def close(self) -> None:
        """Release what the session holds: stops background commands."""
        self.context.shell_tasks.kill_all()

    # ------------------------------------------------------------ sub-agents

    async def run_subagent(self, call_id: str, agent_type: str, prompt: str) -> str:
        """Run a sub-agent in a fresh context and return its final report."""
        agent = build_subagent(
            self.context.workspace, agent_type, self.model, self.mcp_servers, self._model_settings()
        )
        run_input: Any = prompt
        tool_calls = 0
        # Sub-agents read a lot; give each its own compaction so it cannot overflow.
        guard = ContextGuard(self, call_id=call_id)
        run_config = RunConfig(call_model_input_filter=guard.filter)
        while True:
            result = Runner.run_streamed(
                agent,
                run_input,
                context=self.context,
                max_turns=SUBAGENT_MAX_TURNS,
                run_config=run_config,
            )
            try:
                async for event in result.stream_events():
                    if event.type == "raw_response_event":
                        if event.data.type == "response.completed":
                            usage = getattr(event.data.response, "usage", None)
                            if usage is not None and usage.input_tokens:
                                guard.reported = usage.input_tokens + usage.output_tokens
                        continue
                    if event.type != "run_item_stream_event":
                        continue
                    if event.item.type == "tool_call_item":
                        raw = event.item.raw_item
                        name = getattr(raw, "name", None) or "tool"
                        title = call_title(name, parse_args(getattr(raw, "arguments", None)))
                        tool_calls += 1
                        self.ui.tool_progress(
                            call_id, f"{title} · " + t("tool call #{count}", count=tool_calls)
                        )
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
        self.context_tokens = self._guard.floor = estimate_tokens(self.history)
        self.save()
        self.ui.compaction("done", before, self.context_tokens)
        return before, self.context_tokens

    async def run_turn(self, text: str) -> str | None:
        """Run one user turn to completion, pausing for tool approvals.

        Returns the agent's final answer. A turn that fails leaves the history
        untouched; a turn that is cancelled or runs out of model turns keeps the
        work finished so far, so the user can ask to continue.
        """
        images, problems = find_images(text, self.context.workspace)
        for problem in problems:
            self.ui.notice(t("Image not attached — {problem}", problem=problem))
        if images:
            names = ", ".join(path.name for path in images)
            self.ui.notice(t("Attached {count} image(s): {names}", count=len(images), names=names))
        message = {"role": "user", "content": user_content(text, images)}
        run_input: Any = [*self.history, message]
        calls: dict[str, tuple[str, dict[str, Any]]] = {}
        self.context.checkpoints.begin_turn(text)
        self._guard.compacted = None
        run_config = RunConfig(call_model_input_filter=self._guard.filter)
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
        self.history = self._guard.collapse(result.to_input_list())
        self._guard.compacted = None
        usage = result.context_wrapper.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.save()
        self.ui.turn_done(usage.input_tokens, usage.output_tokens)
        return result.final_output

    def _keep_partial(self, result: Any, note: str | None = None) -> None:
        """Keep what an unfinished turn already did, optionally followed by a note."""
        try:
            items = complete_history(self._guard.collapse(result.to_input_list()))
        except Exception:  # never let bookkeeping mask the original error
            return
        self._guard.compacted = None
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
