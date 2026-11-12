"""Tests for the config file and how settings are resolved."""

from __future__ import annotations

import json
import stat

import pytest
from rich.console import Console

from nori.cli import main
from nori.config import (
    ConfigError,
    default_config_path,
    load_config,
    mask_key,
    resolve_settings,
    run_setup,
    save_config,
)


def write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_load_config(tmp_path, monkeypatch):
    assert load_config(tmp_path / "missing.json") == {}
    monkeypatch.setenv("MY_KEY", "sk-env")
    path = write(
        tmp_path / "c.json",
        {"base_url": " https://x/v1 ", "api_key": "${MY_KEY}", "model": "m", "extra": 1, "api": ""},
    )
    assert load_config(path) == {"base_url": "https://x/v1", "api_key": "sk-env", "model": "m"}


@pytest.mark.parametrize("content", ["{oops", "[]", '{"model": 3}', '{"api": "grpc"}'])
def test_invalid_config(tmp_path, content):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path / "c.json", content))


def test_precedence():
    config = {"base_url": "https://file/v1", "api_key": "sk-file", "model": "file-model"}
    env = {
        "OPENAI_API_KEY": "sk-env",
        "OPENAI_BASE_URL": "https://env/v1",
        "NORI_MODEL": "env-model",
    }
    # the config file beats the generic OPENAI_* variables
    s = resolve_settings(config, env)
    assert (s.api_key, s.base_url, s.model) == ("sk-file", "https://file/v1", "env-model")
    # the command line beats everything
    s = resolve_settings(config, env, model="cli-model", base_url="https://cli/v1", api="responses")
    assert (s.base_url, s.model, s.api) == ("https://cli/v1", "cli-model", "responses")
    # the environment fills in what the file leaves out
    s = resolve_settings({}, env)
    assert (s.api_key, s.base_url, s.model) == ("sk-env", "https://env/v1", "env-model")


def test_api_defaults_to_chat_for_third_party_endpoints():
    assert resolve_settings({}, {}).api == "responses"
    assert resolve_settings({"base_url": "https://api.openai.com/v1"}, {}).api == "responses"
    assert resolve_settings({"base_url": "https://other/v1"}, {}).api == "chat"
    assert resolve_settings({"base_url": "https://other/v1", "api": "responses"}, {}).api == "responses"
    assert resolve_settings({"api": "responses"}, {"NORI_API": "chat"}).api == "chat"
    with pytest.raises(ConfigError):
        resolve_settings({}, {"NORI_API": "nope"})


def test_save_is_private_and_round_trips(tmp_path):
    path = tmp_path / "dir" / "config.json"
    save_config(path, {"api_key": "sk-1", "base_url": "", "model": "m", "junk": "x"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_config(path) == {"api_key": "sk-1", "model": "m"}


def test_paths_and_masking(tmp_path):
    assert default_config_path({}, tmp_path) == tmp_path / ".nori" / "config.json"
    assert default_config_path({"NORI_CONFIG": "/x/y.json"}, tmp_path).as_posix() == "/x/y.json"
    assert mask_key("sk-abcdefghijkl") == "sk-…ijkl"
    assert "ijkl" not in mask_key("short") and mask_key(None) == "(未设置)"


def test_setup_writes_and_updates(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    console = Console(file=open(tmp_path / "out.txt", "w"))

    answers = iter(["https://x/v1", "my-model"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "sk-secret-key-0001")
    assert run_setup(path, console) is True
    assert load_config(path) == {
        "base_url": "https://x/v1",
        "api_key": "sk-secret-key-0001",
        "model": "my-model",
    }

    # pressing Enter keeps current values; the key is never echoed in prompts
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    monkeypatch.setattr("getpass.getpass", lambda prompt: prompts.append(prompt) or "")
    assert run_setup(path, console) is True
    assert load_config(path)["api_key"] == "sk-secret-key-0001"
    assert not any("sk-secret-key-0001" in prompt for prompt in prompts)

    # an empty key is refused
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "")
    assert run_setup(tmp_path / "new.json", console) is False
    assert not (tmp_path / "new.json").exists()


def test_cli_reports_missing_key_and_bad_config(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = tmp_path / "config.json"
    assert main(["--config", str(config), "-C", str(tmp_path), "hi"]) == 2
    assert "--setup" in capsys.readouterr().out

    write(config, "{broken")
    assert main(["--config", str(config), "-C", str(tmp_path), "hi"]) == 2
    assert "配置有误" in capsys.readouterr().out


def test_max_turns(tmp_path):
    assert resolve_settings({}, {}).max_turns == 200
    path = write(tmp_path / "c.json", {"api_key": "k", "max_turns": 500})
    config = load_config(path)
    assert resolve_settings(config, {}).max_turns == 500
    assert resolve_settings(config, {}, max_turns=30).max_turns == 30
    save_config(path, config)
    assert load_config(path)["max_turns"] == 500
    for bad in (0, -1, "many", True, 1.5):
        with pytest.raises(ConfigError):
            load_config(write(tmp_path / "bad.json", {"max_turns": bad}))
