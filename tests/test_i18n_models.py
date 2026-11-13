"""Tests for the interface language, model switching and pickers."""

from __future__ import annotations

import asyncio
import io
import json
import string

import pytest
from agents import set_tracing_disabled
from agents.testing import ScriptedModel, assistant_message, function_call
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from i18n_keys import translation_keys
from nori import i18n
from nori.cli import build_parser, detect_language, main
from nori.commands import fuzzy_rank, picker_choices, run_command
from nori.config import ConfigError, load_config, resolve_language, resolve_settings, save_config
from nori.i18n import ZH, normalize_language, set_language, t
from nori.session import PlainUI, Session
from nori.store import SessionRecord, SessionStore
from nori.tui import Tui
from test_tui import DOWN, ENTER, ESC, Harness

set_tracing_disabled(True)


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


# --- catalog ----------------------------------------------------------------


def test_every_text_has_a_chinese_translation():
    keys = translation_keys()
    assert len(keys) > 150
    missing = sorted(keys - ZH.keys())
    assert not missing, f"add these to nori.i18n.ZH: {missing}"
    unused = sorted(ZH.keys() - keys)
    assert not unused, f"remove these stale entries from nori.i18n.ZH: {unused}"


def test_translations_keep_their_placeholders():
    for english, chinese in ZH.items():
        assert placeholders(english) == placeholders(chinese), english
        assert chinese.strip(), english


def test_language_switching():
    set_language("en")
    assert t("Model") == "Model" and t("Saved to {path}", path="x") == "Saved to x"
    assert set_language("zh") and t("Model") == "模型" and t("Saved to {path}", path="x") == "已保存到 x"
    assert t("not in the catalog") == "not in the catalog"
    assert not set_language("fr") and i18n.get_language() == "zh"  # unsupported: unchanged
    for raw, expected in [("zh-CN", "zh"), ("ZH_cn.UTF-8", "zh"), ("中文", "zh"), ("Chinese", "zh"),
                          ("EN", "en"), ("en_US", "en"), ("english", "en"), ("fr", None), ("", None)]:
        assert normalize_language(raw) == expected


def test_language_precedence_and_default(tmp_path):
    assert resolve_language({}, {}) == "en"  # English unless asked otherwise
    assert resolve_language({"language": "zh"}, {}) == "zh"
    assert resolve_language({"language": "zh"}, {"NORI_LANG": "en"}) == "en"
    assert resolve_language({"language": "zh"}, {"NORI_LANG": "en"}, "zh-CN") == "zh"
    assert resolve_language({}, {"NORI_LANG": "klingon"}) == "en"

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_key": "k", "language": "zh-CN"}))
    assert load_config(path)["language"] == "zh"
    path.write_text(json.dumps({"language": "klingon"}))
    with pytest.raises(ConfigError):
        load_config(path)


def test_cli_language_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NORI_LANG")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = tmp_path / "config.json"

    assert detect_language(["--config", str(config)]) == "en"
    assert main(["--config", str(config), "-C", str(tmp_path), "hi"]) == 2
    assert "No API key found" in capsys.readouterr().out

    assert main(["--config", str(config), "-C", str(tmp_path), "--lang", "zh", "hi"]) == 2
    assert "没有找到 API Key" in capsys.readouterr().out

    config.write_text(json.dumps({"language": "zh"}))
    assert detect_language(["--config", str(config)]) == "zh"
    assert detect_language(["--config", str(config), "--lang", "en"]) == "en"
    set_language("zh")
    assert "界面语言" in build_parser().format_help()
    set_language("en")
    assert "interface language" in build_parser().format_help()

    assert main(["--lang", "klingon"]) == 2
    assert "Unknown language" in capsys.readouterr().err


# --- English interface ------------------------------------------------------


def make_session(tmp_path, steps=(), **kwargs):
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    session = Session(tmp_path, model=ScriptedModel(list(steps)), ui=PlainUI(console), **kwargs)
    return session, out


def run_lines(session, *commands):
    lines: list[str] = []
    for command in commands:
        asyncio.run(run_command(session, command, lambda r: lines.append(str(r))))
    return lines


