from __future__ import annotations

import re
from enum import StrEnum

from .usage_tracker import ToolUsageSnapshot, ToolUsageTracker

_ASCII_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")  # CJK Unified Ideographs


class ToolTier(StrEnum):
    """Tool importance tiers."""

    ALWAYS = "always"
    FREQUENT = "frequent"
    ON_DEMAND = "on_demand"
    HIDDEN = "hidden"


class SkillLoadLevel(StrEnum):
    """Skill context load levels."""

    SUMMARY = "summary"
    OVERVIEW = "overview"
    FULL = "full"


# Chinese keywords must be exactly two characters long: CJK text is tokenized
# into character bigrams (see _tokenize), so only 2-char keywords can match.
TASK_PATTERNS: dict[str, set[str]] = {
    "implementation": {
        "implement",
        "create",
        "add",
        "build",
        "write",
        "develop",
        "实现",
        "创建",
        "新增",
        "添加",
        "构建",
        "开发",
        "编写",
    },
    "debugging": {
        "fix",
        "bug",
        "error",
        "failing",
        "broken",
        "debug",
        "修复",
        "调试",
        "报错",
        "错误",
        "失败",
        "故障",
        "崩溃",
    },
    "exploration": {
        "find",
        "search",
        "where",
        "show",
        "list",
        "locate",
        "查找",
        "搜索",
        "哪里",
        "哪个",
        "显示",
        "列出",
        "定位",
    },
    "review": {"review", "diff", "pr", "pull", "analyze", "审查", "评审", "检查", "分析", "对比"},
    "automation": {"run", "command", "script", "shell", "bash", "test", "运行", "执行", "命令", "脚本", "测试"},
}


TOOL_KEYWORDS: dict[str, set[str]] = {
    "read_file": {"read", "open", "content", "file", "source", "读取", "打开", "查看", "文件", "内容", "源码"},
    "grep": {"search", "grep", "find", "pattern", "regex", "搜索", "查找", "正则", "匹配"},
    "think": {"plan", "reason", "analyze", "think", "思考", "分析", "计划", "推理"},
    "web_search": {
        "web",
        "internet",
        "search",
        "docs",
        "documentation",
        "online",
        "current",
        "联网",
        "搜索",
        "上网",
        "网络",
        "文档",
        "资料",
    },
    "web_fetch": {
        "web",
        "internet",
        "fetch",
        "url",
        "page",
        "website",
        "docs",
        "content",
        "网页",
        "抓取",
        "获取",
        "链接",
        "页面",
        "网站",
    },
    "bash": {"run", "command", "shell", "execute", "build", "test", "运行", "执行", "命令", "终端", "构建", "测试"},
    "write_file": {"write", "create", "save", "generate", "overwrite", "写入", "创建", "保存", "生成", "覆盖"},
    "apply_patch": {
        "edit",
        "patch",
        "modify",
        "change",
        "update",
        "fix",
        "编辑",
        "修改",
        "更改",
        "更新",
        "修复",
        "补丁",
    },
    "todo": {"todo", "task", "plan", "checklist", "待办", "任务", "清单", "计划"},
    "task": {"parallel", "delegate", "subagent", "task", "并行", "委派", "委托", "代理", "任务"},
    "memory": {"remember", "history", "context", "memory", "记住", "记忆", "历史"},
}


TASK_TOOL_AFFINITY: dict[str, set[str]] = {
    "implementation": {"read_file", "apply_patch", "write_file", "grep"},
    "debugging": {"read_file", "grep", "bash", "apply_patch"},
    "exploration": {"read_file", "grep", "web_search", "web_fetch"},
    "review": {"read_file", "grep", "think", "web_search", "web_fetch"},
    "automation": {"bash", "task", "todo", "memory"},
}


