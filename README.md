# simple-code-agent

基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 实现的命令行 code agent：在终端里用自然语言让它读代码、改代码、跑命令。

## 功能

- 交互式 REPL 和一次性执行两种模式，回答流式输出
- 内置工具：`read_file`、`write_file`、`edit_file`、`list_dir`、`glob_files`、`grep`、`run_shell`
- **Skill**：从 `SKILL.md` 加载可复用的任务指引，按需加载，也可用 `/<skill>` 直接调用
- **MCP**：通过 `.mcp.json` 接入 stdio / HTTP / SSE 的 MCP 服务器，其工具自动提供给 agent
- **任务计划**：多步骤任务会用 `update_plan` 维护一份清单并显示在终端
- 写文件、执行命令前需要人工确认（使用 SDK 原生的工具审批机制），可选"本次会话总是允许"或 `-y` 全部自动批准
- 文件工具限制在工作区目录内
- 多轮对话保留上下文；运行中按 Ctrl-C 中断当前任务
- 工作区根目录有 `AGENTS.md`（或 `CLAUDE.md`）时自动作为项目说明注入系统提示
- 支持 Responses API 和 Chat Completions API，可通过 `OPENAI_BASE_URL` 接第三方兼容服务

## 安装

需要 Python 3.11+。

```bash
pip install -e .          # 或: uv pip install -e .
export OPENAI_API_KEY=sk-...
```

## 使用

```bash
code-agent                          # 在当前目录进入交互模式
code-agent "给 utils.py 补上单元测试"  # 执行一个任务后退出
code-agent -C path/to/project       # 指定工作区
code-agent -m gpt-5.1 -y            # 指定模型，并自动批准所有操作
```

| 参数 | 说明 |
| --- | --- |
| `-m, --model` | 模型名；默认读 `CODE_AGENT_MODEL`，否则用 SDK 默认模型 |
| `-C, --cwd` | 工作区目录，默认当前目录 |
| `-y, --yes` | 自动批准写文件和执行命令 |
| `--api {responses,chat}` | 使用哪种 API，默认 `responses`（也可用 `CODE_AGENT_API` 设置） |
| `--no-mcp` / `--no-skills` | 不加载 MCP 服务器 / skill |
| `--max-turns` | 单个任务的最大模型轮数，默认 50 |

交互模式下的命令：

| 命令 | 说明 |
| --- | --- |
| `/help` | 显示帮助 |
| `/clear` | 清空对话上下文 |
| `/model [name]` | 查看或切换模型 |
| `/yes` | 开关自动批准 |
| `/skills` | 列出可用的 skill |
| `/<skill> [要求]` | 直接使用某个 skill |
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

### 使用第三方兼容服务

```bash
export OPENAI_BASE_URL=https://your-provider.example/v1
export OPENAI_API_KEY=...
code-agent --api chat -m your-model-name
```

### 环境变量

| 变量 | 说明 |
| --- | --- |
| `OPENAI_API_KEY` | 必需 |
| `OPENAI_BASE_URL` | 可选，自定义 API 地址 |
| `CODE_AGENT_MODEL` | 默认模型 |
| `CODE_AGENT_API` | `responses` 或 `chat` |
| `CODE_AGENT_TRACING` | 设为 `1` 时开启 SDK 的 tracing（会把运行数据上传到 OpenAI，默认关闭） |

## 安全说明

- 文件工具只能访问工作区内的路径，但 `run_shell` 执行的是任意 shell 命令，不受此限制——这正是它默认需要确认的原因。
- `-y` 会跳过所有确认（包括是否启动项目 `.mcp.json` 里的服务器），只建议在可信项目或沙箱环境里使用。
- skill 的内容会进入模型上下文并影响它的行为，只使用你信任的 skill。

## 项目结构

```
src/code_agent/
  tools.py       # 工具实现与审批策略
  skills.py      # skill 的发现与解析
  mcp_config.py  # MCP 配置加载与服务器构建
  agent.py       # 系统提示与 Agent 构建
  cli.py         # 命令行入口、REPL、流式渲染、审批交互
tests/           # 用 SDK 的 ScriptedModel 驱动真实运行循环的测试
```

## 开发

```bash
pip install -e ".[dev]"
pytest
```
