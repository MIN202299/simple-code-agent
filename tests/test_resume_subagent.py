"""Tests for saved sessions (resume) and sub-agents."""

from __future__ import annotations

import asyncio
import io

from agents import set_tracing_disabled
from agents.testing import ScriptedModel, assistant_message, function_call
from rich.console import Console

from nori.cli import main
from nori.commands import run_command
from nori.render import render_history
from nori.session import PlainUI, Session
from nori.store import SessionRecord, SessionStore

set_tracing_disabled(True)


def make_session(tmp_path, steps, approver=None, **kwargs):
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    session = Session(
        workspace, model=ScriptedModel(steps), ui=PlainUI(console, approver), **kwargs
    )
    return session, out


def tool_outputs(session):
    return [
        item["output"]
        for item in session.history
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


# --- store ------------------------------------------------------------------


def test_store_round_trip_listing_and_prefix_lookup(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    here, there = tmp_path / "a", tmp_path / "b"
    first = SessionRecord(id="20260101-000000-aaaa", workspace=str(here), title="first",
                          history=[{"role": "user", "content": "one"}])
    second = SessionRecord(id="20260102-000000-bbbb", workspace=str(here), title="second",
                           history=[{"role": "user", "content": "two"}], input_tokens=7)
    other = SessionRecord(id="20260103-000000-cccc", workspace=str(there),
                          history=[{"role": "user", "content": "x"}])
    empty = SessionRecord(id="20260104-000000-dddd", workspace=str(here))
    for record in (first, second, other, empty):
        store.save(record)
    (tmp_path / "sessions" / "broken.json").write_text("{nope")
    (tmp_path / "sessions" / "wrong.json").write_text('{"history": "not a list"}')

    assert {r.id for r in store.list(here)} == {first.id, second.id}  # no empty, no other dirs
    assert store.load(second.id).input_tokens == 7
    assert store.load("20260102").title == "second"  # unique prefix
    assert store.load("2026") is None  # ambiguous
    assert store.load("../etc/passwd") is None and store.load("missing") is None
    assert store.latest(there).id == other.id and store.latest(tmp_path / "none") is None
    assert second.turns == 1
    assert SessionStore(tmp_path / "does-not-exist").list(here) == []


# --- resume -----------------------------------------------------------------


def test_conversation_is_saved_and_can_be_resumed(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session, _ = make_session(
        tmp_path,
        [[function_call("list_dir", {}, call_id="c1")], [assistant_message("empty")]],
        store=store,
    )
    asyncio.run(session.run_turn("what   is\nhere?"))

    saved = store.latest(session.context.workspace)
    assert saved.id == session.session_id and saved.title == "what is here?"
    assert len(saved.history) == 4 and saved.turns == 1

    later, out = make_session(tmp_path, [[assistant_message("as I said, empty")]], store=store)
    later.resume(saved)
    later.ui.show_history(later.history)
    assert "已恢复会话（1 轮对话）" in out.getvalue()
    asyncio.run(later.run_turn("remind me"))

    sent = later.agent.model.last_call.input
    assert [i.get("role") or i.get("type") for i in sent] == [
        "user", "function_call", "function_call_output", "assistant", "user",
    ]  # fmt: skip
    # resuming continues the same session file rather than starting a new one
    assert later.session_id == saved.id and len(store.list(later.context.workspace)) == 1
    assert store.load(saved.id).turns == 2 and store.load(saved.id).created == saved.created


def test_interrupted_turn_is_saved_and_no_store_means_no_files(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session, _ = make_session(
        tmp_path,
        [[function_call("run_shell", {"command": "sleep 30"}, call_id="c1")]],
        store=store,
        auto_approve=True,
    )

    async def scenario():
        turn = asyncio.ensure_future(session.run_turn("go"))
        while not session.agent.model.calls:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)

    asyncio.run(scenario())
    assert [i["content"] for i in store.load(session.session_id).history] == [
        "go",
        "[Request interrupted by user]",
    ]

    unsaved, _ = make_session(tmp_path, [[assistant_message("hi")]])
    asyncio.run(unsaved.run_turn("hello"))
    assert len(list((tmp_path / "sessions").glob("*.json"))) == 1


def test_resume_command_and_clear_start_a_new_session(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    old, _ = make_session(tmp_path, [[assistant_message("old answer")]], store=store)
    asyncio.run(old.run_turn("old question"))

    session, out = make_session(tmp_path, [[assistant_message("new answer")]], store=store)
    lines: list = []

    def run(line):
        return asyncio.run(run_command(session, line, lambda r: lines.append(str(r))))

    run("/resume")
    assert any("old question" in line for line in lines) and "/resume <序号>" in lines[-1]
    run("/resume 9")
    assert "找不到会话" in lines[-1]
    run("/resume 1")
    assert session.session_id == old.session_id and len(session.history) == 2
    assert "已恢复会话" in out.getvalue()

    run("/clear")
    assert session.history == [] and session.session_id != old.session_id
    asyncio.run(session.run_turn("fresh"))
    assert len(store.list(session.context.workspace)) == 2  # the old one is still there

    run("/resume " + old.session_id)  # by id
    assert session.session_id == old.session_id


def test_render_history():
    items = [
        {"role": "user", "content": "[The earlier part of this conversation was compacted ...]"},
        {"role": "user", "content": "fix it"},
        {"type": "reasoning", "id": "r"},
        {"type": "function_call", "call_id": "a", "name": "run_shell", "arguments": '{"command":"ls"}'},
        {"type": "function_call_output", "call_id": "a", "output": "a.py\n[exit code: 0]"},
        {"role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
        {"role": "user", "content": "[Request interrupted by user]"},
    ]
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    for entry in render_history(items):
        console.print(entry)
    text = out.getvalue()
    assert "更早的对话已压缩" in text and "❯ fix it" in text and "● Bash(ls)" in text
    assert "a.py" in text and "● done" in text and "已中断" in text
    assert "省略了更早的 3 条" in str(render_history(items, limit=2)[0])


def test_cli_session_flags(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = SessionStore()
    assert store.directory == tmp_path / ".nori" / "sessions"

    assert main(["-C", str(workspace), "--list-sessions"]) == 0
    assert "还没有保存的会话" in capsys.readouterr().out
    assert main(["-C", str(workspace), "--resume", "nope", "hi"]) == 2
    assert "找不到会话" in capsys.readouterr().out

    store.save(SessionRecord(id="20260101-000000-aaaa", workspace=str(workspace.resolve()),
                             title="saved one", history=[{"role": "user", "content": "x"}]))
    assert main(["-C", str(workspace), "--list-sessions"]) == 0
    listing = capsys.readouterr().out
    assert "20260101-000000-aaaa" in listing and "saved one" in listing


# --- sub-agents -------------------------------------------------------------


def test_sub_agent_runs_in_its_own_context_and_reports_back(tmp_path):
    session, out = make_session(
        tmp_path,
        [
            [function_call("spawn_agent", {"description": "find todos", "prompt": "Find TODOs.",
                                           "agent_type": "explore"}, call_id="c1")],
            [function_call("grep", {"pattern": "TODO"}, call_id="s1")],  # the sub-agent
            [assistant_message("REPORT: one TODO in a.py:1")],  # the sub-agent's report
            [assistant_message("There is one TODO.")],
        ],
    )
    (session.context.workspace / "a.py").write_text("# TODO: fix\n")

    assert asyncio.run(session.run_turn("any todos? (secret context)")) == "There is one TODO."

    model = session.agent.model
    main_first, sub_first, sub_second, main_last = model.calls
    # the sub-agent starts from the prompt alone and has a restricted toolset
    assert sub_first.input == [{"content": "Find TODOs.", "role": "user"}]
    assert "sub-agent" in sub_first.system_instructions
    assert sorted(t.name for t in sub_first.tools) == ["glob_files", "grep", "list_dir", "read_file"]
    assert "spawn_agent" in [t.name for t in main_first.tools]
    assert "secret context" not in str(sub_second.input)
    # the main agent gets only the report, not the sub-agent's intermediate steps
    assert tool_outputs(session) == ["REPORT: one TODO in a.py:1"]
    assert "a.py:1: # TODO" not in str(main_last.input)
    printed = out.getvalue()
    assert "● Agent(find todos)" in printed and "│ Grep(TODO) · 第 1 次工具调用" in printed


def test_general_sub_agent_goes_through_approval(tmp_path):
    asked = []

    def approver(item):
        asked.append(item.tool_name)
        return "yes" if len(asked) == 1 else "no"

    session, _ = make_session(
        tmp_path,
        [
            [function_call("spawn_agent", {"description": "write files", "prompt": "Write."},
                           call_id="c1")],
            [function_call("write_file", {"path": "ok.txt", "content": "1"}, call_id="s1")],
            [function_call("write_file", {"path": "no.txt", "content": "2"}, call_id="s2")],
            [assistant_message("wrote ok.txt; no.txt was rejected")],
            [assistant_message("done")],
        ],
        approver=approver,
    )
    asyncio.run(session.run_turn("go"))

    assert asked == ["write_file", "write_file"]  # spawning itself needs no approval
    workspace = session.context.workspace
    assert (workspace / "ok.txt").exists() and not (workspace / "no.txt").exists()
    general_tools = [t.name for t in session.agent.model.calls[1].tools]
    assert "write_file" in general_tools and "run_shell" in general_tools
    assert "spawn_agent" not in general_tools and "update_plan" not in general_tools


def test_sub_agent_errors_are_reported_to_the_main_agent(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [
                function_call("spawn_agent", {"description": "x", "prompt": "p", "agent_type": "boss"},
                              call_id="c1"),
                function_call("spawn_agent", {"description": "x", "prompt": "  "}, call_id="c2"),
            ],
            [assistant_message("ok")],
        ],
    )
    asyncio.run(session.run_turn("go"))
    bad_type, empty_prompt = tool_outputs(session)
    assert "unknown agent_type" in bad_type and "must not be empty" in empty_prompt


def test_interrupting_stops_a_running_sub_agent(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [function_call("spawn_agent", {"description": "slow", "prompt": "Sleep."}, call_id="c1")],
            [function_call("run_shell", {"command": "sleep 30"}, call_id="s1")],
        ],
        auto_approve=True,
    )

    async def scenario():
        turn = asyncio.ensure_future(session.run_turn("go"))
        while len(session.agent.model.calls) < 2:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        started = asyncio.get_running_loop().time()
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(scenario()) < 3
    assert [i["content"] for i in session.history] == ["go", "[Request interrupted by user]"]
