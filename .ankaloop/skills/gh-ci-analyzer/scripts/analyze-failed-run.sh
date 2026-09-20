#!/bin/bash

# 分析单个失败的 GitHub Actions run
# 用法: ./analyze-failed-run.sh <run-id>

set -e

if [ $# -eq 0 ]; then
    echo "用法: $0 <run-id>"
    echo "获取 run-id: gh run list --status failed"
    exit 1
fi

RUN_ID=$1

echo "=== 分析失败 Run: $RUN_ID ==="
echo

# 获取 run 基本信息
echo "## Run 基本信息"
gh run view "$RUN_ID" --json databaseId,headBranch,conclusion,createdAt,workflow | jq -r '
"Run ID: \(.databaseId)",
"分支: \(.headBranch)",
"状态: \(.conclusion)",
"创建时间: \(.createdAt)",
"Workflow: \(.workflow.name)"
'
echo

# 获取失败 job 信息
echo "## 失败 Job 信息"
gh run view "$RUN_ID" --json jobs | jq -r '.jobs[] | select(.conclusion == "failure") | 
"Job: \(.name) | 状态: \(.conclusion) | 开始: \(.startedAt) | 结束: \(.completedAt)"
'
echo

# 获取失败日志
echo "## 失败日志 (最后 50 行)"
gh run view "$RUN_ID" --log-failed | tail -50
echo

# 生成简单的分析报告
echo "## 快速分析"
echo "Run $RUN_ID 在分支 $(gh run view "$RUN_ID" --json headBranch | jq -r '.headBranch') 上失败"
echo "失败时间: $(gh run view "$RUN_ID" --json completedAt | jq -r '.completedAt')"
echo
echo "建议下一步操作:"
echo "1. 查看完整日志: gh run view $RUN_ID --log-failed"
echo "2. 查看相关 PR: gh pr list --head $(gh run view "$RUN_ID" --json headBranch | jq -r '.headBranch')"
echo "3. 检查代码变更: gh diff $(gh run view "$RUN_ID" --json headCommit | jq -r '.oid')~1 $(gh run view "$RUN_ID" --json headCommit | jq -r '.oid')"