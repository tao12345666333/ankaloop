"""Context and tool-schema construction for agents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .compaction import estimate_request_tokens, estimate_tokens
from .config import AnkaloopConfig, ContextConfig
from .mcp_naming import mcp_tool_name, unique_function_name
from .memory_review import MEMORY_GUIDANCE
from .progressive.context_budget import estimate_text_tokens
from .progressive.usage_tracker import ToolUsageTracker
from .tool_execution import ToolCapability

if TYPE_CHECKING:
    from .agent import Agent


def _repair_tool_call_pairing(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair assistant tool calls and tool responses after context trimming."""
    repaired: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    pending_names: dict[str, str] = {}

    def flush_pending() -> None:
        for call_id in pending_ids:
            result: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "[Tool result unavailable: synthesized to keep tool-call history paired]",
            }
            paired_name = pending_names.get(call_id)
            if paired_name:
                result["name"] = paired_name
            repaired.append(result)
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
                function = call.get("function")
                if isinstance(call_id, str) and call_id:
                    pending_ids.append(call_id)
                    if isinstance(function, dict) and isinstance(function.get("name"), str):
                        pending_names[call_id] = function["name"]
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id in pending_ids:
                pending_ids.remove(call_id)
                repaired.append(message)
        else:
            flush_pending()
            repaired.append(message)
    flush_pending()
    return repaired


