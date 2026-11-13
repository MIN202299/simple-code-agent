# Nori

```
 /\_/\ ♪
( ^.^ )    Nori — your AI pair programmer in the terminal
 > ^ <~
```

**English** · [简体中文](README.zh-CN.md)

Nori is a coding agent for the terminal, built on the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python). Describe what you want in plain language and it reads code, edits files and runs commands for you. It works with OpenAI and with any OpenAI-compatible endpoint. The name comes from the small black cat that lives next to the input box.

## Features

- **Terminal UI** — pinned input box, streamed Markdown, tool calls with live status and diffs, approval dialogs, a `/` command panel and `@` file completion
- **Permission modes** — `default`, `accept-edits`, `plan` and `auto`, switched with Shift+Tab
- **Plan mode** — the agent researches read-only and proposes a plan; nothing changes until you approve it
- **Allow rules** — "don't ask again" for a command prefix, a host or a tool, remembered per project
- **Undo** — `/undo` reverts the file changes of the last turn
- **Context management** — configurable context window, `/compact`, and automatic compaction when it fills up
- **Sessions** — conversations are saved; continue the last one with `nori -c` or pick one with `/resume`
- **Sub-agents** — delegate research or independent subtasks to agents with their own context, in parallel
- **Web access** — `web_search` and `web_fetch`
- **Images** — mention an image file or paste one from the clipboard
- **Background commands** — start a dev server and keep working
- **`!command`** — run a shell command yourself and share its output with the agent
- **Skills** and **MCP servers**
- **Model switching** — a `/model` picker, with per-model context windows
- **English and Chinese interface**
- **A pet** that reacts to what the agent is doing and levels up with you
- **Standalone executables** for Linux, macOS and Windows; Homebrew formula

## Install

**Homebrew** (macOS on Apple silicon, Linux x86_64):

```bash
brew tap min202299/nori https://github.com/MIN202299/simple-code-agent
brew install min202299/nori/nori
```

