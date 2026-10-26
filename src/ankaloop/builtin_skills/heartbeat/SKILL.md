---
name: heartbeat
description: Periodic heartbeat check that reads HEARTBEAT.md from the workspace and executes any tasks listed there. Use for autonomous background monitoring, periodic maintenance, and proactive task execution. Triggered by a cron schedule.
triggers:
  - schedule: "*/30 * * * *"
    command: "Read HEARTBEAT.md in the workspace root (if it exists). Follow any instructions or tasks listed there. If nothing needs attention, reply [SILENT]."
    notify: false
    timeout: 300
---

# Heartbeat

Periodic agent wake-up to check for tasks in `HEARTBEAT.md`.

## How It Works

1. Every 30 minutes, the scheduler triggers this skill
2. Read `HEARTBEAT.md` from the workspace root
3. If file is empty or has no actionable content → reply `[SILENT]` and stop
4. If file has tasks → execute them one by one
5. Mark completed tasks by moving them to the `## Completed` section
6. If `telegram-sender` skill is available and the task warrants notification, send a Telegram update

## HEARTBEAT.md Format

```markdown
# Heartbeat Tasks

## Active Tasks

- Check CI status for open PRs and fix any failures
- Review and merge dependabot PRs if tests pass

## Completed

- [2026-02-17] Cleaned up stale branches
```

## Rules

- **Empty = skip**: If `HEARTBEAT.md` is missing, empty, or only has headers/comments, reply `[SILENT]`
- **Task markers**: Lines starting with `- ` under `## Active Tasks` are actionable
- **Completion**: After finishing a task, move it to `## Completed` with today's date prefix
- **Errors**: If a task fails, leave it in Active Tasks and append `(FAILED: reason)` to the line
- **Safety**: Never delete `HEARTBEAT.md`, only modify task entries
- **Scope**: Tasks should be self-contained and completable within the timeout (5 minutes)

## Skipable Content

These are not actionable (skip them):
- Empty lines
- Lines starting with `#` (headers)
- Lines starting with `<!--` (HTML comments)
- Lines with only `- [ ]` or `- [x]` (empty checkboxes)
