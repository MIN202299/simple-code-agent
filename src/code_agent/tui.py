"""Interactive terminal UI.

The UI is drawn inline rather than on the alternate screen: the transcript is
printed into the terminal's normal scrollback (so it can be scrolled, selected
and copied as usual) and a small live area stays pinned at the bottom with the
text being streamed, a status line, the input box and a footer.
"""

from __future__ import annotations

import asyncio
import io
import time
from collections import deque
from pathlib import Path
from typing import Any

from agents import AgentsException, MaxTurnsExceeded, ToolApprovalItem
from openai import OpenAIError
from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output import Output
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea
from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.text import Text

from . import __version__
from .commands import Command, match_commands, run_command
from .render import (
    call_title,
    parse_args,
    render_assistant,
    render_call,
    render_output,
    render_user,
    split_blocks,
    tool_label,
)
from .session import Session

SPINNER = "·✢✳✶✻✽✻✶✳✢"
LIVE_LINES = 12
MENU_ROWS = 8
EXIT_CONFIRM_SECONDS = 2.0
# Keys typed just before an approval dialog opens were meant for the input box;
# ignore them briefly so type-ahead cannot approve a tool call by accident.
APPROVAL_GRACE_SECONDS = 0.5

STYLE = Style.from_dict(
    {
        "rule": "fg:ansibrightblack",
        "prompt": "fg:ansibrightblack",
        "status": "fg:ansiyellow",
        "status.dim": "fg:ansibrightblack",
        "footer": "fg:ansibrightblack",
        "footer.auto": "fg:ansimagenta",
        "footer.notice": "fg:ansiyellow",
        "queued": "fg:ansibrightblack italic",
        "menu": "fg:ansibrightblack",
        "menu.selected": "fg:ansicyan bold",
        "approval.title": "fg:ansiyellow bold",
        "approval.selected": "fg:ansicyan bold",
        "approval.dim": "fg:ansibrightblack",
    }
)

APPROVAL_CHOICES = ("yes", "always", "no")


