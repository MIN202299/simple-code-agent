# simple-code-agent

基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 实现的命令行 code agent：在终端里用自然语言让它读代码、改代码、跑命令。

## 功能

- **TUI**：类似 Claude Code 的终端界面——底部固定输入框，流式 Markdown 渲染，工具调用与 diff 预览，审批对话框，斜杠命令补全
- **配置文件**：在 `~/.code_agent/config.json` 中配置 base url、api key 和模型，首次运行有交互式向导
- 内置工具：`read_file`、`write_file`、`edit_file`、`list_dir`、`glob_files`、`grep`、`run_shell`
- **Skill**：从 `SKILL.md` 加载可复用的任务指引，按需加载，也可用 `/<skill>` 直接调用
- **MCP**：通过 `.mcp.json` 接入 stdio / HTTP / SSE 的 MCP 服务器，其工具自动提供给 agent
- **任务计划**：多步骤任务会用 `update_plan` 维护一份清单并显示在终端
- 写文件、执行命令前需要人工确认（使用 SDK 原生的工具审批机制），可选"本次会话不再询问"或全部自动批准
- 文件工具限制在工作区目录内
- 工作区根目录有 `AGENTS.md`（或 `CLAUDE.md`）时自动作为项目说明注入系统提示
- 支持 Responses API 和 Chat Completions API，可接第三方兼容服务

## 安装

需要 Python 3.11+。

```bash
pip install -e .          # 或: uv pip install -e .
code-agent --setup        # 填写 base url、api key、模型
```

## 配置

配置保存在 `~/.code_agent/config.json`：

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

- `code-agent --setup` 会交互式地创建或修改这个文件（Key 输入不回显，文件权限为 600）。
- 没有配置 Key 时，首次启动会自动进入这个向导。
- 用 `--config PATH` 或环境变量 `CODE_AGENT_CONFIG` 可以换一个配置文件，方便在多个服务之间切换。
- 只读取用户级配置，不读取项目目录里的配置：否则一个仓库就能通过改 `base_url` 把你的 Key 发到别处。

优先级从高到低：

1. 命令行参数 `--model`、`--base-url`、`--api`
2. 环境变量 `CODE_AGENT_MODEL`、`CODE_AGENT_API`
3. 配置文件
4. 通用环境变量 `OPENAI_API_KEY`、`OPENAI_BASE_URL`

配置文件优先于 `OPENAI_*`，因为它是专门为本工具写的，而 `OPENAI_*` 是所有 OpenAI 客户端共用的。

## 使用

```bash
code-agent                          # 在当前目录打开 TUI
code-agent -C path/to/project       # 指定工作区
code-agent "给 utils.py 补上单元测试"  # 执行一个任务后退出（逐行输出，适合脚本）
code-agent -m other-model -y        # 临时换模型，并自动批准所有操作
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
| `--max-turns` | 单个任务的最大模型轮数，默认 50 |

标准输入或输出不是终端时（例如管道），自动使用简单模式。

## TUI

```
╭──────────────────────────────────────────────╮
│ ✻ Code Agent v0.2.0                          │
│                                              │
│   模型    your-model-name                    │
│   接口    https://… · chat · key sk-…1234    │
│   工作区  ~/project                          │
╰──────────────────────────────────────────────╯

> 修复 calc.py 里的 bug

● Read(calc.py)
  ⎿  Read 2 lines

● Update(calc.py)
     -    return a - b
     +    return a + b
──────────────────────────────────────────────────
 需要确认 · Update
   Update(calc.py)

 是否允许执行？
 ❯ 1. 是
   2. 是，本次会话不再询问 Update
   3. 否，让 agent 换个做法 (esc)
```

界面是内联绘制的，不占用备用屏幕：对话记录直接进入终端的滚动缓冲区，可以照常滚动、选择和复制。

| 按键 | 作用 |
| --- | --- |
| `Enter` | 发送 |
| `Alt+Enter` / `Ctrl-J` | 换行 |
| `Esc` | 中断正在执行的任务 |
| `↑` / `↓` | 历史输入；在命令菜单和审批对话框中移动选择 |
| `/` | 打开命令菜单，`Tab` 补全，`Enter` 执行 |
| `Shift+Tab` | 开关自动批准 |
| `Ctrl-C` | 中断任务；空闲时清空输入，再按一次退出 |
| `Ctrl-D` | 退出 |
| `Ctrl-L` | 清屏 |

- agent 工作时可以继续输入，发送的消息会排队，在当前任务结束后依次执行。
- 中断任务后，已经完成的步骤会保留在上下文里，可以接着说"继续"。
- 审批对话框弹出后的前半秒不响应按键，避免正在输入的内容误批准操作。

命令：

| 命令 | 说明 |
| --- | --- |
| `/help` | 显示帮助 |
| `/clear` | 清空对话上下文 |
| `/model [name]` | 查看或切换模型（仅本次会话） |
| `/yes` | 开关自动批准 |
| `/skills` | 列出可用的 skill |
| `/<skill> [text]` | 直接使用某个 skill |
| `/mcp` | 列出已连接的 MCP 服务器及其工具 |
| `/exit` | 退出 |

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
3. `~/.code_agent/skills/`
4. `~/.agents/skills/`
5. `~/.claude/skills/`

## MCP

在 `<工作区>/.mcp.json`（项目级）或 `~/.code_agent/mcp.json`（用户级）中配置，格式与其他 coding agent 通用的 `mcpServers` 一致；同名时项目级覆盖用户级：

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
| `CODE_AGENT_CONFIG` | 配置文件路径 |
| `CODE_AGENT_MODEL` | 模型名，覆盖配置文件 |
| `CODE_AGENT_API` | `responses` 或 `chat`，覆盖配置文件 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 配置文件里没有对应项时使用 |
| `CODE_AGENT_TRACING` | 设为 `1` 时开启 SDK 的 tracing（会把运行数据上传到 OpenAI，默认关闭） |

## 安全说明

- 文件工具只能访问工作区内的路径，但 `run_shell` 执行的是任意 shell 命令，不受此限制——这正是它默认需要确认的原因。
- `-y` 会跳过所有确认（包括是否启动项目 `.mcp.json` 里的服务器），只建议在可信项目或沙箱环境里使用。
- skill 的内容会进入模型上下文并影响它的行为，只使用你信任的 skill。
- 配置文件里的 API Key 是明文保存的（权限 600）。不想落盘可以写 `"api_key": "${MY_KEY}"`。

## 项目结构

```
src/code_agent/
  cli.py         # 入口：参数解析，选择 TUI / 简单模式 / 一次性执行
  tui.py         # 终端界面（prompt_toolkit）
  render.py      # 工具调用、结果、Markdown 的显示格式
  session.py     # 对话与运行循环、审批流程、中断处理
  commands.py    # 斜杠命令
  config.py      # 配置文件与设置向导
  tools.py       # 工具实现与审批策略
  skills.py      # skill 的发现与解析
  mcp_config.py  # MCP 配置加载与服务器构建
  agent.py       # 系统提示与 Agent 构建
tests/           # 用 SDK 的 ScriptedModel 驱动真实运行循环；TUI 用管道输入做无头测试
```

## 开发

```bash
pip install -e ".[dev]"
pytest
```
