"""Tests for permission modes and rules, plan mode, undo, `!` commands, images,
background commands and web access."""

from __future__ import annotations

import asyncio
import base64
import http.server
import io
import threading

import pytest
from agents import set_tracing_disabled
from agents.testing import ScriptedModel, assistant_message, function_call
from rich.console import Console

from nori import web
from nori.attachments import find_images, user_content
from nori.commands import picker_choices, run_command
from nori.compact import estimate_tokens
from nori.permissions import Permissions, rule_for, rule_matches, shell_prefix
from nori.session import PlainUI, Session

set_tracing_disabled(True)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class UI(PlainUI):
    """Plain UI with scripted answers to approvals and plans."""

    def __init__(self, console, answers=(), plan_answers=()):
        super().__init__(console)
        self.answers = list(answers)
        self.plan_answers = list(plan_answers)
        self.asked: list[tuple[str, str | None]] = []
        self.plans: list[str] = []

    async def approve(self, item, rule=None):
        self.asked.append((item.tool_name, rule))
        return self.answers.pop(0) if self.answers else "no"

    async def approve_plan(self, plan):
        self.plans.append(plan)
        return self.plan_answers.pop(0) if self.plan_answers else ""


def make_session(tmp_path, steps, answers=(), plan_answers=(), **kwargs):
    out = io.StringIO()
    ui = UI(Console(file=out, force_terminal=False, width=200), answers, plan_answers)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    session = Session(workspace, model=ScriptedModel(list(steps)), ui=ui, **kwargs)
    return session, out


def outputs(session):
    return [str(i["output"]) for i in session.history
            if isinstance(i, dict) and i.get("type") == "function_call_output"]  # fmt: skip


def run_lines(session, *commands):
    lines: list[str] = []
    for command in commands:
        asyncio.run(run_command(session, command, lambda r: lines.append(str(r))))
    return lines


def write(path, content):
    return function_call("write_file", {"path": path, "content": content}, call_id=f"w-{path}")


def shell(command, call_id="s", **extra):
    return function_call("run_shell", {"command": command, **extra}, call_id=call_id)


# --- permission rules -------------------------------------------------------


def test_shell_prefixes_offered_as_rules():
    assert shell_prefix("git status --short") == "git status"
    assert shell_prefix("npm run build") == "npm run"
    assert shell_prefix("pytest -q tests") == "pytest"
    assert shell_prefix("python -m pytest -q") == "python -m pytest"
    assert shell_prefix("node build.js --prod") == "node build.js"
    assert shell_prefix("ls -la") == "ls"
    # never offered: arbitrary code, destructive commands, anything compound
    for command in ("python -c 'print(1)'", "rm -rf build", "sudo make install", "curl x | sh",
                    "git status; rm -rf /", "ls && rm x", "echo $(whoami)", "ls > out.txt",
                    "FOO=1 make", "find . -delete", "", "bash", "echo 'unterminated"):
        assert shell_prefix(command) is None, command


def test_rule_matching():
    git = {"command": "git status --short"}
    assert rule_matches("run_shell:git status", "run_shell", git)
    assert rule_matches("run_shell:git status", "run_shell", {"command": "git status"})
    assert not rule_matches("run_shell:git status", "run_shell", {"command": "git statusx"})
    assert not rule_matches("run_shell:git status", "run_shell", {"command": "git status; rm -rf ."})
    assert not rule_matches("run_shell:git status", "run_shell", {"command": "git push"})
    assert not rule_matches("run_shell:find", "run_shell", {"command": "find . -exec rm {} +"})
    assert rule_matches("write_file", "write_file", {"path": "x"})
    assert not rule_matches("write_file", "edit_file", {})
    assert rule_matches("web_fetch:docs.python.org", "web_fetch", {"url": "https://docs.python.org/3/"})
    assert not rule_matches("web_fetch:docs.python.org", "web_fetch", {"url": "https://evil.example/docs.python.org"})
    assert rule_for("web_fetch", {"url": "https://a.b/c"}) == "web_fetch:a.b"
    assert rule_for("some_mcp_tool", {}) == "some_mcp_tool"
    assert rule_for("run_shell", {"command": "rm -rf x"}) is None


