# GitHub CLI (gh) 备忘单

## 认证

```bash
# 登录
gh auth login

# 检查状态
gh auth status

# 查看当前用户
gh api user
```

## Workflow Runs

### 列表操作

```bash
# 列出最近的 runs
gh run list

# 按状态过滤
gh run list --status failed
gh run list --status completed
gh run list --status in_progress

# 按分支过滤
gh run list --branch main
gh run list --branch feature/test

# 按 workflow 过滤
gh run list --workflow "CI.yml"
gh run list --workflow "Deploy"

# 限制数量
gh run list --limit 10
gh run list --limit 50

# JSON 输出
gh run list --json databaseId,headBranch,conclusion
gh run list --json databaseId,headBranch,conclusion,workflow,createdAt
```

### 查看详情

```bash
# 查看特定 run
gh run view <run-id>

# 查看最新 run
gh run view

# 按分支查看最新 run
gh run view --branch main

# 按 workflow 查看最新 run
gh run view --workflow "CI.yml"

# JSON 输出
gh run view <run-id> --json
gh run view <run-id> --json jobs,headBranch,conclusion
```

### 查看日志

```bash
# 查看失败日志
gh run view <run-id> --log-failed

# 查看完整日志
gh run view <run-id> --log

# 查看特定 job 日志
gh run view <run-id> --job <job-id>

# 实时跟踪日志
gh run watch <run-id>
```

## Pull Requests

### PR 列表

```bash
# 列出 PRs
gh pr list

# 按状态过滤
gh pr list --state open
gh pr list --state closed
gh pr list --state merged

# 按作者过滤
gh pr list --author @me

# 按分支过滤
gh pr list --head feature/test
gh pr list --base main

# JSON 输出
gh pr list --json number,title,headRefName
```

### PR 详情

```bash
# 查看当前 PR
gh pr view

# 查看特定 PR
gh pr view <pr-number>

# JSON 输出
gh pr view --json number,title,body,headRefName,baseRefName
gh pr view --json statusCheckRollup
```

### PR 检查状态

```bash
# 查看当前 PR 的检查
gh pr checks

# 查看特定 PR 的检查
gh pr checks <pr-number>

# JSON 输出
gh pr checks --json name,status,conclusion
```

## Commits

```bash
# 查看提交详情
gh commit view <sha>

# 查看提交差异
gh diff <sha1> <sha2>
gh diff HEAD~1 HEAD

# JSON 输出
gh commit view <sha> --json message,author,committer
```

## Issues

```bash
# 列出 issues
gh issue list

# 查看特定 issue
gh issue view <issue-number>

# JSON 输出
gh issue list --json number,title,state
```

## 高级用法

### JSON 查询

```bash
# 使用 jq 处理 JSON 输出
gh run list --json | jq '.[] | select(.conclusion == "failure")'

# 获取失败 runs 的 ID
gh run list --status failed --json | jq -r '.[].databaseId'

# 获取特定 workflow 的 runs
gh run list --json | jq '.[] | select(.workflow.name == "CI")'
```

### 批量操作

```bash
# 批量查看失败 runs
for run_id in $(gh run list --status failed --json | jq -r '.[].databaseId'); do
    echo "=== Run $run_id ==="
    gh run view "$run_id" --log-failed | head -20
done

# 批量取消 runs
gh run list --status in_progress --json | jq -r '.[].databaseId' | xargs -I {} gh run cancel {}
```

### 环境变量

```bash
# 设置 GitHub token
export GH_TOKEN=your_token_here

# 设置 GitHub Enterprise
export GH_HOST=github.company.com
```

## 常见错误排查

### 权限问题
```bash
# 检查认证
gh auth status

# 重新登录
gh auth login --scopes "repo,workflow"
```

### API 限制
```bash
# 查看剩余 API 调用
gh api rate_limit

# 等待重置
echo "等待 API 重置..."
```

### 网络问题
```bash
# 设置代理
export https_proxy=http://proxy.company.com:8080
export http_proxy=http://proxy.company.com:8080
```

## 配置文件

配置文件位置：
- Linux/macOS: `~/.config/gh/config.yml`
- Windows: `%APPDATA%\gh\config.yml`

示例配置：
```yaml
github.com:
  user: your-username
  oauth_token: ghp_xxxxxxxxxxxxxxxxxxxxxxxx
  git_protocol: https
```

## 有用的别名

```bash
# 在 .bashrc 或 .zshrc 中添加
alias ghr='gh run list'
alias ghrf='gh run list --status failed'
alias ghv='gh run view'
alias ghvl='gh run view --log-failed'
alias ghpr='gh pr list'
alias ghpc='gh pr checks'
```