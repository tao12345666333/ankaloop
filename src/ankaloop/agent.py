"""Agent execution engine with tool support, hooks, and MCP integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.status import Status

from .agent_spec import ResolvedAgentSpec, get_default_agent_spec
from .application_services import ApplicationServices
from .compaction import (
    SmartCompactor,
    estimate_request_tokens,
    estimate_tokens,
    get_model_context_window,
)
from .config import AnkaloopConfig, ChatConfig, ContextConfig, ModelConfig, load_config
from .constants import CONFIG_DIR_NAME
from .context_builder import ContextBuilder
from .hooks import run_post_tool_use_hooks, run_pre_tool_use_hooks, run_user_prompt_hooks
from .llm import ContextOverflowError, ProviderError, classify_provider_error
from .mcp_client import list_mcp_tools
from .mcp_naming import is_mcp_tool_name, mcp_tool_name
from .memory import get_memory_manager
from .memory_review import run_memory_review
from .multi_agent import AgentConfig, get_agent_registry
from .progressive.context_budget import ContextBudget, ContextBudgetManager, estimate_text_tokens
from .progressive.relevance import RelevanceScorer
from .progressive.skill_view import ProgressiveSkillView
from .progressive.tool_view import ProgressiveToolView
from .project_rules import ProjectRulesLoader
from .runtime import (
    CancellationResult,
    MessagePriority,
    RuntimeClosedError,
    SessionRuntime,
    TurnHandle,
    TurnRequest,
)
from .session_search import get_transcript_store  # noqa: F401 - legacy patch seam
from .session_state import SessionState
from .session_store import SessionStore, SessionTimelineStore
from .skills import get_skill_manager
from .tool_execution import (
    ToolCapability,
    ToolExecutionContext,
    ToolExecutor,
)
from .tool_journal import JournalRecovery, ToolJournal
from .tool_loop import ToolLoop
from .turn_service import TurnService

logger = logging.getLogger(__name__)

DEFAULT_BASH_TOOL_LIMIT = 100
MEMORY_REVIEW_TURN_INTERVAL = 10
MEMORY_LOG_USER_LIMIT = 1000
MEMORY_LOG_AGENT_LIMIT = 2000


class AgentExecutionError(Exception):
    """Raised when agent execution fails."""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class MaxStepsReached(Exception):
    """Raised when agent reaches maximum execution steps."""

    pass


def _repair_tool_call_pairing(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure assistant tool_calls and tool results stay strictly paired.

    Gemini-family backends reject requests where a function-call turn is not
    followed by exactly one function-response part per call. History that
    passes through trimming or compaction can lose responses, so missing ones
    are synthesized and orphaned tool results are dropped.
    """
    repaired: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    pending_names: dict[str, str] = {}

    def flush_pending() -> None:
        for call_id in pending_ids:
            tool_result: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "[Tool result unavailable: synthesized to keep tool-call history paired]",
            }
            # Strict providers reject an empty function name, so only carry the
            # name when the paired call actually reported one. Provider
            # extra_content (e.g. Gemini thought signatures) stays on the call.
            paired_name = pending_names.get(call_id)
            if paired_name:
                tool_result["name"] = paired_name
            repaired.append(tool_result)
        pending_ids.clear()
        pending_names.clear()

    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            flush_pending()
            repaired.append(message)
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id")
                if isinstance(call_id, str) and call_id:
                    pending_ids.append(call_id)
                    function = call.get("function")
                    if isinstance(function, dict) and isinstance(function.get("name"), str):
                        pending_names[call_id] = function["name"]
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id in pending_ids:
                pending_ids.remove(call_id)
                repaired.append(message)
            # Orphaned tool results are dropped.
        else:
            flush_pending()
            repaired.append(message)
    flush_pending()
    return repaired