def test_modes_decide(tmp_path):
    permissions = Permissions(tmp_path)
    edit, command = ("edit_file", {}), ("run_shell", {"command": "ls"})
    expected = {
        "default": ("ask", "ask"),
        "accept-edits": ("allow", "ask"),
        "plan": ("deny", "ask"),
        "auto": ("allow", "allow"),
    }
    for mode, verdicts in expected.items():
        assert permissions.set_mode(mode)
        assert (permissions.decide(*edit), permissions.decide(*command)) == verdicts, mode
    assert not permissions.set_mode("nonsense") and permissions.mode == "auto"

    permissions.set_mode("default")
    assert [permissions.cycle() for _ in range(4)] == ["accept-edits", "plan", "default", "accept-edits"]
    permissions.set_mode("auto")
    assert permissions.cycle() == "default"  # Shift+Tab leaves auto mode


def test_rules_are_saved_per_workspace_outside_the_project(tmp_path):
    path = tmp_path / "home" / "permissions.json"
    one = Permissions(tmp_path / "a", path=path)
    one.add_rule("run_shell:git status")
    one.add_rule("run_shell:git status")  # no duplicates
    one.add_rule("write_file")

    assert Permissions(tmp_path / "a", path=path).rules == ["run_shell:git status", "write_file"]
    assert Permissions(tmp_path / "b", path=path).rules == []  # another project
    assert not list((tmp_path / "a").glob("**/*")) if (tmp_path / "a").exists() else True

    again = Permissions(tmp_path / "a", path=path)
    assert again.remove_rule(0) == "run_shell:git status" and again.remove_rule(9) is None
    again.clear_rules()
    assert Permissions(tmp_path / "a", path=path).rules == []
    path.write_text("{broken")
    assert Permissions(tmp_path / "a", path=path).rules == []


def test_dont_ask_again_adds_a_prefix_rule(tmp_path):
    path = tmp_path / "permissions.json"
    session, _ = make_session(
        tmp_path,
        [[shell("git status", "c1")], [shell("git status --short", "c2")],
         [shell("git status; echo hacked", "c3")], [shell("rm -rf nothing", "c4")],
         [assistant_message("done")]],
        answers=["always", "no", "no"],
        permissions_path=path,
    )
    asyncio.run(session.run_turn("go"))

    ui = session.ui
    assert ui.asked == [
        ("run_shell", "run_shell:git status"),  # asked once, with the rule on offer
        ("run_shell", None),                    # a compound command never matches the rule
        ("run_shell", None),                    # and no rule is offered for rm
    ]  # fmt: skip
    assert session.context.permissions.rules == ["run_shell:git status"]
    assert "rejected" in outputs(session)[2] and "rejected" in outputs(session)[3]

    lines = run_lines(session, "/permissions")
    assert any("git status" in line for line in lines)
    run_lines(session, "/permissions remove 1")
    assert Permissions(session.context.workspace, path=path).rules == []
    assert "还没有放行规则" in run_lines(session, "/permissions")[0]


def test_accept_edits_mode(tmp_path):
    session, _ = make_session(
        tmp_path, [[write("a.txt", "1")], [shell("echo hi")], [assistant_message("done")]],
        answers=["yes"], mode="accept-edits",
    )  # fmt: skip
    asyncio.run(session.run_turn("go"))
    assert (session.context.workspace / "a.txt").exists()
    assert [name for name, _ in session.ui.asked] == ["run_shell"]  # edits were not asked about


# --- plan mode --------------------------------------------------------------


