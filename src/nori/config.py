"""User configuration: API endpoint, key and model.

Settings live in `~/.nori/config.json`:

    {
      "base_url": "https://api.example.com/v1",
      "api_key": "sk-...",
      "model": "your-model",
      "api": "chat",
      "context_window": 128000,
      "auto_compact": true
    }

Only the user-level file is read. A project-level file is deliberately not
supported: a repository that could set `base_url` could redirect your API key.
"""

from __future__ import annotations

import getpass
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from .branding import APP_NAME, app_dir
from .compact import DEFAULT_CONTEXT_WINDOW
from .mcp_config import expand_env

CONFIG_KEYS = ("base_url", "api_key", "model", "api")
EXTRA_KEYS = ("max_turns", "context_window", "auto_compact")
# Turn limit for runs nobody is watching (one-shot and piped). The interactive UI
# has no limit by default: compaction handles long tasks and Esc stops a runaway one.
UNATTENDED_MAX_TURNS = 200
MIN_CONTEXT_WINDOW = 4_000
APIS = ("responses", "chat")
OPENAI_HOST = "api.openai.com"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    base_url: str | None
    model: str | None
    api: str
    max_turns: int | None = None  # None: no limit when interactive
    context_window: int = DEFAULT_CONTEXT_WINDOW
    auto_compact: bool = True


def default_config_path(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("NORI_CONFIG"):
        return Path(env["NORI_CONFIG"]).expanduser()
    return app_dir(home) / "config.json"


def load_config(path: Path) -> dict[str, Any]:
    """Read the config file. A missing file is an empty config."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: 顶层必须是一个 JSON 对象")
    config: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        value = data.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise ConfigError(f"{path}: {key} 必须是字符串")
        config[key] = expand_env(value.strip())
    if "api" in config and config["api"] not in APIS:
        raise ConfigError(f"{path}: api 必须是 {' 或 '.join(APIS)}")
    for key, minimum in (("max_turns", 1), ("context_window", MIN_CONTEXT_WINDOW)):
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ConfigError(f"{path}: {key} 必须是不小于 {minimum} 的整数")
        config[key] = value
    if data.get("auto_compact") is not None:
        if not isinstance(data["auto_compact"], bool):
            raise ConfigError(f"{path}: auto_compact 必须是 true 或 false")
        config["auto_compact"] = data["auto_compact"]
    return config


def save_config(path: Path, config: Mapping[str, Any]) -> None:
    """Write the config file, readable only by the owner since it holds a key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {key: config[key] for key in (*CONFIG_KEYS, *EXTRA_KEYS) if config.get(key) is not None}
    data = {key: value for key, value in data.items() if value != ""}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.chmod(path, 0o600)


def resolve_settings(
    config: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api: str | None = None,
    max_turns: int | None = None,
    context_window: int | None = None,
    auto_compact: bool | None = None,
) -> Settings:
    """Combine sources. Precedence: command line > NORI_* > config file > OPENAI_*.

    The config file outranks the generic OPENAI_* variables because it is
    specific to this tool, while those are shared with every OpenAI client.
    """
    env = os.environ if env is None else env

    def pick(*values: str | None) -> str | None:
        return next((value for value in values if value), None)

    resolved_base_url = pick(base_url, config.get("base_url"), env.get("OPENAI_BASE_URL"))
    resolved_api = pick(api, env.get("NORI_API"), config.get("api"))
    if resolved_api is None:
        # Compatible third-party services almost always implement only Chat Completions.
        third_party = resolved_base_url is not None and OPENAI_HOST not in resolved_base_url
        resolved_api = "chat" if third_party else "responses"
    if resolved_api not in APIS:
        raise ConfigError(f"api 必须是 {' 或 '.join(APIS)}，而不是 {resolved_api!r}")
    return Settings(
        api_key=pick(config.get("api_key"), env.get("OPENAI_API_KEY")),
        base_url=resolved_base_url,
        model=pick(model, env.get("NORI_MODEL"), config.get("model")),
        api=resolved_api,
        max_turns=max_turns or config.get("max_turns"),
        context_window=max(
            context_window or config.get("context_window") or DEFAULT_CONTEXT_WINDOW,
            MIN_CONTEXT_WINDOW,
        ),
        auto_compact=config.get("auto_compact", True) if auto_compact is None else auto_compact,
    )


def parse_token_count(text: str) -> int | None:
    """Parse a context size such as `128000`, `128,000`, `200k` or `1m`."""
    text = text.strip().lower().replace(",", "").replace("_", "")
    factor = 1
    if text.endswith(("k", "m")):
        factor = 1_000 if text.endswith("k") else 1_000_000
        text = text[:-1]
    try:
        value = int(float(text) * factor)
    except ValueError:
        return None
    return value if value >= MIN_CONTEXT_WINDOW else None


def update_config(path: Path, **changes: Any) -> None:
    """Change individual settings in the config file, keeping the rest."""
    config = load_config(path)
    config.update(changes)
    save_config(path, config)


def mask_key(key: str | None) -> str:
    if not key:
        return "(未设置)"
    return f"{key[:3]}…{key[-4:]}" if len(key) > 10 else "…"


def run_setup(path: Path, console: Console) -> bool:
    """Interactively create or update the config file. Returns True if saved."""
    try:
        current = load_config(path)
    except ConfigError:
        current = {}
    console.print(f"[bold]配置 {APP_NAME}[/bold]  [dim]{path}[/dim]")
    console.print("[dim]直接回车保留方括号里的当前值。[/dim]")

    def ask(label: str, key: str, secret: bool = False) -> str:
        shown = mask_key(current.get(key)) if secret else current.get(key, "")
        prompt = f"{label} [{shown}]: " if current.get(key) else f"{label}: "
        answer = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        return answer or current.get(key, "")

    try:
        config = {
            "base_url": ask("Base URL（留空使用 OpenAI 官方地址）", "base_url"),
            "api_key": ask("API Key（输入不回显）", "api_key", secret=True),
            "model": ask("模型名", "model"),
        }
        shown_window = current.get("context_window", DEFAULT_CONTEXT_WINDOW)
        window_answer = input(
            f"模型的上下文窗口大小，如 128k、200k、1m [{shown_window}]: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]已取消，未保存。[/yellow]")
        return False
    for key in ("api", *EXTRA_KEYS):  # settings the wizard does not ask about
        if current.get(key) is not None:
            config[key] = current[key]
    if window_answer:
        window = parse_token_count(window_answer)
        if window is None:
            console.print(
                f"[yellow]无法识别的上下文窗口大小 {window_answer!r}（至少 {MIN_CONTEXT_WINDOW}），"
                "保持原值。[/yellow]"
            )
        else:
            config["context_window"] = window
    if not config["api_key"]:
        console.print("[red]API Key 不能为空，未保存。[/red]")
        return False
    save_config(path, config)
    console.print(f"[green]已保存到 {path}[/green]")
    return True
