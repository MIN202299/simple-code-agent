"""Tests for context-window tracking and conversation compaction."""

from __future__ import annotations

import asyncio
import io

from agents import set_tracing_disabled
from agents.testing import ScriptedModel, assistant_message, function_call
from openai import OpenAIError
from rich.console import Console

from nori.commands import run_command
from nori.compact import SUMMARY_PREFIX, estimate_tokens, find_cut, transcript_text
from nori.session import PlainUI, Session

set_tracing_disabled(True)

USER = {"role": "user", "content": "hi"}
ASSISTANT = {"role": "assistant", "content": "hello"}


def call(call_id):
    return {"type": "function_call", "call_id": call_id, "name": "t", "arguments": "{}"}


def output(call_id, text="ok"):
    return {"type": "function_call_output", "call_id": call_id, "output": text}


def make_session(tmp_path, steps, **kwargs):
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    session = Session(tmp_path, model=ScriptedModel(steps), ui=PlainUI(console), **kwargs)
    return session, out


def big_output_command(chars: int) -> str:
    return f"python3 -c \"print('x' * {chars})\""


def test_estimate_tokens_counts_cjk_per_character():
    assert estimate_tokens([{"role": "user", "content": "a" * 400}]) in range(100, 120)
    assert estimate_tokens([{"role": "user", "content": "汉" * 400}]) in range(400, 420)


def test_find_cut_never_splits_a_tool_call_from_its_result():
    items = [USER, call("a"), output("a"), ASSISTANT, USER, call("b"), call("c"), output("b"), output("c")]
    assert find_cut(items, keep_tokens=0) == len(items)  # summarise everything
    assert find_cut(items, keep_tokens=10**6) == 0  # everything fits: nothing to cut
    for keep in range(0, 400, 5):
        cut = find_cut(items, keep)
        head = items[:cut]
        calls = {i["call_id"] for i in head if i.get("type") == "function_call"}
        outputs = {i["call_id"] for i in head if i.get("type") == "function_call_output"}
        assert calls == outputs, f"keep={keep} cut={cut}"

    # a split may not land between parallel calls and their results
    assert find_cut(items, keep_tokens=estimate_tokens(items[6:])) in (5, 9)
    # nor directly after a reasoning item
    reasoning = [USER, ASSISTANT, {"type": "reasoning", "id": "r"}, call("a"), output("a")]
    assert find_cut(reasoning, keep_tokens=estimate_tokens(reasoning[3:])) == 5
    # an unanswered call at the end cannot be summarised away
    assert find_cut([USER, call("a")], keep_tokens=0) == 0


def test_transcript_text():
    items = [
        USER,
        {"role": "assistant", "content": [{"type": "output_text", "text": "let me look"}]},
        {"type": "reasoning", "id": "r"},
        {"type": "function_call", "call_id": "a", "name": "grep", "arguments": '{"pattern":"x"}'},
        output("a", "y" * 5000),
    ]
    text = transcript_text(items, max_chars=100_000)
    assert "User: hi" in text and "Agent: let me look" in text
    assert '[Agent called tool grep: {"pattern":"x"}]' in text
    assert "…[truncated]" in text and len(text) < 3000
    assert transcript_text(items, max_chars=50).startswith("[The beginning of the transcript")


def test_manual_compact_replaces_history_with_a_summary(tmp_path):
    session, out = make_session(
        tmp_path,
        [
            [function_call("list_dir", {}, call_id="c1")],
            [assistant_message("it is empty")],
            [assistant_message("SUMMARY: listed the directory, it was empty.")],
            [assistant_message("continuing")],
        ],
    )
    lines: list = []
    asyncio.run(session.run_turn("list files"))
    assert len(session.history) == 4

    asyncio.run(run_command(session, "/compact 重点保留文件列表", lines.append))

    assert session.history == [
        {"role": "user", "content": SUMMARY_PREFIX + "SUMMARY: listed the directory, it was empty."}
    ]
    assert "已压缩上下文" in out.getvalue()
    summary_request = session.agent.model.last_call
    prompt = summary_request.input[0]["content"]
    assert "User: list files" in prompt and "[Tool result: (empty directory)]" in prompt
    assert prompt.endswith("重点保留文件列表")
    assert summary_request.tools == []  # the summariser cannot call tools

    asyncio.run(session.run_turn("go on"))
    sent = session.agent.model.last_call.input
    assert [item["role"] for item in sent] == ["user", "user"]
    assert sent[0]["content"].startswith(SUMMARY_PREFIX) and sent[1]["content"] == "go on"


