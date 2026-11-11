"""Tests for skills, the plan tool and MCP servers."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest
from agents import set_tracing_disabled
from agents.mcp import MCPServerManager
from agents.testing import ScriptedModel, assistant_message, function_call
from rich.console import Console

from code_agent.agent import build_instructions
from code_agent.cli import connect_mcp, select_mcp_specs
from code_agent.commands import match_commands, run_command, skill_message
from code_agent.mcp_config import MCPConfigError, build_server, load_mcp_specs
from code_agent.session import PlainUI, Session
from code_agent.skills import discover_skills, parse_front_matter

set_tracing_disabled(True)
ECHO_SERVER = Path(__file__).with_name("mcp_echo_server.py")


def make_console():
    out = io.StringIO()
    return Console(file=out, force_terminal=False, width=200), out


def make_session(tmp_path, steps, **kwargs):
    console, out = make_console()
    session = Session(tmp_path, model=ScriptedModel(steps), ui=PlainUI(console), **kwargs)
    return session, out


def tool_outputs(session):
    return [
        item["output"]
        for item in session.history
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


def write_skill(root: Path, name: str, text: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(text)
    return directory


# --- skills ---------------------------------------------------------------


def test_front_matter_parsing():
    meta, body = parse_front_matter(
        '---\nname: "pdf"\ndescription: >\n  Work with\n  PDF files: read, merge\n'
        "# comment\nlicense: MIT\n---\n# Body\ntext\n"
    )
    assert meta == {"name": "pdf", "description": "Work with PDF files: read, merge", "license": "MIT"}
    assert body == "# Body\ntext"
    assert parse_front_matter("no front matter") == ({}, "no front matter")
    assert parse_front_matter("---\nname: x\nnever closed") == ({}, "---\nname: x\nnever closed")


def test_discovery_priority_and_fallbacks(tmp_path):
    workspace, home = tmp_path / "ws", tmp_path / "home"
    write_skill(workspace / ".agents/skills", "review", "---\ndescription: project review\n---\nP")
    write_skill(home / ".claude/skills", "review", "---\ndescription: user review\n---\nU")
    write_skill(home / ".code_agent/skills", "Deploy It", "# Deploy\nShip the app.\n")
    (workspace / ".agents/skills/not-a-skill").mkdir()

    skills = discover_skills(workspace, home)

    assert set(skills) == {"review", "deploy-it"}
    assert skills["review"].description == "project review"
    assert skills["deploy-it"].description == "Ship the app."
    prompt = build_instructions(workspace, skills)
    assert "- review: project review" in prompt and "load_skill" in prompt
    assert "# Skills" not in build_instructions(workspace, {})


def test_agent_loads_skill_and_reads_its_files_but_cannot_write_them(tmp_path):
    workspace, home = tmp_path / "ws", tmp_path / "home"
    workspace.mkdir()
    directory = write_skill(
        home / ".agents/skills", "review", "---\ndescription: d\n---\nSee checklist.md"
    )
    (directory / "checklist.md").write_text("check tests\n")
    skills = discover_skills(workspace, home)
    checklist = str(directory / "checklist.md")

    session, _ = make_session(
        workspace,
        [
            [function_call("load_skill", {"name": "review"}, call_id="c1")],
            [function_call("read_file", {"path": checklist}, call_id="c2")],
            [function_call("write_file", {"path": checklist, "content": "x"}, call_id="c3")],
            [function_call("load_skill", {"name": "nope"}, call_id="c4")],
            [assistant_message("ok")],
        ],
        skills=skills,
        auto_approve=True,
    )
    asyncio.run(session.run_turn("review this"))

    loaded, read, write, unknown = tool_outputs(session)
    assert "See checklist.md" in loaded and str(directory) in loaded
    assert "check tests" in read
    assert "outside the workspace" in write
    assert (directory / "checklist.md").read_text() == "check tests\n"
    assert "unknown skill" in unknown and "review" in unknown


def test_skill_message_includes_instructions_and_request(tmp_path):
    write_skill(tmp_path / ".agents/skills", "review", "---\ndescription: d\n---\nDo the review.")
    skill = discover_skills(tmp_path, tmp_path / "home")["review"]
    message = skill_message(skill, "focus on auth.py")
    assert "Do the review." in message and message.endswith("Request: focus on auth.py")


# --- plan -----------------------------------------------------------------


def test_update_plan_is_stored_and_rendered(tmp_path):
    steps = [
        {"step": "read code", "status": "completed"},
        {"step": "fix bug", "status": "in_progress"},
        {"step": "run tests", "status": "pending"},
    ]
    session, out = make_session(
        tmp_path,
        [
            [function_call("update_plan", {"steps": steps}, call_id="c1")],
            [function_call("update_plan", {"steps": [{"step": "x", "status": "bad"}]}, call_id="c2")],
            [assistant_message("ok")],
        ],
    )
    asyncio.run(session.run_turn("go"))

    assert session.context.plan == steps
    good, bad = tool_outputs(session)
    assert good == "[x] read code\n[~] fix bug\n[ ] run tests"
    assert "invalid status" in bad
    printed = out.getvalue()
    assert "● Update Todos" in printed
    assert "☒ read code" in printed and "☐ fix bug" in printed and "☐ run tests" in printed


# --- MCP ------------------------------------------------------------------


def write_mcp_config(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}))


def test_config_loading_merging_and_expansion(tmp_path, monkeypatch):
    workspace, home = tmp_path / "ws", tmp_path / "home"
    monkeypatch.setenv("MY_TOKEN", "secret")
    write_mcp_config(
        home / ".code_agent/mcp.json",
        {
            "shared": {"command": "user-cmd"},
            "remote": {"type": "http", "url": "https://x/mcp", "headers": {"A": "Bearer ${MY_TOKEN}"}},
            "legacy": {"type": "sse", "url": "https://x/sse"},
            "off": {"command": "x", "disabled": True},
        },
    )
    write_mcp_config(
        workspace / ".mcp.json",
        {"shared": {"command": "proj-cmd", "args": ["--port", "${PORT:-8080}"]}},
    )

    specs = {spec.name: spec for spec in load_mcp_specs(workspace, home)}

    assert set(specs) == {"shared", "remote", "legacy"}
    assert specs["shared"].source == "project"
    assert specs["shared"].describe() == "proj-cmd --port 8080"
    assert specs["remote"].config["headers"] == {"A": "Bearer secret"}
    assert [specs[n].transport for n in ("shared", "remote", "legacy")] == ["stdio", "http", "sse"]
    for spec in specs.values():
        assert build_server(spec, workspace).name == spec.name


@pytest.mark.parametrize("content", ["{not json", "[]", '{"mcpServers": {"a": {"args": []}}}'])
def test_invalid_config_is_rejected(tmp_path, content):
    (tmp_path / ".mcp.json").write_text(content)
    with pytest.raises(MCPConfigError):
        load_mcp_specs(tmp_path, tmp_path / "home")


def test_project_servers_need_trust(tmp_path, monkeypatch):
    workspace, home = tmp_path / "ws", tmp_path / "home"
    write_mcp_config(home / ".code_agent/mcp.json", {"mine": {"command": "a"}})
    write_mcp_config(workspace / ".mcp.json", {"theirs": {"command": "b"}})
    specs = load_mcp_specs(workspace, home)
    console, out = make_console()

    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert [s.name for s in select_mcp_specs(specs, False, console)] == ["mine"]
    assert "theirs: b" in out.getvalue()

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert [s.name for s in select_mcp_specs(specs, False, console)] == ["mine", "theirs"]

    def no_prompt(_prompt):
        raise AssertionError("must not prompt")

    monkeypatch.setattr("builtins.input", no_prompt)
    assert len(select_mcp_specs(specs, True, console)) == 2
    assert len(select_mcp_specs(specs[:1], False, console)) == 1


def test_mcp_tools_run_through_a_real_stdio_server_with_approval(tmp_path):
    write_mcp_config(
        tmp_path / ".mcp.json",
        {
            "echo": {"command": sys.executable, "args": [str(ECHO_SERVER)], "env": {"PREFIX": ">"}},
            "broken": {"command": sys.executable, "args": ["-c", "raise SystemExit(1)"]},
        },
    )
    specs = load_mcp_specs(tmp_path, tmp_path / "home")
    console, out = make_console()
    asked = []

    def approver(item):
        asked.append(item.tool_name)
        return "always" if len(asked) == 2 else "no"

    # Mirrors main(): one asyncio.Runner, a separate run() per step.
    with asyncio.Runner() as runner:
        manager = MCPServerManager(
            [build_server(spec, tmp_path) for spec in specs], connect_in_parallel=True
        )
        try:
            servers = runner.run(connect_mcp(manager, console))
            assert [server.name for server in servers] == ["echo"]
            assert "broken 连接失败" in out.getvalue()

            session = Session(
                tmp_path,
                model=ScriptedModel(
                    [
                        [function_call("shout", {"text": "a"}, call_id="c1")],
                        [function_call("shout", {"text": "b"}, call_id="c2")],
                        [function_call("shout", {"text": "c"}, call_id="c3")],
                        [assistant_message("done")],
                        [function_call("shout", {"text": "d"}, call_id="c4")],
                        [assistant_message("done again")],
                    ]
                ),
                ui=PlainUI(console, approver),
                mcp_servers=servers,
            )
            assert runner.run(session.run_turn("go")) == "done"
            assert runner.run(session.run_turn("again")) == "done again"
        finally:
            runner.run(manager.cleanup_all())

    assert asked == ["shout", "shout"]  # rejected, always-allowed, then no more prompts
    outputs = [str(o) for o in tool_outputs(session)]
    assert "rejected" in outputs[0]
    assert all(expected in output for expected, output in zip([">B", ">C", ">D"], outputs[1:]))
    tool_names = [tool.name for tool in session.agent.model.first_call.tools]
    assert "shout" in tool_names and "read_file" in tool_names


# --- slash commands ---------------------------------------------------------


def test_commands_and_completion(tmp_path):
    write_skill(tmp_path / ".agents/skills", "review", "---\ndescription: d\n---\nDo the review.")
    skills = discover_skills(tmp_path, tmp_path / "home")
    session, _ = make_session(tmp_path, [], skills=skills)
    lines: list[str] = []

    def run(line):
        return asyncio.run(run_command(session, line, lines.append))

    assert [c.name for c in match_commands(session, "/")][-1] == "review"
    assert [c.name for c in match_commands(session, "/m")] == ["model", "mcp"]
    assert match_commands(session, "/model x") == [] and match_commands(session, "hi") == []

    assert run("/exit").exit
    session.history = ["x"]
    run("/clear")
    assert session.history == []
    run("/yes")
    assert session.context.auto_approve is True
    run("/model other")
    assert session.model_name == "other" and "other" in lines[-1]
    assert "Do the review." in run("/review auth.py").prompt
    run("/nope")
    assert "未知命令" in lines[-1]
    run("/mcp")
    assert "没有已连接" in lines[-1]
    run("/skills")
    assert any("/review" in line for line in lines)
