<p align="center">
  <img src="assets/brand/ankaloop-wordmark.svg" alt="AnkaLoop" width="700">
</p>

<p align="center">
  <strong>A batteries-included coding-agent runtime that keeps working across terminals,
  servers, and Telegram.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/ankaloop/"><img src="https://img.shields.io/pypi/v/ankaloop?label=PyPI&color=1488ff" alt="PyPI version"></a>
  <a href="https://pypi.org/project/ankaloop/"><img src="https://img.shields.io/pypi/pyversions/ankaloop?color=0957f5" alt="Supported Python versions"></a>
  <a href="https://github.com/tao12345666333/ankaloop/actions/workflows/ci.yml"><img src="https://github.com/tao12345666333/ankaloop/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="https://opensource.org/licenses/Apache-2.0"><img src="https://img.shields.io/badge/license-Apache--2.0-22dff3" alt="Apache-2.0 license"></a>
  <a href="https://github.com/tao12345666333/ankaloop/stargazers"><img src="https://img.shields.io/github/stars/tao12345666333/ankaloop?style=flat&color=1488ff" alt="GitHub stars"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#why-ankaloop">Why AnkaLoop</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="docs/QUICK_START.md">Docs</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

AnkaLoop is for developers who want a useful coding agent now—not another framework to assemble.
It combines a capable tool loop, persistent context, multi-agent delegation, skills, memory, MCP,
hooks, and automation in one Python package that you can run locally or self-host.

```bash
python -m pip install ankaloop
anka init
anka
```

## Why AnkaLoop

- **Useful on the first run** — read, search, edit, patch, and execute code with built-in tools.
- **Persistent by design** — sessions, searchable history, project rules, and long-term memory
  survive beyond a single prompt.
- **One runtime, multiple surfaces** — use the same agent from the CLI, an HTTP/WebSocket server,
  or Telegram.
- **Built for real tasks** — context compaction, progress events, cancellation, bounded tool use,
  and provider retries make long-running work observable and controllable.
- **Multi-agent without plumbing** — delegate focused work to built-in explorer, planner, and coder
  agents.
- **Open and extensible** — add MCP servers, reusable skills, slash commands, hooks, and custom
  agent specifications without replacing the built-in experience.
- **Provider-flexible** — connect to OpenAI, Anthropic, GMI, or any OpenAI-compatible endpoint.

## Quick start

AnkaLoop requires **Python 3.11+** and credentials for a supported model provider.

### Install from PyPI

```bash
# Install the latest stable release
python -m pip install ankaloop

# Configure a provider, then start in the current project
anka init
anka

# Or run one task and exit
anka --once "summarize this repository and suggest the next test to run"
```

The package installs both `anka` (recommended) and `ankaloop` commands.

### Run without installing

```bash
uvx ankaloop init
uvx ankaloop
```

### Install optional Telegram support

```bash
python -m pip install "ankaloop[telegram]"
anka telegram setup
```

## What is included

| Area | Capabilities |
| --- | --- |
| **Coding loop** | File reading and search, patching and writing, shell execution, planning, todos, and subagent tasks |
| **Context** | Persistent sessions, `AGENTS.md` rules, progressive loading, and smart compaction |
| **Memory** | Durable facts, episodic history, persona files, and full-text session search |
| **Agents** | Built-in `coder`, `explorer`, `planner`, and `focused_coder` roles |
| **Interfaces** | Rich terminal UI, FastAPI HTTP/WebSocket server, and Telegram bot |
| **Research** | Built-in web search/fetch plus MCP integration over stdio and HTTP/SSE |
| **Extensions** | Skills, slash commands, hooks, custom YAML agent specs, and an event bus |
| **Automation** | Cron-compatible jobs for systemd, Kubernetes, and external schedulers |

## How it fits together

```text
 CLI ───────────────┐
 HTTP / WebSocket ──┼──▶ Agent runtime ──▶ Tools / MCP ──▶ Your project
 Telegram ──────────┘         │
                              ├── Sessions & memory
                              ├── Skills & project rules
                              └── Subagents
```

All interfaces use the same core runtime. Conversation state is persistent, while project rules,
skills, and memory provide context across requests.

## Usage

```bash
# Chat and one-shot tasks
anka
anka --once "create a hello.py file"
anka -t explorer --once "find all TODO comments"
anka --agent path/to/agent.yaml

# Sessions and agents
anka --session my-session
anka --list-sessions
anka --list-types
anka --clear

# MCP
anka mcp tools --server custom
anka mcp call --server custom --tool example_tool --args '{"query":"rust async"}'

# Server and remote client
anka serve
anka attach http://localhost:8080

# Telegram
anka telegram setup
anka telegram start
```

Run `anka --help` or `anka <command> --help` for the complete command reference.

## Configuration

Run `anka init` for the interactive provider wizard. Configuration lives in
`~/.config/ankaloop/config.toml`. Set `ANKA_CONFIG_DIR_NAME=amcp` to keep the legacy
`~/.config/amcp/` path from pre-rebrand deployments.

### OpenAI-compatible endpoint

```toml
[chat]
active_provider = "primary"

[chat.providers.primary]
api_type = "openai"
base_url = "https://api.example.com/v1"
model = "provider/model-name"
```

