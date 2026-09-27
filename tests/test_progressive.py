from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ankaloop.config import ContextConfig
from ankaloop.progressive.context_budget import ContextBudgetManager
from ankaloop.progressive.relevance import RelevanceScorer
from ankaloop.progressive.skill_view import ProgressiveSkillView
from ankaloop.progressive.tool_view import ProgressiveToolView
from ankaloop.progressive.usage_tracker import ToolUsageSnapshot, ToolUsageTracker
from ankaloop.skills import SkillMetadata


def _tool_spec(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_context_budget_allocation_is_stable():
    cfg = ContextConfig(min_prompt_budget=1800)
    manager = ContextBudgetManager(model="unknown-model", config=cfg)
    budget = manager.calculate_budget(conversation_tokens=24000)

    assert budget.prompt_budget == 116000
    allocated = budget.base_prompt + budget.tools + budget.skills + budget.memory + budget.rules + budget.buffer
    assert allocated == budget.prompt_budget


def test_context_budget_never_exceeds_remaining_headroom():
    cfg = ContextConfig(response_ratio=0.25, min_prompt_budget=2500)
    manager = ContextBudgetManager(model="unknown-model", config=cfg)

    exhausted = manager.calculate_budget(conversation_tokens=190000)

    assert exhausted.prompt_budget == 0
    assert exhausted.base_prompt == 0
    assert exhausted.tools == 0
    assert exhausted.skills == 0
    assert exhausted.memory == 0
    assert exhausted.rules == 0
    assert exhausted.buffer == 0


def test_progressive_tool_view_keeps_always_tools_and_respects_budget():
    scorer = RelevanceScorer()
    view = ProgressiveToolView(scorer)
    tools = [
        _tool_spec("read_file", "Read files from workspace"),
        _tool_spec("grep", "Search text patterns"),
        _tool_spec("think", "Internal planning"),
        _tool_spec("apply_patch", "Edit files with patch diffs"),
        _tool_spec("task", "Delegate work to subagents"),
    ]

    history = [
        {
            "tool": "apply_patch",
            "timestamp": (datetime.now() - timedelta(seconds=20)).isoformat(),
        }
    ]
    usage = ToolUsageTracker.from_history(history)

    result = view.select_tools(
        tools=tools,
        user_input="Implement a feature and modify existing code.",
        conversation=[],
        usage=usage,
        budget_tokens=120,
        relevance_threshold=0.2,
        tier_overrides={},
    )

    names = {t["function"]["name"] for t in result.selected_tools}
    assert {"read_file", "grep", "think"}.issubset(names)
    assert len(result.selected_tools) <= len(tools)


def test_tokenize_extracts_cjk_bigrams_and_ascii_words():
    tokens = RelevanceScorer._tokenize("帮我修复登录模块")

    assert {"修复", "登录", "模块"}.issubset(tokens)

    mixed = RelevanceScorer._tokenize("fix 这个 bug")
    assert {"fix", "bug", "这个"}.issubset(mixed)


def test_cjk_keyword_validation_rejects_unmatchable_lengths():
    from ankaloop.progressive.relevance import _validate_cjk_keywords

    for bad in ["数据库", "查日志", "查"]:
        with pytest.raises(ValueError, match="exactly two characters"):
            _validate_cjk_keywords({"TOOL_KEYWORDS": {"bash": {bad}}})

    _validate_cjk_keywords({"TASK_PATTERNS": {"debugging": {"修复"}}})


def test_classify_task_recognizes_chinese_input():
    scorer = RelevanceScorer()

    assert scorer.classify_task("帮我修复登录模块里那个失败的测试，顺便改一下文件") == "debugging"  # noqa: RUF001
    assert scorer.classify_task("帮我查一下这个函数在哪里定义") == "exploration"


def test_chinese_input_scores_on_demand_tools_above_default_threshold():
    scorer = RelevanceScorer()
    threshold = ContextConfig().tool_relevance_threshold
    usage = ToolUsageSnapshot()

    patch_score = scorer.score_tool(
        tool_name="apply_patch",
        tool_description="Edit files with patch diffs",
        user_input="帮我修复登录模块里那个失败的测试，顺便改一下文件",  # noqa: RUF001
        conversation_text="",
        usage=usage,
    )
    assert patch_score == pytest.approx(0.325, abs=1e-3)
    assert patch_score > threshold

    memory_score = scorer.score_tool(
        tool_name="memory",
        tool_description="Remember conversation details",
        user_input="帮我把这个记住",
        conversation_text="",
        usage=usage,
    )
    assert memory_score == pytest.approx(0.15, abs=1e-3)
    assert memory_score > threshold


def test_english_scoring_is_unchanged_by_cjk_keywords():
    scorer = RelevanceScorer()

    score = scorer.score_tool(
        tool_name="apply_patch",
        tool_description="edit files",
        user_input="fix the failing test in the login module and update the file",
        conversation_text="",
        usage=ToolUsageSnapshot(),
    )

    assert scorer.classify_task("fix the failing test in the login module and update the file") == "debugging"
    assert score == pytest.approx(0.3786, abs=1e-3)


def test_progressive_tool_view_selects_tools_for_chinese_input():
    scorer = RelevanceScorer()
    view = ProgressiveToolView(scorer)
    tools = [
        _tool_spec("read_file", "Read files from workspace"),
        _tool_spec("apply_patch", "Edit files with patch diffs"),
        _tool_spec("memory", "Remember conversation details"),
        _tool_spec("task", "Delegate work to subagents"),
    ]

    result = view.select_tools(
        tools=tools,
        user_input="帮我修复登录模块里那个失败的测试，顺便改一下文件",  # noqa: RUF001
        conversation=[],
        usage=ToolUsageSnapshot(),
        budget_tokens=10000,
        relevance_threshold=ContextConfig().tool_relevance_threshold,
        tier_overrides={},
    )

    names = {t["function"]["name"] for t in result.selected_tools}
    assert {"read_file", "apply_patch"}.issubset(names)
    assert "memory" in result.excluded_tools
    assert "task" in result.excluded_tools

    remember = view.select_tools(
        tools=tools,
        user_input="帮我把这个记住",
        conversation=[],
        usage=ToolUsageSnapshot(),
        budget_tokens=10000,
        relevance_threshold=ContextConfig().tool_relevance_threshold,
        tier_overrides={},
    )

    remember_names = {t["function"]["name"] for t in remember.selected_tools}
    assert "memory" in remember_names
    assert "task" in remember.excluded_tools


def test_progressive_skill_view_falls_back_when_budget_is_small():
    scorer = RelevanceScorer()
    view = ProgressiveSkillView(scorer)

    active = SkillMetadata(
        name="python-refactor",
        description="Refactor Python code safely",
        location="/tmp/skill/SKILL.md",
        body="# Steps\n" + "do detailed transformations\n" * 200,
    )
    secondary = SkillMetadata(
        name="ci-helper",
        description="Debug CI and flaky tests",
        location="/tmp/ci/SKILL.md",
        body="# Diagnose\nCollect failing logs",
    )

    result = view.build_prompt(
        skills=[active, secondary],
        user_input="Please refactor this module",
        active_skills={"python-refactor"},
        budget_tokens=45,
        relevance_threshold=0.2,
    )

    assert "python-refactor" in result.prompt
    assert "do detailed transformations" not in result.prompt
