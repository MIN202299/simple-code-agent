# Nori

```
 /\_/\ ♪
( ^.^ )    Nori — 终端里的 AI 编程搭档
 > ^ <~
```

Nori 是一个基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 的命令行 code agent：在终端里用自然语言让它读代码、改代码、跑命令。名字来自常驻输入框旁边的那只小黑猫。

## 功能

- **TUI**：底部固定输入框，流式 Markdown 渲染，带状态的工具调用与 diff 预览，审批对话框，`/` 命令和 `@` 文件补全
- **宠物 Nori**：一只会随 agent 状态变表情、陪你干活会升级的小猫
- **上下文管理**：可配置上下文窗口大小，支持 `/compact` 手动压缩和快满时自动压缩
- **可打包**：一条命令打成不依赖 Python 的单文件可执行程序
- **配置文件**：在 `~/.nori/config.json` 中配置 base url、api key 和模型，首次运行有交互式向导
- 内置工具：`read_file`、`write_file`、`edit_file`、`list_dir`、`glob_files`、`grep`、`run_shell`
- **Skill**：从 `SKILL.md` 加载可复用的任务指引，按需加载，也可用 `/<skill>` 直接调用
- **MCP**：通过 `.mcp.json` 接入 stdio / HTTP / SSE 的 MCP 服务器，其工具自动提供给 agent
- **任务计划**：多步骤任务会用 `update_plan` 维护一份清单并显示在终端
- 写文件、执行命令前需要人工确认（使用 SDK 原生的工具审批机制），可选"本次会话不再询问"或全部自动批准
- 文件工具限制在工作区目录内
- 工作区根目录有 `AGENTS.md`（或 `CLAUDE.md`）时自动作为项目说明注入系统提示
- 支持 Responses API 和 Chat Completions API，可接第三方兼容服务

## 安装