def test_english_is_used_throughout_by_default(tmp_path):
    set_language("en")
    session, out = make_session(
        tmp_path,
        [[function_call("run_shell", {"command": "seq 1 20"}, call_id="c1")],
         [assistant_message("done")]],
        auto_approve=True,
    )
    asyncio.run(session.run_turn("go"))
    lines = run_lines(session, "/help", "/status", "/context", "/nope", "/clear", "/yes")
    text = out.getvalue() + "\n".join(lines)

    for expected in ("… +15 lines", "Show help", "Switch the model", "Auto-approve", "Total usage",
                     "Window 128,000 tokens", "Unknown command /nope", "Started a new conversation.",
                     "Auto-approve is off."):
        assert expected in text, expected
    assert not any("一" <= ch <= "鿿" for ch in text), "Chinese text leaked into English UI"


def test_lang_command_switches_and_saves(tmp_path):
    set_language("en")
    config = tmp_path / "config.json"
    save_config(config, {"api_key": "k"})
    session, _ = make_session(tmp_path)
    session.config_path = config

    lines = run_lines(session, "/lang")
    assert "Language: English" in lines[0]
    lines = run_lines(session, "/lang zh")
    assert i18n.get_language() == "zh" and "界面语言已设为 中文" in lines[0] and "已保存" in lines[0]
    assert load_config(config) == {"api_key": "k", "language": "zh"}
    assert "显示帮助" in "\n".join(run_lines(session, "/help"))
    lines = run_lines(session, "/lang klingon")
    assert "不支持的语言" in lines[0] and i18n.get_language() == "zh"
    run_lines(session, "/language en")  # alias
    assert i18n.get_language() == "en"


# --- models -----------------------------------------------------------------


def test_models_config_and_per_model_context_window(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "api_key": "k", "model": "big", "context_window": 50_000,
        "models": ["small", {"name": "big", "context_window": 200_000}],
    }))
    config = load_config(path)
    assert config["models"] == [{"name": "small"}, {"name": "big", "context_window": 200_000}]
    settings = resolve_settings(config, {})
    assert settings.context_window == 200_000  # the model's own window wins
    assert resolve_settings(config, {}, model="small").context_window == 50_000
    assert resolve_settings(config, {}, context_window=8_000).context_window == 8_000

    save_config(path, config)  # round trip keeps the compact spelling
    assert json.loads(path.read_text())["models"] == ["small", {"name": "big", "context_window": 200_000}]
    for bad in ("x", [1], [{"context_window": 5}], [{"name": "a", "context_window": 10}], [""]):
        path.write_text(json.dumps({"models": bad}))
        with pytest.raises(ConfigError):
            load_config(path)


def test_model_command_switches_saves_and_applies_the_window(tmp_path):
    config = tmp_path / "config.json"
    save_config(config, {"api_key": "k", "model": "small",
                         "models": [{"name": "small"}, {"name": "big", "context_window": 200_000}]})
    session = Session(tmp_path, model="small")
    session.config_path = config
    session.models = load_config(config)["models"]

    async def endpoint_models():
        return ["remote-b", "remote-a", "big"]

    session.fetch_models = endpoint_models
    assert asyncio.run(session.available_models()) == ["small", "big", "remote-a", "remote-b"]

    lines = run_lines(session, "/model")
    assert "当前模型: small" in lines[0] and "big, remote-a, remote-b" in lines[1]

    lines = run_lines(session, "/model big")
    assert session.model == "big" and session.agent.model == "big"
    assert session.context_window == 200_000 and "200,000" in lines[1]
    assert load_config(config)["model"] == "big" and "已保存" in lines[0]

    # /context on a model with its own window updates that entry, not the global one
    run_lines(session, "/context 300k")
    saved = load_config(config)
    assert saved["models"][1] == {"name": "big", "context_window": 300_000}
    assert "context_window" not in saved

    session.fetch_models = None
    session._fetched_models = None
    session.config_path = None
    lines = run_lines(session, "/model unlisted")
    assert session.model == "unlisted" and "仅本次会话有效" in lines[0]
    assert asyncio.run(session.available_models()) == ["small", "big", "unlisted"]


def test_failing_model_listing_is_ignored(tmp_path):
    session = Session(tmp_path, model="m")

    async def broken():
        raise RuntimeError("404")

    session.fetch_models = broken
    assert asyncio.run(session.available_models()) == ["m"]


def test_fuzzy_rank():
    assert fuzzy_rank("mo", "model") == 0 and fuzzy_rank("de", "model") == 1
    assert fuzzy_rank("mdl", "model") == 2 and fuzzy_rank("xyz", "model") is None
    assert fuzzy_rank("", "anything") == 0 and fuzzy_rank("GPT", "gpt-5") == 0