def test_plan_mode_blocks_edits_until_the_plan_is_approved(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [write("early.txt", "x")],  # refused: plan mode
            [function_call("present_plan", {"plan": "1. write a.txt"}, call_id="p1")],  # rejected
            [function_call("present_plan", {"plan": "1. write a.txt\n2. test"}, call_id="p2")],
            [write("a.txt", "1")],  # now allowed without asking (accept-edits)
            [assistant_message("implemented")],
        ],
        plan_answers=["", "accept-edits"],
        mode="plan",
    )
    workspace = session.context.workspace
    assert asyncio.run(session.run_turn("add a file")) == "implemented"

    refused, rejected, approved, written = outputs(session)
    assert "Plan mode is on" in refused and not (workspace / "early.txt").exists()
    assert "did not approve" in rejected and "approved the plan" in approved
    assert (workspace / "a.txt").read_text() == "1"
    assert session.ui.plans == ["1. write a.txt", "1. write a.txt\n2. test"]
    assert session.mode == "accept-edits" and session.ui.asked == []

    calls = session.agent.model.calls
    assert "Plan mode is ON" in calls[0].system_instructions
    assert "Plan mode is ON" not in calls[-1].system_instructions  # instructions follow the mode
    assert "present_plan" in [tool.name for tool in calls[0].tools]


def test_present_plan_outside_plan_mode_and_mode_command(tmp_path):
    session, _ = make_session(
        tmp_path,
        [[function_call("present_plan", {"plan": "x"}, call_id="p")], [assistant_message("ok")]],
    )
    asyncio.run(session.run_turn("go"))
    assert "Plan mode is not on" in outputs(session)[0] and session.ui.plans == []

    assert "当前模式: default" in run_lines(session, "/mode")[0]
    run_lines(session, "/mode plan")
    assert session.mode == "plan"
    run_lines(session, "/plan")  # alias
    assert session.mode == "plan"
    assert "未知模式" in run_lines(session, "/mode turbo")[0] and session.mode == "plan"
    run_lines(session, "/yes")
    assert session.mode == "auto" and session.context.auto_approve
    _, choices = asyncio.run(picker_choices(session, "mode"))
    assert [(c.label, c.current) for c in choices] == [
        ("default", False), ("accept-edits", False), ("plan", False), ("auto", True)]


# --- undo -------------------------------------------------------------------


def test_undo_reverts_the_last_turn_that_changed_files(tmp_path):
    session, _ = make_session(
        tmp_path,
        [
            [write("keep.txt", "v1")], [assistant_message("one")],
            [write("keep.txt", "v2"), write("new.txt", "n")],
            [function_call("edit_file", {"path": "keep.txt", "old_string": "v2", "new_string": "v3"},
                           call_id="e1")],
            [assistant_message("two")],
            [assistant_message("just talking")],
            [assistant_message("after undo")],
        ],
        auto_approve=True,
    )  # fmt: skip
    workspace = session.context.workspace
    asyncio.run(session.run_turn("first"))
    asyncio.run(session.run_turn("second   change"))
    asyncio.run(session.run_turn("chat"))  # changes nothing: undo skips it
    assert (workspace / "keep.txt").read_text() == "v3"

    lines = run_lines(session, "/undo")
    assert "second change" in lines[0] and any("keep.txt" in line for line in lines)
    assert (workspace / "keep.txt").read_text() == "v1" and not (workspace / "new.txt").exists()
    # the model is told, so it does not assume its edits are still there
    asyncio.run(session.run_turn("what now"))
    assert "reverted the file changes" in str(session.agent.model.last_call.input[-2]["content"])

    run_lines(session, "/undo")  # goes back one more turn: the file is removed again
    assert not (workspace / "keep.txt").exists()
    assert "没有可撤销的内容" in run_lines(session, "/undo")[0]


# --- ! commands -------------------------------------------------------------


def test_user_shell_command_runs_and_enters_the_context(tmp_path):
    session, _ = make_session(tmp_path, [[assistant_message("I see 42")]])
    output, code = asyncio.run(session.user_shell("echo 42; exit 3"))
    assert (output, code) == ("42", 3)
    asyncio.run(session.run_turn("what did it print?"))
    sent = session.agent.model.last_call.input
    assert "$ echo 42; exit 3\n42\n[exit code: 3]" in sent[0]["content"]
    assert session._title() == "what did it print?"  # the bookkeeping item is not the title


