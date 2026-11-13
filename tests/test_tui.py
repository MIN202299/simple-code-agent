"""Headless tests of the TUI: real key presses through a pipe, scripted model."""

from __future__ import annotations

import asyncio
import time

from agents import set_tracing_disabled
from agents.testing import ScriptedModel, assistant_message, function_call
from openai import OpenAIError
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nori.session import Session
from nori.skills import discover_skills
from nori.pet import load_pet
from nori.tui import Tui

set_tracing_disabled(True)

ENTER, ESC, UP, DOWN, TAB, CTRL_C, CTRL_D = "\r", "\x1b", "\x1b[A", "\x1b[B", "\t", "\x03", "\x04"
SHIFT_TAB, ALT_ENTER = "\x1b[Z", "\x1b\r"


class Harness:
    def __init__(self, tui: Tui, pipe) -> None:
        self.tui = tui
        self.pipe = pipe

    def send(self, keys: str) -> None:
        self.pipe.send_text(keys)

    @property
    def text(self) -> str:
        return "\n".join(self.tui.transcript)

    async def until(self, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, f"timed out; transcript:\n{self.text}"
            await asyncio.sleep(0.01)

    async def idle(self) -> None:
        """Wait until submitted input has been fully processed."""
        await asyncio.sleep(0.05)
        await self.until(lambda: not self.tui.busy and not self.tui._queue)
        await asyncio.sleep(0.05)


def run_tui(tmp_path, steps, scenario, **session_kwargs):
    """Start the TUI, run `scenario(harness)` against it, then quit."""

    async def main():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            session = Session(tmp_path, model=ScriptedModel(steps), **session_kwargs)
            tui = Tui(session)
            tui.approval_grace = 0.0
            harness = Harness(tui, pipe)
            app = asyncio.ensure_future(tui.run())
            await harness.until(lambda: tui.app.is_running)
            try:
                await scenario(harness)
            finally:
                if not app.done():
                    tui.app.exit()
                await app
            return harness

    return asyncio.run(main())


def test_message_is_streamed_into_the_transcript(tmp_path):
    async def scenario(h: Harness):
        h.send("hello there" + ENTER)
        await h.idle()

    h = run_tui(tmp_path, [[assistant_message("Hi!\n\nSecond *paragraph*.")]], scenario)

    assert "Nori" in h.tui.transcript[0]
    assert "❯ hello there" in h.text
    assert "● Hi!" in h.text and "Second paragraph." in h.text
    assert h.tui.input.text == ""


def test_approval_dialog_yes_no_and_always(tmp_path):
    steps = [
        [function_call("write_file", {"path": "a.txt", "content": "1"}, call_id="c1")],
        [function_call("write_file", {"path": "b.txt", "content": "2"}, call_id="c2")],
        [function_call("write_file", {"path": "c.txt", "content": "3"}, call_id="c3")],
        [function_call("write_file", {"path": "d.txt", "content": "4"}, call_id="c4")],
        [assistant_message("done")],
    ]

    async def scenario(h: Harness):
        tui = h.tui
        h.send("write files" + ENTER)
        await h.until(lambda: tui._approval is not None)
        dialog = "".join(text for _style, text in tui._approval_text())
        assert "Write(a.txt)" in dialog and "❯ 1. 是" in dialog
        h.send("typed text is swallowed")
        h.send(ENTER)  # 1. yes
        await h.until(lambda: (tmp_path / "a.txt").exists())

        await h.until(lambda: tui._approval is not None)
        h.send(ESC)  # no
        await h.until(lambda: "rejected" in h.text)

        await h.until(lambda: tui._approval is not None)
        h.send(DOWN + ENTER)  # 2. always
        await h.idle()  # d.txt is then written without asking

    h = run_tui(tmp_path, steps, scenario)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt", "c.txt", "d.txt"]
    assert h.tui.session.context.permissions.rules == ["write_file"]
    assert h.tui.input.text == ""
    assert "● Write(a.txt)" in h.text and "+1" in h.text and "done" in h.text


def test_type_ahead_cannot_approve(tmp_path):
    steps = [
        [function_call("write_file", {"path": "a.txt", "content": "1"}, call_id="c1")],
        [assistant_message("done")],
    ]

    async def scenario(h: Harness):
        tui = h.tui
        tui.approval_grace = 60.0
        h.send("go" + ENTER)
        await h.until(lambda: tui._approval is not None)
        h.send(ENTER + "1" + "2")
        await asyncio.sleep(0.2)
        assert tui._approval is not None and not (tmp_path / "a.txt").exists()
        tui.approval_grace = 0.0
        h.send("3")
        await h.idle()

    run_tui(tmp_path, steps, scenario)
    assert not (tmp_path / "a.txt").exists()


def test_escape_interrupts_and_the_conversation_continues(tmp_path):
    steps = [
        [function_call("run_shell", {"command": "sleep 30"}, call_id="c1")],
        [assistant_message("never shown")],
    ]

    async def scenario(h: Harness):
        tui = h.tui
        h.send("go" + ENTER)
        await h.until(lambda: bool(tui._calls))
        live = "".join(text for _style, text in tui._calls_text())
        assert "Bash(sleep 30)" in live and "Bash(sleep 30)" not in h.text
        assert tui.mood == "working"
        await asyncio.sleep(0.2)
        h.send("queued while busy" + ENTER)
        await h.until(lambda: list(tui._queue) == ["queued while busy"])
        started = time.monotonic()
        h.send(ESC)
        await h.until(lambda: "已中断" in h.text)
        assert time.monotonic() - started < 3
        await h.idle()

    h = run_tui(tmp_path, steps, scenario, auto_approve=True)

    assert "● Bash(sleep 30)\n  ⎿  已中断" in h.text
    assert h.text.index("已中断") < h.text.index("❯ queued while busy")
    assert "never shown" in h.text  # the queued message ran as the next turn
    sent = h.tui.session.agent.model.last_call.input
    assert [i["content"] for i in sent if i.get("role") == "user"] == [
        "go",
        "[Request interrupted by user]",
        "queued while busy",
    ]


def test_slash_menu_commands_and_skills(tmp_path):
    skill_dir = tmp_path / ".agents/skills/review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\ndescription: review code\n---\nDo the review.")
    skills = discover_skills(tmp_path, tmp_path / "home")

    async def scenario(h: Harness):
        tui = h.tui
        h.send("/")
        await h.until(lambda: len(tui._menu()) == 18)
        menu = "".join(text for _style, text in tui._menu_text())
        assert "❯   /help" in menu and "1/18" in menu and "/model [name]" in menu  # eight rows shown, the list scrolls
        review = tui._menu()[-1]
        assert (review.label, review.description) == ("/review", "review code")

        h.send("m" + DOWN + ENTER)  # /model, /mcp -> second entry runs immediately
        await h.until(lambda: "没有已连接的 MCP 服务器" in h.text)

        h.send("/mo" + TAB)  # completes and waits for an argument
        await h.until(lambda: tui.input.text == "/model ")
        h.send("gpt-x" + ENTER)
        await h.until(lambda: tui.session.model == "gpt-x")
        use_scripted_model(tui.session)

        h.send("/rev" + ENTER)  # skills take an argument: Enter completes first
        await h.until(lambda: tui.input.text == "/review ")
        h.send("auth.py" + ENTER)
        await h.idle()

        h.send(SHIFT_TAB)
        await h.until(lambda: tui.session.mode == "accept-edits")
        h.send("line one" + ALT_ENTER + "line two")
        await h.until(lambda: tui.input.text == "line one\nline two")
        h.send(CTRL_C)  # clears the input
        await h.until(lambda: tui.input.text == "")
        h.send("/exit" + ENTER)
        await h.until(lambda: not tui.app.is_running)

    model = ScriptedModel([[assistant_message("reviewed")]])

    def use_scripted_model(session: Session) -> None:
        session.model = model
        session.agent = session._build_agent()

    h = run_tui(tmp_path, [], scenario, skills=skills)

    assert "❯ /review auth.py" in h.text and "reviewed" in h.text
    prompt = model.last_call.input[-1]["content"]
    assert "Do the review." in prompt and prompt.endswith("Request: auth.py")


