"""Attribute, event, and span-name constants used by AnkaLoop tracing.

The ``gen_ai.*`` names follow the OpenTelemetry GenAI semantic conventions,
which are still in *Development* status. Keeping every name here means a
convention rename is a one-file change.
"""

from __future__ import annotations

# --- GenAI semantic conventions -------------------------------------------

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS = "gen_ai.usage.cache_write.input_tokens"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_TYPE = "gen_ai.tool.type"
GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result"
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"

ERROR_TYPE = "error.type"

OPERATION_INVOKE_AGENT = "invoke_agent"
OPERATION_CHAT = "chat"
OPERATION_EXECUTE_TOOL = "execute_tool"

TOOL_TYPE_FUNCTION = "function"
TOOL_TYPE_EXTENSION = "extension"

# --- AnkaLoop-specific attributes ------------------------------------------

ANKALOOP_SOURCE = "ankaloop.source"
ANKALOOP_CANCELLED = "ankaloop.cancelled"
ANKALOOP_TURN_ID = "ankaloop.turn.id"
ANKALOOP_TURN_PRIORITY = "ankaloop.turn.priority"
ANKALOOP_TURN_QUEUE_WAIT_MS = "ankaloop.turn.queue_wait_ms"
ANKALOOP_TURN_STEPS = "ankaloop.turn.steps"
ANKALOOP_TURN_LLM_CALLS = "ankaloop.turn.llm_calls"
ANKALOOP_TURN_TOOL_CALLS = "ankaloop.turn.tool_calls"
ANKALOOP_CONTEXT_TOKENS = "ankaloop.context.tokens"
ANKALOOP_CONTEXT_WINDOW = "ankaloop.context.window"
ANKALOOP_CONTEXT_USAGE_ESTIMATED = "ankaloop.context.usage_estimated"
ANKALOOP_TOOLS_EXPOSED = "ankaloop.tools.exposed"
ANKALOOP_TOOL_STEP = "ankaloop.tool.step"
ANKALOOP_TOOL_OPERATION_ID = "ankaloop.tool.operation_id"
ANKALOOP_TOOL_RECOVERY_MODE = "ankaloop.tool.recovery_mode"
ANKALOOP_TOOL_DENIED_BY = "ankaloop.tool.denied_by"
ANKALOOP_TOOL_SUCCESS = "ankaloop.tool.success"
ANKALOOP_TOOL_RESULT_LENGTH = "ankaloop.tool.result_length"
ANKALOOP_MCP_SERVER = "ankaloop.mcp.server"
ANKALOOP_HOOK_EVENT = "ankaloop.hook.event"
ANKALOOP_HOOK_HANDLER_COUNT = "ankaloop.hook.handler_count"
ANKALOOP_HOOK_DECISION = "ankaloop.hook.decision"
ANKALOOP_HOOK_SUCCESS = "ankaloop.hook.success"
ANKALOOP_TASK_ID = "ankaloop.task.id"
ANKALOOP_TASK_AGENT_TYPE = "ankaloop.task.agent_type"
ANKALOOP_TASK_PARENT_SESSION_ID = "ankaloop.task.parent_session_id"
ANKALOOP_COMPACTION_STRATEGY = "ankaloop.compaction.strategy"
ANKALOOP_COMPACTION_INPUT_TOKENS = "ankaloop.compaction.input_tokens"
ANKALOOP_COMPACTION_OUTPUT_TOKENS = "ankaloop.compaction.output_tokens"
ANKALOOP_COMPACTION_GENERATION = "ankaloop.compaction.generation"

SPAN_PREPARE_CONTEXT = "ankaloop.prepare_context"
SPAN_COMPACT_CONTEXT = "ankaloop.compact_context"
SPAN_HOOK_PREFIX = "ankaloop.hook"

# Denial reasons for ``ankaloop.tool.denied_by``.
DENIED_BY_CAPABILITY = "capability"
DENIED_BY_HOOK = "hook"
DENIED_BY_LIMIT = "limit"
DENIED_BY_ARGUMENTS = "arguments"

# --- OpenInference aliases (optional dual-write) ---------------------------

OPENINFERENCE_SPAN_KIND = "openinference.span.kind"
OPENINFERENCE_SPAN_KINDS = {
    OPERATION_INVOKE_AGENT: "AGENT",
    OPERATION_CHAT: "LLM",
    OPERATION_EXECUTE_TOOL: "TOOL",
}
OPENINFERENCE_ALIASES = {
    GEN_AI_PROVIDER_NAME: "llm.provider",
    GEN_AI_REQUEST_MODEL: "llm.model_name",
    GEN_AI_USAGE_INPUT_TOKENS: "llm.token_count.prompt",
    GEN_AI_USAGE_OUTPUT_TOKENS: "llm.token_count.completion",
    GEN_AI_TOOL_NAME: "tool.name",
    GEN_AI_TOOL_CALL_ARGUMENTS: "input.value",
    GEN_AI_TOOL_CALL_RESULT: "output.value",
    GEN_AI_INPUT_MESSAGES: "input.value",
    GEN_AI_OUTPUT_MESSAGES: "output.value",
}

# Agent events that are mirrored onto the active span. ``message.chunk`` and
# ``llm.usage`` are excluded: chunks are too chatty, and usage is recorded as
# attributes on the ``chat`` span instead.
MIRRORED_EVENT_PREFIXES = ("turn.", "tool.", "provider.", "context.", "memory.")
UNMIRRORED_EVENTS = frozenset({"llm.usage", "message.chunk"})
