---
name: gh-pr-review
description: Review GitHub Pull Requests using the gh CLI. Use when a user asks to review a PR, perform code review, check a pull request, or provide feedback on proposed changes. Triggers on phrases like "review PR", "review pull request", "check PR", "code review".
---

# GitHub PR Review Skill

Review Pull Requests by fetching the diff, analyzing changes, and posting structured review comments.

## Prerequisites

- `gh` CLI authenticated (`gh auth status`)
- Repository must be a git clone with a GitHub remote

## Review Workflow

1. **Fetch PR metadata**: Run `gh pr view <number> --json title,body,files,additions,deletions,author,baseRefName,headRefName`
2. **Fetch the diff**: Run `gh pr diff <number>` to get the full diff
3. **Analyze the changes** by category:
   - **Architecture**: New files/modules, structural decisions
   - **Correctness**: Logic bugs, edge cases, error handling
   - **Security**: Input validation, secrets, auth
   - **Performance**: Unnecessary allocations, N+1 queries, blocking I/O
   - **Style**: Naming, consistency with codebase conventions
   - **Tests**: Coverage, edge cases, test quality
4. **Produce a structured review** (see format below)

## Review Output Format

```markdown
## PR Review: <title>

**PR**: #<number> | **Author**: <author> | **Branch**: <head> → <base>
**Files changed**: <count> | **+<additions>** / **-<deletions>**

### Summary
<1-2 sentence overview of what this PR does>

### 👍 Strengths
- <what's done well>

### 🔍 Issues Found

#### Critical
- [ ] <file>:<line> — <description>

#### Suggestions
- [ ] <file>:<line> — <description>

#### Nits
- <minor style/naming suggestions>

### 📋 Checklist
- [ ] Tests added/updated
- [ ] No secrets or credentials
- [ ] Error handling adequate
- [ ] Documentation updated if needed

### Verdict
<APPROVE | REQUEST_CHANGES | COMMENT> — <reasoning>
```

## Posting the Review

After generating the review, offer to post it as a PR comment:

```bash
gh pr review <number> --comment --body "<review content>"
```

Or for approve/request-changes:

```bash
gh pr review <number> --approve --body "<review content>"
gh pr review <number> --request-changes --body "<review content>"
```

## Tips

- For large PRs (>500 lines), focus on architectural decisions and critical issues first
- Check for consistency with the existing codebase patterns
- Look at test coverage relative to the complexity of changes
- Use `gh pr checks <number>` to see CI status
- When reviewing Python, pay attention to type hints, docstrings, and error handling