# --- images -----------------------------------------------------------------


def test_images_mentioned_in_a_message_are_attached(tmp_path):
    session, out = make_session(tmp_path, [[assistant_message("a red square")]])
    workspace = session.context.workspace
    (workspace / "shot.png").write_bytes(PNG)
    (workspace / "my shot.jpg").write_bytes(PNG)
    (workspace / "notes.txt").write_text("x")
    (workspace / "huge.png").write_bytes(b"\x89PNG" + b"0" * 5_000_001)

    text = "compare @shot.png and 'my shot.jpg', see notes.txt, missing.png, huge.png. Also shot.png."
    images, problems = find_images(text, workspace)
    assert [p.name for p in images] == ["shot.png", "my shot.jpg"]
    assert problems == ["huge.png: larger than 5 MB"]
    assert find_images(r"look at my\ shot.jpg", workspace)[0][0].name == "my shot.jpg"
    assert user_content("plain", []) == "plain"

    asyncio.run(session.run_turn(text))
    content = session.agent.model.last_call.input[0]["content"]
    assert content[0] == {"type": "input_text", "text": text}
    assert [part["type"] for part in content[1:]] == ["input_image", "input_image"]
    assert content[1]["image_url"].startswith("data:image/png;base64,iVBOR")
    assert content[2]["image_url"].startswith("data:image/jpeg;base64,")
    assert "已附加 2 张图片" in out.getvalue() and "图片未附加" in out.getvalue()
    assert session._title().startswith("compare @shot.png")

    # a megabyte of image data must not look like a million tokens
    big = [{"role": "user", "content": [{"type": "input_text", "text": "hi"},
                                        {"type": "input_image", "image_url": "data:x;base64," + "A" * 4_000_000}]}]
    assert estimate_tokens(big) < 3_000


# --- background commands ----------------------------------------------------


def test_background_command_output_and_kill(tmp_path):
    script = "import time\nprint('ready', flush=True)\ntime.sleep(60)\n"
    session, _ = make_session(
        tmp_path,
        [
            [shell("python3 server.py", "c1", background=True)],
            [function_call("shell_output", {"task_id": "bg1", "wait": 5}, call_id="c2")],
            [function_call("shell_kill", {"task_id": "bg1"}, call_id="c3")],
            [function_call("shell_output", {"task_id": "bg1"}, call_id="c4")],
            [function_call("shell_output", {"task_id": "bg9"}, call_id="c5")],
            [shell("echo quick", "c6", background=True)],
            [function_call("shell_output", {"task_id": "bg2", "wait": 5}, call_id="c7")],
            [assistant_message("done")],
        ],
        auto_approve=True,
    )
    (session.context.workspace / "server.py").write_text(script)

    async def scenario():
        await session.run_turn("start the server")
        return [task.running for task in session.context.shell_tasks.tasks.values()]

    assert asyncio.run(scenario()) == [False, False]
    started, first, killed, after, unknown, _, quick = outputs(session)
    assert "Started in the background as bg1" in started
    assert first == "ready\n[bg1: running]"
    assert killed == "Stopped bg1." and "exited with code" in after
    assert "unknown task" in unknown and "bg1" in unknown
    assert quick == "quick\n[bg2: exited with code 0]"
    assert "bg1" in "\n".join(run_lines(session, "/tasks"))


def test_closing_the_session_stops_background_commands(tmp_path):
    session, _ = make_session(
        tmp_path, [[shell("sleep 60", "c1", background=True)], [assistant_message("ok")]],
        auto_approve=True,
    )  # fmt: skip

    async def scenario():
        await session.run_turn("go")
        task = session.context.shell_tasks.tasks["bg1"]
        assert task.running
        session.close()
        await asyncio.wait_for(task.process.wait(), timeout=5)
        return task.running

    assert asyncio.run(scenario()) is False


