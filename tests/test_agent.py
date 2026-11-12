"""End-to-end tests driving the real SDK run loop with a scripted model."""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from agents import MaxTurnsExceeded, set_tracing_disabled
from agents.testing import ScriptedModel, assistant_message, function_call
from rich.console import Console

from code_agent.render import call_title, render_output, split_blocks
from code_agent.session import PlainUI, Session, complete_history

set_tracing_disabled(True)


def make_session(tmp_path, steps, approver=None, auto_approve=False):
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    session = Session(
        tmp_path,
        model=ScriptedModel(steps),
        auto_approve=auto_approve,
        ui=PlainUI(console, approver),
    )
    return session, out


def tool_outputs(session):
    return [
        item["output"]
        for item in session.history
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


def test_read_only_tools_run_without_approval(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nprint(x)\n")

    def approver(item):
        raise AssertionError("read-only tools must not ask for approval")

    session, out = make_session(
        tmp_path,
        [
            [
                function_call("read_file", {"path": "a.py"}, call_id="c1"),
                function_call("grep", {"pattern": "print"}, call_id="c2"),
                function_call("glob_files", {"pattern": "**/*.py"}, call_id="c3"),
                function_call("list_dir", {}, call_id="c4"),
            ],
            [assistant_message("done reading")],
        ],
        approver=approver,
    )
    final = asyncio.run(session.run_turn("read it"))

    assert final == "done reading"
    read, grep, glob, listing = tool_outputs(session)
    assert "1\tx = 1" in read and "2\tprint(x)" in read
    assert grep == "a.py:2: print(x)"
    assert glob == "a.py"
    assert listing == "a.py"
    printed = out.getvalue()
    assert "● Read(a.py)" in printed and "Read 2 lines" in printed
    assert "done reading" in printed


def test_approved_write_edit_and_shell(tmp_path):
    asked = []

    def approver(item):
        asked.append(item.tool_name)
        return "yes"

    session, _ = make_session(
        tmp_path,
        [
            [function_call("write_file", {"path": "pkg/m.py", "content": "a = 1\n"}, call_id="c1")],
            [
                function_call(
                    "edit_file",
                    {"path": "pkg/m.py", "old_string": "a = 1", "new_string": "a = 2"},
                    call_id="c2",
                )
            ],
            [function_call("run_shell", {"command": "cat pkg/m.py"}, call_id="c3")],
            [assistant_message("ok")],
        ],
        approver=approver,
    )
    assert asyncio.run(session.run_turn("go")) == "ok"

    assert asked == ["write_file", "edit_file", "run_shell"]
    assert (tmp_path / "pkg/m.py").read_text() == "a = 2\n"
    assert tool_outputs(session)[-1] == "a = 2\n[exit code: 0]"


def test_rejected_tool_is_not_executed(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [function_call("write_file", {"path": "x.txt", "content": "hi"}, call_id="c1")],
            [assistant_message("understood")],
        ],
        approver=lambda item: "no",
    )
    assert asyncio.run(session.run_turn("go")) == "understood"

    assert not (tmp_path / "x.txt").exists()
    assert "rejected" in tool_outputs(session)[0]


def test_always_allow_stops_asking_for_that_tool_only(tmp_path):
    asked = []

    def approver(item):
        asked.append(item.tool_name)
        return "always"

    session, _ = make_session(
        tmp_path,
        [
            [function_call("write_file", {"path": "1.txt", "content": "1"}, call_id="c1")],
            [function_call("write_file", {"path": "2.txt", "content": "2"}, call_id="c2")],
            [function_call("run_shell", {"command": "echo hi"}, call_id="c3")],
            [assistant_message("ok")],
        ],
        approver=approver,
    )
    asyncio.run(session.run_turn("go"))

    assert asked == ["write_file", "run_shell"]
    assert (tmp_path / "2.txt").read_text() == "2"


def test_auto_approve_and_history_carries_over(tmp_path):
    def approver(item):
        raise AssertionError("auto-approve must not ask")

    model_steps = [
        [function_call("run_shell", {"command": "echo one"}, call_id="c1")],
        [assistant_message("first")],
        [assistant_message("second")],
    ]
    session, _ = make_session(tmp_path, model_steps, approver=approver, auto_approve=True)
    asyncio.run(session.run_turn("turn 1"))
    asyncio.run(session.run_turn("turn 2"))

    last_input = json.dumps(session.agent.model.last_call.input, default=str)
    assert "turn 1" in last_input and "turn 2" in last_input and "first" in last_input

    session.clear()
    assert session.history == []


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (("read_file", {"path": "../outside.txt"}), "outside the workspace"),
        (("read_file", {"path": "/etc/passwd"}), "outside the workspace"),
        (("read_file", {"path": "missing.txt"}), "is not a file"),
        (("edit_file", {"path": "a.txt", "old_string": "zzz", "new_string": "y"}), "not found"),
        (("edit_file", {"path": "a.txt", "old_string": "x", "new_string": "y"}), "occurs 2 times"),
        (("grep", {"pattern": "("}), "invalid regular expression"),
        (("run_shell", {"command": "sleep 5", "timeout": 1}), "timed out"),
        (("run_shell", {"command": "exit 3"}), "[exit code: 3]"),
    ],
)
def test_tool_errors_are_reported_to_the_model(tmp_path, call, expected):
    (tmp_path / "a.txt").write_text("x x\n")
    (tmp_path.parent / "outside.txt").write_text("secret")
    name, args = call
    session, _ = make_session(
        tmp_path,
        [[function_call(name, args, call_id="c1")], [assistant_message("ok")]],
        auto_approve=True,
    )
    asyncio.run(session.run_turn("go"))

    assert expected in tool_outputs(session)[0]
    assert (tmp_path / "a.txt").read_text() == "x x\n"


