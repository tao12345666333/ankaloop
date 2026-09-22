# Security Guidance Plugin

Security validation hooks that monitor dangerous patterns and provide warnings before potentially risky operations.

## Overview

This plugin uses the new simplified Markdown hook format to define security rules. Instead of complex TOML or JSON configurations, you write hooks in Markdown with YAML frontmatter.

## Features

- **Pattern-based detection** - Regex patterns to catch dangerous operations
- **Flexible actions** - Warn, block, or ask for confirmation
- **Easy configuration** - Simple Markdown format
- **Multiple security checks**:
  - Dangerous shell commands
  - Credential exposure
  - Dangerous file operations
  - Security anti-patterns

## Hooks

### `block-dangerous-commands.md`
Blocks extremely dangerous shell commands like `rm -rf /`, `dd if=`, etc.

### `warn-credential-exposure.md`
Warns when writing files that might contain credentials.

### `warn-security-antipatterns.md`
Warns about common security anti-patterns in code.

## Hook Format (Simplified Markdown)

The new Markdown hook format is easy to read and write:

```markdown
---
name: my-security-hook
enabled: true
event: bash              # or: file, stop, session_start, session_end, prompt
action: warn             # or: block
pattern: rm\s+-rf
---

⚠️ **Warning Message**

Detailed explanation of the warning.
Why this is dangerous and what to do instead.
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier for the hook |
| `enabled` | No | Whether hook is active (default: true) |
| `event` | Yes | Event type to hook |
| `action` | No | `warn` or `block` (default: warn) |
| `pattern` | No* | Regex pattern to match |
| `conditions` | No* | Advanced conditions (alternative to pattern) |

*At least one of `pattern` or `conditions` is required.

### Event Types

| Event | Description | Matched Against |
|-------|-------------|-----------------|
| `bash` | Shell command execution | Command string |
| `file` | File write operations | File path and content |
| `stop` | Session ending | Transcript content |
| `session_start` | Session beginning | - |
| `session_end` | Session ending | - |
| `prompt` | User prompt submission | Prompt text |

### Actions

| Action | Behavior |
|--------|----------|
| `warn` | Shows warning but allows operation |
| `block` | Prevents operation from executing |

## Installation

1. Copy to your project:
   ```bash
   cp -r examples/plugins/security-guidance .ankaloop/plugins/
   ```

2. Or copy to user config:
   ```bash
   cp -r examples/plugins/security-guidance ~/.config/ankaloop/plugins/
   ```

## Customization

Create your own hooks in `.ankaloop/hooks/` or `.ankaloop/plugins/*/hooks/`:

```markdown
---
name: warn-production-db
enabled: true
event: bash
pattern: (mysql|psql|mongo).*production
action: warn
---

🔴 **Production Database Access Detected!**

You appear to be connecting to a production database.
Please ensure you have:
- Proper authorization
- A backup if making changes
- Tested your queries on staging first
```

## Requirements

- AnkaLoop v0.14.0 or later (with Markdown hook support)

## Author

AnkaLoop Team
