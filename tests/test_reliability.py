"""Tests for retries and for sub-agent context protection."""

from __future__ import annotations

import asyncio
import io

import pytest
from agents import ModelRetryAdvice, ModelRetryNormalizedError, set_tracing_disabled
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from openai import OpenAIError
from rich.console import Console

from nori.compact import SUMMARY_PREFIX
from nori.session import PlainUI, Session

set_tracing_disabled(True)


def failure(status: int | None = None, network: bool = False, started: bool = False) -> ModelStep:
    """A scripted model error, described the way a provider adapter would."""
    advice = ModelRetryAdvice(
        normalized=ModelRetryNormalizedError(status_code=status, is_network_error=network),
        response_started=started,
    )
    return ModelStep.raise_error(OpenAIError(f"status {status}"), retry_advice=advice)


def make_session(tmp_path, steps, **kwargs):
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    session = Session(tmp_path, model=ScriptedModel(steps), ui=PlainUI(console), **kwargs)
    session.retry_delay = 0.0
    session.agent = session._build_agent()
    return session, out


def test_transient_errors_are_retried(tmp_path):
    session, out = make_session(
        tmp_path,
        [failure(429), failure(network=True), failure(503), [assistant_message("finally")]],
    )
    assert asyncio.run(session.run_turn("go")) == "finally"
    printed = out.getvalue()
    assert "请求失败（HTTP 429），正在重试（1/4）" in printed
    assert "正在重试（3/4）" in printed and len(session.agent.model.calls) == 4


def test_retries_are_limited_and_permanent_errors_fail_at_once(tmp_path):
    session, out = make_session(tmp_path, [failure(429)] * 3 + [[assistant_message("x")]], retries=2)
    with pytest.raises(OpenAIError):
        asyncio.run(session.run_turn("go"))
    assert len(session.agent.model.calls) == 3 and session.history == []

    for step in (failure(401), failure(400), failure(429, started=True)):
        session, out = make_session(tmp_path, [step, [assistant_message("never")]])
        with pytest.raises(OpenAIError):
            asyncio.run(session.run_turn("go"))
        assert len(session.agent.model.calls) == 1 and "重试" not in out.getvalue()

    session, _ = make_session(tmp_path, [failure(429), [assistant_message("never")]], retries=0)
    with pytest.raises(OpenAIError):
        asyncio.run(session.run_turn("go"))


def test_retry_in_the_middle_of_a_task_keeps_going(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [function_call("write_file", {"path": "a.txt", "content": "1"}, call_id="c1")],
            failure(502),
            [assistant_message("done")],
        ],
        auto_approve=True,
    )
    assert asyncio.run(session.run_turn("go")) == "done"
    assert (tmp_path / "a.txt").exists() and len(session.history) == 4


def test_sub_agent_compacts_its_own_context(tmp_path):
    big = "python3 -c \"print('x' * 20000)\""
    session, out = make_session(
        tmp_path,
        [
            [function_call("spawn_agent", {"description": "dig", "prompt": "Dig."}, call_id="c1")],
            [function_call("run_shell", {"command": big}, call_id="s1")],  # sub-agent
            [assistant_message("SUB SUMMARY")],  # summariser, for the sub-agent only
            [assistant_message("REPORT")],  # sub-agent
            [assistant_message("ok")],
        ],
        auto_approve=True,
        context_window=4_000,
    )
    assert asyncio.run(session.run_turn("go")) == "ok"

    calls = session.agent.model.calls
    sub_after_compaction = calls[3].input
    assert sub_after_compaction == [{"role": "user", "content": SUMMARY_PREFIX + "SUB SUMMARY"}]
    # the main conversation was not compacted: it still starts with the user's message
    assert calls[4].input[0] == {"content": "go", "role": "user"}
    assert "正在压缩它的上下文" in out.getvalue() and "已压缩上下文" not in out.getvalue()


# --- grep backends ----------------------------------------------------------


def grep_output(tmp_path, args, monkeypatch=None, use_ripgrep=True):
    import shutil

    from nori import tools

    if not use_ripgrep:
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    elif shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    session, _ = make_session(
        tmp_path, [[function_call("grep", args, call_id="c1")], [assistant_message("ok")]]
    )
    asyncio.run(session.run_turn("go"))
    return next(i["output"] for i in session.history if i.get("type") == "function_call_output")


@pytest.mark.parametrize("use_ripgrep", [True, False])
def test_grep_gives_the_same_results_with_and_without_ripgrep(tmp_path, monkeypatch, use_ripgrep):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("import os\nvalue = TODO_fix()\n")
    (tmp_path / "b.txt").write_text("todo: lower case\nnothing\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("TODO in a dependency\n")
    (tmp_path / ".hidden.py").write_text("TODO hidden\n")

    def run(**args):
        return grep_output(tmp_path, {"pattern": "TODO", **args}, monkeypatch, use_ripgrep)

    assert run() == ".hidden.py:1: TODO hidden\nsrc/a.py:2: value = TODO_fix()"
    assert run(ignore_case=True).splitlines() == [
        ".hidden.py:1: TODO hidden", "b.txt:1: todo: lower case", "src/a.py:2: value = TODO_fix()"]
    assert run(include="*.txt") == "No matches found."
    assert run(path="src/a.py") == "src/a.py:2: value = TODO_fix()"
    assert grep_output(tmp_path, {"pattern": "("}, monkeypatch, use_ripgrep).startswith(
        "Error: invalid regular expression")


def test_grep_falls_back_to_python_for_patterns_ripgrep_rejects(tmp_path):
    (tmp_path / "a.py").write_text("foobar\nbar\n")
    # look-behind is valid in Python's regex but not in ripgrep's default engine
    assert grep_output(tmp_path, {"pattern": "(?<=foo)bar"}) == "a.py:1: foobar"
