"""Risk assessment of shell commands, for auto mode.

In auto mode a command runs without asking only if it is recognised as
low-risk: it inspects things, or it is a routine development command that
stays inside the workspace and is easy to undo. Everything else still asks.

The assessment is deliberately conservative and purely static: a command is
low-risk only if every part of it is on a list of known-safe commands with
known-safe arguments. Anything not understood is "unknown", which asks. It is
a convenience, not a sandbox — it reads the command line, not what a script
or a test suite does once it runs.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

# Reasons, in English; they are translated when shown.
NOT_KNOWN = "not a known low-risk command"
DESTRUCTIVE = "destructive or hard to undo"
NETWORK = "sends or downloads data over the network"
INSTALLS = "installs packages, which runs their install scripts"
GIT_HISTORY = "changes git history, the working tree or the remote"
HIDDEN = "uses shell features that hide what will run"
REDIRECTS = "writes to a file through redirection"
OUTSIDE = "touches a path outside the workspace"
PRIVILEGED = "runs with elevated privileges or runs arbitrary code"
REASONS = (NOT_KNOWN, DESTRUCTIVE, NETWORK, INSTALLS, GIT_HISTORY, HIDDEN, REDIRECTS, OUTSIDE, PRIVILEGED)


@dataclass(frozen=True)
class Risk:
    level: str  # "low", "high" or "unknown"
    reason: str = ""

    @property
    def low(self) -> bool:
        return self.level == "low"


LOW = Risk("low")

# --- commands that only inspect ------------------------------------------------

READ_ONLY = frozenset(
    "ls ll la dir cat head tail wc pwd echo printf which whereis type whoami id date uname "
    "hostname file stat du df tree grep egrep fgrep rg ag ack sort uniq cut tr diff cmp comm "
    "basename dirname realpath readlink jq yq column nl tac rev fold fmt expand test true false "
    "sleep cd pushd popd ps uptime nproc arch locale tty cal seq md5sum sha1sum sha256sum "
    "shasum cksum strings hexdump xxd od bat less more man help".split()
)
# Read-only unless given one of these arguments.
RESTRICTED_ARGUMENTS = {
    "find": frozenset("-exec -execdir -delete -ok -okdir -fprint -fprintf -fls".split()),
    "sed": frozenset("-i --in-place".split()),
    "sort": frozenset("-o --output".split()),
    "tree": frozenset("-o".split()),
    "date": frozenset("-s --set".split()),
    "hostname": frozenset("-F --file".split()),
}

# --- git -----------------------------------------------------------------------

GIT_READ = frozenset(
    "status log diff show blame shortlog describe rev-parse rev-list ls-files ls-tree ls-remote "
    "cat-file grep reflog whatchanged name-rev merge-base count-objects var version help "
    "check-ignore fetch".split()
)
# Reversible, local, routine.
GIT_LOCAL = frozenset("add commit stash init mv".split())
# Listing forms of commands that can also change things.
GIT_LIST_ONLY = {
    "branch": frozenset("-a --all -r --remotes -v -vv --verbose --list -l --show-current --merged --no-merged --contains".split()),
    "tag": frozenset("-l --list -n --contains --sort".split()),
    "remote": frozenset("-v --verbose show get-url".split()),
    "config": frozenset("--get --get-all --list -l --show-origin --global --local --system".split()),
    "worktree": frozenset("list".split()),
}
GIT_STASH_SAFE = frozenset("list show push save".split())

# --- development commands that run project code inside the workspace ------------

DEV_TOOLS = frozenset(
    "pytest tox nox ruff black flake8 pylint mypy pyright isort bandit coverage pyflakes "
    "eslint prettier tsc jest vitest mocha stylelint biome "
    "rustfmt gofmt golangci-lint shellcheck hadolint yamllint markdownlint "
    "phpunit rspec rubocop ctest".split()
)
SUBCOMMAND_TOOLS = {
    "cargo": frozenset("check test build clippy fmt doc bench tree metadata".split()),
    "go": frozenset("test build vet fmt list version env doc".split()),
    "dotnet": frozenset("test build".split()),
    "mvn": frozenset("test compile verify validate".split()),
    "gradle": frozenset("test build check".split()),
    "make": frozenset("test tests check lint build fmt format typecheck all".split()),
    "just": frozenset("test check lint build fmt format".split()),
    "uv": frozenset("lock tree version".split()),
    "pip": frozenset("list show freeze check --version".split()),
    "pip3": frozenset("list show freeze check --version".split()),
    "npm": frozenset("test ls list outdated view --version".split()),
    "pnpm": frozenset("test ls list outdated --version".split()),
    "yarn": frozenset("test list outdated --version".split()),
    "bun": frozenset("test --version".split()),
    "docker": frozenset("ps images version info".split()),
    "kubectl": frozenset("version".split()),
}
# `npm run <script>` and friends: only scripts whose name says what they do.
SCRIPT_RUNNERS = frozenset("npm pnpm yarn bun".split())
SAFE_SCRIPTS = frozenset("test tests lint build typecheck type-check check format fmt compile".split())
# `python -m <module>` / `uv run <tool>` / `npx <tool>`: allowed for the tools above.
PYTHON = frozenset("python python3 py".split())
SAFE_MODULES = frozenset(
    "pytest unittest ruff black flake8 mypy pyright isort pylint coverage compileall py_compile "
    "json.tool pyflakes doctest".split()
)
RUNNERS = frozenset("npx pnpx bunx".split())
WORKSPACE_FILE_COMMANDS = frozenset("mkdir touch cp".split())

# --- commands named for a clearer reason when they ask -------------------------

HIGH = {
    **dict.fromkeys(
        "rm rmdir shred dd mkfs fdisk mkswap truncate mv chmod chown chgrp ln kill pkill killall "
        "shutdown reboot halt poweroff systemctl service launchctl crontab".split(),
        DESTRUCTIVE,
    ),
    **dict.fromkeys(
        "curl wget ssh scp sftp rsync nc ncat netcat telnet ftp aws gcloud az gh heroku vercel "
        "netlify fly".split(),
        NETWORK,
    ),
    **dict.fromkeys(
        "sudo su doas eval exec source sh bash zsh fish xargs env nohup osascript powershell".split(),
        PRIVILEGED,
    ),
    **dict.fromkeys("apt apt-get yum dnf brew pacman apk snap gem".split(), INSTALLS),
}
INSTALL_SUBCOMMANDS = frozenset("install i add ci update upgrade remove uninstall sync publish".split())
GIT_RISKY = frozenset(
    "push pull reset clean checkout switch restore rebase merge cherry-pick revert rm am apply "
    "gc prune filter-branch filter-repo submodule clone bisect".split()
)

# Substitution, here-documents, multiple lines, and variables: a variable can
# expand to any path or command, so its use cannot be judged from the text.
_HIDDEN = re.compile(r"`|\$|<\(|>\(|<<|\n")
# Redirections that do not write a file anyone cares about.
_SAFE_REDIRECT = re.compile(r"\s*(?:[12]?>>?\s*/dev/null|&>\s*/dev/null|[12]>&[12])")
_OPERATORS = frozenset({"&&", "||", ";", "|"})
_WRAPPERS = frozenset("time nice command builtin".split())
_SAFE_ABSOLUTE = ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr")


def _segments(command: str) -> list[list[str]] | Risk:
    """Split a command line into simple commands, or say why it cannot be trusted."""
    if _HIDDEN.search(command):
        return Risk("unknown", HIDDEN)
    command = _SAFE_REDIRECT.sub("", command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return Risk("unknown", HIDDEN)
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _OPERATORS:
            segments.append([])
        elif token and all(char in "<>" for char in token) or token in (">&", "&>", ">|"):
            return Risk("unknown", REDIRECTS)
        elif token and all(char in "&|;()" for char in token):
            return Risk("unknown", HIDDEN)  # background jobs, subshells
        else:
            segments[-1].append(token)
    if any(not segment for segment in segments):
        return Risk("unknown", HIDDEN)
    return segments


def _outside(argument: str, workspace: Path) -> bool:
    """Whether a command argument names a path outside the workspace."""
    value = argument.split("=", 1)[1] if argument.startswith("-") and "=" in argument else argument
    if not value or value.startswith("-"):
        return False
    if value in _SAFE_ABSOLUTE:
        return False
    if value.startswith("~"):
        return True
    if "://" in value:
        return False  # a URL, not a path; network commands are judged by name
    path = Path(value)
    if not path.is_absolute() and ".." not in path.parts:
        return False
    try:
        resolved = (workspace / path).resolve()
    except OSError:
        return True
    return resolved != workspace and workspace not in resolved.parents


def _git(arguments: list[str]) -> Risk:
    # Skip global options such as `-C dir` or `--no-pager` to find the subcommand.
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        index += 2 if arguments[index] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    if index >= len(arguments):
        return LOW  # bare `git` prints help
    subcommand, rest = arguments[index], arguments[index + 1 :]
    if subcommand in GIT_READ:
        return LOW
    if subcommand == "stash":
        return LOW if not rest or rest[0] in GIT_STASH_SAFE else Risk("high", GIT_HISTORY)
    if subcommand in GIT_LOCAL:
        if subcommand == "commit" and any(flag in rest for flag in ("--amend", "--no-verify", "-n")):
            return Risk("high", GIT_HISTORY)
        return LOW
    if subcommand in GIT_LIST_ONLY:
        allowed = GIT_LIST_ONLY[subcommand]
        flags = [argument for argument in rest if argument.startswith("-")]
        words = [argument for argument in rest if not argument.startswith("-")]
        if subcommand in ("remote", "worktree"):
            return LOW if not rest or rest[0] in allowed else Risk("high", GIT_HISTORY)
        if subcommand == "config":
            return LOW if flags and all(flag in allowed for flag in flags) and len(words) <= 1 else Risk("high", GIT_HISTORY)
        # branch / tag: listing takes no positional name unless a listing flag is given
        if all(flag.split("=")[0] in allowed for flag in flags) and (not words or flags):
            return LOW
        return Risk("high", GIT_HISTORY)
    if subcommand in GIT_RISKY:
        return Risk("high", GIT_HISTORY)
    return Risk("unknown", NOT_KNOWN)


def _simple(words: list[str], workspace: Path) -> Risk:
    """Assess one simple command (no pipes or operators)."""
    while words and words[0] in _WRAPPERS:
        words = words[1:]
    if words and words[0] == "timeout" and len(words) > 2:
        words = words[2:]
    if not words:
        return Risk("unknown", HIDDEN)
    name, arguments = words[0], words[1:]
    if "=" in name:
        return Risk("unknown", HIDDEN)  # VAR=value command
    name = name.rsplit("/", 1)[-1] if name.startswith("./node_modules/.bin/") else name
    if "/" in name:
        return Risk("unknown", NOT_KNOWN)  # a script or a binary by path

    if any(_outside(argument, workspace) for argument in arguments):
        return Risk("unknown", OUTSIDE)
    first = arguments[0] if arguments else ""

    if name in HIGH:
        return Risk("high", HIGH[name])
    if name == "git":
        return _git(arguments)
    if name in READ_ONLY:
        banned = RESTRICTED_ARGUMENTS.get(name, frozenset())
        if any(argument.split("=")[0] in banned for argument in arguments):
            return Risk("high", DESTRUCTIVE)
        return LOW
    if name in ("sed", "find", "awk"):
        if name == "awk":
            return Risk("unknown", NOT_KNOWN)  # awk can run commands
        banned = RESTRICTED_ARGUMENTS[name]
        if any(argument.split("=")[0] in banned or argument.startswith("-i") and name == "sed" for argument in arguments):
            return Risk("high", DESTRUCTIVE)
        return LOW
    if name in DEV_TOOLS:
        return LOW
    if name in WORKSPACE_FILE_COMMANDS:
        return LOW  # arguments were already checked to be inside the workspace
    if name in SCRIPT_RUNNERS and first in ("run", "run-script") and len(arguments) > 1:
        return LOW if arguments[1] in SAFE_SCRIPTS else Risk("unknown", NOT_KNOWN)
    if name in SUBCOMMAND_TOOLS:
        if first in SUBCOMMAND_TOOLS[name]:
            return LOW
        if name == "uv" and first == "run" and len(arguments) > 1:
            return _simple(arguments[1:], workspace)
        if first in INSTALL_SUBCOMMANDS:
            return Risk("high", INSTALLS)
        return Risk("unknown", NOT_KNOWN)
    if name in PYTHON:
        if first in ("--version", "-V"):
            return LOW
        if first == "-m" and len(arguments) > 1:
            if arguments[1] in SAFE_MODULES:
                return LOW
            if arguments[1] == "pip":
                return _simple(["pip", *arguments[2:]], workspace)
        return Risk("unknown", NOT_KNOWN)  # runs a script or inline code
    if name in RUNNERS:
        tool = next((argument for argument in arguments if not argument.startswith("-")), "")
        return LOW if tool in DEV_TOOLS else Risk("unknown", NOT_KNOWN)
    if name in ("node", "ruby", "perl", "php", "deno"):
        return LOW if first in ("--version", "-v") else Risk("unknown", NOT_KNOWN)
    return Risk("unknown", NOT_KNOWN)


def assess_shell(command: str, workspace: Path) -> Risk:
    """How risky a shell command line is. Low only if every part of it is."""
    command = command.strip()
    if not command:
        return Risk("unknown", NOT_KNOWN)
    segments = _segments(command)
    if isinstance(segments, Risk):
        return segments
    worst = LOW
    for words in segments:
        risk = _simple(words, workspace)
        if risk.level == "high":
            return risk
        if not risk.low:
            worst = risk
    return worst