# --- web --------------------------------------------------------------------

PAGE = """<html><head><title>Docs  Page</title><style>p{}</style></head><body>
<nav><a href="/home">Home</a></nav><h1>Install</h1>
<p>Run <code>pip install x</code> then see <a href="guide.html">the guide</a>.</p>
<ul><li>one</li><li>two</li></ul><pre>line 1
  line 2</pre><script>alert(1)</script></body></html>"""


@pytest.fixture
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            routes = {
                "/page": (200, "text/html; charset=utf-8", PAGE.encode()),
                "/data.json": (200, "application/json", b'{"a": 1}'),
                "/bin": (200, "application/octet-stream", b"\x00\x01"),
            }
            status, kind, body = routes.get(self.path, (404, "text/plain", b"nope"))
            self.send_response(status)
            self.send_header("content-type", kind)
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_fetch_turns_html_into_readable_text(server, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    text = web.fetch(f"{server}/page")
    assert text.startswith(f"URL: {server}/page\nTitle: Docs Page\n")
    assert "# Install" in text and "Run `pip install x` then see" in text
    assert f"[the guide]({server}/guide.html)" in text and "- one\n- two" in text
    assert "```\nline 1\n  line 2\n```" in text
    assert "alert(1)" not in text and "p{}" not in text
    assert web.fetch(f"{server}/data.json").endswith('{"a": 1}')
    for url, message in ((f"{server}/missing", "HTTP 404"), (f"{server}/bin", "cannot read content"),
                         ("file:///etc/passwd", "only http and https"), ("ftp://x/y", "only http")):
        with pytest.raises(web.WebError, match=message):
            web.fetch(url)


def test_web_fetch_tool_asks_and_offers_a_host_rule(tmp_path, server, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    session, _ = make_session(
        tmp_path,
        [[function_call("web_fetch", {"url": f"{server}/page"}, call_id="f1")],
         [function_call("web_fetch", {"url": f"{server}/data.json"}, call_id="f2")],
         [function_call("web_fetch", {"url": f"{server}/missing"}, call_id="f3")],
         [assistant_message("read it")]],
        answers=["always"],
    )
    asyncio.run(session.run_turn("read the docs"))
    assert session.ui.asked == [("web_fetch", "web_fetch:127.0.0.1")]  # once; then the rule applies
    page, data, missing = outputs(session)
    assert "# Install" in page and '{"a": 1}' in data and missing == "Error: HTTP 404 Not Found"


def test_search_result_parsers_and_fallback(monkeypatch):
    duck = '''<div class="result results_links"><h2><a rel="nofollow" class="result__a"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=x">Python <b>Docs</b></a></h2>
      <a class="result__snippet" href="x">The official <b>docs</b>.</a></div>
      <div class="result"><a class="result__a" href="https://example.com/a">Plain</a></div>'''
    assert web.parse_duckduckgo(duck) == [
        web.SearchResult("Python Docs", "https://docs.python.org/3/", "The official docs."),
        web.SearchResult("Plain", "https://example.com/a", ""),
    ]
    bing = '''<ol><li class="b_algo"><h2><a href="https://a.example/x?y=1&amp;z=2">A &amp; B</a></h2>
      <div><p>Snippet <strong>one</strong></p></div></li><li class="b_algo"><h2>no link</h2></li></ol>'''
    assert web.parse_bing(bing) == [web.SearchResult("A & B", "https://a.example/x?y=1&z=2", "Snippet one")]

    calls = []

    def fake(engine, query):
        calls.append(engine)
        if engine == "duckduckgo":
            raise web.WebError("blocked")
        return [web.SearchResult("T", "https://u", "s")] * 20

    monkeypatch.setattr(web, "_search_with", fake)
    assert len(web.search("q")) == 8 and calls == ["duckduckgo", "bing"]  # falls back, capped
    monkeypatch.setattr(web, "_search_with", lambda engine, query: [])
    with pytest.raises(web.WebError, match="duckduckgo: no results; bing: no results"):
        web.search("q")


def test_web_search_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "search", lambda query, engine: [
        web.SearchResult("Title", "https://u.example", "snippet"), web.SearchResult("T2", "https://v", "")])
    session, _ = make_session(
        tmp_path,
        [[function_call("web_search", {"query": "asyncio docs"}, call_id="q1")], [assistant_message("ok")]],
    )
    asyncio.run(session.run_turn("search"))
    assert outputs(session) == ["Title\nhttps://u.example\nsnippet\n\nT2\nhttps://v"]
    assert session.ui.asked == []  # searching needs no approval


# --- the same features in the TUI -------------------------------------------


def run_tui(tmp_path, steps, scenario, **kwargs):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from nori.tui import Tui
    from test_tui import Harness

    async def main():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            session = Session(tmp_path, model=ScriptedModel(list(steps)), **kwargs)
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


def dialog(tui) -> str:
    return "".join(text for _style, text in tui._approval_text())


def footer(tui) -> str:
    return "".join(text for _style, text in tui._footer_text())


def test_tui_plan_mode_flow(tmp_path):
    steps = [
        [function_call("present_plan", {"plan": "## Plan\n\n1. create a.txt"}, call_id="p1")],
        [write("a.txt", "1")],
        [assistant_message("done")],
    ]

    async def scenario(h):
        tui = h.tui
        h.send("\x1b[Z\x1b[Z")  # Shift+Tab twice: default -> accept-edits -> plan
        await h.until(lambda: tui.session.mode == "plan")
        assert "计划模式" in footer(tui)
        h.send("add a file\r")
        await h.until(lambda: tui._approval is not None)
        text = dialog(tui)
        assert "计划已就绪" in text and "❯ 1. 是，并自动接受文件修改" in text and "继续规划" in text
        assert "1 create a.txt" in h.text  # the plan itself is in the transcript, as Markdown
        h.send("\r")
        await h.idle()

    h = run_tui(tmp_path, steps, scenario)
    assert (tmp_path / "a.txt").exists() and h.tui.session.mode == "accept-edits"
    assert "自动接受修改" in footer(h.tui)


def test_tui_rule_option_shift_tab_and_shell_mode(tmp_path):
    steps = [
        [shell("git status", "c1")],
        [write("a.txt", "1")],
        [shell("rm -f nothing", "c3")],
        [assistant_message("done")],
    ]

    async def scenario(h):
        tui = h.tui
        h.send("go\r")
        await h.until(lambda: tui._approval is not None)
        assert "2. 是，本项目中不再询问 `git status`" in dialog(tui)
        h.send("2")
        # a file edit: switching to accept-edits answers it without a second key press
        await h.until(lambda: tui._approval is not None and "Write(a.txt)" in dialog(tui))
        assert "2. 是，本项目中不再询问 Write" in dialog(tui)
        h.send("\x1b[Z")
        await h.until(lambda: (tmp_path / "a.txt").exists())
        # no standing rule is offered for rm: only yes / no
        await h.until(lambda: tui._approval is not None and "rm -f" in dialog(tui))
        assert len(tui._approval["options"]) == 2 and "不再询问" not in dialog(tui)
        h.send("\x1b")
        await h.idle()

        h.send("!echo from-user")
        await h.until(lambda: tui.input.text == "!echo from-user")
        assert tui._line_prefix(0, 0)[0][0] == "class:prompt.shell"
        assert tui._rule_style() == "class:rule.shell"
        h.send("\r")
        await h.until(lambda: "from-user\n" in h.text + "\n" and "exit code: 0" in h.text)
        await h.idle()

    h = run_tui(tmp_path, steps, scenario)
    session = h.tui.session
    assert session.context.permissions.rules == ["run_shell:git status"]
    assert session.mode == "accept-edits"
    assert "$ echo from-user\nfrom-user" in session.history[-1]["content"]
