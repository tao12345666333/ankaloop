#!/bin/bash

# 批量分析多个失败的 GitHub Actions runs
# 用法: ./batch-analyze.sh [limit]

set -e

LIMIT=${1:-5}

echo "=== 批量分析最近 $LIMIT 个失败的 Runs ==="
echo

# 获取失败的 runs
FAILED_RUNS=$(gh run list --status failed --limit "$LIMIT" --json databaseId,headBranch,workflow)

if [ -z "$FAILED_RUNS" ]; then
    echo "没有找到失败的 runs"
    exit 0
fi

echo "找到 $(echo "$FAILED_RUNS" | jq length) 个失败的 runs:"
echo "$FAILED_RUNS" | jq -r '.[] | "- Run \(.databaseId): \(.workflow.name) on \(.headBranch)"'
echo

# 分析每个失败的 run
echo "## 详细分析"
echo "$FAILED_RUNS" | jq -r '.[] | .databaseId' | while read -r run_id; do
    echo
    echo "### Run $run_id"
    echo "---"
    
    # 获取基本信息
    workflow=$(gh run view "$run_id" --json workflow | jq -r '.workflow.name')
    branch=$(gh run view "$run_id" --json headBranch | jq -r '.headBranch')
    conclusion=$(gh run view "$run_id" --json conclusion | jq -r '.conclusion')
    
    echo "**Workflow**: $workflow"
    echo "**分支**: $branch"
    echo "**状态**: $conclusion"
    
    # 获取失败 jobs
    failed_jobs=$(gh run view "$run_id" --json jobs | jq -r '.jobs[] | select(.conclusion == "failure") | .name')
    if [ -n "$failed_jobs" ]; then
        echo "**失败 Jobs**: $failed_jobs"
    fi
    
    # 获取关键错误信息
    echo "**关键错误**:"
    gh run view "$run_id" --log-failed | grep -i -E "(error|exception|failed|traceback)" | head -3 | sed 's/^/  /'
    
    echo
done

# 生成总结报告
echo
echo "## 总结报告"
echo "分析了 $(echo "$FAILED_RUNS" | jq length) 个失败的 runs"
echo

# 统计失败类型
echo "### 失败 Workflow 分布"
echo "$FAILED_RUNS" | jq -r '.[] | .workflow.name' | sort | uniq -c | sort -nr
echo

# 统计分支分布
echo "### 失败分支分布"
echo "$FAILED_RUNS" | jq -r '.[] | .headBranch' | sort | uniq -c | sort -nr
echo

echo "### 建议行动"
echo "1. 优先处理最频繁失败的 workflow"
echo "2. 检查失败集中的分支是否有特殊问题"
echo "3. 查看具体 run 的完整日志进行深入分析"
echo "4. 考虑改进 CI 配置或测试稳定性"