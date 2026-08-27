"""Provider/tool execution loop orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.status import Status

from .compaction import CompactionConfig, estimate_request_tokens, get_model_context_window
from .config import AnkaloopConfig
from .hooks import HookDecision
from .llm import ProviderError
from .tool_execution import (
    ToolCallProtocolError,
    ToolCapability,
    ToolExecutionContext,
    ToolExecutor,
    normalize_tool_calls,
)
from .tool_journal import classify_recovery_mode
from .ui import LiveUI

if TYPE_CHECKING:
    from .agent import Agent
logger = logging.getLogger(__name__)


class ToolLoop:
    """Own provider rounds, tool execution, hooks, deltas, and usage accounting."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def run(
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
        """Run chat with tools and enhanced tracking."""
        cfg = cfg or self._agent._resolve_turn_config()

        # Use new LLM client abstraction
        from .llm import create_llm_client

        llm_client = create_llm_client(cfg.chat)
        self._agent._attach_context_overflow_observer(llm_client)

        # Override the chat function to add our tracking
        result = await self._agent._enhanced_chat_with_tools(
            llm_client=llm_client,
            messages=messages,
            tools=tools,
            tool_registry=tool_registry,
            stream=stream,
            status=status,
            work_dir=work_dir,
            return_message_delta=True,
            cfg=cfg,
        )
        assert isinstance(result, tuple)
        return result

    async def enhanced_chat_with_tools(
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
        """Enhanced version of _chat_with_tools with better tracking."""
        max_steps = max_steps or self._agent.max_steps

        # Reset per-request counters at the start of each request
        self._agent.current_request_tool_calls = 0
        self._agent.current_request_llm_calls = 0

        # Create a working copy of messages
        messages = [dict(message) for message in messages]
        message_delta: list[dict[str, Any]] = []

        def append_canonical(message: dict[str, Any]) -> None:
            messages.append(message)
            message_delta.append(deepcopy(message))

        def completed(text: str) -> str | tuple[str, list[dict[str, Any]]]:
            if return_message_delta:
                return text, message_delta
            return text

        used_tools = False

        cfg = cfg or self._agent._resolve_turn_config()
        model_config = cfg.chat.model_config if cfg.chat else None
        model = getattr(llm_client, "model", None) or self._agent._resolve_model_name(cfg)
        workspace_root = (work_dir or Path.cwd()).resolve()
        exposed_tools = {name for tool in tools if (name := tool.get("function", {}).get("name"))}
        capability = ToolCapability.from_spec(
            self._agent.agent_spec.tools,
            self._agent.agent_spec.exclude_tools,
            self._agent.agent_spec.can_delegate,
        )
        executor = ToolExecutor(
            context=ToolExecutionContext(
                session_id=self._agent.session_id,
                workspace_root=workspace_root,
                turn_id=str(self._agent.execution_context.get("turn_id", "direct")),
            ),
            capability=capability,
            exposed_tools=exposed_tools,
            registry=self._agent.services.tool_registry,
            mcp_registry=tool_registry,
            config=cfg,
            task_manager=self._agent._task_manager,
        )
        compaction_config = CompactionConfig()
        context_window = get_model_context_window(model, model_config=model_config)
        input_token_budget = int(
            context_window * (1 - compaction_config.safety_margin) * compaction_config.threshold_ratio
        )

        for step in range(max_steps):
            self._agent.step_count = step + 1
            status.update(
                f"[bold]Agent {self._agent.name}[/bold] - LLM Call {self._agent.current_request_llm_calls + 1}"
            )

            # Define stream callback if streaming is enabled
            stream_callback = None
            if stream:

                def _stream_callback(chunk: str):
                    self._agent._emit_event("message.chunk", {"content": chunk})

                stream_callback = _stream_callback

            messages = self._agent._fit_tool_context(messages, tools, input_token_budget)
            estimated_input_tokens = estimate_request_tokens(messages, tools)
            try:
                resp = await self._agent._call_llm(
                    llm_client,
                    messages=messages,
                    tools=tools,
                    stream_callback=stream_callback,
                    cfg=cfg.chat,
                )
            except Exception as call_error:
                if isinstance(call_error, ProviderError) and call_error.partial_output:
                    raise
                if not self._agent._is_tool_call_pairing_error(call_error):
                    raise
                logger.warning(
                    "Provider rejected tool-call history (%s); repairing pairing and retrying once",
                    call_error,
                )
                messages = self._agent._fit_tool_context(
                    self._agent._repair_tool_call_pairing(messages),
                    tools,
                    input_token_budget,
                )
                estimated_input_tokens = estimate_request_tokens(messages, tools)
                resp = await self._agent._call_llm(
                    llm_client,
                    messages=messages,
                    tools=tools,
                    stream_callback=stream_callback,
                    cfg=cfg.chat,
                )
            self._agent._record_llm_usage(resp, estimated_input_tokens, context_window)

            if resp.tool_calls:
                try:
                    tool_calls = normalize_tool_calls(resp.tool_calls)
                except ToolCallProtocolError as protocol_error:
                    if protocol_error.tool_calls is None:
                        raise
                    logger.warning(
                        "Provider returned malformed tool calls (%s); synthesizing tool results",
                        protocol_error,
                    )
                    tool_calls = normalize_tool_calls(protocol_error.tool_calls)
                used_tools = True
                status.update(f"[bold]Agent {self._agent.name}[/bold] - Executing {len(tool_calls)} tool(s)...")

                # Check if any tool should be limited before processing
                limited_tools = []
                for tc in tool_calls:
                    tool_name = tc.name
                    if self._agent._should_limit_tool_calls(tool_name, cfg):
                        limited_tools.append(tool_name)

                if limited_tools:
                    status.update(
                        f"[bold]Agent {self._agent.name}[/bold] - Tools {limited_tools} limited, forcing response..."
                    )
                    self._agent.console.print(f"[yellow]Tools {limited_tools} limited, forcing response[/yellow]")
                    append_canonical(
                        {
                            "role": "assistant",
                            "content": resp.content or "",
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.name,
                                        "arguments": tc.raw_arguments,
                                    },
                                }
                                | ({"extra_content": tc.extra_content} if tc.extra_content else {})
                                for tc in tool_calls
                            ],
                        }
                    )
                    for tc in tool_calls:
                        append_canonical(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "name": tc.name,
                                "content": (
                                    "Tool call limited: further calls to this tool are not allowed in this request"
                                ),
                            }
                        )
                    # Add system message to force response
                    messages.append(
                        {
                            "role": "system",
                            "content": f"You have already called the following tools too many times: {', '.join(limited_tools)}. Please analyze the information you have and provide your response without calling these tools again.",
                        }
                    )
                    # Get a final response from the LLM with the current messages
                    try:
                        messages = self._agent._fit_tool_context(messages, [], input_token_budget)
                        estimated_input_tokens = estimate_request_tokens(messages)
                        final_resp = await self._agent._call_llm(
                            llm_client,
                            messages=messages,
                            cfg=cfg.chat,
                        )
                        self._agent._record_llm_usage(final_resp, estimated_input_tokens, context_window)
                        final_text = final_resp.content or ""
                        status.update(f"[bold]Agent {self._agent.name}[/bold] - ✅ Complete")
                        return completed(final_text)
                    except Exception as e:
                        status.update(f"[bold]Agent {self._agent.name}[/bold] - ⚠️ Error getting final response")
                        if isinstance(e, ProviderError):
                            raise
                        raise self._agent._agent_execution_error(f"Could not get final response: {e}") from e

                append_canonical(
                    {
                        "role": "assistant",
                        "content": resp.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": tc.raw_arguments,
                                },
                            }
                            | ({"extra_content": tc.extra_content} if tc.extra_content else {})
                            for tc in tool_calls
                        ],
                    }
                )

                # Process tool calls with Live UI
                with LiveUI() as live_ui:
                    for tc in tool_calls:
                        tool_name = tc.name
                        tool_id = tc.id
                        if tc.argument_error:
                            tool_result_text = f"Tool argument error: {tc.argument_error}"
                            block = live_ui.add_tool(tool_name, {})
                            live_ui.finish_tool(block, success=False, result=tool_result_text)
                            append_canonical(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": tool_result_text,
                                }
                            )
                            continue
                        assert tc.arguments is not None
                        args = tc.arguments

                        if tool_name not in exposed_tools or not capability.allows(tool_name):
                            tool_result_text = f"Tool permission denied: '{tool_name}' is not authorized"
                            block = live_ui.add_tool(tool_name, args)
                            live_ui.finish_tool(block, success=False, result=tool_result_text)
                            self._agent._emit_event(
                                "tool.call_denied",
                                {
                                    "tool_name": tool_name,
                                    "tool_id": tool_id,
                                    "reason": tool_result_text,
                                },
                            )
                            append_canonical(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": tool_result_text,
                                }
                            )
                            continue

                        # Hooks must inspect the same canonical arguments that
                        # execution will use. For example, grep's common
                        # ``path`` near-miss becomes ``paths`` here.
                        args = executor.prepare_model_arguments(tool_name, args)

                        # Run PreToolUse hooks
                        pre_hook_output = await self._agent._run_pre_tool_use_hooks(
                            session_id=self._agent.session_id,
                            tool_name=tool_name,
                            tool_input=args,
                            tool_use_id=tool_id,
                            project_dir=workspace_root,
                        )

                        # Check hook decision
                        if pre_hook_output.decision == HookDecision.DENY:
                            # Tool execution denied by hook
                            tool_result_text = (
                                f"Tool denied by hook: {pre_hook_output.decision_reason or 'No reason given'}"
                            )
                            block = live_ui.add_tool(tool_name, args)
                            live_ui.finish_tool(block, success=False, result=tool_result_text)
                            append_canonical(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": tool_result_text,
                                }
                            )
                            continue

                        # Apply any input updates from hooks
                        if pre_hook_output.updated_input:
                            args = {**args, **pre_hook_output.updated_input}

                        # Record tool call
                        tool_call_record = {
                            "step": self._agent.step_count,
                            "tool": tool_name,
                            "args": tc.raw_arguments,
                            "timestamp": datetime.now().isoformat(),
                        }
                        self._agent.tool_calls_history.append(tool_call_record)
                        self._agent.current_conversation_tool_calls.append(tool_call_record)
                        self._agent.current_request_tool_calls += 1  # Track per-request tool calls

                        # Add tool block to UI
                        block = live_ui.add_tool(tool_name, args)

                        # Emit tool call start event
                        self._agent._emit_event(
                            "tool.call_start",
                            {
                                "tool_name": tool_name,
                                "tool_id": tool_id,
                                "arguments": args,
                                "step": self._agent.step_count,
                            },
                        )

                        # Track execution time
                        tool_start_time = time.time()

                        # T1 boundary: preflight (permissions, hooks, argument
                        # preparation) has passed and execution is imminent.
                        # The intent is durable BEFORE the tool runs; a crash
                        # after this point leaves an open operation that
                        # recovery must treat as indeterminate.  A journal
                        # write failure blocks execution rather than run an
                        # unjournaled side effect.
                        journal_turn_id = str(self._agent.execution_context.get("turn_id", "direct"))
                        self._agent._tool_journal.tool_intent(
                            journal_turn_id,
                            tool_id,
                            tool_name,
                            args,
                        )
                        self._agent._emit_event(
                            "tool.intent",
                            {
                                "operation_id": f"{journal_turn_id}:{tool_id}",
                                "tool_name": tool_name,
                                "tool_id": tool_id,
                                "recovery_mode": classify_recovery_mode(tool_name),
                            },
                        )

                        # Execute tool
                        try:
                            tool_result = await executor.execute(tool_name, args)
                            if tool_result.success:
                                tool_result_text = tool_result.content
                                tool_response_data = {
                                    "success": True,
                                    "content": tool_result_text,
                                }
                                live_ui.finish_tool(block, success=True, result=tool_result_text)
                            else:
                                tool_result_text = f"Error: {tool_result.error}"
                                tool_response_data = {
                                    "success": False,
                                    "error": tool_result.error,
                                }
                                live_ui.finish_tool(block, success=False, result=tool_result_text)

                            # Run PostToolUse hooks
                            post_hook_output = await self._agent._run_post_tool_use_hooks(
                                session_id=self._agent.session_id,
                                tool_name=tool_name,
                                tool_input=args,
                                tool_response=tool_response_data,
                                tool_use_id=tool_id,
                                project_dir=workspace_root,
                            )

                            # Apply any response updates from hooks
                            if post_hook_output.updated_response:
                                tool_result_text = json.dumps(post_hook_output.updated_response, ensure_ascii=False)

                            # Add hook feedback if any
                            if post_hook_output.feedback:
                                tool_result_text += f"\n\n[Hook feedback: {post_hook_output.feedback}]"

                            # Calculate execution duration
                            tool_duration_ms = (time.time() - tool_start_time) * 1000

                            # Emit tool call complete event
                            tool_success = (
                                tool_response_data.get("success", True)
                                if isinstance(tool_response_data, dict)
                                else True
                            )
                            self._agent._emit_event(
                                "tool.call_complete",
                                {
                                    "tool_name": tool_name,
                                    "tool_id": tool_id,
                                    "success": tool_success,
                                    "duration_ms": tool_duration_ms,
                                    "result_length": len(tool_result_text),
                                },
                            )

                            # Add tool result to messages (truncate large results)
                            MAX_TOOL_RESULT_LEN = 8000
                            truncated_result = tool_result_text
                            if len(tool_result_text) > MAX_TOOL_RESULT_LEN:
                                truncated_result = tool_result_text[:MAX_TOOL_RESULT_LEN] + "\n... [truncated]"

                            append_canonical(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": truncated_result,
                                }
                            )
                            # T2 boundary: the result is now part of the
                            # conversation draft; journal the settlement.
                            self._agent._settle_tool_operation(
                                journal_turn_id,
                                tool_id,
                                success=bool(tool_response_data.get("success", True)),
                                duration_ms=tool_duration_ms,
                            )

                        except asyncio.CancelledError:
                            tool_duration_ms = (time.time() - tool_start_time) * 1000
                            self._agent._emit_event(
                                "tool.call_cancelled",
                                {
                                    "tool_name": tool_name,
                                    "tool_id": tool_id,
                                    "success": False,
                                    "settled": True,
                                    "duration_ms": tool_duration_ms,
                                },
                            )
                            raise
                        except Exception as e:
                            error_msg = f"Tool {tool_name} error: {type(e).__name__}: {e}"
                            live_ui.finish_tool(block, success=False, result=error_msg)

                            # Calculate execution duration
                            tool_duration_ms = (time.time() - tool_start_time) * 1000

                            # Emit tool call error event
                            self._agent._emit_event(
                                "tool.call_error",
                                {
                                    "tool_name": tool_name,
                                    "tool_id": tool_id,
                                    "success": False,
                                    "error": error_msg,
                                    "duration_ms": tool_duration_ms,
                                },
                            )

                            append_canonical(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": error_msg,
                                }
                            )
                            self._agent._settle_tool_operation(
                                journal_turn_id,
                                tool_id,
                                success=False,
                                duration_ms=tool_duration_ms,
                                error=error_msg,
                            )

                continue
            else:
                # No tool calls, return the response
                final_text = resp.content or ""
                if stream and not used_tools:
                    # For streaming, we'll implement a simple version
                    pass

                status.update(f"[bold]Agent {self._agent.name}[/bold] - ✅ Complete")
                return completed(final_text)

        # Max steps reached
        status.update(f"[bold]Agent {self._agent.name}[/bold] - ⚠️ Max steps reached")
        raise self._agent._max_steps_reached(self._agent.max_steps)
