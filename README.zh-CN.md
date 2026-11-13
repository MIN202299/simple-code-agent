# Nori

```
 /\_/\ ♪
( ^.^ )    Nori — 终端里的 AI 编程搭档
 > ^ <~
```

[English](README.md) · **简体中文**

Nori 是一个运行在终端里的 code agent，基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python)。用自然语言告诉它你想做什么，它来读代码、改文件、跑命令。支持 OpenAI 以及任何兼容 OpenAI 接口的服务。名字来自常驻输入框旁边的那只小黑猫。

## 功能

- **终端界面**：底部固定输入框，流式 Markdown，带实时状态和 diff 的工具调用，审批对话框，`/` 命令面板和 `@` 文件补全
- **权限模式**：`default`、`accept-edits`、`auto`、`plan`、`bypass`，用 Shift+Tab 切换
- **auto 模式**：文件修改和低风险命令直接执行，有风险的仍然询问
- **计划模式**：agent 只读调研并给出计划，你批准之前不会改动任何东西
- **放行规则**：对某个命令前缀、某个域名或某个工具选择"不再询问"，按项目记住
- **撤销**：`/undo` 撤销上一轮的文件改动
- **上下文管理**：可配置上下文窗口，`/compact` 手动压缩，快满时自动压缩
- **会话**：对话自动保存，`nori -c` 继续上一次，`/resume` 挑一个恢复
- **子 agent**：把调研或独立的子任务交给拥有独立上下文的子 agent，可并行
- **联网**：`web_search` 和 `web_fetch`
- **图片**：在消息里提到图片文件，或从剪贴板粘贴
- **后台命令**：启动开发服务器后继续干活
- **`!命令`**：自己直接执行 shell 命令，并把输出交给 agent
- **Skill** 与 **MCP 服务器**
- **模型切换**：`/model` 选择面板，每个模型可以有自己的上下文窗口
- **中英文界面**
- **宠物**：会随 agent 的状态变表情，陪你干活会升级
- **独立可执行程序**：Linux、macOS、Windows；支持 Homebrew 安装

## 安装

**Homebrew**（Apple 芯片的 macOS，x86_64 的 Linux）：

```bash
brew tap min202299/tap https://github.com/MIN202299/simple-code-agent
brew install nori
```