**Download** — get the file for your system from [Releases](https://github.com/MIN202299/simple-code-agent/releases). No Python needed.

- `nori-<os>-<arch>.tar.gz` (`.zip` on Windows) unpacks to a `nori/` folder; run `nori/nori`. This is the faster one to start.
- `nori-<os>-<arch>` is a single executable. Rename it to `nori`, `chmod +x` it and put it on your `PATH`. It unpacks itself on every start, which costs a few seconds.
- On macOS, a file downloaded with a browser is quarantined: run `xattr -dr com.apple.quarantine <file or folder>` once.

**From source** (Python 3.11+):

```bash
pip install -e .      # or: uv pip install -e .   ·   pipx install .
```

Then configure it once:

```bash
nori --setup          # asks for base URL, API key, model and context window
```

## Usage

```bash
nori                              # open the UI in the current directory
nori -C path/to/project           # work in another directory
nori -c                           # continue the last session here
nori "add unit tests for utils.py"   # run one task and exit (plain output, good for scripts)
nori --mode plan                  # start in plan mode
nori -m other-model -y            # another model for this run, approving everything
```

| Option | Meaning |
| --- | --- |
| `-m, --model` | Model name; overrides the config file |
| `--base-url` | API base URL; overrides the config file |
| `--api {responses,chat}` | Which API to use |
| `--mode MODE` | Permission mode: `default`, `accept-edits`, `plan` or `auto` |
| `-y, --yes` | Same as `--mode auto`: approve everything |
| `-c, --continue` | Continue the most recent session in this workspace |
| `-r, --resume ID` | Resume a session by ID (a prefix is enough) |
| `--list-sessions` | List the saved sessions of this workspace and exit |
| `--no-save` | Do not save this session |
| `-C, --cwd` | Workspace directory (default: current directory) |
| `--context-window TOKENS` | Context window of the model, e.g. `200k` (default 128000) |
| `--no-auto-compact` | Do not compact automatically |
| `--max-turns N` | Maximum model turns per task (UI: unlimited; one-shot and plain mode: 200) |
| `--lang LANG` | Interface language: `en` or `zh` |
| `--plain` | Plain line-by-line output instead of the UI |
| `--no-mcp` / `--no-skills` | Do not load MCP servers / skills |
| `--config PATH` | Config file path |
| `--setup` | Create or edit the config file, then exit |

When standard input or output is not a terminal (a pipe, for example), plain mode is used automatically.

## Configuration

Settings live in `~/.nori/config.json`:

```json
{
  "base_url": "https://your-provider.example/v1",
  "api_key": "sk-...",
  "model": "your-model-name",
  "context_window": 128000,
  "language": "en"
}
```

| Key | Meaning |
| --- | --- |
| `api_key` | API key. May be written as `${ENV_VAR}` to read it from the environment |
| `base_url` | API base URL; omit for the official OpenAI endpoint |
| `model` | Model name; omit for the SDK's default |
| `api` | `responses` or `chat`. Default: `chat` when `base_url` is not OpenAI's, otherwise `responses` |
| `context_window` | Context window of the model in tokens (default 128000) |
| `auto_compact` | Compact automatically when the context fills up (default `true`) |
| `models` | Models offered by `/model`: names, or `{"name": ..., "context_window": ...}` |
| `mode` | Permission mode to start in (default `default`) |
| `language` | `en` (default) or `zh` |
| `retries` | Retries of a model request after a transient error (default 4, `0` turns it off) |
| `max_turns` | Maximum model turns per task |
| `search_engine` | `duckduckgo` (default) or `bing` |

Precedence, highest first: command-line options, `NORI_*` environment variables, the config file, then the generic `OPENAI_API_KEY` / `OPENAI_BASE_URL`. The config file outranks `OPENAI_*` because it is written for this tool, while those are shared by every OpenAI client.

Only the user-level file is read. A project-level file is deliberately not supported: a repository that could set `base_url` could redirect your API key.

| Variable | Meaning |
| --- | --- |
| `NORI_CONFIG` | Config file path |
| `NORI_MODEL`, `NORI_API`, `NORI_LANG` | Override the corresponding settings |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Used when the config file has no such setting |
| `NORI_TRACING` | Set to `1` to enable the SDK's tracing, which uploads run data to OpenAI (off by default) |

## The interface

```
❯ fix the bug in calc.py

● Read(calc.py)
  ⎿  Read 2 lines

● Update(calc.py)
     -    return a - b
     +    return a + b
  ⎿  Edited calc.py (1 replacement)  +1 -1

● The minus sign was wrong; it is a plus now.

  =^.^= Done · 6s · 2 tool calls · ↑ 3.1k ↓ 212

──────────────────────────────────────────────  /\_/\
❯ Describe what you want to do…                ( o.o )
──────────────────────────────────────────────  > ^ <
  ? shortcuts · / commands · @ files     your-model · context 3.3k (3%) · ⎇ main
```

The UI is drawn inline, not on the alternate screen: the transcript goes into your terminal's normal scrollback, so you can scroll, select and copy as usual.

| Key | Action |
| --- | --- |
| `Enter` | Send |
| `Alt+Enter` / `Ctrl-J` | New line |
| `Esc` | Interrupt the running task; close a menu |
| `Shift+Tab` | Switch permission mode |
| `/` | Command panel: fuzzy matching, `Tab` completes, `Enter` runs |
| `@` | File menu: fuzzy path matching |
| `!` | Run a shell command yourself |
| `Ctrl-V` | Paste an image from the clipboard |
| `?` | Show shortcuts (when the input is empty) |
| `↑` / `↓` | Input history; move in menus and dialogs |
| `Ctrl-C` | Interrupt; when idle, clear the input, then quit on the second press |
| `Ctrl-D` | Quit |
| `Ctrl-L` | Clear the screen |

You can keep typing while the agent works: messages are queued and run in order. After an interruption the finished steps stay in context, so "continue" picks up where it stopped.

| Command | Action |
| --- | --- |
| `/help` | Show help |
| `/model [name]` | Switch the model; without a name, opens a picker |
| `/mode [mode]` | Switch the permission mode; without a name, opens a picker |
| `/clear` | Start a new conversation (the old one can still be resumed) |
| `/compact [focus]` | Summarise the conversation to free up context |
| `/resume [number]` | Resume an earlier session; without a number, opens a picker |
| `/undo` | Revert the file changes of the last turn |
| `/context [size]` | Show or set the context window, e.g. `/context 200k` |
| `/permissions` | Show or remove this project's allow rules |
| `/tasks` | List background commands |
| `/status` | Show model, endpoint, mode and usage |
| `/skills`, `/<skill> [request]` | List skills; use one directly |
| `/mcp` | List connected MCP servers and their tools |
| `/lang [en\|zh]` | Switch the interface language |
| `/pet` | Meet your pet |
| `/yes` | Toggle auto mode |
| `/exit` | Quit |

## Permissions

By default Nori asks before it changes a file, runs a command, fetches a URL or calls an MCP tool. Reading and searching inside the workspace never asks.

### Modes

| Mode | File edits | Commands and other actions |
| --- | --- | --- |
| `default` | ask | ask |
| `accept-edits` | allowed | ask |
| `plan` | refused | ask |
| `auto` | allowed | allowed |

`Shift+Tab` steps through `default → accept-edits → plan`. `auto` is reached with `/mode auto`, `/yes`, `--mode auto` or `-y`; it is not in the Shift+Tab cycle so it cannot be switched on by accident. The current mode is shown in the footer and in the colour of the input box.

### Plan mode

In plan mode the agent can read, search and browse, but file edits are refused. When it understands the task it shows you a plan and asks:

```
 Plan ready
 Carry out this plan?
 ❯ 1. Yes, and accept file edits without asking
   2. Yes, and ask before each edit
   3. No, keep planning (esc)
```

Approving leaves plan mode and the agent starts implementing; declining keeps it planning, and you can tell it what to change. Start in plan mode with `nori --mode plan`, `/plan`, or Shift+Tab.

### Allow rules

Every approval dialog offers "don't ask again", which adds a rule for this project:

| What you approved | Rule | Covers |
| --- | --- | --- |
| `git status --short` | `git status` | commands starting with `git status` |
| `python -m pytest -q` | `python -m pytest` | that module, any arguments |
| a file edit | `Update` / `Write` | every call of that tool |
| fetching `https://docs.python.org/3/` | `docs.python.org` | that host |

- A rule never matches a compound command (`;`, `&&`, `|`, redirection, `$(...)`), so `git status; rm -rf .` still asks.
- No rule is offered for commands that are destructive or can run anything (`rm`, `sudo`, `curl`, `python -c`, ...). Those ask every time.
- Rules are stored in `~/.nori/permissions.json`, keyed by project path — not in the project, so cloning a repository cannot grant itself permissions.
- `/permissions` lists them; `/permissions remove <n>` and `/permissions clear` delete them.

## Undo

`/undo` restores the files changed in the most recent turn that changed any: edited files get their previous content back and files that turn created are deleted. Run it again to go back one more turn. The agent is told, so it does not assume its edits are still in place.

Only changes made through the edit tools are tracked. What a shell command did (`rm`, `git checkout`, a code generator) cannot be undone this way, and checkpoints are kept in memory, so they are gone when you quit. For anything important, use git.

## Context management

A model can only see so much at once. Nori tracks how full the context is and compacts before it overflows.

- **Window size** — tell Nori how big your model's window is: in `nori --setup`, with `/context 200k`, with `"context_window"` in the config file, or with `--context-window`. Sizes can be written as `128000`, `200k` or `1m`. The footer shows usage, e.g. `context 34k (27%)`.
- **Automatic compaction** — at 80% of the window, the older part of the conversation is replaced by a summary written by the model, and the most recent part (up to 20k tokens, at most 20% of the window) is kept verbatim. This also happens in the middle of a long task, which then simply continues.
- **Manual compaction** — `/compact`, optionally with what to keep: `/compact keep the migration progress`.
- Compaction costs one extra model call, and a summary cannot keep every detail. Turn the automatic part off with `--no-auto-compact` or `"auto_compact": false`.
- If the provider does not report token usage, sizes are estimated from the text and are approximate.

## Sessions

Each conversation is saved to `~/.nori/sessions/` after every turn, including interrupted ones.

```bash
nori -c                   # continue the most recent session in this workspace
nori --list-sessions      # list them
nori -r 20261112-0813     # resume by ID; a prefix is enough
```

In the UI, `/resume` opens a list to pick from. Resuming restores the conversation, not the files: what the model remembers about your code may be out of date. Session files are plain text and contain whatever the model read; use `--no-save` to keep nothing.

## Models

`/model` opens a picker with the models from your config file plus the ones your endpoint lists. Type to filter, or type a name that is not in the list and press Enter to use it. The choice is saved as the default.

Models with different context windows can be given their own, and the window follows the model when you switch:

```json
{
  "model": "fast-model",
  "context_window": 128000,
  "models": ["fast-model", { "name": "big-model", "context_window": 200000 }]
}
```

## Sub-agents

The agent can hand a task to a sub-agent, which works in a separate context and returns only its final report. That keeps long searches out of the main conversation.

| Type | Can do | Good for |
| --- | --- | --- |
| `explore` | read, search, browse | finding things in a large codebase, research |
| `general` | everything | independent subtasks |

Several sub-agents started in one reply run in parallel. Their actions go through the same approvals, they compact their own context, and they cannot start sub-agents themselves.

## Web access, images and shell

- **Web** — `web_search` returns titles, links and snippets; `web_fetch` reads a page as text. Fetching asks first, with "don't ask again for this host". Search scrapes a public search page (DuckDuckGo by default, Bing as a fallback), so it can break when those pages change.
- **Images** — mention an image in your message (`@screenshot.png`, a path, or a file dragged into the terminal) and it is sent along. `Ctrl-V` pastes an image from the clipboard. PNG, JPEG, GIF and WebP up to 5 MB; the model must support image input.
- **`!command`** — runs the command directly, shows the output, and adds both to the conversation so the agent can see them. Nothing is asked, since you typed it.
- **Background commands** — the agent can start a long-running command (a dev server, a watcher) in the background, read its output later and stop it. The footer shows how many are running, `/tasks` lists them, and they are stopped when you quit.

## Skills

A skill is a directory with a `SKILL.md`: front matter with a name and description, then instructions. It can carry scripts and templates.

```markdown
---
name: review
description: Review code against the team checklist. Use when asked to review code.
---
1. Read checklist.md (next to this file)
2. Check the changes item by item and report
```

Only names and descriptions go into the system prompt; the agent loads the full text when a task matches. You can also invoke one yourself: `/review focus on auth.py`.

Searched, first match wins: `<workspace>/.agents/skills/`, `<workspace>/.claude/skills/`, `~/.nori/skills/`, `~/.agents/skills/`, `~/.claude/skills/`.

## MCP

Configure servers in `<workspace>/.mcp.json` (project) or `~/.nori/mcp.json` (user), in the usual `mcpServers` format:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "env": { "API_KEY": "${MY_API_KEY}" }
    },
    "remote": { "type": "http", "url": "https://example.com/mcp" }
  }
}
```

An entry with `command` is a stdio server; one with `url` uses Streamable HTTP, or SSE with `"type": "sse"`. `${VAR}` is expanded from the environment. A project's `.mcp.json` can start arbitrary commands, so Nori asks whether you trust it before starting those servers.

## Language

The interface is in English by default and can be switched to Chinese: `nori --lang zh`, `NORI_LANG=zh`, `"language": "zh"`, or `/lang` in the UI. This only affects Nori's own text; the model answers in the language you write in.

## The pet

Nori sits next to the input box and reacts to what is going on: thinking `( o.O )`, working `( >.< )`, waiting for your approval with a `!`, pleased `( ^.^ )` when a task is done, `( x.x )` on an error, and asleep when you have been away. Every finished task earns experience. `/pet` shows its level, `/pet name Mochi` renames it, `/pet hide` hides it.

## Reliability

- Model requests that fail with a rate limit, an overloaded server or a network error are retried with exponential backoff (`retries`, default 4).
- When a task hits the turn limit or is interrupted, the work done so far stays in context.
- A failed summary never fails the task: it continues with the full context.

## Security notes

- File tools are confined to the workspace. `run_shell` is not — it runs arbitrary commands, which is why it asks.
- `auto` mode and `-y` skip every confirmation, including whether to start a project's MCP servers. Use them only in projects and environments you trust.
- Skills and fetched web pages are text the model reads and may act on. Use skills you trust and stay in a mode that asks when browsing unknown sites.
- The API key in the config file is stored in plain text (file mode 600). Write `"api_key": "${MY_KEY}"` to keep it out of the file.

## Building executables

```bash
pip install -e ".[build]"
python scripts/build.py                          # one file: dist/nori
python scripts/build.py --archive nori-linux-x86_64   # folder build, packed as dist/<name>.tar.gz
```

PyInstaller cannot cross-compile, so each platform is built on that platform. The release workflow (`.github/workflows/release.yml`) does this for Linux, macOS and Windows, publishes a GitHub Release, updates the Homebrew formula and then installs it with Homebrew as a check. Trigger it by pushing a `v*` tag, or run it manually with a tag name.

## Development

```bash
pip install -e ".[dev]"
pytest
```

```
src/nori/
  cli.py          argument parsing; kept light so --help and --version are instant
  app.py          wires everything together and starts the chosen front end
  tui.py          the terminal UI (prompt_toolkit)
  session.py      the conversation: run loop, approvals, compaction guard, sub-agents
  permissions.py  modes and allow rules
  tools.py        the agent's tools
  commands.py     slash commands and pickers
  compact.py      summarising history
  checkpoints.py  file snapshots for /undo
  shelltasks.py   background commands
  web.py          fetching pages and searching
  attachments.py  images
  store.py        saved sessions
  config.py       config file and setup wizard
  i18n.py         interface language: English keys, Chinese catalog
  render.py       how tool calls, results and Markdown are shown
  pet.py          the pet
  skills.py, mcp_config.py, agent.py, branding.py, util.py
tests/            the real SDK run loop driven by a scripted model; the UI is tested headlessly
```

To rebrand, change `src/nori/branding.py` and the names in `pyproject.toml`.