def test_ctrl_c_twice_and_ctrl_d_exit(tmp_path):
    async def twice(h: Harness):
        h.send(CTRL_C)
        await asyncio.sleep(0.1)
        assert h.tui.app.is_running
        assert "再按一次" in "".join(text for _style, text in h.tui._footer_text())
        h.send(CTRL_C)
        await h.until(lambda: not h.tui.app.is_running)

    async def ctrl_d(h: Harness):
        h.send(CTRL_D)
        await h.until(lambda: not h.tui.app.is_running)

    run_tui(tmp_path, [], twice)
    run_tui(tmp_path, [], ctrl_d)


def test_api_errors_are_shown_and_the_ui_survives(tmp_path):
    error = OpenAIError("connection refused")

    async def scenario(h: Harness):
        h.send("one" + ENTER)
        await h.until(lambda: "出错" in h.text)
        await h.idle()
        h.send("two" + ENTER)
        await h.idle()

    h = run_tui(tmp_path, [error, [assistant_message("recovered")]], scenario)
    assert "OpenAIError: connection refused" in h.text and "recovered" in h.text


def test_file_mentions_help_panel_and_pet(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("hi\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "calc.js").write_text("")

    async def scenario(h: Harness):
        tui = h.tui
        h.send("?")
        await h.until(lambda: tui._show_help)
        assert "shift+tab" in "".join(text for _style, text in tui._help_text())
        assert tui.input.text == ""
        h.send(ESC)
        await h.until(lambda: not tui._show_help)

        h.send("look at @cal")
        await h.until(lambda: [item.label for item in tui._menu()] == ["@src/calc.py"])
        h.send(ENTER)  # accepts the completion instead of sending
        await h.until(lambda: tui.input.text == "look at @src/calc.py ")
        assert not tui._queue and not tui.busy
        h.send("please" + ENTER)
        await h.idle()

        h.send("/pet name 小黑" + ENTER)
        await h.until(lambda: tui.pet.name == "小黑")
        h.send("/status" + ENTER)
        await h.until(lambda: "累计用量" in h.text)

    pet_file = tmp_path.parent / f"{tmp_path.name}-pet.json"
    steps = [
        [function_call("read_file", {"path": "src/calc.py"}, call_id="c1")],
        [assistant_message("looked")],
    ]

    async def main():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            session = Session(tmp_path, model=ScriptedModel(steps))
            tui = Tui(session, pet=load_pet(pet_file))
            harness = Harness(tui, pipe)
            app = asyncio.ensure_future(tui.run())
            await harness.until(lambda: tui.app.is_running)
            try:
                await scenario(harness)
            finally:
                tui.app.exit()
                await app
            return harness

    h = asyncio.run(main())

    assert "❯ look at @src/calc.py please" in h.text
    assert "● Read(src/calc.py)\n  ⎿  Read 1 lines" in h.text
    assert "完成 · " in h.text and "1 次工具调用" in h.text
    assert "小黑" in h.text and "下次见" in h.tui.transcript[-1]
    saved = load_pet(pet_file)
    assert (saved.name, saved.tasks, saved.tools) == ("小黑", 1, 1)


def test_sub_agent_progress_and_nested_approval(tmp_path):
    steps = [
        [function_call("spawn_agent", {"description": "write it", "prompt": "Write."}, call_id="c1")],
        [function_call("write_file", {"path": "a.txt", "content": "hello"}, call_id="s1")],
        [assistant_message("REPORT: wrote a.txt")],
        [assistant_message("done")],
    ]

    async def scenario(h: Harness):
        tui = h.tui
        h.send("delegate" + ENTER)
        await h.until(lambda: tui._approval is not None)
        live = "".join(text for _style, text in tui._calls_text())
        assert "Agent(write it)" in live and "Write(a.txt) · 第 1 次工具调用" in live
        # the sub-agent's call is not in the live area, so the dialog shows the preview
        dialog = "".join(text for _style, text in tui._approval_text())
        assert "Write(a.txt)" in dialog and "+hello" in dialog
        h.send(ENTER)
        await h.idle()

    h = run_tui(tmp_path, steps, scenario)
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert "● Agent(write it)\n  ⎿  REPORT: wrote a.txt" in h.text and "done" in h.text


def test_file_mention_anywhere_in_the_text(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("x = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "calc-notes.md").write_text("n\n")
    (tmp_path / "README.md").write_text("hi\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / ".DS_Store").write_text("")

    def labels(tui):
        return [item.label for item in tui._menu()]

    async def scenario(h: Harness):
        tui = h.tui
        buffer = tui.input.buffer

        # right after Chinese text, with no space before the @
        h.send("请看一下@cal")
        await h.until(lambda: labels(tui) == ["@src/calc.py", "@docs/calc-notes.md"])
        panel = "".join(text for _style, text in tui._menu_text())
        assert "@calc.py" in panel and "src/" in panel  # file name, with its directory beside it
        h.send(ENTER)
        await h.until(lambda: tui.input.text == "请看一下@src/calc.py ")

        # in the middle of existing text: what follows the cursor is kept
        buffer.text = "对比和这个文件"
        buffer.cursor_position = 2
        h.send("@read")
        await h.until(lambda: labels(tui) == ["@README.md"])
        h.send(TAB)
        await h.until(lambda: tui.input.text == "对比@README.md 和这个文件")
        assert buffer.cursor_position == len("对比@README.md ")

        # an e-mail address or a handle is not a file mention
        for text in ("mail me at bob@cal", "version-1@cal", "a.b@cal"):
            buffer.text = text
            buffer.cursor_position = len(text)
            await asyncio.sleep(0.02)
            assert tui._menu() == [], text

        # hidden files appear only when asked for, and junk files never
        buffer.text = "@"
        buffer.cursor_position = 1
        await h.until(lambda: bool(tui._menu()))
        assert labels(tui) == ["@README.md", "@src/calc.py", "@docs/calc-notes.md"]  # shallow, short first
        h.send(".")
        await h.until(lambda: labels(tui) == ["@.env"])

        # letters in order are enough
        buffer.text = "@clcpy"
        buffer.cursor_position = 6
        await h.until(lambda: labels(tui) == ["@src/calc.py"])
        buffer.reset()

    run_tui(tmp_path, [], scenario)


def test_menu_styles_do_not_collide_with_prompt_toolkit_defaults():
    from nori.tui import STYLE

    # prompt_toolkit gives the class "menu" a grey background, which used to leak in
    names = {name for name, _style in STYLE.style_rules}
    assert not any(name == "menu" or name.startswith("menu.") for name in names)
    assert {"palette", "palette.selected"} <= names
