"""MCP server configuration.

Servers are declared in the same `mcpServers` JSON format other coding agents use:

    {"mcpServers": {
        "files":  {"command": "npx", "args": ["-y", "some-mcp-server"], "env": {"K": "${K}"}},
        "remote": {"type": "http", "url": "https://example.com/mcp", "headers": {...}}
    }}

`~/.nori/mcp.json` holds the user's servers and `<workspace>/.mcp.json` the
project's. Project servers override user servers with the same name.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import RunContextWrapper
from agents.mcp import MCPServer, MCPServerSse, MCPServerStdio, MCPServerStreamableHttp

from .branding import app_dir

PROJECT_CONFIG = ".mcp.json"
DEFAULT_TIMEOUT_SECONDS = 30.0
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class MCPConfigError(Exception):
    pass


@dataclass(frozen=True)
class MCPServerSpec:
    name: str
    config: dict[str, Any]
    source: str  # "user" or "project"

    @property
    def transport(self) -> str:
        if "command" in self.config:
            return "stdio"
        return "sse" if self.config.get("type") == "sse" else "http"

    def describe(self) -> str:
        if self.transport == "stdio":
            return " ".join([str(self.config["command"]), *map(str, self.config.get("args", []))])
        return str(self.config.get("url", ""))


def user_config_path(home: Path | None = None) -> Path:
    return app_dir(home) / "mcp.json"


def expand_env(value: Any) -> Any:
    """Expand `${VAR}` and `${VAR:-default}` in strings, recursively."""
    if isinstance(value, str):
        return _VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def _read(path: Path, source: str) -> list[MCPServerSpec]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MCPConfigError(f"{path}: {exc}") from exc
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise MCPConfigError(f"{path}: expected an object with an 'mcpServers' object")
    specs = []
    for name, config in servers.items():
        if not isinstance(config, dict) or not ("command" in config or "url" in config):
            raise MCPConfigError(f"{path}: server {name!r} needs a 'command' or a 'url'")
        if config.get("disabled"):
            continue
        specs.append(MCPServerSpec(name, expand_env(config), source))
    return specs


def load_mcp_specs(workspace: Path, home: Path | None = None) -> list[MCPServerSpec]:
    """Load user and project server specs; project entries win on name clashes."""
    merged: dict[str, MCPServerSpec] = {}
    for spec in _read(user_config_path(home), "user"):
        merged[spec.name] = spec
    for spec in _read(workspace / PROJECT_CONFIG, "project"):
        merged[spec.name] = spec
    return list(merged.values())


async def _needs_approval(ctx: RunContextWrapper[Any], _agent: Any, tool: Any) -> bool:
    state = ctx.context
    return not (state.auto_approve or tool.name in state.always_allowed)


def build_server(spec: MCPServerSpec, workspace: Path) -> MCPServer:
    """Create (but do not connect) the SDK server object for a spec."""
    config = spec.config
    common: dict[str, Any] = {
        "name": spec.name,
        "cache_tools_list": True,
        "client_session_timeout_seconds": float(config.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
        # MCP tools can do anything, so they go through the same approval flow
        # as the built-in write/shell tools.
        "require_approval": _needs_approval,
    }
    if spec.transport == "stdio":
        params: dict[str, Any] = {
            "command": str(config["command"]),
            "args": [str(a) for a in config.get("args", [])],
            "cwd": str(config.get("cwd") or workspace),
        }
        if config.get("env"):
            # Extend rather than replace the environment so PATH etc. still work.
            params["env"] = {**os.environ, **{k: str(v) for k, v in config["env"].items()}}
        return MCPServerStdio(params=params, **common)  # type: ignore[arg-type]
    params = {"url": str(config["url"])}
    if config.get("headers"):
        params["headers"] = {k: str(v) for k, v in config["headers"].items()}
    cls = MCPServerSse if spec.transport == "sse" else MCPServerStreamableHttp
    return cls(params=params, **common)  # type: ignore[arg-type]