def _validate_cjk_keywords(tables: dict[str, dict[str, set[str]]]) -> None:
    """Reject CJK keywords that bigram tokenization can never match.

    CJK input is tokenized into 2-character bigrams (plus a unigram for
    isolated characters), so a CJK keyword must be exactly two characters;
    any other length silently never matches.
    """
    for table_name, table in tables.items():
        for entry, keywords in table.items():
            for keyword in keywords:
                if not keyword.isascii() and len(keyword) != 2:
                    raise ValueError(
                        f"{table_name}[{entry!r}] keyword {keyword!r} must be exactly "
                        "two characters: CJK text is tokenized into bigrams"
                    )


_validate_cjk_keywords({"TASK_PATTERNS": TASK_PATTERNS, "TOOL_KEYWORDS": TOOL_KEYWORDS})


class RelevanceScorer:
    """Score tool and skill relevance for the current request."""

    def score_tool(
        self,
        *,
        tool_name: str,
        tool_description: str,
        user_input: str,
        conversation_text: str,
        usage: ToolUsageSnapshot,
        relevant_tools: set[str] | None = None,
    ) -> float:
        user_tokens = self._tokenize(user_input)
        context_tokens = user_tokens | self._tokenize(conversation_text)
        tool_tokens = TOOL_KEYWORDS.get(tool_name, set()) | self._tokenize(tool_description)

        # Score ASCII and CJK keyword vocabularies separately and keep the
        # better one. Overlap is normalized by vocabulary size, so merging
        # both scripts into one set would dilute scores for inputs that can
        # only ever match one of them.
        ascii_tool_tokens = {token for token in tool_tokens if token.isascii()}
        cjk_tool_tokens = tool_tokens - ascii_tool_tokens
        keyword_score = max(
            self._overlap_score(ascii_tool_tokens, context_tokens),
            self._overlap_score(cjk_tool_tokens, context_tokens),
        )

        task_type = self.classify_task(user_input)
        affinity_tools = TASK_TOOL_AFFINITY.get(task_type, set())
        task_affinity = 1.0 if tool_name in affinity_tools else 0.0

        recency = ToolUsageTracker.recency_score(usage, tool_name)
        frequency = ToolUsageTracker.frequency_score(usage, tool_name)
        cooccurrence = ToolUsageTracker.cooccurrence_score(usage, tool_name, relevant_tools or set())

        score = keyword_score * 0.45 + task_affinity * 0.25 + recency * 0.15 + frequency * 0.10 + cooccurrence * 0.05
        return max(0.0, min(score, 1.0))

    def score_skill(
        self,
        *,
        skill_name: str,
        skill_description: str,
        user_input: str,
        active_skills: set[str],
    ) -> float:
        if skill_name in active_skills:
            return 1.0

        input_tokens = self._tokenize(user_input)
        skill_tokens = self._tokenize(skill_name) | self._tokenize(skill_description)
        overlap = self._overlap_score(skill_tokens, input_tokens)

        task_type = self.classify_task(user_input)
        task_boost = 0.2 if task_type in self._tokenize(skill_description) else 0.0
        score = overlap * 0.8 + task_boost
        return max(0.0, min(score, 1.0))

    def classify_task(self, user_input: str) -> str:
        tokens = self._tokenize(user_input)
        best_task = "general"
        best_score = 0
        for task, keywords in TASK_PATTERNS.items():
            overlap = len(tokens & keywords)
            if overlap > best_score:
                best_score = overlap
                best_task = task
        return best_task

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Tokenize into ASCII words and CJK character bigrams.

        CJK text has no whitespace word boundaries, so emit bigrams (plus a
        unigram for isolated characters) to make keyword overlap work without
        a full segmenter.
        """
        tokens = set(_ASCII_WORD_RE.findall(text.lower()))
        for run in _CJK_RUN_RE.findall(text):
            if len(run) == 1:
                tokens.add(run)
            else:
                tokens.update(run[i : i + 2] for i in range(len(run) - 1))
        return tokens

    @staticmethod
    def _overlap_score(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        overlap = len(a & b)
        return min(overlap / max(len(a), 1), 1.0)
