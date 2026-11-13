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
from .i18n import DEFAULT_LANGUAGE, normalize_language, t
from .mcp_config import expand_env

CONFIG_KEYS = ("base_url", "api_key", "model", "api")
EXTRA_KEYS = ("max_turns", "context_window", "auto_compact", "language", "models", "retries")
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
    default_context_window: int = DEFAULT_CONTEXT_WINDOW  # for models without their own
    language: str = DEFAULT_LANGUAGE
    retries: int = 4  # retries of a model request after a transient error
    # Models offered by /model: {"name": ..., "context_window": ... (optional)}
    models: tuple[dict[str, Any], ...] = ()


def model_window(models: Any, name: str | None) -> int | None:
    """The context window configured for one model, if any."""
    for entry in models or ():
        if entry.get("name") == name:
            return entry.get("context_window")
    return None


def _load_models(path: Path, value: Any) -> list[dict[str, Any]]:
    """Normalise `models`: each entry is a name or {"name", "context_window"}."""
    error = ConfigError(f"{path}: " + t("models must be a list of model names or objects with a name"))
    if not isinstance(value, list):
        raise error
    models: list[dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise error
        model: dict[str, Any] = {"name": entry["name"].strip()}
        window = entry.get("context_window")
        if window is not None:
            if isinstance(window, bool) or not isinstance(window, int) or window < MIN_CONTEXT_WINDOW:
                raise error
            model["context_window"] = window
        if not model["name"]:
            raise error
        models.append(model)
    return models


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
        raise ConfigError(f"{path}: " + t("the top level must be a JSON object"))
    config: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        value = data.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise ConfigError(f"{path}: " + t("{key} must be a string", key=key))
        config[key] = expand_env(value.strip())
    if "api" in config and config["api"] not in APIS:
        raise ConfigError(f"{path}: " + t("api must be responses or chat"))
    for key, minimum in (("max_turns", 1), ("context_window", MIN_CONTEXT_WINDOW)):
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ConfigError(
                f"{path}: " + t("{key} must be an integer of at least {minimum}", key=key, minimum=minimum)
            )
        config[key] = value
    if data.get("auto_compact") is not None:
        if not isinstance(data["auto_compact"], bool):
            raise ConfigError(f"{path}: " + t("auto_compact must be true or false"))
        config["auto_compact"] = data["auto_compact"]
    if data.get("retries") is not None:
        retries = data["retries"]
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 20:
            raise ConfigError(f"{path}: " + t("retries must be an integer from 0 to 20"))
        config["retries"] = retries
    if data.get("language") not in (None, ""):
        language = normalize_language(str(data["language"]))
        if language is None:
            raise ConfigError(f"{path}: " + t("language must be en or zh"))
        config["language"] = language
    if data.get("models") is not None:
        config["models"] = _load_models(path, data["models"])
    return config


def save_config(path: Path, config: Mapping[str, Any]) -> None:
    """Write the config file, readable only by the owner since it holds a key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {key: config[key] for key in (*CONFIG_KEYS, *EXTRA_KEYS) if config.get(key) is not None}
    data = {key: value for key, value in data.items() if value not in ("", [])}
    if "models" in data:  # write entries without a window as plain names
        data["models"] = [m if len(m) > 1 else m["name"] for m in data["models"]]
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
    language: str | None = None,
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
        raise ConfigError(t("api must be responses or chat, not {value}", value=repr(resolved_api)))
    resolved_model = pick(model, env.get("NORI_MODEL"), config.get("model"))
    models = tuple(config.get("models") or ())
    return Settings(
        api_key=pick(config.get("api_key"), env.get("OPENAI_API_KEY")),
        base_url=resolved_base_url,
        model=resolved_model,
        api=resolved_api,
        max_turns=max_turns or config.get("max_turns"),
        # A window configured for the chosen model beats the general setting.
        context_window=max(
            context_window
            or model_window(models, resolved_model)
            or config.get("context_window")
            or DEFAULT_CONTEXT_WINDOW,
            MIN_CONTEXT_WINDOW,
        ),
        auto_compact=config.get("auto_compact", True) if auto_compact is None else auto_compact,
        default_context_window=max(
            context_window or config.get("context_window") or DEFAULT_CONTEXT_WINDOW,
            MIN_CONTEXT_WINDOW,
        ),
        language=resolve_language(config, env, language),
        retries=config.get("retries", 4),
        models=models,
    )


def resolve_language(
    config: Mapping[str, Any], env: Mapping[str, str] | None = None, language: str | None = None
) -> str:
    """Interface language: command line > NORI_LANG > config file > English."""
    env = os.environ if env is None else env
    for candidate in (language, env.get("NORI_LANG"), config.get("language")):
        normalized = normalize_language(candidate)
        if normalized:
            return normalized
    return DEFAULT_LANGUAGE


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
        return t("(not set)")
    return f"{key[:3]}…{key[-4:]}" if len(key) > 10 else "…"


def run_setup(path: Path, console: Console) -> bool:
    """Interactively create or update the config file. Returns True if saved."""
    try:
        current = load_config(path)
    except ConfigError:
        current = {}
    console.print(f"[bold]{t('Set up {app}', app=APP_NAME)}[/bold]  [dim]{path}[/dim]")
    console.print(f"[dim]{t('Press Enter to keep the current value shown in brackets.')}[/dim]")

    def ask(label: str, key: str, secret: bool = False) -> str:
        shown = mask_key(current.get(key)) if secret else current.get(key, "")
        prompt = f"{label} [{shown}]: " if current.get(key) else f"{label}: "
        answer = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        return answer or current.get(key, "")

    try:
        config = {
            "base_url": ask(t("Base URL (leave empty for the official OpenAI endpoint)"), "base_url"),
            "api_key": ask(t("API key (input is hidden)"), "api_key", secret=True),
            "model": ask(t("Model name"), "model"),
        }
        shown_window = current.get("context_window", DEFAULT_CONTEXT_WINDOW)
        window_answer = input(
            t("Context window of the model, e.g. 128k, 200k, 1m") + f" [{shown_window}]: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        console.print(f"\n[yellow]{t('Cancelled; nothing was saved.')}[/yellow]")
        return False
    for key in ("api", *EXTRA_KEYS):  # settings the wizard does not ask about
        if current.get(key) is not None:
            config[key] = current[key]
    if window_answer:
        window = parse_token_count(window_answer)
        if window is None:
            message = t(
                "Cannot understand the context window {value} (minimum {minimum}); keeping the old value.",
                value=repr(window_answer),
                minimum=MIN_CONTEXT_WINDOW,
            )
            console.print(f"[yellow]{message}[/yellow]")
        else:
            config["context_window"] = window
    if not config["api_key"]:
        console.print(f"[red]{t('The API key must not be empty; nothing was saved.')}[/red]")
        return False
    save_config(path, config)
    console.print(f"[green]{t('Saved to {path}', path=path)}[/green]")
    return True
