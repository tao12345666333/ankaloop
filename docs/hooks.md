# AnkaLoop Hooks System

The AnkaLoop Hooks system allows you to extend and customize agent behavior through external commands or Python scripts. Inspired by [Claude Code's hooks system](https://code.claude.com/docs/en/hooks), it provides a flexible way to:

- Validate and modify tool inputs before execution
- Process and modify tool outputs after execution
- Block dangerous operations
- Log and audit agent activities
- Add custom behaviors at key lifecycle points

## Configuration

Hooks are configured via TOML or JSON files:

- **Project-level**: `.ankaloop/hooks.toml` or `.ankaloop/hooks.json`
- **User-level**: `~/.config/ankaloop/hooks.toml` or `~/.config/ankaloop/hooks.json`

Project-level hooks override user-level hooks.

## Hook Events

| Event | Description | Use Cases |
|-------|-------------|-----------|
| `PreToolUse` | Before tool execution | Validate inputs, block operations, modify parameters |
| `PostToolUse` | After tool execution | Validate outputs, add feedback, log results |
| `UserPromptSubmit` | When user submits a prompt | Validate prompts, add context, block certain requests |
| `SessionStart` | When a new session begins | Initialize logging, load context |
| `SessionEnd` | When a session ends | Cleanup, save state, generate reports |
| `Stop` | When agent is about to stop | Override stop behavior, add final actions |
| `PreCompact` | Before context compaction | Save important context, log compaction events |

## Configuration Format

### TOML Format

```toml
[hooks.PreToolUse]
[[hooks.PreToolUse.handlers]]
matcher = "write_file|apply_patch"  # Regex pattern to match tool names
type = "command"                  # "command" or "python"
command = "./scripts/validate-writes.sh"
timeout = 30                      # Timeout in seconds
failure_mode = "closed"           # "open" (default) or "closed"
enabled = true

[[hooks.PreToolUse.handlers]]
matcher = "*"                     # Match all tools
type = "python"
script = "./scripts/log_all_tools.py"
timeout = 5

[hooks.PostToolUse]
[[hooks.PostToolUse.handlers]]
matcher = "write_file"
type = "command"
command = "$ANKA_PROJECT_DIR/scripts/lint-file.sh"
timeout = 60
```

`failure_mode` controls what happens when a hook cannot execute or times out:

- `open` logs the failure and allows execution to continue.
- `closed` denies the operation. Use this for policy and security enforcement hooks.

This setting is independent from a hook's normal allow/deny result.

### JSON Format

```json
{
  "hooks": {
    "PreToolUse": {
      "handlers": [
        {
          "matcher": "write_file|apply_patch",
          "type": "command",
          "command": "./scripts/validate-writes.sh",
          "timeout": 30,
          "enabled": true
        }
      ]
    }
  }
}
```

## Handler Types

### Command Handlers

Execute shell commands with hook input passed via stdin as JSON.

```toml
[[hooks.PreToolUse.handlers]]
type = "command"
command = "./scripts/my-hook.sh"
```

Environment variables available:
- `$ANKA_PROJECT_DIR` - Project directory path
- `$ANKA_SESSION_ID` - Current session ID
- `$ANKA_HOOK_EVENT` - Hook event name
- `$ANKA_TOOL_NAME` - Tool name (for tool-related events)

### Python Script Handlers

Execute Python scripts with hook input passed via stdin.

```toml
[[hooks.PreToolUse.handlers]]
type = "python"
script = "./scripts/validate.py"
```

### Python Function Handlers

Call Python functions directly (must be importable).

```toml
[[hooks.PreToolUse.handlers]]
type = "python"
function = "mypackage.hooks.validate_tool"
```

## Hook Input

Hooks receive JSON input via stdin:

```json
{
  "session_id": "abc123",
  "hook_event_name": "PreToolUse",
  "cwd": "/home/user/project",
  "tool_name": "write_file",
  "tool_input": {
    "path": "/path/to/file.py",
    "content": "file content"
  },
  "tool_use_id": "toolu_01ABC123..."
}
```

Additional fields by event type:
- `PostToolUse`: `tool_response` - response from tool execution
- `UserPromptSubmit`: `prompt` - user's prompt text
- `Notification`: `message`, `notification_type`

## Hook Output

### Exit Code Behavior

- **Exit 0**: Success - stdout is processed (JSON or text feedback)
- **Exit 2**: Blocking error - stderr becomes error message, tool is denied
- **Other**: Non-blocking error - logged as warning, execution continues

### JSON Output

For advanced control, output JSON to stdout:

```json
{
  "continue": true,
  "stopReason": "Reason for stopping",
  "feedback": "Message shown to the model",
  "systemMessage": "Message shown to the user",
  "suppressOutput": false,
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "Approved by policy",
    "updatedInput": {
      "modified_field": "new value"
    }
  }
}
```

### PreToolUse Decisions

The `permissionDecision` field controls tool execution:
- `"allow"` - Bypass permission checks and execute
- `"deny"` - Block execution with reason shown to model
- `"ask"` - Prompt user for confirmation

Use `updatedInput` to modify tool parameters before execution.

### PostToolUse Decisions

Use `updatedResponse` to modify the tool response before it's sent to the model.

## Examples

### 1. Block Writes to Sensitive Files

```python
#!/usr/bin/env python3
import json
import sys

BLOCKED_PATTERNS = [".env", ".secrets", "credentials", ".ssh/"]

input_data = json.load(sys.stdin)
tool_name = input_data.get("tool_name", "")
tool_input = input_data.get("tool_input", {})

if tool_name in ("write_file", "apply_patch"):
    file_path = tool_input.get("path", "").lower()
    for pattern in BLOCKED_PATTERNS:
        if pattern in file_path:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Writing to '{file_path}' is blocked for security"
                }
            }
            print(json.dumps(output))
            sys.exit(0)

sys.exit(0)  # Allow all other operations
```

### 2. Log All Tool Calls

```python
#!/usr/bin/env python3
import json
import sys
from datetime import datetime
from pathlib import Path

input_data = json.load(sys.stdin)
log_entry = {
    "timestamp": datetime.now().isoformat(),
    "session_id": input_data.get("session_id"),
    "tool_name": input_data.get("tool_name"),
    "tool_input": input_data.get("tool_input"),
}

log_file = Path("/tmp/ankaloop_tool_calls.log")
with open(log_file, "a") as f:
    f.write(json.dumps(log_entry) + "\n")

sys.exit(0)
```

### 3. Run Linting After File Writes

```bash
#!/bin/bash
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('path',''))")

if [[ "$FILE_PATH" == *.py ]] && command -v ruff &> /dev/null; then
    LINT_OUTPUT=$(ruff check "$FILE_PATH" 2>&1 || true)
    if [ -n "$LINT_OUTPUT" ]; then
        echo "{\"feedback\": \"Lint issues: $LINT_OUTPUT\"}"
    fi
fi

exit 0
```

## Matcher Patterns

The `matcher` field supports:
- Exact match: `"write_file"` matches only `write_file`
- Regex patterns: `"write_file|apply_patch"` matches both
- Regex wildcards: `"mcp__.*"` matches all MCP tools, whose exposed names use the `mcp__server__tool` format
- All tools: `"*"` or `""` matches everything

## Programmatic Usage

```python
from ankaloop.hooks import (
    get_hooks_manager,
    run_pre_tool_use_hooks,
    run_post_tool_use_hooks,
    HookDecision,
)

# Run hooks before tool use
output = await run_pre_tool_use_hooks(
    session_id="my-session",
    tool_name="write_file",
    tool_input={"path": "/test.py", "content": "..."},
)

if output.decision == HookDecision.DENY:
    print(f"Blocked: {output.decision_reason}")
elif output.updated_input:
    # Use modified input
    tool_input = {**tool_input, **output.updated_input}
```

## Security Considerations

1. **Only trust project-level hooks** - User-level hooks can be overridden
2. **Validate hook scripts** - Review before enabling new hooks
3. **Use timeouts** - Prevent runaway hooks from blocking execution
4. **Limit permissions** - Run hooks with minimal required permissions
5. **Log hook activity** - Monitor hooks for unexpected behavior

## See Also

- [Example hooks configuration](../examples/hooks/hooks.toml)
- [Example hook scripts](../examples/hooks/)
- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
