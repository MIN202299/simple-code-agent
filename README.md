# simple-code-agent

基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 实现的命令行 code agent：在终端里用自然语言让它读代码、改代码、跑命令。

## 功能

- 交互式 REPL 和一次性执行两种模式，回答流式输出
- 内置工具：`read_file`、`write_file`、`edit_file`、`list_dir`、`glob_files`、`grep`、`run_shell`
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
| `--max-turns` | 单个任务的最大模型轮数，默认 50 |

交互模式下的命令：`/help`、`/clear`（清空上下文）、`/model [name]`、`/yes`（开关自动批准）、`/exit`。

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
- `-y` 会跳过所有确认，只建议在可信项目或沙箱环境里使用。

## 项目结构

```
src/code_agent/
  tools.py   # 工具实现与审批策略
  agent.py   # 系统提示与 Agent 构建
  cli.py     # 命令行入口、REPL、流式渲染、审批交互
tests/       # 用 SDK 的 ScriptedModel 驱动真实运行循环的测试
```

## 开发

```bash
pip install -e ".[dev]"
pytest
```
