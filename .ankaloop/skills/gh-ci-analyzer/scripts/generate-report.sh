#!/bin/bash

# 生成结构化的 CI 失败分析报告
# 用法: ./generate-report.sh <run-id> [output-file]

set -e

if [ $# -eq 0 ]; then
    echo "用法: $0 <run-id> [output-file]"
    exit 1
fi

RUN_ID=$1
OUTPUT_FILE=${2:-"ci-failure-report-$RUN_ID.md"}

echo "生成 CI 失败分析报告..."
echo "Run ID: $RUN_ID"
echo "输出文件: $OUTPUT_FILE"
echo

# 获取 run 信息
RUN_INFO=$(gh run view "$RUN_ID" --json databaseId,headBranch,conclusion,createdAt,completedAt,workflow,headCommit)
WORKFLOW_NAME=$(echo "$RUN_INFO" | jq -r '.workflow.name')
BRANCH=$(echo "$RUN_INFO" | jq -r '.headBranch')
CONCLUSION=$(echo "$RUN_INFO" | jq -r '.conclusion')
CREATED_AT=$(echo "$RUN_INFO" | jq -r '.createdAt')
COMPLETED_AT=$(echo "$RUN_INFO" | jq -r '.completedAt')
COMMIT_SHA=$(echo "$RUN_INFO" | jq -r '.headCommit.oid')

# 获取失败 job 信息
FAILED_JOBS=$(gh run view "$RUN_ID" --json jobs | jq -r '.jobs[] | select(.conclusion == "failure")')

# 获取 PR 信息（如果有）
PR_INFO=$(gh pr list --head "$BRANCH" --json number,title 2>/dev/null || echo '{"number": null, "title": null}')
PR_NUMBER=$(echo "$PR_INFO" | jq -r '.number // "无"')
PR_TITLE=$(echo "$PR_INFO" | jq -r '.title // "无"')

# 生成报告
cat > "$OUTPUT_FILE" << EOF
# CI 失败分析报告

## 基本信息
- **Run ID**: $RUN_ID
- **Workflow**: $WORKFLOW_NAME
- **分支**: $BRANCH
- **提交**: $COMMIT_SHA
- **开始时间**: $CREATED_AT
- **结束时间**: $COMPLETED_AT
- **PR**: $PR_NUMBER$(if [ "$PR_NUMBER" != "无" ]; then echo " - $PR_TITLE"; fi)

## 失败概览
- **状态**: $CONCLUSION
- **持续时间**: $(date -d "$COMPLETED_AT" +%s) - $(date -d "$CREATED_AT" +%s) | awk '{printf "%d 秒", \$1}')

EOF

# 添加失败 jobs 信息
if [ -n "$FAILED_JOBS" ]; then
    echo "## 失败 Jobs" >> "$OUTPUT_FILE"
    echo "$FAILED_JOBS" | jq -r '.[] | "- **\(.name)**: \(.conclusion) (开始: \(.startedAt), 结束: \(.completedAt))"' >> "$OUTPUT_FILE"
    echo >> "$OUTPUT_FILE"
fi

# 添加关键错误日志
echo "## 关键错误日志" >> "$OUTPUT_FILE"
echo '```' >> "$OUTPUT_FILE"
gh run view "$RUN_ID" --log-failed | head -100 >> "$OUTPUT_FILE"
echo '```' >> "$OUTPUT_FILE"
echo >> "$OUTPUT_FILE"

# 添加分析部分
cat >> "$OUTPUT_FILE" << EOF
## 初步分析

### 错误类型识别
\$(gh run view "$RUN_ID" --log-failed | grep -i -E "(error|exception|failed)" | head -5 | sed 's/^/- /')

### 可能的根本原因
[需要根据具体日志内容进行分析]

## 修复建议

### 立即行动
1. 查看完整日志: \`gh run view $RUN_ID --log-failed\`
2. 检查代码变更: \`gh diff $COMMIT_SHA~1 $COMMIT_SHA\`
EOF

if [ "$PR_NUMBER" != "无" ]; then
    echo "3. 查看 PR 详情: \`gh pr view $PR_NUMBER\`" >> "$OUTPUT_FILE"
fi

cat >> "$OUTPUT_FILE" << EOF

### 长期改进
1. [根据失败类型添加具体建议]
2. [考虑改进 CI 配置]
3. [增强错误处理]

## 相关资源
- **Run 详情**: \`gh run view $RUN_ID\`
- **完整日志**: \`gh run view $RUN_ID --log\`
- **提交详情**: \`gh commit view $COMMIT_SHA\`
EOF

if [ "$PR_NUMBER" != "无" ]; then
    echo "- **PR 链接**: \`gh pr view $PR_NUMBER --web\`" >> "$OUTPUT_FILE"
fi

echo "报告已生成: $OUTPUT_FILE"
echo "查看报告: cat $OUTPUT_FILE"
echo "编辑报告: vim $OUTPUT_FILE"