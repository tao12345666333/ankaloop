---
name: gh-ci-analyzer
description: 使用 gh CLI 分析 GitHub Actions CI 日志，提供结构化的故障诊断和报告
---

# GitHub Actions CI 分析器

这个 skill 指导 agent 如何使用 GitHub CLI (gh) 来分析 GitHub Actions CI/CD 日志，诊断失败原因，并提供结构化的分析报告。

## 前置条件

确保已安装并认证 GitHub CLI：
```bash
gh auth login
gh auth status
```

## 核心命令

### 1. 查看 Workflow Runs

```bash
# 列出最近的 workflow runs
gh run list

# 列出特定 workflow 的 runs
gh run list --workflow "CI.yml"

# 限制显示数量
gh run list --limit 10

# 按状态过滤
gh run list --status failed
gh run list --status completed
```

### 2. 查看 Run 详情

```bash
# 查看特定 run 的基本信息
gh run view <run-id>

# 查看特定 workflow 的最新 run
gh run view --workflow "CI.yml"

# 查看特定分支的最新 run
gh run view --branch main
```

### 3. 查看失败日志

```bash
# 查看失败 job 的日志
gh run view <run-id> --log-failed

# 查看特定 job 的日志
gh run view <run-id> --job <job-id>

# 查看完整日志（包括成功的）
gh run view <run-id> --log
```

### 4. 查看 PR 检查状态

```bash
# 查看当前 PR 的检查状态
gh pr checks

# 查看特定 PR 的检查状态
gh pr checks <pr-number>

# 查看 PR 的详细状态
gh pr view <pr-number> --json statusCheckRollup
```

## 分析流程

### 步骤 1: 识别失败的 Run

```bash
# 查看最近的失败 runs
gh run list --status failed --limit 5
```

记录失败的 run ID 和 workflow 名称。

### 步骤 2: 获取失败详情

```bash
# 查看失败 run 的概览
gh run view <run-id>

# 查看失败日志
gh run view <run-id> --log-failed
```

### 步骤 3: 分析失败模式

在日志中寻找以下关键信息：

1. **错误消息**: 直接的 error 或 exception 信息
2. **失败步骤**: 哪个具体的 job 或 step 失败
3. **时间戳**: 失败发生的时间点
4. **环境信息**: OS、Python 版本、依赖版本等
5. **资源限制**: 内存、磁盘空间、超时等

### 步骤 4: 关联 PR 信息（如果适用）

```bash
# 查看关联的 PR
gh pr view --json number,title,headRefName

# 查看 PR 的检查状态
gh pr checks
```

## 常见失败类型分析

### 1. 测试失败

**识别特征**:
- AssertionError, unittest failures
- pytest exit codes
- 测试覆盖率报告

**分析要点**:
- 哪个测试失败
- 失败的具体断言
- 期望值 vs 实际值
- 是否为 flaky test

### 2. 依赖安装失败

**识别特征**:
- pip/conda install errors
- 版本冲突
- 网络超时

**分析要点**:
- 哪个包安装失败
- 版本要求冲突
- 索引源问题
- 缓存问题

### 3. 构建失败

**识别特征**:
- 编译错误
- 语法错误
- linting failures

**分析要点**:
- 具体的编译错误
- 代码规范问题
- 类型检查失败

### 4. 环境问题

**识别特征**:
- 权限错误
- 资源不足
- 配置问题

**分析要点**:
- 权限设置
- 内存/磁盘使用
- 环境变量配置

## 结构化报告格式

### CI 失败分析报告模板

```markdown
# CI 失败分析报告

## 基本信息
- **Run ID**: <run-id>
- **Workflow**: <workflow-name>
- **分支**: <branch-name>
- **提交**: <commit-sha>
- **时间**: <timestamp>
- **PR**: <pr-number> (如果适用)

## 失败概览
- **失败 Job**: <job-name>
- **失败 Step**: <step-name>
- **错误类型**: <error-category>
- **失败时间**: <failure-timestamp>

## 错误详情
```
[粘贴关键错误日志]
```

## 根本原因分析
[详细分析失败的根本原因]

## 影响范围
- **受影响功能**: <affected-features>
- **影响程度**: <high/medium/low>
- **是否阻塞性**: <yes/no>

## 修复建议
1. **立即修复**: <immediate-fix-steps>
2. **长期改进**: <long-term-improvements>
3. **预防措施**: <prevention-measures>

## 相关资源
- **失败日志链接**: <log-url>
- **PR 链接**: <pr-url>
- **提交链接**: <commit-url>
```

## 高级分析技巧

### 1. 批量分析多个失败

```bash
# 获取最近 10 个失败的 runs
gh run list --status failed --limit 10 --json databaseId,headBranch,conclusion

# 批量查看失败日志
for run_id in <run-ids>; do
    echo "=== Run $run_id ==="
    gh run view $run_id --log-failed
done
```

### 2. 搜索特定错误模式

```bash
# 在日志中搜索特定错误
gh run view <run-id> --log | grep -i "error\|exception\|failed"

# 搜索超时相关
gh run view <run-id> --log | grep -i "timeout"
```

### 3. 性能分析

```bash
# 查看 job 执行时间
gh run view <run-id> --json jobs | jq '.jobs[] | {name: .name, duration: .completedAt - .startedAt}'
```

## 自动化脚本

参考 `scripts/` 目录中的辅助脚本：
- `analyze-failed-run.sh`: 分析单个失败 run
- `batch-analyze.sh`: 批量分析多个失败
- `generate-report.sh`: 生成结构化报告

## 故障排除

### gh CLI 问题
```bash
# 检查认证状态
gh auth status

# 重新认证
gh auth login

# 检查版本
gh --version
```

### 权限问题
确保有足够的仓库权限：
- `repo` scope 用于私有仓库
- `public_repo` scope 用于公开仓库

## 最佳实践

1. **及时分析**: 失败后尽快分析，避免日志过期
2. **保存关键日志**: 将重要失败日志保存到本地
3. **建立模式库**: 记录常见失败模式和解决方案
4. **定期回顾**: 定期分析失败趋势，改进 CI 配置
5. **自动化报告**: 建立自动化的失败报告生成流程