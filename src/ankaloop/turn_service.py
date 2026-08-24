"""Transactional conversation-turn orchestration."""

from __future__ import annotations

import asyncio
import logging
import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from .config import ChatConfig
from .llm import ProviderError
from .session_state import CompactionCheckpoint
from .tool_execution import ToolCallProtocolError

if TYPE_CHECKING:
    from .agent import Agent
logger = logging.getLogger(__name__)
MEMORY_REVIEW_TURN_INTERVAL = 10


class TurnService:
    """Own draft execution, commit/rollback, and best-effort projections."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def process_message(self, user_input: str, work_dir: Path | None, stream: bool, show_progress: bool) -> str:
        """
        Process a single message (internal implementation).

        This is the core message processing logic, extracted from run()
        to support queue-based processing.
        """
        committed_state = self._agent._session_state
        draft = committed_state.clone()
        self._agent._apply_session_state(draft)
        self._agent._reset_current_conversation_tool_calls()

        # Run UserPromptSubmit hooks
        try:
            prompt_hook_output = await self._agent._run_user_prompt_hooks(
                session_id=self._agent.session_id,
                prompt=user_input,
                project_dir=work_dir,
            )
        except BaseException:
            self._agent._apply_session_state(committed_state)
            raise

        # Check if hook denied the prompt
        if not prompt_hook_output.continue_execution:
            if prompt_hook_output.stop_reason:
                self._agent.console.print(f"[yellow]Prompt blocked: {prompt_hook_output.stop_reason}[/yellow]")
            self._agent._apply_session_state(committed_state)
            return prompt_hook_output.stop_reason or "Prompt blocked by hook"

        # Show hook feedback if any
        if prompt_hook_output.feedback:
            self._agent.console.print(f"[dim]Hook: {prompt_hook_output.feedback}[/dim]")

        # Journal the durable start of this turn.  turn_id is assigned by the
        # runtime before the processor runs, so the journal and the eventual
        # commit share one identity.  Best-effort: a failure here must not
        # block the turn, but it is logged loudly because crash evidence for
        # the whole turn is then missing.
        turn_id = str(self._agent.execution_context.get("turn_id", uuid.uuid4()))
        try:
            self._agent._tool_journal.turn_started(turn_id, user_input)
        except Exception as exc:
            logger.warning("Tool journal turn_started write failed: %s", exc)

        turn_messages = [{"role": "user", "content": user_input}]

        try:
            with self._agent._create_progress_context(show_progress) as status:
                status.update(f"[bold]Agent {self._agent.name}[/bold] thinking...")

                # Prepare messages with conversation history
                # Freeze provider and related settings for this turn. A
                # `/model use` or config-file edit affects the next turn, but
                # cannot mix providers between chat, compaction, and memory.
                turn_config = self._agent._resolve_turn_config()
                history_to_add = draft.model_context(turn_messages)
                conversation_tokens = self._agent._estimate_tokens(history_to_add)
                system_prompt = self._agent._get_system_prompt(
                    work_dir,
                    user_input=user_input,
                    conversation_tokens=conversation_tokens,
                    cfg=turn_config,
                )
                messages = [{"role": "system", "content": system_prompt}]

                # Build tools before compaction so their schemas are included in
                # the request-size decision.
                tools, tool_registry = await self._agent._build_tools_and_registry(
                    user_input=user_input,
                    conversation_history=history_to_add,
                    cfg=turn_config,
                )

                # Apply compaction if context is too large
                cfg = turn_config
                model = self._agent._resolve_model_name(cfg)
                compaction_chat = replace(cfg.chat) if cfg.chat else ChatConfig()
                compaction_chat.model = model

                from .llm import create_llm_client

                client = create_llm_client(compaction_chat)
                self._agent._attach_context_overflow_observer(client)
                compactor = self._agent._smart_compactor(
                    client,
                    model,
                    model_config=cfg.chat.model_config if cfg.chat else None,
                )

                request_tokens = self._agent._estimate_tokens(messages + history_to_add, tools)
                if (
                    request_tokens > compactor.threshold_tokens
                    and self._agent._estimate_tokens(history_to_add) >= compactor.config.min_tokens_to_compact
                ):
                    preserve_turns = max(compactor.config.preserve_last // 2, 1)
                    covered_turn_count = max(len(draft.turns) - preserve_turns, 0)
                    previous_covered_turns = draft.checkpoint.covered_turn_count if draft.checkpoint is not None else 0
                    if covered_turn_count > previous_covered_turns:
                        if not self._agent.ephemeral:
                            status.update(f"[bold]Agent {self._agent.name}[/bold] saving memories before compaction...")
                            await self._agent._run_memory_review(
                                conversation_snapshot=history_to_add,
                                system_prompt=system_prompt,
                                work_dir=work_dir,
                                status=status,
                                cfg=turn_config,
                            )
                            self._agent._last_memory_review_turn_count = len(draft.turns) + 1
                        status.update(f"[bold]Agent {self._agent.name}[/bold] compacting context...")
                        covered_message_count = draft.turns[covered_turn_count - 1].end_message
                        previous_message_count = (
                            draft.checkpoint.covered_message_count if draft.checkpoint is not None else 0
                        )
                        checkpoint_input = deepcopy(draft.checkpoint.context) if draft.checkpoint is not None else []
                        checkpoint_input.extend(deepcopy(draft.messages[previous_message_count:covered_message_count]))
                        checkpoint_context, compaction_result = await asyncio.to_thread(
                            compactor.compact_checkpoint,
                            checkpoint_input,
                        )
                        draft.checkpoint = CompactionCheckpoint(
                            context=checkpoint_context,
                            covered_message_count=covered_message_count,
                            covered_turn_count=covered_turn_count,
                            generation=(draft.checkpoint.generation + 1 if draft.checkpoint is not None else 1),
                            strategy=compaction_result.strategy_used.value,
                            strategy_version=1,
                            original_tokens=compaction_result.original_tokens,
                            compacted_tokens=compaction_result.compacted_tokens,
                        )
                        self._agent._emit_event(
                            "context.compacted",
                            {
                                "input_tokens": compaction_result.original_tokens,
                                "output_tokens": compaction_result.compacted_tokens,
                            },
                        )
                        history_to_add = draft.model_context(turn_messages)
                        conversation_tokens = self._agent._estimate_tokens(history_to_add)
                        system_prompt = self._agent._get_system_prompt(
                            work_dir,
                            user_input=user_input,
                            conversation_tokens=conversation_tokens,
                            cfg=turn_config,
                        )
                        messages[0]["content"] = system_prompt
                        self._agent.reset_memory_context_snapshot()
                        self._agent.console.print("[dim]Context compacted to reduce token usage[/dim]")

                messages.extend(history_to_add)

                # Run chat with tools
                result, tool_messages = await self._agent._run_with_tools(
                    messages=messages,
                    tools=tools,
                    tool_registry=tool_registry,
                    stream=stream,
                    status=status,
                    work_dir=work_dir,
                    cfg=turn_config,
                )

                turn_messages.extend(tool_messages)
                turn_messages.append({"role": "assistant", "content": result})
                self._agent._capture_session_state(draft)
                draft.commit_turn(
                    turn_id,
                    turn_messages,
                )
                turn_count = self._agent._conversation_turn_count(draft.messages)
                periodic_review_due = (
                    not self._agent.ephemeral
                    and turn_count - self._agent._last_memory_review_turn_count >= MEMORY_REVIEW_TURN_INTERVAL
                )
                if periodic_review_due:
                    draft.last_memory_review_turn_count = turn_count
                self._agent._commit_session_state(draft)
                try:
                    self._agent._tool_journal.turn_committed(turn_id, draft.revision)
                except Exception as exc:
                    logger.warning("Tool journal turn_committed write failed: %s", exc)
                if periodic_review_due:
                    self._agent._schedule_periodic_memory_review(
                        conversation_snapshot=draft.messages,
                        system_prompt=system_prompt,
                        work_dir=work_dir,
                        cfg=turn_config,
                    )

                if not self._agent.ephemeral:
                    # Best-effort projections for normal persistent conversations.
                    try:
                        memory_mgr = self._agent._memory_manager(self._agent._resolve_memory_project_root(work_dir))
                        summary = self._agent._format_conversation_history_entry(user_input, result)
                        memory_mgr.append_history(
                            content=summary,
                            session_id=self._agent.session_id,
                            tags=["conversation"],
                            scope=self._agent._memory_history_scope(work_dir),
                        )
                    except Exception as e:
                        logger.debug(f"Memory history logging failed (non-critical): {e}")

                    try:
                        self._agent.services.transcript_store.append_turn(
                            session_id=self._agent.session_id,
                            user=user_input,
                            assistant=result,
                            source=str(self._agent.execution_context.get("source", "agent")),
                            chat_id=self._agent.execution_context.get("telegram_chat_id"),
                        )
                    except Exception as e:
                        logger.debug(f"Transcript indexing failed (non-critical): {e}")

                return result

        except asyncio.CancelledError:
            self._agent._apply_session_state(committed_state)
            self._journal_turn_terminal(turn_id, cancelled=True)
            raise
        except (ProviderError, ToolCallProtocolError) as e:
            self._agent._apply_session_state(committed_state)
            self._journal_turn_terminal(turn_id, error=f"{type(e).__name__}")
            raise
        except Exception as e:
            self._agent._apply_session_state(committed_state)
            self._journal_turn_terminal(turn_id, error=f"{type(e).__name__}: {e}")
            if isinstance(e, self._agent._execution_exception_types()):
                raise
            self._agent.console.print(Text.assemble(("Agent execution failed: ", "red"), str(e)))
            raise self._agent._agent_execution_error(f"Agent execution failed: {e}") from e

    def _journal_turn_terminal(self, turn_id: str, *, cancelled: bool = False, error: str | None = None) -> None:
        """Best-effort terminal journal event so open intents become
        ``aborted_unsettled`` instead of suspected crashes."""
        try:
            if cancelled:
                self._agent._tool_journal.turn_cancelled(turn_id)
            else:
                self._agent._tool_journal.turn_failed(turn_id, error)
        except Exception as exc:
            logger.warning("Tool journal terminal write failed for turn %s: %s", turn_id, exc)