def test_edit_replace_all_and_read_paging(tmp_path):
    (tmp_path / "a.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\nx x\n")
    session, _ = make_session(
        tmp_path,
        [
            [
                function_call(
                    "edit_file",
                    {"path": "a.txt", "old_string": "x", "new_string": "y", "replace_all": True},
                    call_id="c1",
                )
            ],
            [function_call("read_file", {"path": "a.txt", "offset": 3, "limit": 2}, call_id="c2")],
            [assistant_message("ok")],
        ],
        auto_approve=True,
    )
    asyncio.run(session.run_turn("go"))

    assert (tmp_path / "a.txt").read_text().endswith("y y\n")
    page = tool_outputs(session)[1]
    assert "3\tline3" in page and "4\tline4" in page and "line5" not in page
    assert "offset=5" in page


def plain(renderable) -> str:
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=200).print(renderable)
    return out.getvalue().rstrip("\n")


def test_call_titles():
    assert call_title("run_shell", {"command": "pytest -q\nls"}) == "Bash(pytest -q)"
    assert call_title("edit_file", {"path": "a.py", "old_string": "x"}) == "Update(a.py)"
    assert call_title("list_dir", {}) == "List(.)"
    assert call_title("update_plan", {"steps": []}) == "Update Todos"
    assert call_title("shout", {"text": "hi", "n": 2}) == "shout(text=hi, n=2)"


def test_output_rendering():
    long = "\n".join(str(i) for i in range(10))
    assert plain(render_output("run_shell", {}, long)).splitlines()[0] == "  ⎿  0"
    assert "… +4 行" in plain(render_output("run_shell", {}, long))
    assert plain(render_output("shout", {}, [{"type": "text", "text": "HI"}])) == "  ⎿  HI"
    assert plain(render_output("update_plan", {}, "[x] a\n[ ] b")) == "  ⎿  ☒ a\n     ☐ b"
    assert plain(render_output("grep", {}, "")) == "  ⎿  (no output)"


def test_edit_and_write_calls_show_a_preview(tmp_path):
    (tmp_path / "a.py").write_text("keep\nold line\n")
    session, out = make_session(
        tmp_path,
        [
            [
                function_call(
                    "edit_file",
                    {"path": "a.py", "old_string": "old line", "new_string": "new line"},
                    call_id="c1",
                )
            ],
            [function_call("write_file", {"path": "b.py", "content": "x = 1\n"}, call_id="c2")],
            [assistant_message("ok")],
        ],
        auto_approve=True,
    )
    asyncio.run(session.run_turn("go"))

    printed = out.getvalue()
    assert "● Update(a.py)" in printed and "-old line" in printed and "+new line" in printed
    assert "● Write(b.py)" in printed and "+x = 1" in printed


def test_split_blocks():
    assert split_blocks("para one\n\npara two\nstill tw") == (["para one"], "para two\nstill tw")
    # nothing is final until the line after the blank one is known
    assert split_blocks("para one\n\n") == ([], "para one\n\n")
    # blank lines inside a code fence do not split
    fenced = "```py\na = 1\n\nb = 2\n```\n\nafter\n"
    assert split_blocks(fenced) == (["```py\na = 1\n\nb = 2\n```"], "after\n")
    # an indented continuation stays with its list item
    assert split_blocks("1. item\n\n   more\n\nnext\n") == (["1. item\n\n   more"], "next\n")


def test_complete_history_drops_unanswered_calls():
    items = [
        {"role": "user", "content": "hi"},
        {"type": "reasoning", "id": "r1"},
        {"type": "function_call", "call_id": "a", "name": "t", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "a", "output": "ok"},
        {"type": "reasoning", "id": "r2"},
        {"type": "function_call", "call_id": "b", "name": "t", "arguments": "{}"},
    ]
    assert complete_history(items) == items[:4]


def test_interrupted_turn_keeps_finished_work(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [function_call("write_file", {"path": "a.txt", "content": "1"}, call_id="c1")],
            [function_call("run_shell", {"command": "sleep 30"}, call_id="c2")],
            [assistant_message("after")],
        ],
        auto_approve=True,
    )

    async def scenario():
        turn = asyncio.ensure_future(session.run_turn("go"))
        while not (tmp_path / "a.txt").exists():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)  # let the shell command start
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        return await session.run_turn("continue")

    assert asyncio.run(scenario()) == "after"
    sent = session.agent.model.last_call.input
    types = [item.get("type") or item.get("role") for item in sent]
    assert types.count("function_call") == types.count("function_call_output") == 1
    contents = [item.get("content") for item in sent if item.get("role") == "user"]
    assert contents == ["go", "[Request interrupted by user]", "continue"]


def test_running_out_of_turns_keeps_the_work_and_can_continue(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [function_call("write_file", {"path": "1.txt", "content": "1"}, call_id="c1")],
            [function_call("write_file", {"path": "2.txt", "content": "2"}, call_id="c2")],
            [function_call("write_file", {"path": "3.txt", "content": "3"}, call_id="c3")],
            [assistant_message("all done")],
        ],
        auto_approve=True,
    )
    session.max_turns = 2
    with pytest.raises(MaxTurnsExceeded):
        asyncio.run(session.run_turn("write three files"))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["1.txt", "2.txt"]
    assert len(tool_outputs(session)) == 2  # the finished steps are still in context

    assert asyncio.run(session.run_turn("继续")) == "all done"
    assert (tmp_path / "3.txt").exists()
    sent = session.agent.model.last_call.input
    assert [i["content"] for i in sent if i.get("role") == "user"] == ["write three files", "继续"]