def test_picker_choices(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(SessionRecord(id="20260101-000000-aaaa", workspace=str(tmp_path), title="old task",
                             history=[{"role": "user", "content": "x"}]))
    session = Session(tmp_path, model="a", store=store)
    session.models = [{"name": "a"}, {"name": "b", "context_window": 200_000}]

    title, choices = asyncio.run(picker_choices(session, "model"))
    assert title == "选择模型"
    assert [(c.label, c.command, c.current) for c in choices] == [
        ("a", "/model a", True), ("b", "/model b", False)]
    assert choices[1].description == "上下文 200k"

    _, sessions = asyncio.run(picker_choices(session, "resume"))
    assert sessions[0].label == "old task" and sessions[0].command == "/resume 20260101-000000-aaaa"
    _, languages = asyncio.run(picker_choices(session, "lang"))
    assert [(c.label, c.current) for c in languages] == [("English", False), ("中文", True)]
    assert asyncio.run(picker_choices(session, "help")) is None


# --- pickers in the TUI -----------------------------------------------------


def run_tui(tmp_path, scenario, **session_kwargs):
    async def run():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            session = Session(tmp_path, model="small", **session_kwargs)
            session.models = [{"name": "small"}, {"name": "big", "context_window": 200_000},
                              {"name": "medium"}]
            tui = Tui(session)
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

    return asyncio.run(run())


def menu_text(tui: Tui) -> str:
    return "".join(text for _style, text in tui._menu_text())


def test_model_picker_in_the_tui(tmp_path):
    async def scenario(h: Harness):
        tui = h.tui
        h.send("/model" + ENTER)  # no argument: open the picker instead of running
        await h.until(lambda: tui._picker is not None and not tui._picker["loading"])
        panel = menu_text(tui)
        assert "选择模型" in panel and "✓ small" in panel and "big" in panel and "上下文 200k" in panel
        assert tui._placeholder() == "输入以筛选…"
        assert not tui._queue and tui.input.text == ""

        h.send("b")  # typing filters the list
        await h.until(lambda: [i.label for i in tui._menu()] == ["big"])
        h.send(ENTER)
        await h.until(lambda: tui.session.model == "big")
        assert tui._picker is None and tui.session.context_window == 200_000

        h.send("/mod" + ENTER)  # the picker opens from a partial name too
        await h.until(lambda: tui._picker is not None and not tui._picker["loading"])
        assert tui._menu()[tui._menu_index].label == "big"  # starts on the current model
        h.send(DOWN + ENTER)
        await h.until(lambda: tui.session.model == "medium")
        assert tui.session.context_window == 128_000  # back to the general window

        h.send("/model" + ENTER)
        await h.until(lambda: tui._picker is not None and not tui._picker["loading"])
        h.send("brand-new-model")  # not in the list: Enter uses the typed name
        await h.until(lambda: tui._menu() == [])
        assert "按 Enter 直接使用这个名字" in menu_text(tui)
        h.send(ENTER)
        await h.until(lambda: tui.session.model == "brand-new-model")

        h.send("/model" + ENTER)
        await h.until(lambda: tui._picker is not None)
        h.send("zzz" + ESC)  # Esc closes without changing anything or sending the filter text
        await h.until(lambda: tui._picker is None)
        await h.idle()
        assert tui.input.text == "" and tui.session.model == "brand-new-model"

    h = run_tui(tmp_path, scenario)
    assert "模型已切换为 big" in h.text and "❯ /model medium" in h.text
    assert "zzz" not in h.text


def test_language_picker_and_empty_resume_picker(tmp_path):
    async def scenario(h: Harness):
        tui = h.tui
        h.send("/lang" + ENTER)
        await h.until(lambda: tui._picker is not None and not tui._picker["loading"])
        assert [i.label for i in tui._menu()] == ["English", "中文"]
        h.send("eng" + ENTER)
        await h.until(lambda: i18n.get_language() == "en")
        await h.idle()
        # the whole UI follows at once
        assert tui._placeholder() == "Describe what you want to do…"
        assert "shortcuts" in "".join(text for _s, text in tui._footer_text())
        h.send("/")
        await h.until(lambda: bool(tui._menu()))
        assert "Show help" in menu_text(tui) and "enter run" in menu_text(tui)
        h.send(ESC)

        # nothing to pick from: the command runs and explains
        h.send("\x15/resume" + ENTER)
        await h.until(lambda: "No sessions to resume" in h.text)
        assert tui._picker is None

    h = run_tui(tmp_path, scenario, store=SessionStore(tmp_path / "sessions"))
    assert "Language set to English" in h.text