class ContextBuilder:
    """Own context-building operations while retaining Agent patch seams."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def get_system_prompt(
        self,
        work_dir: Path | None = None,
        user_input: str = "",
        *,
        conversation_tokens: int,
        cfg: AnkaloopConfig | None = None,
    ) -> str:
        """Get the system prompt budgeted against the actual model conversation."""
        resolved_work_dir = work_dir.resolve() if work_dir else Path.cwd()
        work_dir_str = str(resolved_work_dir)
        cfg = cfg or self._agent._resolve_turn_config()
        context_cfg = cfg.context or ContextConfig()
        model_name = self._agent._resolve_model_name(cfg)

        budget = self._agent._calculate_context_budget(
            conversation_tokens,
            model_name=model_name,
            model_config=cfg.chat.model_config if cfg.chat else None,
            context_config=context_cfg,
        )

        # Note: MCP tools info will be loaded asynchronously during execution
        mcp_tools_info: list[dict[str, Any]] = []

        prompt_vars = {
            "work_dir": work_dir_str,
            "agent_name": self._agent.agent_spec.name,
            "mcp_tools": json.dumps(mcp_tools_info, indent=2),
        }

        # Build base system prompt
        try:
            base_prompt = self._agent.agent_spec.system_prompt.format(**prompt_vars)
        except KeyError as e:
            self._agent.console.print(f"[yellow]Warning: Missing template variable {e}[/yellow]")
            base_prompt = self._agent.agent_spec.system_prompt

        # Load project rules from AGENTS.md files
        project_rules = self._agent._load_project_rules(resolved_work_dir)

        # Load the optional you.md user profile (off unless [context] user_profile = true)
        user_profile = self._agent._load_user_profile(resolved_work_dir, context_cfg.user_profile)

        # Get skills information
        skill_manager = self._agent._skill_manager()

        # Ensure skills are discovered (includes built-in skills)
        if not skill_manager.get_all_skills():
            skill_manager.discover_skills(resolved_work_dir)

        # Build skills context
        skills_summary = ""
        skills_content = ""
        if context_cfg.progressive_skills:
            skill_result = self._agent._progressive_skill_view.build_prompt(
                skills=skill_manager.get_skills(),
                user_input=user_input,
                active_skills={s.name for s in skill_manager.get_active_skills()},
                budget_tokens=budget.skills,
                relevance_threshold=context_cfg.skill_relevance_threshold,
            )
            skills_summary = skill_result.prompt
        else:
            combined_skills = "\n\n".join(
                part
                for part in (
                    skill_manager.build_skills_summary(),
                    skill_manager.get_active_skills_content(),
                )
                if part
            )
            skills_summary = self._agent._trim_to_token_budget(combined_skills, budget.skills)

        # Get session-frozen persona and memory context. Freezing keeps the
        # prompt prefix stable across a long Telegram session.
        persona_context, memory_context = self._agent._get_memory_prompt_context(work_dir)

        # Respect per-component budgets for every system-prompt section.
        base_prompt = self._agent._trim_to_token_budget(
            base_prompt + "\n\n" + MEMORY_GUIDANCE,
            budget.base_prompt,
        )
        if project_rules:
            project_rules = self._agent._trim_to_token_budget(project_rules, budget.rules)
        if user_profile:
            user_profile = self._agent._trim_to_token_budget(user_profile, budget.rules)
        if persona_context:
            persona_context = self._agent._trim_to_token_budget(persona_context, budget.memory)
        remaining_memory_budget = max(budget.memory - estimate_text_tokens(persona_context), 0)
        if memory_context:
            memory_context = self._agent._trim_to_token_budget(memory_context, remaining_memory_budget)
        if skills_content:
            skills_content = self._agent._trim_to_token_budget(skills_content, budget.skills)

        # Combine all parts
        combined_prompt = base_prompt
        if persona_context:
            combined_prompt = persona_context + "\n\n" + combined_prompt
        if project_rules:
            combined_prompt += "\n\n" + project_rules
        if user_profile:
            combined_prompt += "\n\n" + user_profile
        if skills_summary:
            combined_prompt += "\n\n" + skills_summary
        if memory_context:
            combined_prompt += "\n\n" + memory_context
        if skills_content:
            combined_prompt += "\n\n" + skills_content

        self._agent._emit_event(
            "context.budget_allocated",
            {
                "model": model_name,
                "conversation_tokens": conversation_tokens,
                "prompt_budget": budget.prompt_budget,
                "tools_budget": budget.tools,
                "skills_budget": budget.skills,
                "memory_budget": budget.memory,
                "rules_budget": budget.rules,
            },
        )

        return combined_prompt

    async def build_tools_and_registry(
        self,
        user_input: str = "",
        conversation_history: list[dict[str, Any]] | None = None,
        *,
        cfg: AnkaloopConfig | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]]]:
        """Build list of available tools and registry for MCP tool dispatch.

        Combined method to avoid duplicate MCP server calls.

        Returns:
            Tuple of (tools list, registry dict)
        """
        tools: list[dict[str, Any]] = []
        registry: dict[str, tuple[str, str]] = {}
        conversation = conversation_history or self._agent.conversation_history

        capability = ToolCapability.from_spec(
            self._agent.agent_spec.tools,
            self._agent.agent_spec.exclude_tools,
            self._agent.agent_spec.can_delegate,
        )

        tool_registry = self._agent.services.tool_registry
        for tool_name in tool_registry.list_tools():
            if not capability.allows(tool_name):
                continue
            tool_spec = tool_registry.get_tool_spec(tool_name)
            if tool_spec:
                tools.append(tool_spec)

        # Load MCP tools
        cfg = cfg or self._agent._resolve_turn_config()
        chat_cfg = cfg.chat

        # Decide which servers to include
        if chat_cfg and chat_cfg.mcp_tools_enabled is False:
            selected = []
        elif chat_cfg and chat_cfg.mcp_servers:
            selected = [s for s in chat_cfg.mcp_servers if s in cfg.servers]
        else:
            selected = list(cfg.servers.keys())

        # Load MCP tools asynchronously (single call per server). Server and
        # tool names are untrusted, and providers disagree on legal function
        # names (Kimi rejects the dots Gemini accepts), so every MCP tool is
        # exposed under a sanitized alias that the registry maps back to its
        # (server, tool) pair.
        exposed_names = {tool_name for tool in tools if (tool_name := tool.get("function", {}).get("name"))}
        for name in selected:
            try:
                server = cfg.servers[name]
                info_list = await self._agent._list_mcp_tools(server)
                for info in info_list:
                    tname = info.get("name") or "tool"
                    oname = unique_function_name(mcp_tool_name(name, tname), exposed_names)
                    if not capability.allows(oname):
                        continue
                    exposed_names.add(oname)
                    params = info.get("inputSchema") or {"type": "object"}
                    tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": oname,
                                "description": info.get("description", ""),
                                "parameters": params,
                            },
                        }
                    )
                    # Also add to registry
                    registry[oname] = (name, tname)
            except (OSError, ValueError, KeyError) as e:
                self._agent.console.print(f"[yellow]MCP tool discovery failed for server {name}:[/yellow] {e}")

        context_cfg = cfg.context or ContextConfig()
        if not context_cfg.progressive_tools:
            return tools, registry

        conversation_tokens = estimate_tokens(conversation)
        budget = self._agent._calculate_context_budget(
            conversation_tokens,
            model_name=self._agent._resolve_model_name(cfg),
            model_config=cfg.chat.model_config if cfg.chat else None,
            context_config=context_cfg,
        )
        usage_snapshot = ToolUsageTracker.from_history(self._agent.tool_calls_history)

        selection = self._agent._progressive_tool_view.select_tools(
            tools=tools,
            user_input=user_input,
            conversation=conversation,
            usage=usage_snapshot,
            budget_tokens=budget.tools,
            relevance_threshold=context_cfg.tool_relevance_threshold,
            tier_overrides=context_cfg.tool_tiers,
        )

        selected_tools = selection.selected_tools
        selected_names = {
            tool.get("function", {}).get("name", "") for tool in selected_tools if tool.get("function", {}).get("name")
        }
        filtered_registry = {name: ref for name, ref in registry.items() if name in selected_names}

        self._agent._emit_event(
            "context.tools_filtered",
            {
                "selected_count": len(selected_tools),
                "total_count": len(tools),
                "hidden_count": selection.hidden_count,
                "excluded_tools": selection.excluded_tools,
            },
        )

        return selected_tools, filtered_registry

    @staticmethod
    def fit_tool_context(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        token_budget: int,
    ) -> list[dict[str, Any]]:
        """Fit request-local tool exchanges into the model input budget."""
        fitted = [dict(message) for message in messages]
        if estimate_request_tokens(fitted, tools) <= token_budget:
            return _repair_tool_call_pairing(fitted)

        tool_indexes = [i for i, message in enumerate(fitted) if message.get("role") == "tool"]

        # Retain useful head/tail excerpts, shrinking oldest results first.
        for index in tool_indexes:
            content = fitted[index].get("content")
            if not isinstance(content, str) or len(content) <= 1200:
                continue
            fitted[index]["content"] = (
                content[:600] + "\n... [tool result trimmed for context budget] ...\n" + content[-600:]
            )
            if estimate_request_tokens(fitted, tools) <= token_budget:
                return fitted

        # Remove oldest complete tool-call batches, preferring the latest batch.
        batch_ranges: list[tuple[int, int]] = []
        index = 0
        while index < len(fitted):
            message = fitted[index]
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                index += 1
                continue
            call_ids = {call.get("id") for call in message["tool_calls"]}
            end = index + 1
            seen_ids: set[str | None] = set()
            while end < len(fitted) and fitted[end].get("role") == "tool":
                seen_ids.add(fitted[end].get("tool_call_id"))
                end += 1
            if call_ids and call_ids <= seen_ids:
                batch_ranges.append((index, end))
            index = end

        for start, end in reversed(batch_ranges[:-1]):
            del fitted[start:end]
            if estimate_request_tokens(fitted, tools) <= token_budget:
                return fitted

        # If the newest result alone is too large, reduce all remaining results
        # to explicit placeholders rather than sending a known-oversized request.
        for message in fitted:
            if message.get("role") == "tool" and message.get("content"):
                message["content"] = "[Tool result omitted: context budget exceeded]"
        return _repair_tool_call_pairing(fitted)