class Tui:
    """The interactive front end. Implements the `UI` protocol of `Session`."""

    def __init__(
        self,
        session: Session,
        *,
        subtitle: str = "",
        history: History | None = None,
        output: Output | None = None,
        input: Any = None,
    ) -> None:
        self.session = session
        session.ui = self
        self.subtitle = subtitle
        self.transcript: list[str] = []  # plain-text copy of everything printed

        self._pending = ""  # streamed assistant text not yet printed
        self._message_started = False
        self._queue: deque[str] = deque()
        self._wakeup = asyncio.Event()
        self._turn: asyncio.Task[Any] | None = None
        self._turn_started = 0.0
        self._activity = ""
        self._approval: dict[str, Any] | None = None
        self._menu_index = 0
        self._exit_armed_at = 0.0
        self.approval_grace = APPROVAL_GRACE_SECONDS
        self._color_system = Console().color_system or "standard"

        self.input = TextArea(
            multiline=True,
            wrap_lines=True,
            history=history if history is not None else InMemoryHistory(),
            get_line_prefix=self._line_prefix,
            height=Dimension(min=1, max=12),
            dont_extend_height=True,
        )
        self.input.buffer.on_text_changed += lambda _buffer: self._reset_menu()
        self.app: Application[None] = Application(
            layout=Layout(self._build_layout(), focused_element=self.input),
            key_bindings=self._build_bindings(),
            style=STYLE,
            full_screen=False,
            erase_when_done=True,
            refresh_interval=0.12,
            output=output,
            input=input,
        )
        # A lone Esc must not wait long for a possible escape sequence.
        self.app.timeoutlen = 0.3
        self.app.ttimeoutlen = 0.05

    # ------------------------------------------------------------------ state

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    def _menu(self) -> list[Command]:
        if self._approval is not None:
            return []
        return match_commands(self.session, self.input.text)

    def _reset_menu(self) -> None:
        self._menu_index = 0

    # ----------------------------------------------------------------- output

    def emit(self, renderable: RenderableType, gap: bool = False) -> None:
        """Print something into the scrollback, above the live area."""
        width = self.app.output.get_size().columns
        console = Console(
            file=io.StringIO(),
            force_terminal=True,
            color_system=self._color_system,  # type: ignore[arg-type]
            width=max(width, 20),
            highlight=False,
            record=True,
        )
        if gap:
            console.print()
        console.print(renderable)
        self.transcript.append(console.export_text(clear=False).rstrip("\n"))
        ansi = console.export_text(styles=True)
        print_formatted_text(ANSI(ansi), end="", output=self.app.output)

    def _flush_text(self, final: bool) -> None:
        """Move finished Markdown blocks from the live area into the scrollback."""
        if final:
            blocks, self._pending = ([self._pending] if self._pending.strip() else []), ""
        else:
            blocks, self._pending = split_blocks(self._pending)
        for block in blocks:
            self.emit(render_assistant(block, first=not self._message_started), gap=True)
            self._message_started = True

    # --------------------------------------------------- UI protocol (Session)

    def text_delta(self, delta: str) -> None:
        self._pending += delta
        self._activity = "回复中"
        if "\n" in delta:
            self._flush_text(final=False)
        self.app.invalidate()

    def text_done(self) -> None:
        self._flush_text(final=True)
        self._message_started = False

    def tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._activity = f"执行 {tool_label(name)}"
        self.emit(render_call(name, args), gap=True)

    def tool_output(self, name: str, args: dict[str, Any], output: Any) -> None:
        self._activity = "思考中"
        self.emit(render_output(name, args, output))

    async def approve(self, item: ToolApprovalItem) -> str:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._approval = {
            "item": item,
            "future": future,
            "index": 0,
            "opened": time.monotonic(),
        }
        self.app.invalidate()
        try:
            return await future
        finally:
            self._approval = None
            self.app.invalidate()

    def turn_done(self, input_tokens: int, output_tokens: int) -> None:
        pass  # totals are shown in the footer

    # ------------------------------------------------------------------ layout

    def _line_prefix(self, line_number: int, wrap_count: int) -> StyleAndTextTuples:
        first = line_number == 0 and wrap_count == 0
        return [("class:prompt", "> " if first else "  ")]

    def _build_layout(self) -> HSplit:
        approving = Condition(lambda: self._approval is not None)
        rule = lambda: Window(height=1, char="─", style="class:rule")  # noqa: E731
        return HSplit(
            [
                ConditionalContainer(
                    Window(
                        FormattedTextControl(self._live_text),
                        wrap_lines=True,
                        dont_extend_height=True,
                    ),
                    filter=Condition(lambda: bool(self._pending.strip())),
                ),
                ConditionalContainer(
                    Window(FormattedTextControl(self._status_text), height=2),
                    filter=Condition(lambda: self.busy and self._approval is None),
                ),
                ConditionalContainer(
                    Window(FormattedTextControl(self._queued_text), dont_extend_height=True),
                    filter=Condition(lambda: bool(self._queue)),
                ),
                ConditionalContainer(
                    HSplit([Window(height=1), rule(), self.input, rule()]), filter=~approving
                ),
                ConditionalContainer(
                    Window(
                        FormattedTextControl(self._approval_text),
                        wrap_lines=True,
                        dont_extend_height=True,
                    ),
                    filter=approving,
                ),
                ConditionalContainer(
                    Window(FormattedTextControl(self._menu_text), dont_extend_height=True),
                    filter=Condition(lambda: bool(self._menu())),
                ),
                ConditionalContainer(
                    Window(FormattedTextControl(self._footer_text), height=1),
                    filter=Condition(lambda: not self._menu() and self._approval is None),
                ),
            ]
        )

    def _live_text(self) -> StyleAndTextTuples:
        lines = self._pending.strip("\n").split("\n")[-LIVE_LINES:]
        bullet = "  " if self._message_started else "● "
        return [("", "\n" + "\n".join((bullet if i == 0 else "  ") + ln for i, ln in enumerate(lines)))]

    def _status_text(self) -> StyleAndTextTuples:
        elapsed = int(time.monotonic() - self._turn_started)
        frame = SPINNER[int(time.monotonic() * 8) % len(SPINNER)]
        return [
            ("", "\n"),
            ("class:status", f"{frame} {self._activity or '思考中'}… "),
            ("class:status.dim", f"({elapsed}s · esc 中断)"),
        ]

    def _queued_text(self) -> StyleAndTextTuples:
        lines = [f"  ⏳ {text.splitlines()[0][:100]}" for text in self._queue]
        return [("class:queued", "\n".join(lines))]

    def _footer_text(self) -> StyleAndTextTuples:
        if time.monotonic() - self._exit_armed_at < EXIT_CONFIRM_SECONDS:
            return [("class:footer.notice", "  再按一次 Ctrl-C 退出")]
        session = self.session
        parts: StyleAndTextTuples = []
        if session.context.auto_approve:
            parts.append(("class:footer.auto", "  ⏵⏵ 自动批准已开启"))
            parts.append(("class:footer", " (shift+tab 切换)"))
        else:
            parts.append(("class:footer", "  / 命令 · shift+tab 自动批准 · alt+enter 换行"))
        tokens = session.input_tokens + session.output_tokens
        usage = f" · {tokens:,} tokens" if tokens else ""
        parts.append(("class:footer", f" · {session.model_name}{usage}"))
        return parts

    def _menu_text(self) -> StyleAndTextTuples:
        commands = self._menu()
        index = min(self._menu_index, len(commands) - 1)
        start = max(0, min(index - MENU_ROWS + 1, len(commands) - MENU_ROWS))
        width = max(len(command.name) for command in commands) + 3
        out: StyleAndTextTuples = []
        for position, command in enumerate(commands[start : start + MENU_ROWS], start):
            style = "class:menu.selected" if position == index else "class:menu"
            name = f"/{command.name}".ljust(width)
            out.append((style, f"  {name}  {command.description.splitlines()[0][:70]}\n"))
        out[-1] = (out[-1][0], out[-1][1].rstrip("\n"))
        return out

    def _approval_text(self) -> StyleAndTextTuples:
        approval = self._approval
        if approval is None:
            return []
        item: ToolApprovalItem = approval["item"]
        name = item.tool_name or "tool"
        options = [
            "是",
            f"是，本次会话不再询问 {tool_label(name)}",
            "否，让 agent 换个做法 (esc)",
        ]
        out: StyleAndTextTuples = [
            ("", "\n"),
            ("class:rule", "─" * self.app.output.get_size().columns + "\n"),
            ("class:approval.title", f" 需要确认 · {tool_label(name)}\n"),
            ("", f"   {call_title(name, parse_args(item.arguments))}\n\n"),
            ("", " 是否允许执行？\n"),
        ]
        for index, label in enumerate(options):
            selected = index == approval["index"]
            style = "class:approval.selected" if selected else ""
            out.append((style, f" {'❯' if selected else ' '} {index + 1}. {label}\n"))
        out.append(("class:approval.dim", " ↑↓ 选择 · enter 确认"))
        return out

    # ------------------------------------------------------------ key bindings

    def _build_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        approving = Condition(lambda: self._approval is not None)
        menu_open = Condition(lambda: bool(self._menu()))

        def answer(index: int) -> None:
            approval = self._approval
            if approval is None or approval["future"].done():
                return
            if time.monotonic() - approval["opened"] < self.approval_grace:
                return
            approval["future"].set_result(APPROVAL_CHOICES[index])

        # --- approval dialog: it owns the keyboard while it is open
        @kb.add("up", filter=approving)
        @kb.add("c-p", filter=approving)
        def _(event: KeyPressEvent) -> None:
            self._approval["index"] = (self._approval["index"] - 1) % 3  # type: ignore[index]

        @kb.add("down", filter=approving)
        @kb.add("c-n", filter=approving)
        @kb.add("tab", filter=approving)
        def _(event: KeyPressEvent) -> None:
            self._approval["index"] = (self._approval["index"] + 1) % 3  # type: ignore[index]

        @kb.add("enter", filter=approving)
        def _(event: KeyPressEvent) -> None:
            answer(self._approval["index"])  # type: ignore[index]

        for key, index in (("1", 0), ("2", 1), ("3", 2)):
            kb.add(key, filter=approving)(lambda event, index=index: answer(index))

        @kb.add("escape", filter=approving)
        def _(event: KeyPressEvent) -> None:
            answer(2)

        @kb.add("<any>", filter=approving)
        def _(event: KeyPressEvent) -> None:
            pass  # swallow everything else so it does not land in the hidden input

        # --- slash-command menu
        @kb.add("up", filter=menu_open)
        def _(event: KeyPressEvent) -> None:
            self._menu_index = (self._menu_index - 1) % len(self._menu())

        @kb.add("down", filter=menu_open)
        def _(event: KeyPressEvent) -> None:
            self._menu_index = (self._menu_index + 1) % len(self._menu())

        @kb.add("tab", filter=menu_open)
        def _(event: KeyPressEvent) -> None:
            self._complete(submit=False)

        @kb.add("enter", filter=menu_open)
        def _(event: KeyPressEvent) -> None:
            self._complete(submit=True)

        # --- input
        @kb.add("enter", filter=~approving & ~menu_open)
        def _(event: KeyPressEvent) -> None:
            self._submit()

        @kb.add("escape", "enter", filter=~approving)
        @kb.add("c-j", filter=~approving)
        def _(event: KeyPressEvent) -> None:
            self.input.buffer.insert_text("\n")

        @kb.add("escape", filter=~approving)
        def _(event: KeyPressEvent) -> None:
            self._interrupt()

        @kb.add("s-tab")
        def _(event: KeyPressEvent) -> None:
            context = self.session.context
            context.auto_approve = not context.auto_approve

        @kb.add("c-l")
        def _(event: KeyPressEvent) -> None:
            event.app.renderer.clear()

        @kb.add("c-c")
        def _(event: KeyPressEvent) -> None:
            if self.busy:
                self._interrupt()
            elif self.input.text:
                self.input.buffer.reset()
            elif time.monotonic() - self._exit_armed_at < EXIT_CONFIRM_SECONDS:
                event.app.exit()
            else:
                self._exit_armed_at = time.monotonic()

        @kb.add("c-d", filter=Condition(lambda: not self.input.text))
        def _(event: KeyPressEvent) -> None:
            event.app.exit()

        return kb

    def _complete(self, submit: bool) -> None:
        commands = self._menu()
        command = commands[min(self._menu_index, len(commands) - 1)]
        buffer = self.input.buffer
        if submit and not command.takes_arg:
            buffer.text = f"/{command.name}"
            self._submit()
        elif submit and buffer.text == f"/{command.name}":
            self._submit()
        else:
            buffer.text = f"/{command.name} "
            buffer.cursor_position = len(buffer.text)

    def _submit(self) -> None:
        text = self.input.text.strip()
        if not text:
            return
        self.input.buffer.append_to_history()
        self.input.buffer.reset()
        self._queue.append(text)
        self._wakeup.set()

    def _interrupt(self) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()

    # -------------------------------------------------------------- main loop

    def _welcome(self) -> RenderableType:
        session = self.session
        body = Text()
        body.append("✻ ", style="yellow")
        body.append(f"Code Agent v{__version__}\n", style="bold")
        body.append(f"\n  模型    {session.model_name}", style="dim")
        if self.subtitle:
            body.append(f"\n  接口    {self.subtitle}", style="dim")
        workspace = str(session.context.workspace)
        home = str(Path.home())
        if workspace == home or workspace.startswith(home + "/"):
            workspace = "~" + workspace[len(home) :]
        body.append(f"\n  工作区  {workspace}", style="dim")
        body.append(
            f"\n  skills {len(session.context.skills)} · MCP 服务器 {len(session.mcp_servers)}",
            style="dim",
        )
        body.append("\n\n  输入任务开始，/help 查看命令，Ctrl-D 退出", style="dim")
        return Panel(body, border_style="yellow", expand=False, padding=(0, 1))

    async def _run_turn(self, prompt: str) -> None:
        self._turn_started = time.monotonic()
        self._activity = "思考中"
        self._turn = asyncio.ensure_future(self.session.run_turn(prompt))
        try:
            await asyncio.shield(self._turn)
        except asyncio.CancelledError:
            if not self._turn.cancelled():
                # The UI itself is shutting down: stop the turn and propagate.
                self._turn.cancel()
                await asyncio.gather(self._turn, return_exceptions=True)
                raise
            self.emit(Text("  ⎿  已中断", style="red"))
        except MaxTurnsExceeded:
            self.emit(
                Text(f"  ⎿  已达到最大轮数 ({self.session.max_turns})，任务未完成。", style="red")
            )
        except (AgentsException, OpenAIError) as exc:
            self.emit(Text(f"  ⎿  出错: {type(exc).__name__}: {exc}", style="red"))
        finally:
            self._pending = ""
            self._message_started = False
            self._turn = None
            self.app.invalidate()

    async def _worker(self) -> None:
        """Process submitted input one item at a time."""
        while True:
            while not self._queue:
                self._wakeup.clear()
                await self._wakeup.wait()
            text = self._queue.popleft()
            self.app.invalidate()
            self.emit(render_user(text), gap=True)
            if not text.startswith("/"):
                await self._run_turn(text)
                continue
            result = await run_command(self.session, text, self.emit)
            if result.exit:
                self.app.exit()
                return
            if result.prompt:  # a skill invoked as /<skill>
                await self._run_turn(result.prompt)

    async def run(self) -> None:
        self.emit(self._welcome())
        worker = asyncio.ensure_future(self._worker())
        try:
            await self.app.run_async()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


def history_file(home: Path | None = None) -> FileHistory:
    path = (home or Path.home()) / ".code_agent" / "history"
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileHistory(str(path))