def test_compact_with_nothing_to_do(tmp_path):
    session, _ = make_session(tmp_path, [])
    lines: list = []
    asyncio.run(run_command(session, "/compact", lines.append))
    assert "无需压缩" in lines[-1]


def test_auto_compaction_in_the_middle_of_a_turn(tmp_path):
    session, out = make_session(
        tmp_path,
        [
            [function_call("run_shell", {"command": big_output_command(20_000)}, call_id="c1")],
            [assistant_message("SUMMARY ONE")],  # summariser, triggered before the next call
            [function_call("list_dir", {}, call_id="c2")],
            [assistant_message("all done")],
        ],
        auto_approve=True,
        context_window=4_000,
    )
    assert asyncio.run(session.run_turn("do the big thing")) == "all done"

    model = session.agent.model
    summary = {"role": "user", "content": SUMMARY_PREFIX + "SUMMARY ONE"}
    # after compacting, the model sees the summary plus only what happened since
    final_input = model.last_call.input
    assert final_input[0] == summary
    assert [item.get("type") for item in final_input[1:]] == ["function_call", "function_call_output"]
    assert "x" * 100 not in str(final_input)
    # and the stored history matches what the model saw, plus its answer
    assert session.history[0] == summary and len(session.history) == 4
    assert session.context_tokens < 1_000
    assert "已压缩上下文" in out.getvalue()

    # the next turn builds on the compacted history without compacting again
    model.enqueue([assistant_message("next")])
    asyncio.run(session.run_turn("more"))
    assert model.last_call.input[0] == summary and len(model.last_call.input) == 5


def test_repeated_compaction_within_one_turn(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [function_call("run_shell", {"command": big_output_command(20_000)}, call_id="c1")],
            [assistant_message("SUMMARY ONE")],
            [function_call("run_shell", {"command": big_output_command(20_001)}, call_id="c2")],
            [assistant_message("SUMMARY TWO")],
            [assistant_message("finished")],
        ],
        auto_approve=True,
        context_window=4_000,
    )
    assert asyncio.run(session.run_turn("go")) == "finished"

    model = session.agent.model
    second_summary_request = model.calls[3].input[0]["content"]
    assert "SUMMARY ONE" in second_summary_request  # the old summary is folded into the new one
    assert model.last_call.input == [{"role": "user", "content": SUMMARY_PREFIX + "SUMMARY TWO"}]
    assert len(session.history) == 2 and "SUMMARY TWO" in session.history[0]["content"]


def test_auto_compaction_can_be_turned_off_and_survives_failure(tmp_path):
    steps = [
        [function_call("run_shell", {"command": big_output_command(20_000)}, call_id="c1")],
        [assistant_message("done")],
    ]
    session, out = make_session(
        tmp_path, steps, auto_approve=True, context_window=4_000, auto_compact=False
    )
    asyncio.run(session.run_turn("go"))
    assert len(session.history) == 4 and "已压缩" not in out.getvalue()
    assert session.context_share > 1  # over the window, and reported as such

    failing = [steps[0], OpenAIError("summary failed"), steps[1]]
    session, out = make_session(tmp_path, failing, auto_approve=True, context_window=4_000)
    assert asyncio.run(session.run_turn("go")) == "done"
    assert len(session.history) == 4 and "压缩上下文失败" in out.getvalue()


def test_interrupt_after_compaction_keeps_the_compacted_history(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [function_call("run_shell", {"command": big_output_command(20_000)}, call_id="c1")],
            [assistant_message("SUMMARY ONE")],
            [function_call("run_shell", {"command": "sleep 30"}, call_id="c2")],
        ],
        auto_approve=True,
        context_window=4_000,
    )

    async def scenario():
        turn = asyncio.ensure_future(session.run_turn("go"))
        while len(session.agent.model.calls) < 3:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)

    asyncio.run(scenario())
    assert [item.get("content", "")[:20] for item in session.history] == [
        SUMMARY_PREFIX[:20],
        "[Request interrupted",
    ]
