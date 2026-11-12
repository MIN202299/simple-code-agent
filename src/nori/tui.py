"""Interactive terminal UI.

The UI is drawn inline rather than on the alternate screen: the transcript is
printed into the terminal's normal scrollback (so it can be scrolled, selected
and copied as usual) and a live area stays pinned at the bottom with running
tool calls, the text being streamed, a status line, the input box with the pet
beside it, and a footer.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import AgentsException, MaxTurnsExceeded, ToolApprovalItem
from openai import OpenAIError
from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.output import Output
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from . import pet as petlib
from .branding import ACCENT, APP_NAME, TAGLINE, app_dir
from .commands import match_commands, run_command
from .pet import Pet
from .render import (
    call_preview,
    call_title,
    format_duration,
    format_tokens,
    is_error,
    parse_args,
    render_assistant,
    render_call,
    render_output,
    render_user,
    split_blocks,
    tool_label,
)
from .session import Session
from .tools import SKIP_DIRS

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
LIVE_LINES = 12
MENU_ROWS = 8
MAX_FILE_MATCHES = 50
MAX_INDEXED_FILES = 5000
FILE_INDEX_SECONDS = 10.0
EXIT_CONFIRM_SECONDS = 2.0
RESULT_MOOD_SECONDS = 4.0
SLEEP_AFTER_SECONDS = 180.0
PET_MIN_COLUMNS = 64
SUMMARY_MIN_SECONDS = 5.0
# Keys typed just before an approval dialog opens were meant for the input box;
# ignore them briefly so type-ahead cannot approve a tool call by accident.
APPROVAL_GRACE_SECONDS = 0.5

PLACEHOLDER = "描述你想做的事…"
SHORTCUTS = [
    ("enter", "发送"),
    ("alt+enter", "换行"),
    ("esc", "中断任务"),
    ("/", "命令"),
    ("@", "引用文件"),
    ("↑ ↓", "历史输入"),
    ("shift+tab", "切换自动批准"),
    ("ctrl+l", "清屏"),
    ("ctrl+c", "中断 / 清空 / 退出"),
    ("ctrl+d", "退出"),
]

STYLE = Style.from_dict(
    {
        "rule": "fg:ansibrightblack",
        "rule.auto": "fg:ansimagenta",
        "prompt": f"fg:{ACCENT} bold",
        "placeholder": "fg:ansibrightblack",
        "pet": f"fg:{ACCENT}",
        "pet.asleep": "fg:ansibrightblack",
        "spinner": f"fg:{ACCENT}",
        "call": "bold",
        "status": f"fg:{ACCENT}",
        "dim": "fg:ansibrightblack",
        "footer": "fg:ansibrightblack",
        "footer.auto": "fg:ansimagenta bold",
        "footer.notice": "fg:ansiyellow",
        "queued": "fg:ansibrightblack italic",
        "menu": "fg:ansibrightblack",
        "menu.selected": f"fg:{ACCENT} bold",
        "help.key": "bold",
        "approval.title": "fg:ansiyellow bold",
        "approval.selected": f"fg:{ACCENT} bold",
    }
)

APPROVAL_CHOICES = ("yes", "always", "no")
_FILE_TOKEN = re.compile(r"(?:^|\s)@(\S*)$")


@dataclass(frozen=True)
class MenuItem:
    label: str
    description: str
    takes_arg: bool = False


class _Placeholder(Processor):
    """Shows a hint in the empty input box."""

    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        if ti.lineno == 0 and not ti.document.text:
            return Transformation([*ti.fragments, ("class:placeholder", PLACEHOLDER)])
        return Transformation(ti.fragments)


def git_branch(workspace: Path) -> str:
    """Current branch name, read straight from .git/HEAD (no subprocess)."""
    try:
        head = (workspace / ".git" / "HEAD").read_text("utf-8").strip()
    except OSError:
        return ""
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else head[:7]


def short_path(path: Path) -> str:
    text, home = str(path), str(Path.home())
    return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text


class Tui:
    """The interactive front end. Implements the `UI` protocol of `Session`."""

    def __init__(
        self,
        session: Session,
        *,
        pet: Pet | None = None,
        history: History | None = None,
        output: Output | None = None,
        input: Any = None,
    ) -> None:
        self.session = session
        session.ui = self
        self.pet = pet or Pet()
        session.pet = self.pet
        self.transcript: list[str] = []  # plain-text copy of everything printed
        self.approval_grace = APPROVAL_GRACE_SECONDS

        self._pending = ""  # streamed assistant text not yet printed
        self._message_started = False
        self._calls: dict[str, dict[str, Any]] = {}  # tool calls still running
        self._queue: deque[str] = deque()
        self._wakeup = asyncio.Event()
        self._turn: asyncio.Task[Any] | None = None
        self._turn_started = 0.0
        self._turn_tools = 0
        self._turn_output_tokens = 0
        self._verb = ""
        self._approval: dict[str, Any] | None = None
        self._menu_index = 0
        self._menu_cache: tuple[Any, str, list[MenuItem]] = (None, "", [])
        self._menu_dismissed: Any = None
        self._show_help = False
        self._exit_armed_at = 0.0
        self._closed = False
        self._last_activity = time.monotonic()
        self._last_result: tuple[str, float] = ("", 0.0)
        self._files: tuple[float, list[str]] = (0.0, [])
        self._branch = git_branch(session.context.workspace)
        self._color_system = Console().color_system or "standard"

        self.input = TextArea(
            multiline=True,
            wrap_lines=True,
            history=history if history is not None else InMemoryHistory(),
            get_line_prefix=self._line_prefix,
            input_processors=[_Placeholder()],
            height=Dimension(min=1, max=12),
            dont_extend_height=True,
        )
        self.input.buffer.on_text_changed += self._on_text_changed
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

    @property
    def mood(self) -> str:
        """What the pet is feeling, derived from what the agent is doing."""
        now = time.monotonic()
        if self._approval is not None:
            return "waiting"
        if self.busy:
            return "working" if self._calls else "thinking"
        result, at = self._last_result
        if result and now - at < RESULT_MOOD_SECONDS:
            return result
        if now - self._last_activity > SLEEP_AFTER_SECONDS:
            return "sleeping"
        return "idle"

    def _on_text_changed(self, _buffer: Any) -> None:
        self._menu_index = 0
        self._show_help = False
        self._last_activity = time.monotonic()

    # ------------------------------------------------------------------- menu

    def _file_index(self) -> list[str]:
        built, files = self._files
        if files and time.monotonic() - built < FILE_INDEX_SECONDS:
            return files
        workspace = self.session.context.workspace
        files = []
        for directory, dirnames, filenames in os.walk(workspace):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            relative = os.path.relpath(directory, workspace)
            for name in sorted(filenames):
                files.append(name if relative == "." else f"{relative}/{name}".replace(os.sep, "/"))
            if len(files) >= MAX_INDEXED_FILES:
                break
        self._files = (time.monotonic(), files)
        return files

    def _match_files(self, query: str) -> list[MenuItem]:
        query = query.lower()
        scored = []
        for path in self._file_index():
            lowered = path.lower()
            if query not in lowered:
                continue
            name = lowered.rsplit("/", 1)[-1]
            scored.append((not name.startswith(query), query not in name, len(path), path))
        return [MenuItem(f"@{path}", "") for *_rank, path in sorted(scored)[:MAX_FILE_MATCHES]]

    def _menu_state(self) -> tuple[str, list[MenuItem]]:
        """The open completion menu: its kind ("command", "file" or "") and items."""
        if self._approval is not None:
            return "", []
        buffer = self.input.buffer
        key = (buffer.text, buffer.cursor_position)
        if key == self._menu_dismissed:
            return "", []
        if key == self._menu_cache[0]:
            return self._menu_cache[1], self._menu_cache[2]
        kind, items = "", []
        commands = match_commands(self.session, buffer.text)
        if commands:
            kind = "command"
            items = [MenuItem(f"/{c.name}", c.description, c.takes_arg) for c in commands]
        else:
            token = _FILE_TOKEN.search(buffer.document.text_before_cursor)
            if token:
                items = self._match_files(token.group(1))
                kind = "file" if items else ""
        self._menu_cache = (key, kind, items)
        return kind, items

    def _menu(self) -> list[MenuItem]:
        return self._menu_state()[1]

    def _accept_menu(self, submit: bool) -> None:
        kind, items = self._menu_state()
        item = items[min(self._menu_index, len(items) - 1)]
        buffer = self.input.buffer
        if kind == "file":
            before = buffer.document.text_before_cursor
            start = before.rindex("@")
            after = buffer.text[buffer.cursor_position :]
            buffer.text = f"{before[:start]}{item.label} {after}"
            buffer.cursor_position = start + len(item.label) + 1
        elif submit and not item.takes_arg:
            buffer.text = item.label
            self._submit()
        elif submit and buffer.text == item.label:
            self._submit()
        else:
            buffer.text = f"{item.label} "
            buffer.cursor_position = len(buffer.text)

    # ----------------------------------------------------------------- output

    def _columns(self) -> int:
        return max(self.app.output.get_size().columns, 20)

    def _console(self, record: bool = False) -> Console:
        return Console(
            file=io.StringIO(),
            force_terminal=True,
            color_system=self._color_system,  # type: ignore[arg-type]
            width=self._columns(),
            highlight=False,
            record=record,
        )

    def emit(self, renderable: RenderableType, gap: bool = False) -> None:
        """Print something into the scrollback, above the live area."""
        console = self._console(record=True)
        if gap:
            console.print()
        console.print(renderable)
        self.transcript.append(console.export_text(clear=False).rstrip("\n"))
        ansi = console.export_text(styles=True)
        if self._closed:
            # The app is gone, so nothing would run a deferred print: write directly.
            self.app.output.write_raw(ansi)
            self.app.output.flush()
        else:
            print_formatted_text(ANSI(ansi), end="", output=self.app.output)

    def _fragments(self, renderable: RenderableType) -> StyleAndTextTuples:
        """Render a rich object for use inside the live area."""
        console = self._console()
        console.print(renderable, end="")
        return to_formatted_text(ANSI(console.file.getvalue()))  # type: ignore[attr-defined]

    def _flush_text(self, final: bool) -> None:
        """Move finished Markdown blocks from the live area into the scrollback."""
        if final:
            blocks, self._pending = ([self._pending] if self._pending.strip() else []), ""
        else:
            blocks, self._pending = split_blocks(self._pending)
        for block in blocks:
            self.emit(render_assistant(block, first=not self._message_started), gap=True)
            self._message_started = True

    def _finish_call(self, call_id: str, output: Any | None) -> None:
        """Move a tool call from the live area into the scrollback with its result."""
        call = self._calls.pop(call_id, None)
        if call is None:
            return
        name, args = call["name"], call["args"]
        if output is None:
            body: RenderableType = Text("  ⎿  已中断", style="red")
            status = "error"
        else:
            body = render_output(name, args, output)
            status = "error" if is_error(output) else "ok"
        self.emit(Group(render_call(name, args, status), body), gap=True)

    # --------------------------------------------------- UI protocol (Session)

    def text_delta(self, delta: str) -> None:
        if not self._pending and not self._message_started:
            self._verb = petlib.verb("writing")
        self._pending += delta
        if "\n" in delta:
            self._flush_text(final=False)
        self.app.invalidate()

    def text_done(self) -> None:
        self._flush_text(final=True)
        self._message_started = False

    def tool_call(self, call_id: str, name: str, args: dict[str, Any]) -> None:
        self._turn_tools += 1
        self._verb = petlib.verb("working", name)
        preview = call_preview(name, args)
        self._calls[call_id] = {
            "name": name,
            "args": args,
            "started": time.monotonic(),
            "preview": self._fragments(preview) if preview is not None else [],
        }
        self.app.invalidate()

    def tool_output(self, call_id: str, name: str, args: dict[str, Any], output: Any) -> None:
        if call_id not in self._calls:  # e.g. a result for a call from before a restart
            self._calls[call_id] = {"name": name, "args": args, "started": 0.0, "preview": []}
        self._finish_call(call_id, output)
        if not self._calls:
            self._verb = petlib.verb("thinking")
        self.app.invalidate()

    def usage(self, input_tokens: int, output_tokens: int) -> None:
        self._turn_output_tokens += output_tokens

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
        elapsed = time.monotonic() - self._turn_started
        self._last_result = ("happy", time.monotonic())
        levelled_up = self.pet.record_task(self._turn_tools)
        if self._turn_tools or elapsed >= SUMMARY_MIN_SECONDS:
            parts = [format_duration(elapsed)]
            if self._turn_tools:
                parts.append(f"{self._turn_tools} 次工具调用")
            if input_tokens or output_tokens:
                parts.append(f"↑ {format_tokens(input_tokens)} ↓ {format_tokens(output_tokens)}")
            mark = "✓" if self.pet.hidden else petlib.face("happy")
            self.emit(Text(f"  {mark} 完成 · {' · '.join(parts)}", style="dim"), gap=True)
        if levelled_up and not self.pet.hidden:
            self.emit(
                Text(
                    f"  {petlib.face('happy')} {self.pet.name} 升级了！"
                    f"Lv.{self.pet.level} {self.pet.title}",
                    style=f"bold {ACCENT}",
                )
            )

    # ------------------------------------------------------------------ layout

    def _line_prefix(self, line_number: int, wrap_count: int) -> StyleAndTextTuples:
        first = line_number == 0 and wrap_count == 0
        return [("class:prompt", "❯ " if first else "  ")]

    def _rule_style(self) -> str:
        return "class:rule.auto" if self.session.context.auto_approve else "class:rule"

    def _build_layout(self) -> HSplit:
        approving = Condition(lambda: self._approval is not None)
        menu_open = Condition(lambda: bool(self._menu()))
        helping = Condition(lambda: self._show_help)
        rule = lambda: Window(height=1, char="─", style=self._rule_style)  # noqa: E731

        def live(control: Any, **kwargs: Any) -> Window:
            return Window(FormattedTextControl(control), dont_extend_height=True, **kwargs)

        pet_window = ConditionalContainer(
            Window(
                FormattedTextControl(self._pet_text),
                width=petlib.ART_WIDTH + 1,
                height=3,
                dont_extend_height=True,
            ),
            filter=Condition(lambda: not self.pet.hidden and self._columns() >= PET_MIN_COLUMNS),
        )
        input_row = VSplit([HSplit([rule(), self.input, rule()]), pet_window])
        footer = VSplit(
            [
                Window(FormattedTextControl(self._footer_text), height=1),
                Window(
                    FormattedTextControl(self._footer_right_text),
                    height=1,
                    align=WindowAlign.RIGHT,
                    dont_extend_width=True,
                ),
            ]
        )
        return HSplit(
            [
                ConditionalContainer(
                    live(self._calls_text, wrap_lines=True),
                    filter=Condition(lambda: bool(self._calls)),
                ),
                ConditionalContainer(
                    live(self._live_text, wrap_lines=True),
                    filter=Condition(lambda: bool(self._pending.strip())),
                ),
                ConditionalContainer(
                    Window(FormattedTextControl(self._status_text), height=2),
                    filter=Condition(lambda: self.busy and self._approval is None),
                ),
                ConditionalContainer(
                    live(self._queued_text), filter=Condition(lambda: bool(self._queue))
                ),
                ConditionalContainer(HSplit([Window(height=1), input_row]), filter=~approving),
                ConditionalContainer(live(self._approval_text, wrap_lines=True), filter=approving),
                ConditionalContainer(live(self._menu_text), filter=menu_open),
                ConditionalContainer(live(self._help_text), filter=helping & ~menu_open),
                ConditionalContainer(footer, filter=~menu_open & ~helping & ~approving),
            ]
        )

    def _calls_text(self) -> StyleAndTextTuples:
        now = time.monotonic()
        waiting = self._approval is not None
        frame = "○" if waiting else SPINNER[int(now * 10) % len(SPINNER)]
        out: StyleAndTextTuples = []
        for call in self._calls.values():
            out.append(("", "\n"))
            out.append(("class:spinner", f"{frame} "))
            out.append(("class:call", call_title(call["name"], call["args"])))
            if not waiting:
                out.append(("class:dim", f"  {format_duration(now - call['started'])}"))
            if call["preview"]:
                out.append(("", "\n"))
                out.extend(call["preview"])
        return out

    def _live_text(self) -> StyleAndTextTuples:
        lines = self._pending.strip("\n").split("\n")[-LIVE_LINES:]
        bullet = "  " if self._message_started else "● "
        return [("", "\n" + "\n".join((bullet if i == 0 else "  ") + ln for i, ln in enumerate(lines)))]

    def _status_text(self) -> StyleAndTextTuples:
        now = time.monotonic()
        if self.pet.hidden:
            mark = SPINNER[int(now * 10) % len(SPINNER)]
        else:
            mark = petlib.face(self.mood, now)
        details = [format_duration(now - self._turn_started)]
        if self._turn_output_tokens:
            details.append(f"↓ {format_tokens(self._turn_output_tokens)} tokens")
        details.append("esc 中断")
        return [
            ("", "\n"),
            ("class:status", f"{mark} {self._verb or '思考中'}… "),
            ("class:dim", f"({' · '.join(details)})"),
        ]

    def _queued_text(self) -> StyleAndTextTuples:
        lines = [f"  ⏳ {text.splitlines()[0][:100]}" for text in self._queue]
        return [("class:queued", "\n".join(lines))]

    def _pet_text(self) -> StyleAndTextTuples:
        mood = self.mood
        style = "class:pet.asleep" if mood == "sleeping" else "class:pet"
        return [(style, "\n".join(" " + line for line in petlib.art(mood, time.monotonic())))]

    def _footer_text(self) -> StyleAndTextTuples:
        if time.monotonic() - self._exit_armed_at < EXIT_CONFIRM_SECONDS:
            return [("class:footer.notice", "  再按一次 Ctrl-C 退出")]
        if self.session.context.auto_approve:
            return [
                ("class:footer.auto", "  ⏵⏵ 自动批准"),
                ("class:footer", " · shift+tab 关闭"),
            ]
        return [("class:footer", "  ? 快捷键 · / 命令 · @ 文件")]

    def _footer_right_text(self) -> StyleAndTextTuples:
        session = self.session
        parts = [session.model_name]
        if session.context_tokens:
            parts.append(f"上下文 {format_tokens(session.context_tokens)}")
        if self._branch:
            parts.append(f"⎇ {self._branch}")
        return [("class:footer", " · ".join(parts) + "  ")]

    def _help_text(self) -> StyleAndTextTuples:
        out: StyleAndTextTuples = []
        half = (len(SHORTCUTS) + 1) // 2
        for left, right in zip(SHORTCUTS[:half], [*SHORTCUTS[half:], ("", "")]):
            for key, description in (left, right):
                out.append(("class:help.key", f"  {key:<10}"))
                out.append(("class:dim", f" {description}".ljust(22 - len(description))))
            out.append(("", "\n"))
        out.pop()
        return out

    def _menu_text(self) -> StyleAndTextTuples:
        items = self._menu()
        index = min(self._menu_index, len(items) - 1)
        start = max(0, min(index - MENU_ROWS + 1, len(items) - MENU_ROWS))
        width = min(max(len(item.label) for item in items) + 2, 48)
        out: StyleAndTextTuples = []
        for position, item in enumerate(items[start : start + MENU_ROWS], start):
            selected = position == index
            style = "class:menu.selected" if selected else "class:menu"
            description = item.description.splitlines()[0][:60] if item.description else ""
            mark = "❯" if selected else " "
            out.append((style, f" {mark} {item.label.ljust(width)}{description}\n"))
        if len(items) > MENU_ROWS:
            out.append(("class:dim", f"   {index + 1}/{len(items)}\n"))
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
            ("class:approval.title", "─" * self._columns() + "\n"),
            ("class:approval.title", f" 需要确认 · {tool_label(name)}\n"),
            ("", f"   {call_title(name, parse_args(item.arguments))}\n\n"),
            ("", " 是否允许执行？\n"),
        ]
        for index, label in enumerate(options):
            selected = index == approval["index"]
            style = "class:approval.selected" if selected else ""
            out.append((style, f" {'❯' if selected else ' '} {index + 1}. {label}\n"))
        out.append(("class:dim", " ↑↓ 选择 · enter 确认 · shift+tab 全部自动批准"))
        return out

    # ------------------------------------------------------------ key bindings

    def _build_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        approving = Condition(lambda: self._approval is not None)
        menu_open = Condition(lambda: bool(self._menu()))
        empty = Condition(lambda: not self.input.text)

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

        # --- completion menu (slash commands and @files)
        @kb.add("up", filter=menu_open)
        def _(event: KeyPressEvent) -> None:
            self._menu_index = (self._menu_index - 1) % len(self._menu())

        @kb.add("down", filter=menu_open)
        def _(event: KeyPressEvent) -> None:
            self._menu_index = (self._menu_index + 1) % len(self._menu())

        @kb.add("tab", filter=menu_open)
        def _(event: KeyPressEvent) -> None:
            self._accept_menu(submit=False)

        @kb.add("enter", filter=menu_open)
        def _(event: KeyPressEvent) -> None:
            self._accept_menu(submit=True)

        @kb.add("escape", filter=menu_open & ~approving)
        def _(event: KeyPressEvent) -> None:
            buffer = self.input.buffer
            self._menu_dismissed = (buffer.text, buffer.cursor_position)

        # --- input
        @kb.add("enter", filter=~approving & ~menu_open)
        def _(event: KeyPressEvent) -> None:
            self._submit()

        @kb.add("escape", "enter", filter=~approving)
        @kb.add("c-j", filter=~approving)
        def _(event: KeyPressEvent) -> None:
            self.input.buffer.insert_text("\n")

        @kb.add("escape", filter=~approving & ~menu_open)
        def _(event: KeyPressEvent) -> None:
            if self._show_help:
                self._show_help = False
            else:
                self._interrupt()

        @kb.add("?", filter=empty & ~approving)
        def _(event: KeyPressEvent) -> None:
            self._show_help = not self._show_help

        @kb.add("s-tab")
        def _(event: KeyPressEvent) -> None:
            context = self.session.context
            context.auto_approve = not context.auto_approve
            if context.auto_approve and self._approval is not None:
                answer(0)

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

        @kb.add("c-d", filter=empty)
        def _(event: KeyPressEvent) -> None:
            event.app.exit()

        return kb

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
        context = session.context
        workspace = short_path(context.workspace)
        if self._branch:
            workspace += f" (⎇ {self._branch})"

        facts = Table.grid(padding=(0, 2))
        facts.add_column(style="dim", no_wrap=True)
        facts.add_column(overflow="fold")
        facts.add_row("模型", session.model_name)
        if "接口" in session.info:
            facts.add_row("接口", session.info["接口"])
        facts.add_row("工作区", workspace)
        facts.add_row("扩展", f"{len(context.skills)} 个 skill · {len(session.mcp_servers)} 个 MCP 服务器")

        body = Table.grid(padding=(0, 3))
        if not self.pet.hidden:
            cat = Text("\n".join(petlib.art("happy")), style=ACCENT)
            cat.append(f"\n{self.pet.name} Lv.{self.pet.level}", style=f"bold {ACCENT}")
            body.add_row(cat, Group(Text(TAGLINE, style="bold"), Text(), facts))
        else:
            body.add_row(Group(Text(TAGLINE, style="bold"), Text(), facts))
        tips = Text("输入任务开始 · / 命令 · @ 引用文件 · ? 快捷键", style="dim")
        return Panel(
            Group(body, Text(), tips),
            title=f"[bold {ACCENT}]{APP_NAME}[/] [dim]v{__version__}[/]",
            title_align="left",
            border_style=ACCENT,
            expand=False,
            padding=(1, 2),
        )

    def _farewell(self) -> RenderableType:
        session = self.session
        text = Text(style="dim")
        text.append("" if self.pet.hidden else f"{petlib.face('sleeping')} ")
        text.append(f"{self.pet.name} 打了个哈欠，下次见。")
        if session.input_tokens or session.output_tokens:
            text.append(
                f" 本次用量 ↑ {format_tokens(session.input_tokens)}"
                f" ↓ {format_tokens(session.output_tokens)} tokens"
            )
        return text

    async def _run_turn(self, prompt: str) -> None:
        self._turn_started = time.monotonic()
        self._turn_tools = 0
        self._turn_output_tokens = 0
        self._verb = petlib.verb("thinking")
        self._turn = asyncio.ensure_future(self.session.run_turn(prompt))
        failed = True
        try:
            await asyncio.shield(self._turn)
            failed = False
        except asyncio.CancelledError:
            if not self._turn.cancelled():
                # The UI itself is shutting down: stop the turn and propagate.
                self._turn.cancel()
                await asyncio.gather(self._turn, return_exceptions=True)
                raise
            if not self._calls:
                self.emit(Text("  ⎿  已中断", style="red"))
        except MaxTurnsExceeded:
            self.emit(
                Text(
                    f"  ⎿  已达到单次任务的最大轮数 ({self.session.max_turns})，任务未完成。\n"
                    '     已完成的步骤保留在上下文中，输入"继续"可接着执行。\n'
                    "     可用 --max-turns 或配置文件的 max_turns 调大上限。",
                    style="red",
                )
            )
        except (AgentsException, OpenAIError) as exc:
            self.emit(Text(f"  ⎿  出错: {type(exc).__name__}: {exc}", style="red"))
        finally:
            for call_id in list(self._calls):
                self._finish_call(call_id, None)
            if failed:
                self._last_result = ("error", time.monotonic())
            self._pending = ""
            self._message_started = False
            self._turn = None
            self._last_activity = time.monotonic()
            self._branch = git_branch(self.session.context.workspace)
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
        output = self.app.output
        output.set_title(f"{APP_NAME} · {self.session.context.workspace.name}")
        self.emit(self._welcome())
        worker = asyncio.ensure_future(self._worker())
        try:
            await self.app.run_async()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            output.clear_title()
            output.flush()
            self._closed = True
        self.emit(self._farewell())


def history_file(home: Path | None = None) -> FileHistory:
    path = app_dir(home) / "history"
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileHistory(str(path))