需要 Python 3.11+（或者直接用[打包好的可执行程序](#打包成可执行程序)）。

```bash
pip install -e .          # 或: uv pip install -e .
nori --setup        # 填写 base url、api key、模型
```

## 配置

配置保存在 `~/.nori/config.json`：

```json
{
  "base_url": "https://your-provider.example/v1",
  "api_key": "sk-...",
  "model": "your-model-name",
  "api": "chat"
}
```

| 字段 | 说明 |
| --- | --- |
| `api_key` | API Key。可以写成 `${ENV_VAR}` 引用环境变量 |
| `base_url` | API 地址；省略则使用 OpenAI 官方地址 |
| `model` | 模型名；省略则使用 SDK 默认模型 |
| `api` | `responses` 或 `chat`。省略时：设置了非 OpenAI 的 `base_url` 用 `chat`，否则用 `responses` |
| `max_turns` | 单个任务的最大模型轮数（每次模型回复算一轮），默认 200 |
| `context_window` | 模型的上下文窗口大小（token 数），默认 128000。按你所用模型的实际值填写 |
| `auto_compact` | 上下文快满时是否自动压缩会话，默认 `true` |

- `nori --setup` 会交互式地创建或修改这个文件（Key 输入不回显，文件权限为 600）。
- 没有配置 Key 时，首次启动会自动进入这个向导。
- 用 `--config PATH` 或环境变量 `NORI_CONFIG` 可以换一个配置文件，方便在多个服务之间切换。
- 从旧版本升级时，`~/.code_agent` 目录会在首次启动时自动改名为 `~/.nori`。原来的 `CODE_AGENT_*` 环境变量需要手动改成 `NORI_*`。
- 只读取用户级配置，不读取项目目录里的配置：否则一个仓库就能通过改 `base_url` 把你的 Key 发到别处。

优先级从高到低：

1. 命令行参数 `--model`、`--base-url`、`--api`、`--max-turns`、`--context-window`、`--no-auto-compact`
2. 环境变量 `NORI_MODEL`、`NORI_API`
3. 配置文件
4. 通用环境变量 `OPENAI_API_KEY`、`OPENAI_BASE_URL`

配置文件优先于 `OPENAI_*`，因为它是专门为本工具写的，而 `OPENAI_*` 是所有 OpenAI 客户端共用的。

## 使用

```bash
nori                          # 在当前目录打开 TUI
nori -C path/to/project       # 指定工作区
nori "给 utils.py 补上单元测试"  # 执行一个任务后退出（逐行输出，适合脚本）
nori -m other-model -y        # 临时换模型，并自动批准所有操作
```

| 参数 | 说明 |
| --- | --- |
| `-m, --model` | 模型名，覆盖配置文件 |
| `--base-url` | API 地址，覆盖配置文件 |
| `--api {responses,chat}` | 使用哪种 API |
| `--config PATH` | 配置文件路径 |
| `--setup` | 交互式创建或修改配置文件后退出 |
| `-C, --cwd` | 工作区目录，默认当前目录 |
| `-y, --yes` | 自动批准写文件和执行命令 |
| `--plain` | 不使用 TUI，改用逐行输出的简单模式 |
| `--no-mcp` / `--no-skills` | 不加载 MCP 服务器 / skill |
| `--max-turns` | 单个任务的最大模型轮数，默认 200 |
| `--context-window TOKENS` | 模型的上下文窗口大小，默认 128000 |
| `--no-auto-compact` | 上下文快满时不自动压缩会话 |

标准输入或输出不是终端时（例如管道），自动使用简单模式。

## TUI

```
╭─ Nori v0.3.0 ──────────────────────────────────────────╮
│                                                        │
│   /\_/\ ♪    终端里的 AI 编程搭档                       │
│  ( ^.^ )                                               │
│   > ^ <~     模型    your-model-name                   │
│  Nori Lv.3   接口    https://… · chat                  │
│              工作区  ~/project (⎇ main)                │
│              扩展    2 个 skill · 1 个 MCP 服务器       │
│                                                        │
│  输入任务开始 · / 命令 · @ 引用文件 · ? 快捷键           │
╰────────────────────────────────────────────────────────╯

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
  ? 快捷键 · / 命令 · @ 文件      your-model-name · 上下文 3.3k · ⎇ main
```

界面是内联绘制的，不占用备用屏幕：对话记录直接进入终端的滚动缓冲区，可以照常滚动、选择和复制。

- **工具调用**：执行中显示在底部的实时区（转圈和计时），结束后连同结果写入记录，圆点绿色表示成功、红色表示失败或被拒绝。改文件时附带 diff。
- **状态行**：显示宠物的表情、正在做什么、已用时间和本轮输出的 token 数。
- **底栏**：左边是模式和提示，右边是模型、当前上下文大小和 git 分支。开启自动批准后输入框边线变色。
- **排队**：agent 工作时可以继续输入，发送的消息在当前任务结束后依次执行。
- **中断与续跑**：中断任务或达到最大轮数后，已完成的步骤保留在上下文里，输入"继续"即可接着做。
- **防误触**：审批对话框弹出后的前半秒不响应按键，避免正在输入的内容误批准操作。

| 按键 | 作用 |
| --- | --- |
| `Enter` | 发送 |
| `Alt+Enter` / `Ctrl-J` | 换行 |
| `Esc` | 中断正在执行的任务；关闭菜单 |
| `/` | 命令菜单，`Tab` 补全，`Enter` 执行 |
| `@` | 文件菜单，按路径模糊匹配，`Tab` / `Enter` 插入 |
| `?` | 输入框为空时显示快捷键 |
| `↑` / `↓` | 历史输入；在菜单和审批对话框中移动选择 |
| `Shift+Tab` | 开关自动批准 |
| `Ctrl-C` | 中断任务；空闲时清空输入，再按一次退出 |
| `Ctrl-D` | 退出 |
| `Ctrl-L` | 清屏 |

命令：

| 命令 | 说明 |
| --- | --- |
| `/help` | 显示帮助 |
| `/clear` | 清空对话上下文 |
| `/compact [说明]` | 把对话压缩成摘要以释放上下文，可附带要重点保留什么 |
| `/model [name]` | 查看或切换模型（仅本次会话） |
| `/status` | 查看模型、接口、上下文大小和累计用量 |
| `/yes` | 开关自动批准 |
| `/skills` | 列出可用的 skill |
| `/<skill> [text]` | 直接使用某个 skill |
| `/mcp` | 列出已连接的 MCP 服务器及其工具 |
| `/pet` | 看看你的宠物 |
| `/exit` | 退出 |

## 上下文管理

模型一次能看到的内容有上限（上下文窗口）。长任务里读过的文件、命令输出会不断累积，Nori 用压缩来避免撞上这个上限：

- **窗口大小**：用配置文件的 `context_window` 或 `--context-window` 告诉 Nori 你的模型窗口有多大。底栏实时显示当前占用，如 `上下文 34k (27%)`，达到 80% 时变色。
- **自动压缩**：占用达到窗口的 80% 时，Nori 让模型把较早的对话写成摘要并替换掉原文，最近的一部分（最多 20k token，不超过窗口的 20%）原样保留。任务进行到一半也会触发，压缩完接着干，不需要你介入。
- **手动压缩**：`/compact` 立即把整个对话压成摘要；可以附带说明，例如 `/compact 重点保留数据库迁移的进度`。
- **摘要的内容**：目标与约束、已完成的工作（文件、命令及结果）、关键发现、剩余工作和下一步。
- 压缩会多花一次模型调用；摘要不可能保留全部细节，压缩后模型可能需要重新读某些文件。用 `--no-auto-compact` 或 `"auto_compact": false` 可以关掉自动压缩。
- 占用量优先取服务端返回的 token 用量；服务端不返回用量时按字符数估算，数字只是近似值。

## 宠物

Nori 住在输入框右边，表情跟着 agent 的状态走：

| 状态 | 样子 |
| --- | --- |
| 空闲 | `( o.o )`，偶尔眨眼 |
| 思考 | `( o.O )` 加一个问号 |
| 执行工具 | `( >.< )` 爪子在动 |
| 等你确认 | `( o.o )` 加一个感叹号 |
| 任务完成 | `( ^.^ )` ♪ |
| 出错或被中断 | `( x.x )` |
| 你很久没动 | `( -.- )` zZ |

- 每完成一个任务得 10 点经验，每次工具调用得 1 点，攒够了会升级（小奶猫 → 见习喵 → 码农喵 → …）。
- `/pet` 查看等级和战绩，`/pet name 小黑` 改名，`/pet hide` / `/pet show` 隐藏或显示。
- 状态保存在 `~/.nori/pet.json`。终端宽度不足 64 列时自动隐藏。

## Skill

一个 skill 是一个包含 `SKILL.md` 的目录，文件开头用 front matter 写名称和描述，正文是给 agent 的指引，目录里可以放脚本、模板等辅助文件：

```
.agents/skills/
  review/
    SKILL.md
    checklist.md
```

```markdown
---
name: review
description: 按团队清单做代码审查。当用户要求 review 代码时使用。
---
1. 先读 checklist.md（与本文件同目录）
2. 逐项检查改动并给出结论
```

工作方式：

- 启动时只把各 skill 的名称和描述放进系统提示；任务匹配时 agent 调用 `load_skill` 读取正文，再按其中指引执行。
- 也可以手动触发：`/review 重点看 auth.py`。
- skill 目录对 agent 只读，即使它在工作区之外。

查找位置（同名时靠前的优先）：

1. `<工作区>/.agents/skills/`
2. `<工作区>/.claude/skills/`
3. `~/.nori/skills/`
4. `~/.agents/skills/`
5. `~/.claude/skills/`

## MCP

在 `<工作区>/.mcp.json`（项目级）或 `~/.nori/mcp.json`（用户级）中配置，格式与其他 coding agent 通用的 `mcpServers` 一致；同名时项目级覆盖用户级：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "env": { "API_KEY": "${MY_API_KEY}" }
    },
    "remote": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ${TOKEN}" }
    }
  }
}
```

- 有 `command` 的是 stdio 服务器（可选 `args`、`env`、`cwd`）；有 `url` 的是远程服务器，`"type": "sse"` 用 SSE，否则用 Streamable HTTP。
- 字符串里的 `${VAR}` 和 `${VAR:-默认值}` 会用环境变量展开，密钥不必写进文件。
- 可选字段：`"timeout"`（秒，默认 30）、`"disabled": true`。
- 连接失败的服务器会被报告并跳过，不影响其他功能。
- MCP 工具和写文件、执行命令一样，调用前需要确认。
- 项目级 `.mcp.json` 会启动任意命令，所以首次启动时会先询问是否信任（`-y` 时跳过询问）。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `NORI_CONFIG` | 配置文件路径 |
| `NORI_MODEL` | 模型名，覆盖配置文件 |
| `NORI_API` | `responses` 或 `chat`，覆盖配置文件 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 配置文件里没有对应项时使用 |
| `NORI_TRACING` | 设为 `1` 时开启 SDK 的 tracing（会把运行数据上传到 OpenAI，默认关闭） |

## 安全说明

- 文件工具只能访问工作区内的路径，但 `run_shell` 执行的是任意 shell 命令，不受此限制——这正是它默认需要确认的原因。
- `-y` 会跳过所有确认（包括是否启动项目 `.mcp.json` 里的服务器），只建议在可信项目或沙箱环境里使用。
- skill 的内容会进入模型上下文并影响它的行为，只使用你信任的 skill。
- 配置文件里的 API Key 是明文保存的（权限 600）。不想落盘可以写 `"api_key": "${MY_KEY}"`。

## 打包成可执行程序

```bash
pip install -e ".[build]"
python scripts/build.py            # 单文件：dist/nori（Windows 上是 dist/nori.exe）
python scripts/build.py --onedir   # 文件夹：dist/nori/，启动更快
```

- 产物不依赖 Python，拷到 `PATH` 里的任意目录就能用（例如 `/usr/local/bin/nori`）。
- PyInstaller 不能交叉编译：在哪个系统和 CPU 上构建，产物就只能在那上面运行。
- 单文件版每次启动要先解压，会慢几秒；介意的话用 `--onedir`。
- `.github/workflows/release.yml` 会在推送 `v*` 标签时为 Linux、macOS、Windows 各构建一份并发布到 GitHub Release，也可以在 Actions 页面手动触发。

如果只是想全局安装而不需要脱离 Python，可以用 `pipx install .` 或 `uv tool install .`。

## 项目结构

```
src/nori/
  cli.py         # 入口：参数解析，选择 TUI / 简单模式 / 一次性执行
  tui.py         # 终端界面（prompt_toolkit）
  pet.py         # 宠物的表情、台词和成长
  render.py      # 工具调用、结果、Markdown 的显示格式
  session.py     # 对话与运行循环、审批流程、中断处理
  compact.py     # 会话压缩：切分位置、摘要生成
  commands.py    # 斜杠命令
  config.py      # 配置文件与设置向导
  branding.py    # 应用名、主题色、用户目录
  tools.py       # 工具实现与审批策略
  skills.py      # skill 的发现与解析
  mcp_config.py  # MCP 配置加载与服务器构建
  agent.py       # 系统提示与 Agent 构建
scripts/
  build.py       # 用 PyInstaller 打包
  entry.py       # 可执行程序的入口
tests/           # 用 SDK 的 ScriptedModel 驱动真实运行循环；TUI 用管道输入做无头测试
```

想换个名字？改 `src/nori/branding.py` 里的常量、`pyproject.toml` 里的包名和命令名即可。

## 开发

```bash
pip install -e ".[dev]"
pytest
```
