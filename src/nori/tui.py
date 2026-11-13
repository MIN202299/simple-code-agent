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
from collections.abc import Callable
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
from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from . import pet as petlib
from .attachments import clipboard_image
from .branding import ACCENT, APP_NAME, COMMAND, TAGLINE, app_dir, short_path
from .commands import (
    extensions_summary,
    fuzzy_rank,
    match_commands,
    picker_choices,
    run_command,
)
from .compact import COMPACT_AT
from .i18n import t
from .permissions import describe_rule
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
    render_history,
    render_output,
    render_user,
    split_blocks,
    tool_label,
)
from .session import COMPACTED, COMPACTION_FAILED, RETRYING, Session
from .tools import SKIP_DIRS

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # running tool calls
THINKING_SPINNER = "·✢✳✶✻✽✻✶✳✢"  # the status line
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

# Key and English description; descriptions are translated when shown.
SHORTCUTS = [
    ("enter", "send"),
    ("alt+enter", "new line"),
    ("esc", "interrupt the task"),
    ("/", "commands"),
    ("@", "mention a file or image"),
    ("!", "run a shell command"),
    ("ctrl+v", "paste an image"),
    ("↑ ↓", "input history"),
    ("shift+tab", "switch mode"),
    ("ctrl+l", "clear the screen"),
    ("ctrl+c", "interrupt / clear / quit"),
    ("ctrl+d", "quit"),
]

STYLE = Style.from_dict(
    {
        "rule": "fg:ansibrightblack",
        "rule.auto": "fg:ansimagenta",
        "rule.accept-edits": "fg:ansiblue",
        "rule.plan": "fg:ansicyan",
        "rule.shell": "fg:ansiyellow",
        "prompt.shell": "fg:ansiyellow bold",
        "prompt": f"fg:{ACCENT} bold",
        "placeholder": "fg:ansibrightblack",
        "pet": f"fg:{ACCENT}",
        "pet.asleep": "fg:ansibrightblack",
        "spinner": f"fg:{ACCENT}",
        "call": "bold",
        "status": "fg:ansiyellow",
        "dim": "fg:ansibrightblack",
        "footer": "fg:ansibrightblack",
        "footer.auto": "fg:ansimagenta bold",
        "footer.accept-edits": "fg:ansiblue bold",
        "footer.plan": "fg:ansicyan bold",
        "footer.notice": "fg:ansiyellow",
        "queued": "fg:ansibrightblack italic",
        "menu": "fg:ansibrightblack",
        "menu.selected": f"fg:{ACCENT} bold",
        "menu.title": "bold",
        "menu.current": "fg:ansigreen",
        "help.key": "bold",
        "approval.title": "fg:ansiyellow bold",
        "approval.selected": f"fg:{ACCENT} bold",
    }
)

# Footer labels of the non-default permission modes (English keys, translated on display).
MODE_LABELS = {
    "accept-edits": "accept edits on",
    "plan": "plan mode on",
    "auto": "auto-approve on",
}
_FILE_TOKEN = re.compile(r"(?:^|\s)@(\S*)$")


@dataclass(frozen=True)
class MenuItem:
    label: str  # the text inserted or shown, e.g. "/model" or "@src/a.py"
    description: str = ""
    hint: str = ""  # argument hint shown after the label, e.g. "[name]"
    takes_arg: bool = False
    picker: bool = False  # choosing it opens a list of choices
    tag: str = ""  # e.g. "skill"
    command: str = ""  # picker rows: the command line to run
    current: bool = False  # picker rows: the active choice


class _Placeholder(Processor):
    """Shows a hint in the empty input box."""

    def __init__(self, text: Callable[[], str]) -> None:
        self.text = text

    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        if ti.lineno == 0 and not ti.document.text:
            return Transformation([*ti.fragments, ("class:placeholder", self.text())])
        return Transformation(ti.fragments)