**下载**：到 [Releases](https://github.com/MIN202299/simple-code-agent/releases) 页面下载对应系统的文件，不需要 Python。

- `nori-<系统>-<架构>.tar.gz`（Windows 是 `.zip`）解压后是一个 `nori/` 文件夹，运行 `nori/nori`。这个版本启动更快。
- `nori-<系统>-<架构>` 是单个可执行文件。改名为 `nori`，`chmod +x` 后放进 `PATH`。它每次启动都要先解压，会慢几秒。
- macOS 上用浏览器下载的文件会被隔离，执行一次 `xattr -dr com.apple.quarantine <文件或文件夹>` 即可。

**从源码安装**（需要 Python 3.11+）：

```bash
pip install -e .      # 或: uv pip install -e .   ·   pipx install .
```

然后配置一次：

```bash
nori --setup          # 依次填写 base url、api key、模型和上下文窗口
```

## 使用

```bash
nori                              # 在当前目录打开界面
nori -C path/to/project           # 指定工作区
nori -c                           # 继续这里上一次的会话
nori "给 utils.py 补上单元测试"      # 执行一个任务后退出（逐行输出，适合脚本）
nori --mode plan                  # 以计划模式启动
nori --mode auto                  # 低风险操作不再询问
nori -m other-model -y            # 这一次换个模型，并全部自动批准
```

| 参数 | 说明 |
| --- | --- |
| `-m, --model` | 模型名，覆盖配置文件 |
| `--base-url` | API 地址，覆盖配置文件 |
| `--api {responses,chat}` | 使用哪种 API |
| `--mode MODE` | 权限模式：`default`、`accept-edits`、`auto`、`plan` 或 `bypass` |
| `-y, --yes` | 等同 `--mode bypass`：全部自动批准 |
| `-c, --continue` | 继续这个工作区最近的一次会话 |
| `-r, --resume ID` | 恢复指定的会话（ID 可只写开头几位） |
| `--list-sessions` | 列出这个工作区保存的会话后退出 |
| `--no-save` | 不保存本次会话 |
| `-C, --cwd` | 工作区目录，默认当前目录 |
| `--context-window TOKENS` | 模型的上下文窗口大小，如 `200k`，默认 128000 |
| `--no-auto-compact` | 不自动压缩 |
| `--max-turns N` | 单个任务的最大模型轮数（界面里默认不限；一次性执行和简单模式默认 200） |
| `--lang LANG` | 界面语言：`en` 或 `zh` |
| `--plain` | 不用界面，改用逐行输出的简单模式 |
| `--no-mcp` / `--no-skills` | 不加载 MCP 服务器 / skill |
| `--config PATH` | 配置文件路径 |
| `--setup` | 创建或修改配置文件后退出 |

标准输入或输出不是终端时（例如管道），自动使用简单模式。

## 配置

配置保存在 `~/.nori/config.json`：

```json
{
  "base_url": "https://your-provider.example/v1",
  "api_key": "sk-...",
  "model": "your-model-name",
  "context_window": 128000,
  "language": "zh"
}
```

| 字段 | 说明 |
| --- | --- |
| `api_key` | API Key。可以写成 `${ENV_VAR}` 从环境变量读取 |
| `base_url` | API 地址；省略则使用 OpenAI 官方地址 |
| `model` | 模型名；省略则使用 SDK 默认模型 |
| `api` | `responses` 或 `chat`。省略时：`base_url` 不是 OpenAI 的用 `chat`，否则用 `responses` |
| `context_window` | 模型的上下文窗口大小（token 数），默认 128000 |
| `auto_compact` | 上下文快满时是否自动压缩，默认 `true` |
| `models` | `/model` 面板里列出的模型：模型名，或 `{"name": ..., "context_window": ...}` |
| `mode` | 启动时的权限模式，默认 `default` |
| `language` | `en`（默认）或 `zh` |
| `retries` | 模型请求遇到临时性错误时的重试次数，默认 4，`0` 为关闭 |
| `max_turns` | 单个任务的最大模型轮数 |
| `search_engine` | `duckduckgo`（默认）或 `bing` |

优先级从高到低：命令行参数、`NORI_*` 环境变量、配置文件、通用的 `OPENAI_API_KEY` / `OPENAI_BASE_URL`。配置文件优先于 `OPENAI_*`，因为它是专门为本工具写的，而后者是所有 OpenAI 客户端共用的。

只读取用户级配置，不支持项目级配置：否则一个仓库就能通过改 `base_url` 把你的 Key 发到别处。

| 变量 | 说明 |
| --- | --- |
| `NORI_CONFIG` | 配置文件路径 |
| `NORI_MODEL`、`NORI_API`、`NORI_LANG` | 覆盖对应的配置项 |
| `OPENAI_API_KEY`、`OPENAI_BASE_URL` | 配置文件里没有对应项时使用 |
| `NORI_TRACING` | 设为 `1` 时开启 SDK 的 tracing，会把运行数据上传到 OpenAI（默认关闭） |

## 界面

```
❯ 修复 calc.py 里的 bug

● Read(calc.py)
  ⎿  Read 2 lines

● Update(calc.py)
     -    return a - b
     +    return a + b
  ⎿  Edited calc.py (1 replacement)  +1 -1

● 减号写错了，已经改成加号。

  =^.^= 完成 · 6s · 2 次工具调用 · ↑ 3.1k ↓ 212

──────────────────────────────────────────────  /\_/\
❯ 描述你想做的事…                              ( o.o )
──────────────────────────────────────────────  > ^ <
  ? 快捷键 · / 命令 · @ 文件       your-model · 上下文 3.3k (3%) · ⎇ main
```

界面是内联绘制的，不占用备用屏幕：对话记录直接进入终端的滚动缓冲区，可以照常滚动、选择和复制。

| 按键 | 作用 |
| --- | --- |
| `Enter` | 发送 |
| `Alt+Enter` / `Ctrl-J` | 换行 |
| `Esc` | 中断正在执行的任务；关闭菜单 |
| `Shift+Tab` | 切换权限模式 |
| `/` | 命令面板：模糊匹配，`Tab` 补全，`Enter` 执行 |
| `@` | 文件菜单，可在文字中任意位置使用：按文件名和路径模糊匹配 |
| `!` | 自己直接执行 shell 命令 |
| `Ctrl-V` | 从剪贴板粘贴图片 |
| `?` | 显示快捷键（输入框为空时） |
| `↑` / `↓` | 历史输入；在菜单和对话框中移动选择 |
| `Ctrl-C` | 中断任务；空闲时清空输入，再按一次退出 |
| `Ctrl-D` | 退出 |
| `Ctrl-L` | 清屏 |

agent 工作时可以继续输入，消息会排队依次执行。中断后已完成的步骤保留在上下文里，说一句"继续"就能接着做。

| 命令 | 作用 |
| --- | --- |
| `/help` | 显示帮助 |
| `/model [name]` | 切换模型；不带名称则打开选择面板 |
| `/mode [mode]` | 切换权限模式；不带名称则打开选择面板 |
| `/clear` | 开始新对话（旧对话仍可恢复） |
| `/compact [说明]` | 把对话压缩成摘要以释放上下文 |
| `/resume [序号]` | 恢复之前的会话；不带序号则打开选择面板 |
| `/undo` | 撤销上一轮的文件改动 |
| `/context [大小]` | 查看或设置上下文窗口，如 `/context 200k` |
| `/permissions` | 查看或移除本项目的放行规则 |
| `/tasks` | 列出后台命令 |
| `/status` | 查看模型、接口、模式和用量 |
| `/skills`、`/<skill> [要求]` | 列出 skill；直接使用某个 skill |
| `/mcp` | 列出已连接的 MCP 服务器及其工具 |
| `/lang [en\|zh]` | 切换界面语言 |
| `/pet` | 看看你的宠物 |
| `/yes` | 开关 bypass 模式 |
| `/exit` | 退出 |

## 权限

默认情况下，Nori 在修改文件、执行命令、抓取网页或调用 MCP 工具之前都会询问。在工作区内读文件和搜索从不询问。

### 模式

| 模式 | 文件修改 | shell 命令 | 抓取网页、MCP 工具 |
| --- | --- | --- | --- |
| `default` | 询问 | 询问 | 询问 |
| `accept-edits` | 直接允许 | 询问 | 询问 |
| `auto` | 直接允许 | 低风险的直接允许，其余询问 | 询问 |
| `plan` | 拒绝 | 询问 | 询问 |
| `bypass` | 直接允许 | 直接允许 | 直接允许 |

`Shift+Tab` 在 `default → accept-edits → auto → plan` 之间循环。`bypass` 要通过 `/mode bypass`、`/yes`、`--mode bypass` 或 `-y` 进入；它不在 Shift+Tab 的循环里，避免误触开启。当前模式显示在底栏，输入框边线的颜色也会随之变化。

### auto 模式

auto 模式让 agent 自己把日常工作往下做，但遇到可能造成损害的操作仍然停下来问你。文件修改直接生效（它们限制在工作区内，`/undo` 可以撤销）；shell 命令被识别为低风险时直接执行：

| 直接执行 | 例子 |
| --- | --- |
| 查看类 | `ls`、`cat`、`grep`、`find`、`git status`、`git diff`、`git log` |
| 测试、lint、类型检查、构建 | `pytest`、`ruff check`、`mypy`、`tsc`、`npm test`、`npm run lint`、`cargo test`、`go vet`、`make test` |
| 本地、可撤销的 git 操作 | `git add`、`git commit`、`git stash` |
| 在工作区内创建东西 | `mkdir`、`touch`、`cp` |

其余的都会询问，对话框里会说明原因：

| 仍然询问 | 例子 |
| --- | --- |
| 有破坏性或难以撤销 | `rm`、`mv`、`chmod`、`kill`、`sed -i`、`find -delete` |
| 访问网络 | `curl`、`wget`、`ssh`、`gh` |
| 安装软件包 | `npm install`、`pip install`、`brew install` |
| 改写、丢弃或发布的 git 操作 | `git push`、`reset`、`checkout`、`rebase`、`clean`、`commit --amend` |
| 执行任意代码 | `python script.py`、`node build.js`、`./deploy.sh`、`bash -c`、`npm run <其他脚本>` |
| 涉及工作区之外 | `cat /etc/passwd`、`ls ~`、`cat ../x` |
| 难以判断 | 变量（`$HOME`）、`$(...)`、反引号、用 `>` 写文件、后台任务 |

- 复合命令（`&&`、`;`、`|`）只有每一段都是低风险才算低风险。
- 判定偏保守：不认识的命令一律询问。如果某条命令你总是批准，选"不再询问"为它加一条放行规则。
- 它判断的是命令行文本，而不是命令实际做了什么。`pytest`、`npm test` 会运行你项目里的代码，这正是它们有用的原因，所以 auto 模式适合你信任的项目。它是便利功能，不是沙箱。
- 规则是固定写在 `src/nori/risk.py` 里的，没有用模型来判断。

### 计划模式

计划模式下 agent 可以读文件、搜索、浏览网页，但修改文件会被拒绝。它弄清楚任务之后会把计划展示给你并询问：

```
 计划已就绪
 按这个计划执行？
 ❯ 1. 是，并自动接受文件修改
   2. 是，每次修改前仍然询问
   3. 否，继续规划 (esc)
```

批准后退出计划模式，agent 开始实施；拒绝则继续规划，你可以告诉它要改什么。进入计划模式的方式：`nori --mode plan`、`/plan`，或按 Shift+Tab。

### 放行规则

每个审批对话框都有"不再询问"这一项，选择后会为本项目添加一条规则：

| 你批准的操作 | 规则 | 覆盖范围 |
| --- | --- | --- |
| `git status --short` | `git status` | 以 `git status` 开头的命令 |
| `python -m pytest -q` | `python -m pytest` | 这个模块，参数任意 |
| 一次文件修改 | `Update` / `Write` | 该工具的所有调用 |
| 抓取 `https://docs.python.org/3/` | `docs.python.org` | 这个域名 |

- 规则不会匹配复合命令（`;`、`&&`、`|`、重定向、`$(...)`），所以 `git status; rm -rf .` 仍然会询问。
- 有破坏性或能执行任意代码的命令（`rm`、`sudo`、`curl`、`python -c` 等）不提供"不再询问"，每次都问。
- 规则保存在 `~/.nori/permissions.json`，按项目路径区分。它不在项目目录里，所以克隆一个仓库不可能给自己授权。
- `/permissions` 查看；`/permissions remove <序号>` 和 `/permissions clear` 删除。

## 撤销

`/undo` 把最近一轮改过文件的那一轮还原：被修改的文件恢复原内容，那一轮新建的文件被删除。再执行一次就再往前退一轮。agent 会被告知，不会以为自己的修改还在。

只跟踪通过编辑工具做的改动。shell 命令造成的变化（`rm`、`git checkout`、代码生成器）无法这样撤销；快照只保存在内存里，退出后就没有了。重要的东西请用 git。

## 上下文管理

模型一次能看到的内容有上限。Nori 会跟踪上下文的占用，在溢出之前压缩。

- **窗口大小**：告诉 Nori 你的模型窗口有多大，可以在 `nori --setup` 里填，用 `/context 200k`，在配置文件里写 `"context_window"`，或加 `--context-window`。数值可以写成 `128000`、`200k`、`1m`。底栏显示占用，如 `上下文 34k (27%)`。
- **自动压缩**：占用达到窗口的 80% 时，较早的对话会被模型写的摘要替换，最近的部分（最多 20k token，不超过窗口的 20%）原样保留。长任务进行到一半也会触发，压缩完接着干。
- **手动压缩**：`/compact`，可以附带要保留什么：`/compact 重点保留迁移进度`。
- 压缩会多花一次模型调用，摘要保留不了全部细节。用 `--no-auto-compact` 或 `"auto_compact": false` 关闭自动压缩。
- 服务端不返回 token 用量时，占用量按文本估算，只是近似值。

## 会话

每轮对话结束后（包括被中断时），会话自动保存到 `~/.nori/sessions/`。

```bash
nori -c                   # 继续这个工作区最近的一次会话
nori --list-sessions      # 列出保存的会话
nori -r 20261112-0813     # 按 ID 恢复，写开头几位即可
```

在界面里 `/resume` 会打开列表供选择。恢复的是对话而不是文件：模型记得的代码内容可能已经过时。会话文件是明文，包含模型读过的所有内容；不想保存就加 `--no-save`。

## 模型

`/model` 打开选择面板，列出配置文件里的模型和接口提供的模型。直接打字可以筛选；输入一个列表里没有的名字再按回车，就使用这个名字。选择会保存为默认模型。

不同模型的上下文窗口不一样时可以分别指定，切换模型时窗口跟着变：

```json
{
  "model": "fast-model",
  "context_window": 128000,
  "models": ["fast-model", { "name": "big-model", "context_window": 200000 }]
}
```

## 子 agent

agent 可以把一件事交给子 agent。子 agent 在独立的上下文里工作，只交回最终报告，长时间的搜索不会占用主对话。

| 类型 | 能做什么 | 适合 |
| --- | --- | --- |
| `explore` | 读文件、搜索、浏览网页 | 在大代码库里找东西、调研 |
| `general` | 全部 | 相互独立的子任务 |

一次回复里发起的多个子 agent 并行运行。它们的操作走同样的审批，会自己压缩上下文，不能再派生子 agent。

## 联网、图片与 shell

- **联网**：`web_search` 返回标题、链接和摘要；`web_fetch` 把网页读成文本。抓取前会询问，可以选择"不再询问这个域名"。搜索是抓取公开的搜索页面实现的（默认 DuckDuckGo，失败时换 Bing），这些页面改版时可能失效。在访问不了 DuckDuckGo 的网络环境里，可以把 `search_engine` 设为 `bing`。
- **图片**：在消息里提到图片（`@screenshot.png`、路径，或把文件拖进终端），它就会一起发送。`Ctrl-V` 粘贴剪贴板里的图片。支持 PNG、JPEG、GIF、WebP，最大 5 MB；模型需要支持图片输入。
- **`!命令`**：直接执行命令并显示输出，同时把命令和输出加入对话，让 agent 能看到。因为是你自己输入的，不会询问。
- **后台命令**：agent 可以把长时间运行的命令（开发服务器、监听任务）放到后台，之后再读取输出或停止它。底栏显示正在运行的数量，`/tasks` 列出它们，退出时会全部停止。

## Skill

一个 skill 是一个包含 `SKILL.md` 的目录：开头的 front matter 写名称和描述，后面是给 agent 的指引，目录里可以放脚本和模板。

```markdown
---
name: review
description: 按团队清单做代码审查。当用户要求 review 代码时使用。
---
1. 先读 checklist.md（与本文件同目录）
2. 逐项检查改动并给出结论
```

系统提示里只放名称和描述，任务匹配时 agent 才加载全文。也可以自己触发：`/review 重点看 auth.py`。

查找位置（靠前的优先）：`<工作区>/.agents/skills/`、`<工作区>/.claude/skills/`、`~/.nori/skills/`、`~/.agents/skills/`、`~/.claude/skills/`。

## MCP

在 `<工作区>/.mcp.json`（项目级）或 `~/.nori/mcp.json`（用户级）中配置，使用通用的 `mcpServers` 格式：

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

有 `command` 的是 stdio 服务器；有 `url` 的走 Streamable HTTP，加 `"type": "sse"` 则走 SSE。`${VAR}` 会用环境变量展开。项目的 `.mcp.json` 能启动任意命令，所以 Nori 会先问你是否信任，再启动这些服务器。

## 界面语言

界面默认英文，可以切换为中文：`nori --lang zh`、`NORI_LANG=zh`、`"language": "zh"`，或在界面里用 `/lang`。这只影响 Nori 自己的文字；模型用什么语言回答取决于你用什么语言提问。

## 宠物

Nori 住在输入框旁边，对正在发生的事做出反应：思考时 `( o.O )`，干活时 `( >.< )`，等你确认时冒出一个 `!`，任务完成时开心 `( ^.^ )`，出错时 `( x.x )`，你离开久了它就睡着。每完成一个任务都会获得经验。`/pet` 查看等级，`/pet name 小黑` 改名，`/pet hide` 隐藏。

## 可靠性

- 模型请求遇到限流、服务过载或网络错误时，会按指数退避自动重试（`retries`，默认 4 次）。
- 任务达到轮数上限或被中断时，已完成的工作保留在上下文里。
- 生成摘要失败不会让任务失败：会继续使用完整上下文。

## 安全说明

- 文件工具只能访问工作区内的路径。`run_shell` 不受此限制，它执行的是任意命令，所以才需要确认。
- `bypass` 模式和 `-y` 会跳过所有确认，包括是否启动项目里的 MCP 服务器。只在你信任的项目和环境里使用。
- `auto` 模式根据命令的文本来判断，它放行的命令里包括会运行你项目代码的（测试、构建）。请在你信任的项目里使用。
- skill 和抓取到的网页都是模型会读、也可能照着做的文字。只使用你信任的 skill，浏览陌生网站时保持在会询问的模式下。
- 配置文件里的 API Key 是明文保存的（文件权限 600）。写成 `"api_key": "${MY_KEY}"` 可以不让它落盘。

## 构建可执行程序

```bash
pip install -e ".[build]"
python scripts/build.py                          # 单文件：dist/nori
python scripts/build.py --archive nori-linux-x86_64   # 目录版，打包为 dist/<名称>.tar.gz
```

PyInstaller 不能交叉编译，每个平台要在对应的平台上构建。发布工作流（`.github/workflows/release.yml`）会为 Linux、macOS、Windows 分别构建，发布 GitHub Release，更新 Homebrew 配方，并用 Homebrew 实际安装一遍作为检查。触发方式：推送 `v*` 标签，或手动运行并填写标签名。

## 开发

```bash
pip install -e ".[dev]"
pytest
```

```
src/nori/
  cli.py          参数解析；保持轻量，--help 和 --version 即时返回
  app.py          把各部分接起来，启动选定的前端
  tui.py          终端界面（prompt_toolkit）
  session.py      对话：运行循环、审批、压缩守卫、子 agent
  permissions.py  权限模式与放行规则
  risk.py         auto 模式用的 shell 命令风险判定
  tools.py        agent 的工具
  commands.py     斜杠命令与选择面板
  compact.py      历史摘要
  checkpoints.py  /undo 用的文件快照
  shelltasks.py   后台命令
  web.py          网页抓取与搜索
  attachments.py  图片
  store.py        会话存盘
  config.py       配置文件与设置向导
  i18n.py         界面语言：英文为键，附中文词条表
  render.py       工具调用、结果、Markdown 的显示方式
  pet.py          宠物
  skills.py、mcp_config.py、agent.py、branding.py、util.py
tests/            用脚本化模型驱动真实的 SDK 运行循环；界面用无头方式测试
```

想换个名字？改 `src/nori/branding.py` 和 `pyproject.toml` 里的名称即可。
