"""End-to-end tests driving the real SDK run loop with a scripted model."""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from agents import set_tracing_disabled
from agents.testing import ScriptedModel, assistant_message, function_call
from rich.console import Console

from code_agent.cli import Session, format_call, format_output

set_tracing_disabled(True)


def make_session(tmp_path, steps, approver=None, auto_approve=False):
    out = io.StringIO()
    session = Session(
        tmp_path,
        model=ScriptedModel(steps),
        auto_approve=auto_approve,
        console=Console(file=out, force_terminal=False, width=200),
        approver=approver,
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
    assert "read_file(path=a.py)" in printed
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


def test_formatting_helpers():
    assert format_call("run_shell", '{"command": "ls\\n-la"}') == "run_shell(command=ls\\n-la)"
    assert format_call("t", "not json") == "t(arguments=not json)"
    out = format_output("\n".join(str(i) for i in range(10)))
    assert out.startswith("  ⎿ 0") and "+4 行" in out
    assert format_output({"type": "text", "text": "HI"}) == "  ⎿ HI"
    assert format_output([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "  ⎿ a\n    b"