def git_branch(workspace: Path) -> str:
    """Current branch name, read straight from .git/HEAD (no subprocess)."""
    try:
        head = (workspace / ".git" / "HEAD").read_text("utf-8").strip()
    except OSError:
        return ""
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else head[:7]


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
        # An open list of choices (models, sessions, languages); the input box filters it.
        self._picker: dict[str, Any] | None = None
        self._show_help = False
        self._exit_armed_at = 0.0
        self._closed = False
        self._notice: tuple[str, float] = ("", 0.0)  # a short-lived footer message
        self._compacting = False
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
            input_processors=[_Placeholder(self._placeholder)],
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
        picker = self._picker
        key = (buffer.text, buffer.cursor_position, id(picker), len(picker["choices"]) if picker else 0)
        if picker is None and key == self._menu_dismissed:
            return "", []
        if key == self._menu_cache[0]:
            return self._menu_cache[1], self._menu_cache[2]
        kind, items = "", []
        commands = match_commands(self.session, buffer.text) if picker is None else []
        if picker is not None:
            kind = "picker"
            ranked = []
            for order, choice in enumerate(picker["choices"]):
                rank = fuzzy_rank(buffer.text.strip(), choice.label)
                if rank is not None:
                    ranked.append((rank, order, choice))
            items = [
                MenuItem(c.label, c.description, command=c.command, current=c.current)
                for _rank, _order, c in sorted(ranked, key=lambda r: r[:2])
            ]
        elif commands:
            kind = "command"
            items = [
                MenuItem(
                    f"/{c.name}",
                    c.description if c.skill else t(c.description),
                    hint=c.arg,
                    takes_arg=c.takes_arg,
                    picker=c.picker,
                    tag="skill" if c.skill else "",
                )
                for c in commands
            ]
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
        if kind == "picker":
            self._close_picker()
            self._enqueue(item.command)
        elif kind == "command" and submit and item.picker:
            self._open_picker(item.label[1:])
        elif kind == "file":
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

    def _placeholder(self) -> str:
        if self._picker is not None:
            return t("Type to filter…")
        return t("Describe what you want to do…")

    def _open_picker(self, name: str) -> None:
        """Show the choices of a command (models, sessions, languages)."""
        picker: dict[str, Any] = {"name": name, "title": "", "choices": [], "loading": True}
        self._picker = picker
        self.input.buffer.reset()
        self._menu_index = 0

        async def load() -> None:
            result = await picker_choices(self.session, name)
            if self._picker is not picker:
                return  # closed, or replaced by another picker, while loading
            if result is None or not result[1]:
                # Nothing to choose from: let the command itself explain why.
                self._close_picker()
                self._enqueue(f"/{name}")
                return
            picker["title"], picker["choices"] = result
            picker["loading"] = False
            current = next((i for i, c in enumerate(result[1]) if c.current), 0)
            self._menu_index = current
            self.app.invalidate()

        picker["task"] = asyncio.ensure_future(load())

    def _close_picker(self) -> None:
        self._picker = None
        self.input.buffer.reset()
        self._menu_index = 0

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
            body: RenderableType = Text("  ⎿  " + t("Interrupted"), style="red")
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

    def compaction(self, stage: str, before: int = 0, after: int = 0) -> None:
        if stage == "start":
            self._compacting = True
            self._verb = t("Tidying memories, compacting context")
        else:
            self._compacting = False
            self._verb = petlib.verb("thinking")
        if stage == "done":
            self.emit(
                Text(
                    "  ✻ "
                    + t(COMPACTED, before=format_tokens(before), after=format_tokens(after)),
                    style="dim",
                ),
                gap=True,
            )
        elif stage == "failed":
            self.emit(Text("  ✻ " + t(COMPACTION_FAILED), style="yellow"), gap=True)
        self.app.invalidate()

    def tool_progress(self, call_id: str, text: str) -> None:
        if call_id in self._calls:
            self._calls[call_id]["progress"] = text
            self.app.invalidate()

    def retrying(self, attempt: int, limit: int, reason: str) -> None:
        message = t(RETRYING, reason=reason, attempt=attempt, limit=limit)
        self.emit(Text(f"  ✻ {message}", style="yellow"), gap=True)

    def show_history(self, items: list[Any]) -> None:
        for entry in render_history(items):
            self.emit(entry, gap=True)
        self.emit(Text("  ✻ " + t("Session resumed — carry on"), style="dim"), gap=True)

    async def _ask(
        self,
        title: str,
        subject: str,
        question: str,
        options: list[tuple[str, str]],
        cancel: str,
        body: StyleAndTextTuples | None = None,
        allowed_by_mode: Callable[[], bool] | None = None,
    ) -> str:
        """Open the dialog over the input box and wait for a choice.

        `options` are (label, value) pairs; Esc answers `cancel`.
        """
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._approval = {
            "title": title,
            "subject": subject,
            "question": question,
            "options": options,
            "cancel": cancel,
            "body": body or [],
            "allowed_by_mode": allowed_by_mode or (lambda: False),
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

    async def approve(self, item: ToolApprovalItem, rule: str | None = None) -> str:
        name = item.tool_name or "tool"
        args = parse_args(item.arguments)
        # Calls made by a sub-agent are not in the live area, so the dialog has to
        # show what is about to happen itself.
        preview = None if item.call_id in self._calls else call_preview(name, args)
        options = [(t("Yes"), "yes")]
        if rule:
            if ":" in rule:  # a command prefix or a host
                label = t("Yes, and don't ask again for {what} in this project", what=f"`{describe_rule(rule)}`")
            else:
                label = t("Yes, and don't ask again for {what} in this project", what=tool_label(name))
            options.append((label, "always"))
        options.append((t("No, tell the agent to do it differently (esc)"), "no"))
        permissions = self.session.context.permissions
        return await self._ask(
            f"{t('Approval needed')} · {tool_label(name)}",
            call_title(name, args),
            t("Allow this?"),
            options,
            cancel="no",
            body=self._fragments(preview) if preview is not None else [],
            allowed_by_mode=lambda: permissions.decide(name, args) == "allow",
        )

    async def approve_plan(self, plan: str) -> str:
        self.emit(render_assistant(plan), gap=True)
        return await self._ask(
            t("Plan ready"),
            t("The plan is shown above."),
            t("Carry out this plan?"),
            [
                (t("Yes, and accept file edits without asking"), "accept-edits"),
                (t("Yes, and ask before each edit"), "default"),
                (t("No, keep planning (esc)"), ""),
            ],
            cancel="",
        )

    def notice(self, text: str) -> None:
        self.emit(Text(f"  ✻ {text}", style="dim"), gap=True)

    def turn_done(self, input_tokens: int, output_tokens: int) -> None:
        elapsed = time.monotonic() - self._turn_started
        self._last_result = ("happy", time.monotonic())
        levelled_up = self.pet.record_task(self._turn_tools)
        if self._turn_tools or elapsed >= SUMMARY_MIN_SECONDS:
            parts = [format_duration(elapsed)]
            if self._turn_tools:
                parts.append(t("{count} tool calls", count=self._turn_tools))
            if input_tokens or output_tokens:
                parts.append(f"↑ {format_tokens(input_tokens)} ↓ {format_tokens(output_tokens)}")
            mark = "✓" if self.pet.hidden else petlib.face("happy")
            self.emit(Text(f"  {mark} {t('Done')} · {' · '.join(parts)}", style="dim"), gap=True)
        if levelled_up and not self.pet.hidden:
            self.emit(
                Text(
                    f"  {petlib.face('happy')} {t('{name} levelled up!', name=self.pet.name)} "
                    f"Lv.{self.pet.level} {self.pet.title}",
                    style=f"bold {ACCENT}",
                )
            )

    # ------------------------------------------------------------------ layout

    def _line_prefix(self, line_number: int, wrap_count: int) -> StyleAndTextTuples:
        first = line_number == 0 and wrap_count == 0
        if self.input.text.startswith("!"):
            return [("class:prompt.shell", "! " if first else "  ")]
        return [("class:prompt", "❯ " if first else "  ")]

    def _rule_style(self) -> str:
        if self.input.text.startswith("!"):
            return "class:rule.shell"
        mode = self.session.mode
        return "class:rule" if mode == "default" else f"class:rule.{mode}"

    def _build_layout(self) -> HSplit:
        approving = Condition(lambda: self._approval is not None)
        menu_open = Condition(lambda: bool(self._menu()) or self._picker is not None)
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
                    filter=Condition(
                        lambda: (self.busy or self._compacting) and self._approval is None
                    ),
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
            if call.get("progress"):
                out.append(("class:dim", f"\n  ⎿  {call['progress']}"))
        return out

    def _live_text(self) -> StyleAndTextTuples:
        lines = self._pending.strip("\n").split("\n")[-LIVE_LINES:]
        bullet = "  " if self._message_started else "● "
        return [("", "\n" + "\n".join((bullet if i == 0 else "  ") + ln for i, ln in enumerate(lines)))]

    def _status_text(self) -> StyleAndTextTuples:
        now = time.monotonic()
        mark = THINKING_SPINNER[int(now * 8) % len(THINKING_SPINNER)]
        details = [format_duration(now - self._turn_started)]
        if self._turn_output_tokens:
            details.append(f"↓ {format_tokens(self._turn_output_tokens)} tokens")
        details.append(t("esc to interrupt"))
        return [
            ("", "\n"),
            ("class:status", f"{mark} {self._verb or t('Thinking')}… "),
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
            return [("class:footer.notice", "  " + t("Press Ctrl-C again to quit"))]
        notice, shown_at = self._notice
        if notice and time.monotonic() - shown_at < EXIT_CONFIRM_SECONDS:
            return [("class:footer.notice", f"  {notice}")]
        mode = self.session.mode
        out: StyleAndTextTuples
        if mode == "default":
            out = [("class:footer", "  " + t("? shortcuts · / commands · @ files"))]
        else:
            mark = "⏸" if mode == "plan" else "⏵⏵"
            out = [
                (f"class:footer.{mode}", f"  {mark} " + t(MODE_LABELS[mode])),
                ("class:footer", " · " + t("shift+tab to switch")),
            ]
        running = len(self.session.context.shell_tasks.running)
        if running:
            out.append(("class:footer", " · " + t("{count} background", count=running)))
        return out

    def _footer_right_text(self) -> StyleAndTextTuples:
        session = self.session
        out: StyleAndTextTuples = [("class:footer", session.model_name)]
        if session.context_tokens:
            share = session.context_share
            style = "class:footer.notice" if share >= COMPACT_AT else "class:footer"
            out.append(("class:footer", " · "))
            out.append((style, f"{t('context')} {format_tokens(session.context_tokens)} ({share:.0%})"))
        if self._branch:
            out.append(("class:footer", f" · ⎇ {self._branch}"))
        out.append(("", "  "))
        return out

    def _help_text(self) -> StyleAndTextTuples:
        out: StyleAndTextTuples = []
        half = (len(SHORTCUTS) + 1) // 2
        for left, right in zip(SHORTCUTS[:half], [*SHORTCUTS[half:], ("", "")]):
            for key, description in (left, right):
                description = t(description) if description else ""
                out.append(("class:help.key", f"  {key:<10}"))
                out.append(("class:dim", f" {description}" + " " * max(26 - cell_len(description), 1)))
            out.append(("", "\n"))
        out.pop()
        return out

    def _menu_text(self) -> StyleAndTextTuples:
        kind, items = self._menu_state()
        picker = self._picker
        out: StyleAndTextTuples = []
        if picker is not None:
            title = t("Loading…") if picker["loading"] else picker["title"]
            out.append(("class:menu.title", f"   {title}\n"))
            if not items and not picker["loading"]:
                custom = picker["name"] == "model" and self.input.text.strip()
                note = t("No match — press Enter to use this name") if custom else t("No match")
                out.append(("class:dim", f"   {note}\n"))
        index = min(self._menu_index, len(items) - 1)
        start = max(0, min(index - MENU_ROWS + 1, len(items) - MENU_ROWS))
        columns = self._columns()
        width = min(max((cell_len(f"{i.label} {i.hint}") for i in items), default=0) + 2, 44)
        for position, item in enumerate(items[start : start + MENU_ROWS], start):
            selected = position == index
            style = "class:menu.selected" if selected else ""
            mark = "✓" if item.current else " "
            label = item.label if cell_len(item.label) < width else item.label[: width - 2] + "…"
            padding = " " * max(width - cell_len(f"{label} {item.hint}".rstrip()), 1)
            room = max(columns - width - 8 - cell_len(item.tag), 10)
            description = item.description.splitlines()[0] if item.description else ""
            if cell_len(description) > room:
                description = description[: max(room // 2, 8)] + "…"
            out.append((style, f" {'❯' if selected else ' '} "))
            out.append(("class:menu.current", mark) if item.current else ("", mark))
            out.append((style, f" {label}"))
            out.append(("class:dim", f" {item.hint}" if item.hint else ""))
            out.append(("class:menu.selected" if selected else "class:menu", f"{padding}{description}"))
            out.append(("class:dim", f"  {item.tag}\n" if item.tag else "\n"))
        position = f"{index + 1}/{len(items)} · " if len(items) > MENU_ROWS else ""
        if kind == "picker" or picker is not None:
            hint = t("↑↓ select · enter confirm · esc cancel · type to filter")
        elif kind == "file":
            hint = t("↑↓ select · tab/enter insert · esc close")
        else:
            hint = t("↑↓ select · tab complete · enter run · esc close")
        out.append(("class:dim", f"   {position}{hint}"))
        return out

    def _approval_text(self) -> StyleAndTextTuples:
        approval = self._approval
        if approval is None:
            return []
        out: StyleAndTextTuples = [
            ("", "\n"),
            ("class:approval.title", "─" * self._columns() + "\n"),
            ("class:approval.title", f" {approval['title']}\n"),
            ("", f"   {approval['subject']}\n"),
            *approval["body"],
            ("", "\n\n" if approval["body"] else "\n"),
            ("", f" {approval['question']}\n"),
        ]
        for index, (label, _value) in enumerate(approval["options"]):
            selected = index == approval["index"]
            style = "class:approval.selected" if selected else ""
            out.append((style, f" {'❯' if selected else ' '} {index + 1}. {label}\n"))
        out.append(("class:dim", " " + t("↑↓ select · enter confirm · shift+tab switch mode")))
        return out

    # ------------------------------------------------------------ key bindings

    def _build_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        approving = Condition(lambda: self._approval is not None)
        menu_open = Condition(lambda: bool(self._menu()))
        picking = Condition(lambda: self._picker is not None)
        empty = Condition(lambda: not self.input.text and self._picker is None)

        def answer(index: int) -> None:
            approval = self._approval
            if approval is None or approval["future"].done():
                return
            if time.monotonic() - approval["opened"] < self.approval_grace:
                return
            if 0 <= index < len(approval["options"]):
                approval["future"].set_result(approval["options"][index][1])

        def cancel() -> None:
            approval = self._approval
            if approval is None or approval["future"].done():
                return
            if time.monotonic() - approval["opened"] >= self.approval_grace:
                approval["future"].set_result(approval["cancel"])

        def move(step: int) -> None:
            approval = self._approval
            if approval is not None:
                approval["index"] = (approval["index"] + step) % len(approval["options"])

        # --- approval dialog: it owns the keyboard while it is open
        @kb.add("up", filter=approving)
        @kb.add("c-p", filter=approving)
        def _(event: KeyPressEvent) -> None:
            move(-1)

        @kb.add("down", filter=approving)
        @kb.add("c-n", filter=approving)
        @kb.add("tab", filter=approving)
        def _(event: KeyPressEvent) -> None:
            move(1)

        @kb.add("enter", filter=approving)
        def _(event: KeyPressEvent) -> None:
            answer(self._approval["index"])  # type: ignore[index]

        for key, index in (("1", 0), ("2", 1), ("3", 2)):
            kb.add(key, filter=approving)(lambda event, index=index: answer(index))

        @kb.add("escape", filter=approving)
        def _(event: KeyPressEvent) -> None:
            cancel()

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
            self._menu_dismissed = (buffer.text, buffer.cursor_position, id(None), 0)

        # --- picker: owns Enter and Esc even when the filter matches nothing
        @kb.add("enter", filter=picking & ~menu_open)
        def _(event: KeyPressEvent) -> None:
            picker, text = self._picker, self.input.text.strip()
            if picker is not None and picker["name"] == "model" and text and not picker["loading"]:
                self._close_picker()
                self._enqueue(f"/model {text}")  # a model that is not in the list

        @kb.add("escape", filter=picking & ~approving)
        def _(event: KeyPressEvent) -> None:
            self._close_picker()

        @kb.add("up", filter=picking & ~menu_open)
        @kb.add("down", filter=picking & ~menu_open)
        def _(event: KeyPressEvent) -> None:
            pass  # nothing to move through; do not fall back to input history

        # --- input
        @kb.add("enter", filter=~approving & ~menu_open & ~picking)
        def _(event: KeyPressEvent) -> None:
            self._submit()

        @kb.add("escape", "enter", filter=~approving)
        @kb.add("c-j", filter=~approving)
        def _(event: KeyPressEvent) -> None:
            self.input.buffer.insert_text("\n")

        @kb.add("escape", filter=~approving & ~menu_open & ~picking)
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
            self.session.context.permissions.cycle()
            # If the new mode already allows what is being asked about, do not make
            # the user answer it as well.
            approval = self._approval
            if approval is not None and approval.get("allowed_by_mode", lambda: False)():
                answer(0)

        @kb.add("c-l")
        def _(event: KeyPressEvent) -> None:
            event.app.renderer.clear()

        @kb.add("c-v", filter=~approving)
        def _(event: KeyPressEvent) -> None:
            # Text is pasted by the terminal itself; this is for an image on the clipboard.
            image = clipboard_image()
            if image is None:
                self._notice = (t("No image on the clipboard"), time.monotonic())
            else:
                self.input.buffer.insert_text(f"{image} ")

        @kb.add("c-c")
        def _(event: KeyPressEvent) -> None:
            if self._picker is not None:
                self._close_picker()
            elif self.busy:
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
        self._enqueue(text)

    def _enqueue(self, text: str) -> None:
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
        facts.add_row(t("Model"), session.model_name)
        if "Endpoint" in session.info:
            facts.add_row(t("Endpoint"), session.info["Endpoint"])
        facts.add_row(t("Workspace"), workspace)
        facts.add_row(t("Extensions"), extensions_summary(session))

        body = Table.grid(padding=(0, 3))
        if not self.pet.hidden:
            cat = Text("\n".join(petlib.art("happy")), style=ACCENT)
            cat.append(f"\n{self.pet.name} Lv.{self.pet.level}", style=f"bold {ACCENT}")
            body.add_row(cat, Group(Text(t(TAGLINE), style="bold"), Text(), facts))
        else:
            body.add_row(Group(Text(t(TAGLINE), style="bold"), Text(), facts))
        tips = Text(t("Type a task to begin · / commands · @ files · ? shortcuts"), style="dim")
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
        text.append(t("{name} yawns. See you next time.", name=self.pet.name))
        if session.history and session.store is not None:
            text.append(" " + t("Resume this session: {command}", command=f"{COMMAND} --resume {session.session_id}"))
        if session.input_tokens or session.output_tokens:
            text.append(
                f" {t('Usage')} ↑ {format_tokens(session.input_tokens)}"
                f" ↓ {format_tokens(session.output_tokens)} tokens"
            )
        return text

    async def _user_shell(self, command: str) -> None:
        """Run a `!command` typed by the user and show its output."""
        self._verb = t("Running your command")
        output, code = await self.session.user_shell(command)
        text = f"{output}\n[exit code: {code}]".lstrip()
        self.emit(render_output("run_shell", {"command": command}, text))

    async def _run_turn(self, prompt: str) -> None:
        self._verb = petlib.verb("thinking")
        await self._run(self.session.run_turn(prompt))

    async def _run(self, work: Any) -> None:
        """Run something interruptible (a turn, a shell command) and report how it ended."""
        self._turn_started = time.monotonic()
        self._turn_tools = 0
        self._turn_output_tokens = 0
        self._turn = asyncio.ensure_future(work)
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
                self.emit(Text("  ⎿  " + t("Interrupted"), style="red"))
        except MaxTurnsExceeded:
            self.emit(
                Text(
                    f"  ⎿  {t(MAX_TURNS_REACHED, limit=self.session.max_turns)}\n"
                    f"     {t(MAX_TURNS_HINT)}",
                    style="red",
                )
            )
        except (AgentsException, OpenAIError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.emit(Text("  ⎿  " + t("Error: {error}", error=error), style="red"))
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
            if text.startswith("!") and text[1:].strip():
                await self._run(self._user_shell(text[1:].strip()))
                continue
            if not text.startswith("/"):
                await self._run_turn(text)
                continue
            self._turn_started = time.monotonic()  # for the status line of /compact
            result = await run_command(self.session, text, self.emit)
            if result.exit:
                self.app.exit()
                return
            if result.prompt:  # a skill invoked as /<skill>
                await self._run_turn(result.prompt)

    async def run(self, show_resumed: bool = False) -> None:
        output = self.app.output
        output.set_title(f"{APP_NAME} · {self.session.context.workspace.name}")
        self.emit(self._welcome())
        if show_resumed:
            self.show_history(self.session.history)
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
