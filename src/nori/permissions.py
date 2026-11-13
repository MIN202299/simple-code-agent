"""Permission modes and allow rules: what the agent may do without asking.

Modes
    default       ask before file changes, commands and other side effects
    accept-edits  file edits are allowed; commands still ask
    plan          read-only: file edits are refused, the agent proposes a plan
    auto          file edits and low-risk commands are allowed; anything else asks
    bypass        everything is allowed without asking

Rules are per-workspace allowances the user granted with "don't ask again".
They are stored in the user's own directory (`~/.nori/permissions.json`), never
in the project, so cloning a repository can not grant itself permissions.

    write_file                 any call of that tool
    run_shell:git status       shell commands starting with `git status`
    web_fetch:example.com      fetching from that host
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .branding import app_dir
from .risk import Risk, assess_shell

MODES = ("default", "accept-edits", "plan", "auto", "bypass")
# What Shift+Tab steps through. `bypass` is left out so it is never reached by accident.
MODE_CYCLE = ("default", "accept-edits", "auto", "plan")
EDIT_TOOLS = frozenset({"write_file", "edit_file"})
# Tools that are part of planning itself and never need approval.
# Reasons auto mode asks that are not about the command text (English, translated on display).
BACKGROUND = "starts a command that keeps running in the background"
OTHER_TOOL = "only file edits and low-risk commands run without asking"
PLAN_REJECTION = (
    "Plan mode is on: files must not be changed yet. Finish your research, "
    "then call present_plan with your plan and wait for the user's approval."
)

# Commands whose first two words identify what they do (`git status`, `npm test`).
_SUBCOMMAND_TOOLS = frozenset(
    "git npm pnpm yarn cargo go docker kubectl pip pip3 uv poetry make brew apt apt-get "
    "dotnet mvn gradle gh terraform helm conda bundle rake composer".split()
)
# Interpreters run arbitrary code, so a rule must also name the script or module.
_INTERPRETERS = frozenset("python python3 node ruby perl php deno bun npx bash sh zsh".split())
# Never offered as a standing rule: too destructive, or a way to run anything.
_NEVER_RULE = frozenset(
    "rm sudo su dd mkfs chmod chown kill pkill killall shutdown reboot curl wget ssh scp "
    "eval exec source mv xargs env nohup doas".split()
)
_SHELL_OPERATORS = re.compile(r"[;&|<>`\n]|\$\(")
_DANGEROUS_ARGUMENTS = frozenset({"-exec", "-execdir", "-delete", "-ok", "-okdir"})


def _words(command: str) -> list[str] | None:
    """Split a simple command into words; None if it is not a single plain command."""
    if _SHELL_OPERATORS.search(command):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words or "=" in words[0] or _DANGEROUS_ARGUMENTS.intersection(words):
        return None
    return words


def shell_prefix(command: str) -> str | None:
    """The command prefix a "don't ask again" rule would cover, if one is safe to offer."""
    words = _words(command)
    if words is None or words[0] in _NEVER_RULE:
        return None
    first = words[0]
    if first in _INTERPRETERS:
        if len(words) >= 3 and words[1] == "-m":
            return " ".join(words[:3])  # python -m pytest
        if len(words) >= 2 and not words[1].startswith("-"):
            return " ".join(words[:2])  # node build.js
        return None  # python -c "...": arbitrary code
    if first in _SUBCOMMAND_TOOLS and len(words) >= 2 and not words[1].startswith("-"):
        return " ".join(words[:2])
    return first


def rule_for(tool: str, args: dict[str, Any]) -> str | None:
    """The rule that would allow this call from now on, or None if none is offered."""
    if tool == "run_shell":
        prefix = shell_prefix(str(args.get("command", "")))
        return f"run_shell:{prefix}" if prefix else None
    if tool == "web_fetch":
        host = urlparse(str(args.get("url", ""))).hostname
        return f"web_fetch:{host}" if host else None
    return tool or None


def rule_matches(rule: str, tool: str, args: dict[str, Any]) -> bool:
    name, _, detail = rule.partition(":")
    if name != tool:
        return False
    if not detail:
        return True
    if tool == "run_shell":
        command = str(args.get("command", "")).strip()
        if _words(command) is None:
            return False
        return command == detail or command.startswith(detail + " ")
    if tool == "web_fetch":
        return urlparse(str(args.get("url", ""))).hostname == detail
    return False


def describe_rule(rule: str) -> str:
    """A rule in words for display, e.g. `git status` commands."""
    name, _, detail = rule.partition(":")
    return detail if detail else name


def rules_path(home: Path | None = None) -> Path:
    return app_dir(home) / "permissions.json"


class Permissions:
    """The current mode plus the allow rules of one workspace."""

    def __init__(
        self, workspace: Path, mode: str = "default", path: Path | None = None
    ) -> None:
        self.workspace = str(workspace)
        self.mode = mode if mode in MODES else "default"
        self.path = path  # None: rules live only as long as this object
        self.rules: list[str] = []
        self._previous = "default"  # the mode to return to when leaving plan mode
        self.load()

    # ------------------------------------------------------------------ modes

    def set_mode(self, mode: str) -> bool:
        if mode not in MODES:
            return False
        if mode == "plan" and self.mode != "plan":
            self._previous = self.mode if self.mode != "bypass" else "default"
        self.mode = mode
        return True

    def cycle(self) -> str:
        """Step to the next mode (Shift+Tab)."""
        index = MODE_CYCLE.index(self.mode) if self.mode in MODE_CYCLE else -1
        self.set_mode(MODE_CYCLE[(index + 1) % len(MODE_CYCLE)])
        return self.mode

    # -------------------------------------------------------------- decisions

    def decide(self, tool: str, args: dict[str, Any] | None = None) -> str:
        """`allow`, `ask` or `deny` for one tool call."""
        args = args or {}
        if self.mode == "bypass":
            return "allow"
        if tool in EDIT_TOOLS:
            if self.mode == "plan":
                return "deny"
            if self.mode in ("accept-edits", "auto"):
                return "allow"
        if self.mode == "auto" and tool == "run_shell" and self.shell_risk(args).low:
            return "allow"
        if any(rule_matches(rule, tool, args) for rule in self.rules):
            return "allow"
        return "ask"

    def shell_risk(self, args: dict[str, Any]) -> Risk:
        """How risky a run_shell call is, as auto mode judges it."""
        if args.get("background"):
            return Risk("unknown", BACKGROUND)  # keeps running unattended
        return assess_shell(str(args.get("command", "")), Path(self.workspace))

    def auto_reason(self, tool: str, args: dict[str, Any] | None = None) -> str:
        """Why auto mode is asking about this call ("" outside auto mode)."""
        if self.mode != "auto":
            return ""
        if tool == "run_shell":
            return self.shell_risk(args or {}).reason
        return OTHER_TOOL

    # ------------------------------------------------------------------ rules

    def add_rule(self, rule: str) -> None:
        if rule and rule not in self.rules:
            self.rules.append(rule)
            self.save()

    def remove_rule(self, index: int) -> str | None:
        if not 0 <= index < len(self.rules):
            return None
        rule = self.rules.pop(index)
        self.save()
        return rule

    def clear_rules(self) -> None:
        self.rules = []
        self.save()

    def _read_all(self) -> dict[str, list[str]]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def load(self) -> None:
        rules = self._read_all().get(self.workspace, [])
        self.rules = [rule for rule in rules if isinstance(rule, str)] if isinstance(rules, list) else []

    def save(self) -> None:
        if self.path is None:
            return
        data = self._read_all()
        if self.rules:
            data[self.workspace] = self.rules
        else:
            data.pop(self.workspace, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
        except OSError:
            pass  # a rule that could not be saved still applies to this session