Keep credentials out of version control and provide the key through the environment:

```bash
export OPENAI_API_KEY="your-api-key"
anka --once "explain the architecture of this repository"
```

You can define multiple `[chat.providers.<name>]` profiles. Telegram administrators can list them
with `/models` and switch the active profile with `/model use <name>`.

<details>
<summary><strong>Runtime and tool settings</strong></summary>

```toml
[chat]
request_timeout_seconds = 120
max_retries = 2
retry_base_delay_seconds = 0.5
tool_loop_limit = 300
bash_tool_limit = 100
default_max_lines = 400
mcp_tools_enabled = true
write_tool_enabled = true
edit_tool_enabled = true
sync_tool_settle_timeout_seconds = 2.0
default_agent = "coder"

[context]
progressive_tools = true
progressive_skills = true
response_ratio = 0.30
```

</details>

<details>
<summary><strong>MCP servers</strong></summary>

```toml
# HTTP/SSE transport
[servers.remote]
url = "https://example.com/mcp"

# stdio transport
[servers.local]
command = "npx"
args = ["-y", "@some/mcp-server"]
```

Configured tools are exposed as `mcp__<server>__<tool>`. Tool names are normalized for providers
with strict function-name requirements.

</details>

See the [quick-start guide](docs/QUICK_START.md), [skills and commands guide](docs/skills-and-commands.md),
and [hooks guide](docs/hooks.md) for more configuration examples.

## Multi-agent runtime

| Agent | Mode | Purpose |
| --- | --- | --- |
| `coder` | Primary | General coding agent with write access and delegation |
| `explorer` | Subagent | Fast, read-only codebase exploration |
| `planner` | Subagent | Read-only analysis and implementation planning |
| `focused_coder` | Subagent | Bounded implementation of a specific change |

Select an agent with `anka -t <name>`. Primary agents can use the `task` tool to delegate
independent work to subagents.

## Skills, commands, and hooks

- **Skills** inject reusable instructions and resources only when relevant.
- **Slash commands** turn repeatable prompts into `/command` shortcuts with arguments, shell output,
  and file references.
- **Hooks** validate, modify, block, or audit tool calls before and after execution.

Project extensions live under `.ankaloop/`:

```text
.ankaloop/
├── commands/       # TOML slash commands
├── hooks.toml      # pre/post tool hooks
├── memory/         # project knowledge
└── skills/         # reusable SKILL.md packages
```

## Self-hosted server

```bash
anka serve                         # loopback only, http://localhost:8080
anka serve --host 0.0.0.0          # requires authentication
anka attach https://agent.example  # connect from another terminal
```

The server exposes session, prompt, streaming, cancellation, timeline, tool, and agent APIs. Visit
`/docs` on a running server for its OpenAPI interface.

Non-loopback binds require a server API key. Configure `[server.auth]` or pass `--api-key`; deploy
behind TLS or a trusted reverse proxy.

<details>
<summary><strong>Docker</strong></summary>

```bash
docker build -t ankaloop .

# Safe loopback-only default
docker run -it ankaloop serve

# Expose with authentication
docker run -p 8080:8080 \
  -e ANKA_HOST=0.0.0.0 \
  -e ANKA_API_KEY=replace-with-a-secret \
  ankaloop serve
```

</details>

For protocol details, see the [client/server architecture](docs/architecture/client-server.md) and
[API compatibility notes](docs/api/protocol-compatibility.md).

## Telegram

The optional Telegram integration supports direct messages, groups and topics, allowlists,
pairing, streaming responses, bounded queues, cancellation, and session switching.

```bash
python -m pip install "ankaloop[telegram]"
anka telegram setup
anka telegram start
```

Shared commands include `/new`, `/clear`, `/cancel`, `/session list`, and
`/session switch <id>`.

## Install from source

```bash
git clone https://github.com/tao12345666333/ankaloop.git
cd ankaloop

uv sync --extra dev --extra telegram
source .venv/bin/activate
```

Run the local quality suite:

```bash
ruff format --check src tests
ruff check src tests
mypy src/ankaloop --ignore-missing-imports
python -m pytest -q -m "not llm"
```

Tests marked `llm` make live provider calls and require credentials. See
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## Deployment

- Run the FastAPI server directly or package it with the included Dockerfile.
- Use the provided examples for [Docker](deploy/docker/), [Kubernetes](deploy/k8s/),
  [VM deployments](deploy/vm/), and [GMI Cloud](deploy/gmi/).
- [Deploy AnkaLoop on GMI Cloud](https://console.gmicloud.ai/user-console/ie/agentbox/browse-agents/ankaloop)
  or use the maintainer's optional [referral link](https://console.gmicloud.ai/ref/KP3NWZV4).

## Project status

AnkaLoop is under active development. Feedback,
bug reports, documentation improvements, and focused pull requests are welcome.

- [Open an issue](https://github.com/tao12345666333/ankaloop/issues)
- [Read the contributing guide](CONTRIBUTING.md)
- If AnkaLoop is useful to you, consider starring the repository so more developers can find it.

## License

Licensed under the [Apache License 2.0](LICENSE).