def _is_tool_call_pairing_error(error: Exception) -> bool:
    """Check whether an error is a provider-side tool-call pairing rejection."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if "function response parts" in text or ("function call" in text and "invalid_argument" in text):
            return True
        current = current.__cause__
    return False


class BusyError(Exception):
    """Raised when agent session is busy processing another request."""

    pass


class Agent:
    """
    Enhanced agent execution engine with tool calling and conversation management.

    Features:
    - Context management with compression
    - Tool execution tracking and limits
    - Error handling and retries
    - Status reporting and progress indication
    - Conversation history persistence
    - Project rules loading from AGENTS.md
    - Event callbacks for real-time monitoring
    """

    def __init__(
        self,
        agent_spec: ResolvedAgentSpec | None = None,
        session_id: str | None = None,
        *,
        ephemeral: bool = False,
        services: ApplicationServices | None = None,
    ):
        """Initialize an agent, optionally without automatic durable projections."""
        from .task import TaskManager

        self.agent_spec = agent_spec or get_default_agent_spec()
        self._uses_default_services = services is None
        self.services = services or ApplicationServices.default()
        self.ephemeral = ephemeral
        self.console = Console()
        self.tool_registry = self.services.tool_registry
        self.execution_context: dict[str, Any] = {}
        self.step_count = 0
        self.tool_calls_history: list[dict[str, Any]] = []

        # Conversation history management
        self.session_id = session_id or self._generate_session_id()
        self._task_manager = TaskManager()
        self.conversation_history: list[dict[str, Any]] = []
        self._session_store = SessionStore(
            Path.home() / ".config" / CONFIG_DIR_NAME / "sessions",
            self.session_id,
        )
        self._timeline_store = SessionTimelineStore(
            self._session_store.root,
            self.session_id,
        )
        self._tool_journal = ToolJournal(
            self._session_store.root,
            self.session_id,
            enabled=not self.ephemeral,
        )
        self.session_file = self._session_store.path
        self._session_state = SessionState(
            session_id=self.session_id,
            agent_name=self.name,
        )

        # Tool call tracking for per-conversation and per-session limits
        self.current_conversation_tool_calls: list[dict[str, Any]] = []

        # Per-request tracking (reset on each new request)
        self.current_request_tool_calls: int = 0  # Tools called in current request
        self.current_request_llm_calls: int = 0  # LLM calls in current request

        # Session-level cumulative tracking
        self.total_llm_calls: int = 0  # Total LLM calls across entire session
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cached_input_tokens: int = 0
        self.total_cache_write_input_tokens: int = 0
        self.usage_reported_llm_calls: int = 0
        self.estimated_input_llm_calls: int = 0
        self.last_context_tokens: int = 0
        self.last_context_window: int = 0
        self.last_output_tokens: int | None = None
        self.last_usage_from_api: bool = False
        self._last_memory_review_turn_count: int = 0
        self._frozen_memory_project_root: Path | None = None
        self._frozen_persona_context: str = ""
        self._frozen_memory_context: str = ""
        self._pending_memory_review_tasks: set[asyncio.Task[None]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False
        self._close_complete = False

        # Project rules loader (will be initialized with work_dir during run)
        self._project_rules_loader: ProjectRulesLoader | None = None

        # Event callbacks for real-time monitoring (used by server)
        self._event_callbacks: list[Callable[[str, dict[str, Any]], None]] = []

        # Progressive context selection components
        self._relevance_scorer = RelevanceScorer()
        self._progressive_tool_view = ProgressiveToolView(self._relevance_scorer)
        self._progressive_skill_view = ProgressiveSkillView(self._relevance_scorer)
        self.context_builder = ContextBuilder(self)
        self.tool_loop = ToolLoop(self)
        self.turn_service = TurnService(self)
        self._recovery_status: JournalRecovery | None = None
        self._runtime = SessionRuntime(
            self.session_id,
            self._process_turn_request,
            self._on_runtime_event,
        )

        # Ephemeral agents keep turn state in memory but never load a user session.
        if not self.ephemeral:
            self._load_conversation_history()
            self._scan_recovery_status()

        # If this is a new session (no existing history), reset the current conversation counter
        if not self.conversation_history:
            self._reset_current_conversation_tool_calls()

    def _generate_session_id(self) -> str:
        """Generate a unique session ID."""
        return str(uuid.uuid4())[:8]

    def _ensure_sessions_dir(self) -> None:
        """Ensure sessions directory exists."""
        self._session_store.root.mkdir(parents=True, exist_ok=True)

    def _load_conversation_history(self) -> None:
        """Load conversation history from session file."""
        data = self._session_store.load()
        if data is None:
            return
        self._session_state = SessionState.from_snapshot(data, self.session_id)
        self._apply_session_state(self._session_state)
        self.console.print(
            f"[dim]Loaded conversation history: {len(self.conversation_history)} messages, "
            f"{len(self.tool_calls_history)} total tool calls[/dim]"
        )

    def _save_conversation_history(self) -> None:
        """Save the current canonical state, propagating commit failures."""
        candidate = self._session_state.clone()
        candidate = self._capture_session_state(candidate)
        if not self.ephemeral:
            candidate.revision = self._session_store.save(
                candidate.to_snapshot(),
                expected_revision=candidate.revision,
            )
        self._session_state = candidate
        self._apply_session_state(candidate)

    def _capture_session_state(self, state: SessionState) -> SessionState:
        """Copy compatibility attributes into one candidate session state."""
        state.tool_calls_history = deepcopy(self.tool_calls_history)
        state.current_conversation_tool_calls = deepcopy(self.current_conversation_tool_calls)
        state.usage.total_llm_calls = self.total_llm_calls
        state.usage.total_input_tokens = self.total_input_tokens
        state.usage.total_output_tokens = self.total_output_tokens
        state.usage.total_cached_input_tokens = self.total_cached_input_tokens
        state.usage.total_cache_write_input_tokens = self.total_cache_write_input_tokens
        state.usage.usage_reported_llm_calls = self.usage_reported_llm_calls
        state.usage.estimated_input_llm_calls = self.estimated_input_llm_calls
        state.usage.last_context_tokens = self.last_context_tokens
        state.usage.last_context_window = self.last_context_window
        state.usage.last_output_tokens = self.last_output_tokens
        state.usage.last_usage_from_api = self.last_usage_from_api
        state.last_memory_review_turn_count = self._last_memory_review_turn_count
        return state

    def _apply_session_state(self, state: SessionState) -> None:
        """Expose one state through the existing Agent compatibility attributes."""
        self.conversation_history = state.messages
        self.tool_calls_history = state.tool_calls_history
        self.current_conversation_tool_calls = state.current_conversation_tool_calls
        self.total_llm_calls = state.usage.total_llm_calls
        self.total_input_tokens = state.usage.total_input_tokens
        self.total_output_tokens = state.usage.total_output_tokens
        self.total_cached_input_tokens = state.usage.total_cached_input_tokens
        self.total_cache_write_input_tokens = state.usage.total_cache_write_input_tokens
        self.usage_reported_llm_calls = state.usage.usage_reported_llm_calls
        self.estimated_input_llm_calls = state.usage.estimated_input_llm_calls
        self.last_context_tokens = state.usage.last_context_tokens
        self.last_context_window = state.usage.last_context_window
        self.last_output_tokens = state.usage.last_output_tokens
        self.last_usage_from_api = state.usage.last_usage_from_api
        self._last_memory_review_turn_count = state.last_memory_review_turn_count

    def _commit_session_state(self, candidate: SessionState) -> None:
        """Persist a complete candidate before publishing it in memory."""
        if not self.ephemeral:
            candidate.revision = self._session_store.save(
                candidate.to_snapshot(),
                expected_revision=candidate.revision,
            )
        self._session_state = candidate
        self._apply_session_state(candidate)

    async def clear_conversation_history(self) -> None:
        """Cancel owned work and atomically replace the session state."""
        await self.reset_session()

    def _resolve_memory_project_root(self, work_dir: Path | None = None) -> Path:
        """Return the project root used for project-scoped persistent memory."""
        configured_root = self.execution_context.get("memory_project_root")
        if configured_root:
            return Path(configured_root).expanduser().resolve()
        return work_dir.resolve() if work_dir else Path.cwd()

    def _memory_history_scope(self, work_dir: Path | None = None) -> str:
        """Return the default scope for automatic conversation history entries."""
        if self.execution_context.get("memory_project_root"):
            return "project"
        return "project" if work_dir else "user"

    @staticmethod
    def _conversation_turn_count(messages: list[dict[str, Any]]) -> int:
        """Count user turns in a conversation snapshot."""
        return sum(1 for msg in messages if msg.get("role") == "user")

    @staticmethod
    def _trim_memory_log_text(text: str, limit: int) -> str:
        """Trim text for compact, readable memory history logs."""
        stripped = text.strip()
        if len(stripped) <= limit:
            return stripped
        return stripped[:limit].rstrip() + "\n[... truncated ...]"

    def _format_conversation_history_entry(self, user_input: str, result: str) -> str:
        """Format an automatic conversation history entry."""
        user = self._trim_memory_log_text(user_input, MEMORY_LOG_USER_LIMIT)
        agent = self._trim_memory_log_text(result, MEMORY_LOG_AGENT_LIMIT)
        return f"User:\n{user}\n\nAgent:\n{agent}"

    def reset_memory_context_snapshot(self) -> None:
        """Refresh memory prompt context on the next system prompt build."""
        self._frozen_memory_project_root = None
        self._frozen_persona_context = ""
        self._frozen_memory_context = ""

    def _get_memory_prompt_context(self, work_dir: Path | None) -> tuple[str, str]:
        """Return the session-frozen persona and memory prompt context."""
        memory_project_root = self._resolve_memory_project_root(work_dir)
        if self._frozen_memory_project_root != memory_project_root:
            memory_manager = self._memory_manager(memory_project_root)
            self._frozen_memory_project_root = memory_project_root
            self._frozen_persona_context = memory_manager.get_persona_context()
            self._frozen_memory_context = memory_manager.get_memory_context()
        return self._frozen_persona_context, self._frozen_memory_context

    def get_conversation_summary(self) -> dict[str, Any]:
        """Get summary of the conversation (session-level statistics)."""
        return {
            "session_id": self.session_id,
            "agent_name": self.name,
            "message_count": len(self.conversation_history),
            "total_tool_calls": len(self.tool_calls_history),  # Session total
            "total_llm_calls": self.total_llm_calls,  # Session total
            "session_file": str(self.session_file),
        }

    def get_timeline(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent durable execution events for this session."""
        return self._timeline_store.read(limit=limit)

    def delete_persisted_session(self) -> None:
        """Delete the durable conversation snapshot and execution timeline."""
        self._session_store.delete()
        self._timeline_store.delete()
        self._tool_journal.delete()

    def get_token_usage_summary(self) -> dict[str, Any]:
        """Return current context usage and provider-reported session totals."""
        context_window = self.last_context_window
        if not context_window:
            cfg = load_config()
            model_config = cfg.chat.model_config if cfg.chat else None
            context_window = get_model_context_window(
                self._resolve_model_name(cfg),
                model_config=model_config,
            )
        usage_ratio = self.last_context_tokens / context_window if context_window else 0.0
        return {
            "context_tokens": self.last_context_tokens,
            "context_window": context_window,
            "context_usage_ratio": usage_ratio,
            "last_output_tokens": self.last_output_tokens,
            "last_usage_from_api": self.last_usage_from_api,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "total_cached_input_tokens": self.total_cached_input_tokens,
            "total_cache_write_input_tokens": self.total_cache_write_input_tokens,
            "usage_reported_llm_calls": self.usage_reported_llm_calls,
            "estimated_input_llm_calls": self.estimated_input_llm_calls,
            "total_llm_calls": self.total_llm_calls,
        }

    @property
    def name(self) -> str:
        return self.agent_spec.name

    @property
    def max_steps(self) -> int:
        return self.agent_spec.max_steps

    def add_event_callback(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Register an event callback for real-time monitoring.

        Args:
            callback: Function that receives (event_type, event_data)
        """
        self._event_callbacks.append(callback)

    def remove_event_callback(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Remove an event callback.

        Args:
            callback: The callback to remove
        """
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event to all registered callbacks.

        Args:
            event_type: Type of event (e.g., 'tool.call_start', 'tool.call_complete')
            data: Event data
        """
        event_data = {
            "session_id": self.session_id,
            "agent_name": self.name,
            "timestamp": datetime.now().isoformat(),
            **data,
        }
        if event_type == "message.chunk" or event_type.startswith("tool.call_"):
            active_turn = self._runtime.active_turn
            if active_turn is not None:
                event_data["turn_id"] = active_turn.id
                self._runtime.publish_turn_event(active_turn.id, event_type, event_data)
        if not self.ephemeral and event_type.startswith(("turn.", "tool.", "provider.", "llm.", "context.", "memory.")):
            try:
                self._timeline_store.append(
                    event_type,
                    data,
                    timestamp=event_data["timestamp"],
                )
            except Exception as exc:
                logger.debug("Timeline persistence failed (non-critical): %s", exc)
        for callback in self._event_callbacks:
            with contextlib.suppress(Exception):
                callback(event_type, event_data)

    def _resolve_context_config(self, cfg: AnkaloopConfig | None = None) -> ContextConfig:
        """Load context config with defaults."""
        resolved = cfg or load_config()
        return resolved.context or ContextConfig()

    def _resolve_turn_config(self) -> AnkaloopConfig:
        """Resolve one provider/config snapshot for the complete turn.

        Empty AgentSpec model/base URL values inherit the currently active
        provider. Explicit values pin only that field for this agent.
        """
        cfg = deepcopy(load_config())
        chat = cfg.chat or ChatConfig()
        if self.agent_spec.model:
            if chat.model != self.agent_spec.model and (
                chat.model_config is None or chat.model_config.model_id != self.agent_spec.model
            ):
                chat.model_config = None
            chat.model = self.agent_spec.model
        if self.agent_spec.base_url:
            chat.base_url = self.agent_spec.base_url
        cfg.chat = chat
        return cfg

    def _resolve_model_name(self, cfg: AnkaloopConfig | None = None) -> str:
        """Resolve model name used for budget and token decisions."""
        resolved_cfg = cfg or load_config()
        if self.agent_spec.model:
            return self.agent_spec.model
        if resolved_cfg.chat and resolved_cfg.chat.model:
            return resolved_cfg.chat.model
        return "DeepSeek-V3.1-Terminus"

    def _calculate_context_budget(
        self,
        conversation_tokens: int,
        model_name: str | None = None,
        model_config: ModelConfig | None = None,
        context_config: ContextConfig | None = None,
    ) -> ContextBudget:
        """Calculate context budget for current request."""
        context_cfg = context_config or self._resolve_context_config()
        model = model_name or self._resolve_model_name()
        manager = ContextBudgetManager(model=model, config=context_cfg, model_config=model_config)
        return manager.calculate_budget(conversation_tokens)

    def _trim_to_token_budget(self, text: str, token_budget: int) -> str:
        """Trim long text to token budget using a stable head/tail strategy."""
        if token_budget <= 0 or not text:
            return ""

        current_tokens = estimate_text_tokens(text)
        if current_tokens <= token_budget:
            return text

        # ``estimate_text_tokens`` uses floor(chars / 4), so this is the
        # largest character count that can still fit the requested budget.
        char_budget = token_budget * 4 + 3
        if len(text) <= char_budget:
            return text

        marker = "\n\n[... trimmed for context budget ...]\n\n"
        if len(marker) >= char_budget:
            return text[:char_budget]

        content_budget = char_budget - len(marker)
        head_chars = int(content_budget * 0.7)
        tail_chars = max(content_budget - head_chars, 0)
        if tail_chars > 0:
            return text[:head_chars].rstrip() + marker + text[-tail_chars:].lstrip()
        return text[:head_chars].rstrip() + marker

    def _get_system_prompt(
        self,
        work_dir: Path | None = None,
        user_input: str = "",
        *,
        conversation_tokens: int,
        cfg: AnkaloopConfig | None = None,
    ) -> str:
        """Compatibility proxy to the context builder."""
        return self.context_builder.get_system_prompt(
            work_dir,
            user_input,
            conversation_tokens=conversation_tokens,
            cfg=cfg,
        )

    def _load_project_rules(self, work_dir: Path) -> str:
        """Load project rules from AGENTS.md files.

        Args:
            work_dir: Working directory to search from

        Returns:
            Combined project rules content or empty string
        """
        # Initialize or update the project rules loader
        if self._project_rules_loader is None or self._project_rules_loader.work_dir != work_dir:
            self._project_rules_loader = ProjectRulesLoader(work_dir)

        rules = self._project_rules_loader.load_rules()

        # Log loaded files
        if rules:
            loaded_files = self._project_rules_loader.get_loaded_files()
            if loaded_files:
                file_names = [f.name for f in loaded_files]
                self.console.print(f"[dim]📋 Loaded project rules: {', '.join(file_names)}[/dim]")

        return rules

    def get_project_rules_info(self) -> dict[str, Any]:
        """Get information about loaded project rules.

        Returns:
            Dictionary with rules information
        """
        if self._project_rules_loader:
            return self._project_rules_loader.get_rules_summary()
        return {"has_rules": False, "files_loaded": []}

    async def _get_mcp_tools_info(self, cfg: AnkaloopConfig) -> list[dict[str, Any]]:
        """Get information about available MCP tools."""
        tools_info = []

        for server_name, server in cfg.servers.items():
            try:
                tools = await list_mcp_tools(server)
                for tool in tools:
                    tools_info.append(
                        {
                            "name": mcp_tool_name(server_name, tool["name"]),
                            "description": tool.get("description", ""),
                            "server": server_name,
                        }
                    )
            except (OSError, ValueError, KeyError) as e:
                self.console.print(f"[yellow]Warning: Could not load tools from {server_name}: {e}[/yellow]")

        return tools_info

    def _should_limit_tool_calls(self, tool_name: str, cfg: AnkaloopConfig | None = None) -> bool:
        """Check if a tool should be limited to prevent infinite loops."""
        # Per-tool limits (each tool tracked separately)
        current_conversation_calls = sum(
            1 for call in self.current_conversation_tool_calls if call.get("tool") == tool_name
        )

        # read_file: 100 per conversation, 600 per session
        if tool_name == "read_file":
            if current_conversation_calls >= 100:
                self.console.print("[yellow]Per-conversation read_file limit reached (100 calls)[/yellow]")
                return True

            total_session_calls = sum(1 for call in self.tool_calls_history if call.get("tool") == "read_file")
            if total_session_calls >= 600:
                self.console.print("[yellow]Per-session read_file limit reached (600 calls)[/yellow]")
                return True
            return False

        # bash can dump large files and quickly balloon tool context during
        # repo analysis. Keep a configurable per-request loop bound; session
        # totals are still tracked in tool_calls_history for diagnostics.
        if tool_name == "bash":
            limit = self._resolve_bash_tool_limit(cfg)
            if limit > 0 and current_conversation_calls >= limit:
                self.console.print(f"[yellow]Per-request bash limit reached ({limit} calls)[/yellow]")
                return True
            return False

        # MCP tools: 100 per tool per conversation
        return is_mcp_tool_name(tool_name) and current_conversation_calls >= 100

    def _resolve_bash_tool_limit(self, cfg: AnkaloopConfig | None = None) -> int:
        """Resolve the per-request bash limit; values <= 0 disable this limit."""
        resolved = cfg or load_config()
        configured = resolved.chat.bash_tool_limit if resolved.chat else None
        if isinstance(configured, int) and not isinstance(configured, bool):
            return configured
        return DEFAULT_BASH_TOOL_LIMIT

    def _reset_current_conversation_tool_calls(self) -> None:
        """Reset the current conversation tool calls counter for a new conversation."""
        self.current_conversation_tool_calls = []

    def _add_execution_context(self, key: str, value: Any) -> None:
        """Add context information for tool execution."""
        self.execution_context[key] = value

    def _get_context_vars(self) -> dict[str, str]:
        """Get context variables for system prompt."""
        return {
            "step_count": str(self.step_count),
            "max_steps": str(self.max_steps),
            "tools_called": str(len(self.tool_calls_history)),
            "work_dir": str(Path.cwd()),
        }

    async def run(
        self,
        user_input: str,
        work_dir: Path | None = None,
        stream: bool = True,
        show_progress: bool = True,
        priority: MessagePriority = MessagePriority.NORMAL,
        queue_if_busy: bool = True,
    ) -> str:
        """
        Run the agent with the given user input.

        Args:
            user_input: User's request
            work_dir: Working directory for context
            stream: Whether to stream responses
            show_progress: Whether to show progress indicators
            priority: Message priority (for queuing)
            queue_if_busy: Whether to queue the message if session is busy

        Returns:
            Agent's response

        Raises:
            AgentExecutionError: If execution fails
            MaxStepsReached: If max steps exceeded
            BusyError: If session is busy and queue_if_busy is False
        """
        return await self.services.session_service.run(
            self,
            user_input,
            work_dir=work_dir,
            stream=stream,
            show_progress=show_progress,
            priority=priority,
            reject_if_busy=not queue_if_busy,
        )

    async def submit(
        self,
        user_input: str,
        *,
        work_dir: Path | None = None,
        stream: bool = True,
        show_progress: bool = True,
        priority: MessagePriority = MessagePriority.NORMAL,
        reject_if_busy: bool = False,
    ) -> TurnHandle:
        """Submit a turn and return a handle that owns its eventual result."""
        return await self.services.session_service.submit(
            self,
            user_input,
            work_dir=work_dir,
            stream=stream,
            show_progress=show_progress,
            priority=priority,
            reject_if_busy=reject_if_busy,
        )

    async def _submit_turn(
        self,
        user_input: str,
        *,
        work_dir: Path | None = None,
        stream: bool = True,
        show_progress: bool = True,
        priority: MessagePriority = MessagePriority.NORMAL,
        reject_if_busy: bool = False,
    ) -> TurnHandle:
        """Submit directly to the owned runtime for the application session service."""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeClosedError(f"Agent session {self.session_id} is closed")
            if reject_if_busy and self._runtime.is_busy:
                raise BusyError(f"Session {self.session_id} is busy processing another request")
            return await self._runtime.submit(
                user_input,
                work_dir=work_dir,
                stream=stream,
                show_progress=show_progress,
                priority=priority,
            )

    async def _process_turn_request(self, request: TurnRequest) -> str:
        self.execution_context["turn_id"] = request.id
        return await self._process_message(
            request.prompt,
            request.work_dir,
            request.stream,
            request.show_progress,
        )

    def _on_runtime_event(self, event: str, handle: TurnHandle) -> None:
        data: dict[str, Any] = {
            "turn_id": handle.id,
            "turn_status": handle.status.value,
        }
        outcome = handle.outcome
        if outcome and isinstance(outcome.error, ProviderError):
            data["error_kind"] = outcome.error.kind.value
            data["status_code"] = outcome.error.status_code
        self._emit_event(event, data)

    async def _process_message(self, user_input: str, work_dir: Path | None, stream: bool, show_progress: bool) -> str:
        """Compatibility proxy to the transactional turn service."""
        return await self.turn_service.process_message(user_input, work_dir, stream, show_progress)

    def _settle_tool_operation(
        self,
        turn_id: str,
        tool_call_id: str,
        *,
        success: bool,
        duration_ms: float | None = None,
        error: str | None = None,
        args_hash: str | None = None,
    ) -> None:
        """Journal the T2 settlement of one tool operation (best-effort).

        Unlike the T1 intent write, a failed settlement write never fails
        the tool call: the result already happened and the whole-turn
        commit remains the authoritative durability point.  A missed T2
        surfaces later as a conservatively-open operation.
        """
        try:
            self._tool_journal.tool_outcome(
                turn_id,
                tool_call_id,
                success=success,
                duration_ms=duration_ms,
                error=error,
                args_hash=args_hash,
            )
        except Exception as exc:
            logger.warning("Tool journal settlement failed for %s: %s", tool_call_id, exc)
        self._emit_event(
            "tool.settled",
            {
                "operation_id": f"{turn_id}:{tool_call_id}",
                "tool_id": tool_call_id,
                "success": success,
            },
        )

    def _scan_recovery_status(self) -> None:
        """Detect interrupted turns and open tool operations from the journal."""
        try:
            recovery = self._tool_journal.resolve()
        except Exception as exc:
            logger.warning("Tool journal scan failed for session %s: %s", self.session_id, exc)
            return
        self._recovery_status = recovery
        if not recovery.requires_attention:
            return
        open_ops = [d for d in recovery.decisions if d.status != "completed"]
        logger.warning(
            "Session %s recovered with %d interrupted turn(s), %d open/corrupt tool operation(s)",
            self.session_id,
            len(recovery.interrupted_turns),
            len(open_ops),
        )
        for turn in recovery.interrupted_turns:
            logger.warning(
                "Interrupted turn %s (open operations: %s)",
                turn.turn_id,
                ", ".join(turn.open_operations) or "none",
            )
        for decision in open_ops:
            logger.warning(
                "Open tool operation %s (%s): %s - %s",
                decision.operation_id,
                decision.tool_name,
                decision.status,
                decision.reason,
            )
        self._emit_event("turn.recovery_detected", {"summary": recovery.summary()})

    def get_recovery_status(self) -> JournalRecovery:
        """Resolve and return the current journal recovery status."""
        self._recovery_status = self._tool_journal.resolve()
        return self._recovery_status

    def acknowledge_recovery(self) -> dict[str, Any]:
        """Acknowledge all unsettled operations after human review.

        The journal is evidence-only and recovery is fail-closed: without
        an explicit operator decision, an interrupted turn would resurface
        on every startup.  The cached scan is refreshed afterwards so the
        acknowledged state is visible immediately.
        """
        if self._runtime.active_turn is not None:
            raise BusyError("Cannot acknowledge recovery while a turn is active")
        acked = self._tool_journal.mark_recovery_acknowledged()
        self._recovery_status = self._tool_journal.resolve()
        return acked

    async def _run_memory_review(
        self,
        conversation_snapshot: list[dict[str, Any]],
        system_prompt: str,
        work_dir: Path | None,
        status: Any = None,
        *,
        cfg: AnkaloopConfig | None = None,
    ) -> bool:
        """Run a pre-compaction memory flush to save durable memories.

        Inspired by openclaw's pre-compaction memory flush: before the
        conversation context is summarized/compacted, give the agent one
        chance to save important user preferences, facts, and identity
        details to persistent memory.  Failures are silently ignored.
        """
        try:
            cfg = cfg or self._resolve_turn_config()
            model = self._resolve_model_name(cfg)
            memory_chat = replace(cfg.chat) if cfg.chat else ChatConfig()
            memory_chat.model = model

            from .llm import create_llm_client

            client = create_llm_client(memory_chat)
            self._attach_context_overflow_observer(client)

            # Build memory-only tool list from the application registry.
            registry = self.services.tool_registry
            memory_tool = registry.get_tool("memory")
            if not memory_tool:
                return False
            memory_tools = [memory_tool.get_spec()]

            self._emit_event("memory.review_start", {})
            memory_project_root = self._resolve_memory_project_root(work_dir)
            memory_executor = ToolExecutor(
                context=ToolExecutionContext(
                    session_id=self.session_id,
                    workspace_root=memory_project_root,
                    turn_id="memory-review",
                ),
                capability=ToolCapability.from_spec(["memory"], [], False),
                exposed_tools={"memory"},
                registry=registry,
                mcp_registry={},
                config=cfg,
            )

            result = await run_memory_review(
                client=client,
                model=model,
                system_prompt=system_prompt,
                conversation_snapshot=conversation_snapshot,
                tools=memory_tools,
                tool_executor=memory_executor,
            )

            saved = bool(result and result.strip() != "Nothing to save.")
            self._emit_event(
                "memory.review_complete",
                {"saved": saved},
            )

            if saved:
                self.console.print("[dim]Memory flush: saved durable memories[/dim]")
            else:
                logger.debug("Memory flush: nothing to save")
            return saved

        except Exception as e:
            logger.debug(f"Memory flush failed (non-critical): {e}")
            return False

    async def flush_memory(
        self,
        work_dir: Path | None = None,
        *,
        conversation_snapshot: list[dict[str, Any]] | None = None,
        status: Any = None,
    ) -> bool:
        """Review the current conversation and persist durable memories."""
        snapshot = list(conversation_snapshot or self.conversation_history)
        if not snapshot:
            return False
        cfg = self._resolve_turn_config()
        system_prompt = self._get_system_prompt(
            work_dir,
            conversation_tokens=estimate_tokens(snapshot),
            cfg=cfg,
        )
        saved = await self._run_memory_review(
            conversation_snapshot=snapshot,
            system_prompt=system_prompt,
            work_dir=work_dir,
            status=status,
            cfg=cfg,
        )
        self._last_memory_review_turn_count = self._conversation_turn_count(snapshot)
        self._save_conversation_history()
        return saved

    async def _maybe_run_periodic_memory_review(
        self,
        conversation_snapshot: list[dict[str, Any]],
        system_prompt: str,
        work_dir: Path | None,
        status: Any = None,
        *,
        persist: bool = True,
    ) -> None:
        """Schedule an isolated memory review every N user turns."""
        if self.ephemeral:
            return
        turn_count = self._conversation_turn_count(conversation_snapshot)
        if turn_count - self._last_memory_review_turn_count < MEMORY_REVIEW_TURN_INTERVAL:
            return
        self._last_memory_review_turn_count = turn_count
        if persist:
            self._save_conversation_history()
        self._schedule_periodic_memory_review(
            conversation_snapshot=conversation_snapshot,
            system_prompt=system_prompt,
            work_dir=work_dir,
        )

    def _schedule_periodic_memory_review(
        self,
        conversation_snapshot: list[dict[str, Any]],
        system_prompt: str,
        work_dir: Path | None,
        cfg: AnkaloopConfig | None = None,
    ) -> None:
        """Schedule a best-effort review after its checkpoint is committed."""
        if self.ephemeral:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            logger.debug(f"Could not schedule periodic memory review: {exc}")
            return
        task = loop.create_task(
            self._run_isolated_memory_review(
                conversation_snapshot=list(conversation_snapshot),
                system_prompt=system_prompt,
                work_dir=work_dir,
                cfg=deepcopy(cfg) if cfg is not None else None,
            )
        )
        self._pending_memory_review_tasks.add(task)
        task.add_done_callback(self._on_memory_review_task_done)

    async def _run_isolated_memory_review(
        self,
        conversation_snapshot: list[dict[str, Any]],
        system_prompt: str,
        work_dir: Path | None,
        cfg: AnkaloopConfig | None,
    ) -> None:
        """Run a background memory review without mutating chat history."""
        await self._run_memory_review(
            conversation_snapshot=conversation_snapshot,
            system_prompt=system_prompt,
            work_dir=work_dir,
            status=None,
            cfg=cfg,
        )

    def _on_memory_review_task_done(self, task: asyncio.Task[None]) -> None:
        """Remove completed background review tasks and log failures."""
        self._pending_memory_review_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as e:
            logger.debug(f"Periodic memory review failed (non-critical): {e}")

    def is_busy(self) -> bool:
        """Check if this agent's session is currently busy."""
        return self._runtime.is_busy

    def queued_count(self) -> int:
        """Get the number of queued messages for this session."""
        return self._runtime.queued_count

    def has_pending_work(self, *, excluding: TurnHandle | None = None) -> bool:
        """Return whether this session has other active or queued work."""
        return self._runtime.has_pending_work(excluding=excluding)

    def queued_prompts(self) -> list[str]:
        """Get list of queued prompts for this session."""
        return self._runtime.queued_prompts()

    async def clear_queue(self) -> int:
        """Clear all queued messages for this session."""
        return await self._runtime.clear_queue()

    async def cancel(self, *, clear_queue: bool = False) -> CancellationResult:
        """Cancel current work and return the runtime's actual cancellation result."""
        async with self._lifecycle_lock:
            if clear_queue:
                result = await self._runtime.cancel_all()
            else:
                active_cancelled = await self._runtime.cancel_active()
                result = CancellationResult(
                    active_cancelled=active_cancelled,
                    queued_cancelled=0,
                )
            await self._cancel_memory_review_tasks()
            await self._cancel_delegated_tasks()
            return result

    async def cancel_turn(self, turn_id: str) -> bool:
        """Cancel one active or queued turn without creating another owner."""
        async with self._lifecycle_lock:
            handle = self._runtime.get_turn(turn_id)
            was_active = handle is not None and self._runtime.active_turn is handle
            cancelled = await self._runtime.cancel_turn(turn_id)
            if cancelled and was_active:
                await self._cancel_delegated_tasks()
            return cancelled

    async def reset_session(self) -> None:
        """Cancel all work and atomically replace the committed session state."""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeClosedError(f"Agent session {self.session_id} is closed")
            await self._runtime.cancel_all()
            await self._cancel_memory_review_tasks()
            await self._cancel_delegated_tasks()
            candidate = SessionState(
                session_id=self.session_id,
                agent_name=self.name,
                revision=self._session_state.revision,
            )
            if not self.ephemeral:
                candidate.revision = self._session_store.save(
                    candidate.to_snapshot(),
                    expected_revision=candidate.revision,
                )
            self._session_state = candidate
            self._apply_session_state(candidate)
            self.current_request_llm_calls = 0
            self.current_request_tool_calls = 0
            self.reset_memory_context_snapshot()

    async def _cancel_delegated_tasks(self) -> int:
        """Cancel and await delegated tasks owned by this Agent session."""
        return await self._task_manager.cancel_for_session(self.session_id)

    async def _cancel_memory_review_tasks(self) -> None:
        """Cancel and await pending background memory reviews."""
        tasks = list(self._pending_memory_review_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        """Permanently stop the Agent and await all session-owned resources."""
        async with self._lifecycle_lock:
            if self._close_complete:
                return
            self._closed = True
            await self._runtime.close()
            await self._cancel_delegated_tasks()
            await self._cancel_memory_review_tasks()
            self._close_complete = True

    def get_queue_status(self) -> dict[str, Any]:
        """Get queue status for this session."""
        active = self._runtime.active_turn
        return {
            "session_id": self.session_id,
            "status": self._runtime.status.value,
            "is_busy": self._runtime.is_busy,
            "active_turn_id": active.id if active else None,
            "queued_count": self._runtime.queued_count,
            "queued_prompts": self._runtime.queued_prompts(),
        }

    def get_turn(self, turn_id: str) -> TurnHandle | None:
        """Return a turn handle owned by this session."""
        return self._runtime.get_turn(turn_id)

    async def _build_tools_and_registry(
        self,
        user_input: str = "",
        conversation_history: list[dict[str, Any]] | None = None,
        *,
        cfg: AnkaloopConfig | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]]]:
        """Compatibility proxy to the context builder."""
        return await self.context_builder.build_tools_and_registry(user_input, conversation_history, cfg=cfg)

    def _get_read_file_tool_spec(self) -> dict[str, Any]:
        """Get read_file tool specification."""
        return {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a text file from the local workspace. Returns the full file content with line numbers. If no ranges specified, reads the entire file. CRITICAL: You MUST provide a path to a specific FILE, not a directory. Use relative paths from current working directory (e.g., 'src/ankaloop/readfile.py', 'README.md'), NOT absolute paths starting with '/'. COMMON FILES: 'src/ankaloop/readfile.py', 'src/ankaloop/rg.py', 'src/ankaloop/cli.py', 'src/ankaloop/chat.py', 'README.md', 'pyproject.toml'. NEVER use just 'src/ankaloop' - it's a directory, not a file. IMPORTANT: When you get the file content, analyze it and provide your response - don't call the tool again unless you need additional different files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to a specific FILE (not directory). Use relative paths like 'src/ankaloop/readfile.py', NEVER directories like 'src/ankaloop'. COMMON FILES: 'src/ankaloop/readfile.py', 'src/ankaloop/rg.py', 'src/ankaloop/cli.py', 'src/ankaloop/chat.py', 'README.md', 'pyproject.toml'. Always include the file extension (.py, .md, .toml, etc).",
                        },
                        "ranges": {
                            "type": "array",
                            "items": {"type": "string", "pattern": "^\\d+-\\d+$"},
                            "description": "Optional list of line ranges like '1-200'. Use only if you need specific line ranges. For general file analysis, omit this to get the full file.",
                        },
                        "max_lines": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5000,
                            "description": "Safety cap for lines returned per block (default 400)",
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }

    async def _run_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_registry: dict[str, Any],
        stream: bool,
        status: Status,
        work_dir: Path | None = None,
        *,
        cfg: AnkaloopConfig | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Compatibility proxy to the tool loop."""
        return await self.tool_loop.run(messages, tools, tool_registry, stream, status, work_dir, cfg=cfg)

    async def _enhanced_chat_with_tools(
        self,
        llm_client,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_registry: dict[str, Any],
        stream: bool,
        status: Status,
        work_dir: Path | None = None,
        max_steps: int | None = None,
        *,
        return_message_delta: bool = False,
        cfg: AnkaloopConfig | None = None,
    ) -> str | tuple[str, list[dict[str, Any]]]:
        """Compatibility proxy to the tool loop."""
        return await self.tool_loop.enhanced_chat_with_tools(
            llm_client,
            messages,
            tools,
            tool_registry,
            stream,
            status,
            work_dir,
            max_steps,
            return_message_delta=return_message_delta,
            cfg=cfg,
        )

    async def _call_llm(
        self,
        llm_client: Any,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream_callback: Any | None = None,
        cfg: ChatConfig | None = None,
    ) -> Any:
        """Call a provider with timeout and bounded transient-error retries."""
        policy = cfg or ChatConfig()
        timeout = max(0.1, float(policy.request_timeout_seconds))
        max_retries = max(0, int(policy.max_retries))
        base_delay = max(0.0, float(policy.retry_base_delay_seconds))
        emitted_output = False

        def tracked_callback(chunk: str) -> None:
            nonlocal emitted_output
            emitted_output = True
            if stream_callback is not None:
                stream_callback(chunk)

        callback = tracked_callback if stream_callback is not None else None

        async def call_once() -> Any:
            async_chat = getattr(llm_client, "achat", None)
            if callable(async_chat):
                return await async_chat(
                    messages=messages,
                    tools=tools,
                    stream_callback=callback,
                )
            return await asyncio.to_thread(
                llm_client.chat,
                messages=messages,
                tools=tools,
                stream_callback=callback,
            )

        for attempt in range(max_retries + 1):
            try:
                self.current_request_llm_calls += 1
                self.total_llm_calls += 1
                return await asyncio.wait_for(call_once(), timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                provider_error = classify_provider_error(exc, partial_output=emitted_output)
                if isinstance(provider_error, ContextOverflowError):
                    if not provider_error.timeline_emitted:
                        self._record_context_overflow(provider_error)
                    if provider_error is exc:
                        raise
                    raise provider_error from exc
                if not provider_error.retryable or attempt >= max_retries:
                    self._emit_event(
                        "provider.error",
                        {
                            "error_kind": provider_error.kind.value,
                            "status_code": provider_error.status_code,
                            "partial_output": provider_error.partial_output,
                            "attempt": attempt + 1,
                        },
                    )
                    if provider_error is exc:
                        raise
                    raise provider_error from exc

                retry_delay = provider_error.retry_after
                if retry_delay is None:
                    retry_delay = random.uniform(0.0, min(30.0, base_delay * (2**attempt)))
                retry_delay = min(retry_delay, 60.0)
                self._emit_event(
                    "provider.retry",
                    {
                        "error_kind": provider_error.kind.value,
                        "status_code": provider_error.status_code,
                        "attempt": attempt + 2,
                        "delay_seconds": retry_delay,
                    },
                )
                await asyncio.sleep(retry_delay)

        raise AssertionError("provider retry loop exhausted without returning or raising")

    def _attach_context_overflow_observer(self, llm_client: Any) -> None:
        """Attach timeline reporting to a client when it supports local overflow checks."""
        setter = getattr(llm_client, "set_context_overflow_callback", None)
        if callable(setter):
            setter(self._record_context_overflow)

    def _record_context_overflow(self, error: ContextOverflowError) -> None:
        """Record one sanitized context-overflow event for the active session."""
        if error.timeline_emitted:
            return
        error.timeline_emitted = True
        self._emit_event(
            "context.overflow",
            {
                "input_tokens": error.input_tokens,
                "input_limit": error.input_limit,
                "context_window": error.context_window,
                "output_reserve": error.output_reserve,
            },
        )

    def _record_llm_usage(
        self,
        response: Any,
        estimated_input_tokens: int,
        context_window: int,
    ) -> None:
        """Record context occupancy and provider-reported token consumption."""
        usage = getattr(response, "usage", None)
        self.last_context_window = context_window
        if usage is None:
            self.last_context_tokens = estimated_input_tokens
            self.last_output_tokens = None
            self.last_usage_from_api = False
            self.total_input_tokens += estimated_input_tokens
            self.estimated_input_llm_calls += 1
            self._emit_event(
                "llm.usage",
                {
                    "input_tokens": estimated_input_tokens,
                    "output_tokens": None,
                    "context_window": context_window,
                    "usage_from_api": False,
                },
            )
            return

        self.last_context_tokens = usage.prompt_tokens
        self.last_output_tokens = usage.output_tokens
        self.last_usage_from_api = True
        self.total_input_tokens += usage.prompt_tokens
        self.total_output_tokens += usage.output_tokens
        self.total_cached_input_tokens += usage.cached_input_tokens
        self.total_cache_write_input_tokens += usage.cache_write_input_tokens
        self.usage_reported_llm_calls += 1
        self._emit_event(
            "llm.usage",
            {
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "total_tokens": usage.total_tokens,
                "context_window": context_window,
                "usage_from_api": True,
            },
        )

    @staticmethod
    def _fit_tool_context(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        token_budget: int,
    ) -> list[dict[str, Any]]:
        """Compatibility entry point for request-local context fitting."""
        return ContextBuilder.fit_tool_context(messages, tools, token_budget)

    @staticmethod
    def _repair_tool_call_pairing(messages):
        return _repair_tool_call_pairing(messages)

    @staticmethod
    def _is_tool_call_pairing_error(error):
        return _is_tool_call_pairing_error(error)

    @staticmethod
    def _agent_execution_error(message):
        return AgentExecutionError(message)

    @staticmethod
    def _max_steps_reached(max_steps):
        return MaxStepsReached(max_steps)

    @staticmethod
    def _execution_exception_types():
        return (AgentExecutionError, MaxStepsReached)

    def _memory_manager(self, root: Path | None):
        if self._uses_default_services:
            return get_memory_manager(root)
        return self.services.memory_manager(root)

    def _skill_manager(self):
        if self._uses_default_services:
            return get_skill_manager()
        return self.services.skill_manager

    async def _run_user_prompt_hooks(self, **kwargs):
        return await run_user_prompt_hooks(**kwargs)

    async def _run_pre_tool_use_hooks(self, **kwargs):
        return await run_pre_tool_use_hooks(**kwargs)

    async def _run_post_tool_use_hooks(self, **kwargs):
        return await run_post_tool_use_hooks(**kwargs)

    async def _list_mcp_tools(self, server):
        return await list_mcp_tools(server)

    @staticmethod
    def _smart_compactor(*args, **kwargs):
        return SmartCompactor(*args, **kwargs)

    @staticmethod
    def _estimate_tokens(messages, tools=None):
        if tools is None:
            return estimate_tokens(messages)
        return estimate_request_tokens(messages, tools)

    def _create_progress_context(self, show_progress: bool):
        """Create progress display context."""
        if show_progress:
            return Status("[bold]Agent starting...[/bold]", console=self.console)
        else:
            # Return a null context manager for silent operation
            from contextlib import nullcontext

            class NullStatus:
                """Null status object that does nothing."""

                def update(self, *args, **kwargs):
                    pass

            return nullcontext(NullStatus())

    def get_execution_summary(self) -> dict[str, Any]:
        """Get summary of agent execution (per-request statistics)."""
        return {
            "agent_name": self.name,
            "agent_mode": self.agent_spec.mode.value,
            "llm_calls": self.current_request_llm_calls,  # LLM calls in this request
            "max_llm_calls": self.max_steps,  # Max LLM calls allowed per request
            "tools_called": self.current_request_tool_calls,  # Tools called in this request
            "context_vars": self._get_context_vars(),
            "can_delegate": self.agent_spec.can_delegate,
            "is_busy": self.is_busy(),
            "queued_count": self.queued_count(),
        }


# Factory functions for creating agents from multi-agent configurations


def create_agent_from_config(
    config: AgentConfig,
    session_id: str | None = None,
    *,
    ephemeral: bool = False,
    services: ApplicationServices | None = None,
) -> Agent:
    """
    Create an Agent from an AgentConfig.

    This is the primary way to instantiate agents using the multi-agent system.

    Args:
        config: AgentConfig from the multi_agent module
        session_id: Optional session ID for conversation persistence
        ephemeral: Disable automatic session, timeline, history, and transcript persistence

    Returns:
        Configured Agent instance
    """

    # Convert AgentConfig to ResolvedAgentSpec
    from .agent_spec import ResolvedAgentSpec

    spec = ResolvedAgentSpec(
        name=config.name,
        description=config.description,
        mode=config.mode,
        system_prompt=config.system_prompt,
        tools=config.tools,
        exclude_tools=config.excluded_tools,
        max_steps=config.max_steps,
        model="",  # Use default from config
        base_url="",  # Use default from config
        can_delegate=config.can_delegate,
    )

    return Agent(
        agent_spec=spec,
        session_id=session_id,
        ephemeral=ephemeral,
        services=services,
    )


def create_agent_by_name(
    name: str,
    session_id: str | None = None,
    *,
    services: ApplicationServices | None = None,
) -> Agent:
    """
    Create an Agent by looking up its name in the registry.

    Args:
        name: Name of the agent in the registry (e.g., "coder", "explorer", "planner")
        session_id: Optional session ID for conversation persistence

    Returns:
        Configured Agent instance

    Raises:
        ValueError: If agent name is not found in registry
    """
    registry = services.agent_registry if services is not None else get_agent_registry()
    config = registry.get(name)
    if config is None:
        available = registry.list_agents()
        raise ValueError(f"Unknown agent: {name}. Available agents: {', '.join(available)}")

    return create_agent_from_config(config, session_id, services=services)


def create_subagent(
    parent_agent: Agent,
    task_description: str,
    tools: list[str] | None = None,
) -> Agent:
    """
    Create a subagent for a specific task.

    This creates a new agent that inherits the session from the parent
    but has a focused task and possibly restricted tools.

    Args:
        parent_agent: The parent agent creating this subagent
        task_description: Description of the task for the subagent
        tools: Optional list of tools for the subagent

    Returns:
        New Agent configured as a subagent

    Raises:
        ValueError: If parent agent cannot delegate
    """
    if not parent_agent.agent_spec.can_delegate:
        raise ValueError(f"Agent '{parent_agent.name}' cannot delegate to subagents")

    from .multi_agent import create_subagent_config

    config = create_subagent_config(
        parent_name=parent_agent.name,
        task_description=task_description,
        tools=tools,
    )

    # Create subagent with a new session (isolated from parent)
    return create_agent_from_config(config, services=parent_agent.services)


def list_available_agents() -> list[str]:
    """
    List all available agent names.

    Returns:
        List of agent names from the registry
    """
    from .multi_agent import get_agent_registry

    return get_agent_registry().list_agents()


def list_primary_agents() -> list[str]:
    """
    List all primary (main) agent names.

    Returns:
        List of primary agent names
    """
    from .multi_agent import get_agent_registry

    return get_agent_registry().list_primary_agents()


def list_subagent_types() -> list[str]:
    """
    List all available subagent types.

    Returns:
        List of subagent names
    """
    from .multi_agent import get_agent_registry

    return get_agent_registry().list_subagents()
