# Changelog

All notable changes to AnkaLoop are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`ANKA_CHAT_MODEL` environment variable**: the chat model can now be set via the
  environment when the config file does not specify one (precedence: config `chat.model`
  > env > default), matching the existing `ANKA_OPENAI_BASE` / `OPENAI_API_KEY` fallbacks.
  This also makes the GMI AgentBox entrypoint's model export effective again.

---

## [0.15.0] — 2026-09-18

### Changed

- **Tightened CI quality gates**: the lint job now installs the project
  (`pip install -e ".[dev,telegram]"`) so mypy type-checks against real dependencies and
  runs as a blocking check (the previous `continue-on-error` let type errors through), and
  ruff lint/format now cover `tests/` as well as `src/`, matching the Makefile. Coverage
  flags moved out of the default `pytest` addopts into CI and `make test-cov`, so plain
  local test runs no longer pay for coverage collection.
- **Consolidated deployment assets under `deploy/`**: the former `deploy-k8s/`,
  `deploy-vm/`, and `deploy-gmi/` directories now live at `deploy/k8s/`, `deploy/vm/`,
  and `deploy/gmi/`, and the root `Dockerfile` plus compose example moved to
  `deploy/docker/`. All deployment assets were renamed from the legacy `amcp` naming
  to `ankaloop` (systemd unit, paths, namespaces, and deploy-time environment
  variables), and the `docker` mode gained the same in-place self-restart
  supervisor already used by the `vm` and `k8s` modes (`/docker:restart`).
  Deployment examples now contain placeholders only; real values stay in
  gitignored local files.

### Fixed

- Removed the stale "(Agent Model Context Protocol)" expansion from `examples/README.md`,
  corrected the post-clone directory name (`cd ankaloop`) in `CONTRIBUTING.md`, and updated
  the bug report template to ask for the AnkaLoop version instead of "AMCP Version".

---

## [0.14.0] — 2026-08-29

Stable release of the AnkaLoop rename line introduced in 0.14.0-rc.1.

### Added

- **Durable tool-boundary journal** (`#50`): persistent intent and outcome events expose
  interrupted tool calls after a crash without automatically retrying operations that may have
  side effects. A `/recovery` command reports unsettled operations for the current Telegram
  session, acknowledges them durably on demand (idempotently, keeping the evidence), and scans
  orphaned journals read-only; acknowledgement is fail-closed while a turn is still active, and
  unreadable journals are reported as requiring attention instead of being silently skipped.
- **Provider extra headers with OpenRouter attribution** (`#45`): chat provider profiles accept
  `extra_headers` forwarded as default headers, and OpenRouter endpoints get default
  `HTTP-Referer` / title / categories attribution (env-overridable, explicit config wins) so
  AnkaLoop usage is attributed in OpenRouter rankings and analytics.

### Changed

- **Unified application session lifecycle** (`#44`): prompts across CLI, server, embedded client,
  Telegram, and delegated tasks now share canonical turn events, results, error envelopes,
  metrics, and queue ownership; the legacy message queue manager is deprecated.

### Fixed

- **Runtime and persistence reliability** (`#47`): observer failures no longer strand turns,
  synchronous tool cancellation has a bounded settle deadline, failed memory consolidation
  remains retryable without advancing its cursor, and session deletion shares the save lock.
  Telegram delivery hardened in step: typing indicators are bounded without duplicate sends,
  and non-retryable bad requests no longer loop through the retry path.
- **Strict, transactional patch application** (`#48`): `apply_patch` computes every operation
  before writing to disk so a partial failure leaves all files untouched; additions-only hunks
  reject missing anchors instead of silently appending, and fuzzy fallback matching verifies
  deletion lines against file content rather than trusting an arithmetic offset.
- **Bounded MCP bridge thread joins** (`#43`): `_run_coroutine_in_thread` gives up after a
  timeout and raises a tool execution error instead of hanging forever when a coroutine never
  completes.
- **Configuration and authorization enforcement** (`#44`): `edit_tool_enabled`, parent-scoped
  sub-agents, explicit hook fail-open/fail-closed behavior, and the complete Telegram client
  contract are now enforced.
- **Skill directory paths in docs** (`#41`): stale pre-rename `.amcp` paths in the skill-creator
  and session-cleanup SKILL.md examples and a `SkillManager` docstring aligned with the current
  `.ankaloop` layout.

---

## [0.14.0-rc.1] — 2026-08-17

First release under the **AnkaLoop** name (formerly AMCP).

### Added

- **AMCP → AnkaLoop rebrand**: PyPI package renamed `amcp-agent` → `ankaloop`; CLI entry points are now `anka` (recommended) and `ankaloop` (`amcp`/`amcp-agent` removed); module path migrated `src/amcp` → `src/ankaloop`; public classes renamed `AMCPConfig` → `AnkaloopConfig`, `AMCPClient` → `AnkaloopClient`, `AMCPClientError` → `AnkaloopClientError`; environment variables renamed `AMCP_*` → `ANKA_*` (no legacy fallback); hook exports expose `ANKA_*` env vars and the `$ANKA_PROJECT_DIR` placeholder.
- **Config and project directories renamed** (`#39`): user config dir `~/.config/amcp` → `~/.config/ankaloop`, project dir `.amcp/` → `.ankaloop/`, Telegram log `amcp.log` → `ankaloop.log`. New `src/ankaloop/constants.py` is the single source of truth (`CONFIG_DIR_NAME`, `PROJECT_DIR_NAME`); `ANKA_CONFIG_DIR_NAME=amcp` keeps legacy deployments on the old path for gradual migration.
- **AnkaLoop brand assets**: logo, mark, wordmark, and favicon SVG/PNG assets under `assets/brand/`.
- **VM and Kubernetes deployment recipes**: `deploy-vm/` (systemd service, supervisor and restart scripts, install script, env templates) and `deploy-k8s/` (kustomize manifests) for running AnkaLoop on a VM or in-cluster.
- **Release workflow prerelease handling**: rc/beta/alpha tags are now marked as GitHub prereleases.

### Fixed

- **Memory tags passed as a bare string** (`#38`): `"tags": "research"` no longer serializes character-by-character into `HISTORY.md` headers (`[r, e, s, e, a, r, c, h]`) or the SQLite events table; tags are normalized at every `append_history` entry point.
- **Prompt templates rebranded** (`#38`): `coder.md`, `coder_anthropic.md`, `explorer.md`, and `planner.md` open with the AnkaLoop identity instead of AMCP; the rendered prompt no longer depends on the git log block for the name.
- **Telegram `/skills` output** (`#40`): the listing now shows only skill names and status (full details via `/skills show <name>`), and oversized plain-text messages are split at newline boundaries using UTF-16 length so long command output no longer fails with `BadRequest: Message is too long`.
- **AgentBox deploy files rebranded**: `deploy-gmi/gmi-entrypoint.sh` reads `ANKA_*` env vars (previously failed on first boot against `ANKA_*`-only code); Dockerfile and registration manifest aligned.

### Changed

- **README refreshed for the AnkaLoop launch**: rewritten quick start, configuration, and deployment docs.
- **Deploy directory naming unified**: `deploy_k8s` → `deploy-k8s`, matching `deploy-vm`/`deploy-gmi`; local secret/env files untracked with example templates kept.

---

## [0.13.0] — 2026-08-16

### Added

- **Provider error classification and retry** (`#28`): structured `ProviderError` with type, retryability, and `retry_after`; agent-level exponential backoff with jitter for transient errors; partial-stream detection disables retry to prevent duplicate messages.
- **Persistent session timeline** (`#28`): `SessionTimelineStore` appends sanitized JSONL events per session with bounded retention (2000 events, 90% kept). All secrets and content fields are stripped via whitelist before persistence.
- **Server authentication** (`#28`): `validate_security()` refuses to start when auth is enabled without a key, or bound to a non-loopback address without auth. HTTP middleware uses constant-time comparison (`secrets.compare_digest`). WebSocket endpoints enforce auth on upgrade.
- **MCP tool-call pairing repair** (`#27`): traverses full exception chain (not just outermost) for `_is_tool_call_pairing_error`; MCP tools exposed under provider-safe function names.

### Fixed

- **Sub-agent task lifecycle** (`#28`): hardened ownership tracking and cancellation propagation for spawned sub-agents.
- **Agent runtime ownership and cancellation** (`#28`): deterministic turn cancellation with `TurnHandle`; `RuntimeClosedError` and `TurnCancelledError` for clean teardown.
- **Duplicate Telegram memory history** (`#28`): memory log entries no longer duplicated when Telegram delivery retries.
- **Gemini tool-call pairing** (`#27`): `_repair_tool_call_pairing` now synthesizes missing tool responses and drops orphaned results, preserving Gemini thought signatures.
- **Grep singular path** (`#26`): `grep` tool accepts a single `path` string (previously required a list).

---

## [0.12.0] — 2026-07-19

### Added

- **any-llm provider migration** (`#23`): LLM backend migrated from hand-rolled OpenAI client to [`any-llm-sdk`](https://pypi.org/project/any-llm-sdk/), adding first-class support for Anthropic Claude, Google Gemini, and any OpenAI-compatible endpoint. Provider selection is config-driven via `[chat.providers.<name>]`.
- **Telegram scheduled prompt loop**: in-process cron scheduler for recurring autonomous tasks, triggered by natural language or blueprint-based prompts. Store-backed persistence survives restarts.
- **Telegram memory dream loop**: `MemoryDreamer` periodically consolidates recent episodic events into long-term `MEMORY.md` using an LLM pass, discarding transient chatter and keeping durable facts.
- **Session transcript indexing**: `session_search` tool searches persisted conversation history across all sessions via SQLite FTS5.
- **Structured compaction summaries**: context compaction now produces structured summaries instead of flat text, improving follow-up turn quality.
- **Periodic memory review isolation**: memory review runs on a turn interval (`MEMORY_REVIEW_TURN_INTERVAL = 10`) without blocking the main tool loop.
- **Telegram session reset serialization**: `/new` and session abandonment are serialized to prevent race conditions between concurrent turns.
- **Frozen memory prompt context**: memory guidance text is frozen per-turn to avoid mid-turn drift.

### Changed

- **V2-only session schema** (`#24`): legacy V1 session files are no longer loaded; `SessionState` is now the canonical, recoverable representation. Session runtime unified with real cancellation and async execution.
- **Tool capabilities and workspace confinement** (`#24`): tools declare capabilities (read/write/execute); workspace boundaries enforced for file operations; session persistence hardened against partial writes.
- **Default model updated to GPT-5.5**: legacy `gpt-4o` references replaced throughout config defaults and model metadata.
- **Server and Telegram session queues aligned** (`#22`): both transports now use the same bounded-queue logic with consistent backpressure behavior.
- **Telegram `/schedule` command hidden** in favor of natural-language scheduling.
- **Built-in web providers hidden** from the model picker; users configure their own via `config.toml`.
- **Legacy REPL chat path removed**: `repl` read shortcut and legacy chat module cleaned up.

### Fixed

- **All mypy type errors resolved** (72 → 0): full type coverage across `src/amcp`.
- **Configured LLM base URLs preserved**: provider-specific `base_url` no longer overwritten by defaults.
- **Telegram typing indicator**: now stops correctly after response delivery (previously could persist).
- **CI compatibility with latest Ruff**: formatting and lint rules updated.

### Removed

- **ACP (Agent Client Protocol) integration** removed: AMCP no longer ships ACP server/client code. The HTTP/WebSocket API remains the primary remote interface.

---

## [0.11.1] — 2026-07-19

### Fixed

- Patch release with no user-facing changes; version bump to align PyPI publication.

---

## [0.11.0] — 2026-07-06

### Added

- **Telegram memory persistence** (`#21`): memories created in Telegram sessions are now flushed to persistent storage across sessions, not lost on restart.
- **Context usage in Telegram status** (`#20`): the Telegram status line now shows current context window utilization, giving visibility into compaction triggers.
- **Docker image published to GHCR** (`#18`): CI workflow builds and pushes the AMCP image to `ghcr.io/tao12345666333/amcp` on every tag and main push.
- **Telegram provider switching** (`#17`): `/model` command switches active LLM provider at runtime without restart.
- **Telegram sender photo support**: the `telegram-sender` skill and `telegram_send` tool can now send images.

### Fixed

- **Reasoning-only provider replies surfaced**: responses containing only reasoning content (no text body) are no longer silently dropped.
- **Context overflow guard**: LLM requests are checked against the model's context window before sending, preventing provider-side truncation errors.
- **Telegram Markdown rendering** (`#19`): Markdown responses now render correctly in Telegram using `telegramify-markdown`.
- **AMCP user agent on OpenAI clients**: custom `User-Agent` header set on all outbound LLM requests.

### Changed

- **Default provider switched to GMI Cloud** (`#4c1785b`): `gmi` with `openai/gpt-5.5` is the new out-of-box default.
- **Cron CLI commands removed**: scheduling is now handled by the Telegram scheduler and `config.toml` automation jobs.

---

## [0.10.1] — 2026-07-05

### Fixed

- **Bash tool limit raised** (`#13`): default `bash_tool_limit` increased to support longer autonomous tasks without manual override.
- **Project positioning refresh** (`#12`): README and docs updated to reflect the "runtime, not framework" positioning.

---

## [0.10.0] — 2026-01-19

### Added

- **Soul, identity, and persistent memory** with pre-compaction flush: `SOUL.md` and `IDENTITY.md` define the agent's durable persona; memory is flushed before context compaction to prevent loss.
- **SQLite + FTS5 persistent memory**: episodic events and declarative facts stored in `memory.db` with full-text search.
- **Telegram bot integration** (`#6`): full Telegram support with DM/group policies, pairing via one-time codes, topic/thread support, rate limiting, typing indicators, and bounded per-session queues.
- **In-process assistant scheduler**: cron-based autonomous task execution in Telegram mode, with blueprint-based and custom scheduled prompts.
- **First-class `TelegramSendTool`**: agent can send messages and photos directly via Telegram Bot API.
- **Web search and fetch tools**: built-in `web_search` and `web_fetch` work without external API keys.
- **Networked research skill**: multi-source web research with synthesis.
- **Skills system** (`#2`, `#3`): reusable skill definitions (Markdown + YAML frontmatter) with auto-triggers, parameters, hot reload, and `/skill:` command. Built-in `skill-creator` for self-evolution.
- **Slash commands**: TOML-based custom commands with `{{args}}`, `!{shell}`, and `@{file}` interpolation.
- **Heartbeat builtin skill**: file-driven periodic health checks and proactive task execution.
- **Multimedia message support** for Telegram: photos, audio, documents parsed and forwarded to the agent.
- **Unified interaction routing**: slash commands share a single routing layer across CLI, server, and Telegram.
- **Progressive context view (Phase 9 MVP)**: tools and skills loaded dynamically based on relevance scoring and context budget.

### Changed

- **Bash tool promoted to always tier**: available from the first turn without progressive loading.
- **Project documentation refreshed**: README, contributing guide, and project structure docs updated.
- **License unified to Apache-2.0**.

### Fixed

- **Telegram session isolation after failures**: failed sessions are quarantined rather than blocking the chat.
- **Model config propagation**: `model_config` now correctly flows to context window resolution; default raised to 200K.
- **Thread safety for `TodoTool` and global tool registry**.

### Removed

- **Daemon process management**: removed in favor of Docker/systemd for production deployments.
- **Toad TUI support**: removed to reduce surface area.
- **Legacy `jobs` system**: replaced by `config.toml` automation and the Telegram scheduler.

---

## [0.9.0] — 2026-01-07

### Added

- **Client-Server architecture (Phase 2-3)**: FastAPI HTTP/WebSocket server with streaming, SSE events, session management, and a Python client SDK (`embedded`, `http_client`, `ws_client` transports).
- **Live rendering**: streaming responses rendered incrementally in the CLI.

---

## [0.8.0] — 2026-01-05

### Added

- **Toad TUI option** (`#1`): optional terminal UI mode.

### Fixed

- **72 mypy type checking errors resolved**.

---

## [0.7.0] — 2026-01-02

### Added

- **Indentation-aware file reading mode**: `read_file` supports anchor-based context expansion around functions and classes.
- **Installation instructions** improved in README.

### Fixed

- **`readfile` indexing bug**.

---

## [0.6.0] — 2025-12-29

### Added

- **Skills and commands system**: extensible agent capabilities via skill definitions and slash commands.
- **`apply_patch` tool**: diff-based patching for efficient multi-line edits.

---

## [0.5.0] — 2025-12-27

### Added

- **Hooks system**: configurable `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`, `Stop`, and `PreCompact` hooks via TOML or JSON.

---

## [0.4.0] — 2025-11-30

### Added

- Initial public release: core agent engine, built-in tools (`read_file`, `grep`, `bash`, `write_file`), TOML configuration, CLI interface, and Dockerfile.

[Unreleased]: https://github.com/tao12345666333/ankaloop/compare/v0.15.0...HEAD
[0.15.0]: https://github.com/tao12345666333/ankaloop/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/tao12345666333/ankaloop/compare/v0.14.0-rc.1...v0.14.0
[0.14.0-rc.1]: https://github.com/tao12345666333/ankaloop/compare/v0.13.0...v0.14.0-rc.1
[0.13.0]: https://github.com/tao12345666333/ankaloop/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/tao12345666333/ankaloop/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/tao12345666333/ankaloop/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/tao12345666333/ankaloop/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/tao12345666333/ankaloop/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/tao12345666333/ankaloop/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/tao12345666333/ankaloop/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/tao12345666333/ankaloop/compare/v0.7.3...v0.8.0
[0.7.0]: https://github.com/tao12345666333/ankaloop/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/tao12345666333/ankaloop/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/tao12345666333/ankaloop/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/tao12345666333/ankaloop/releases/tag/v0.4.0
