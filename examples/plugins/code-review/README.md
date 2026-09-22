# Code Review Plugin

Multi-agent automated code review with confidence-based scoring to filter false positives.

## Overview

The Code Review Plugin automates code review by analyzing code from multiple perspectives using specialized agents. It uses confidence scoring to filter out false positives, ensuring only high-quality, actionable feedback is provided.

## Command: `/code-review`

Performs automated code review on specified files or recent changes.

**Usage:**
```bash
# Review specific files
/code-review src/auth.py src/utils.py

# Review recent git changes
/code-review --git

# Review with specific focus
/code-review --focus security src/
```

**Options:**
- `--git`: Review uncommitted changes
- `--focus <area>`: Focus on specific area (security, performance, style, bugs)
- `--threshold <N>`: Confidence threshold (default: 50)

## How It Works

1. **Gathers context** - Reads project guidelines and conventions
2. **Launches multiple agents in parallel:**
   - **Bug Detector**: Scans for obvious bugs and logic errors
   - **Security Checker**: Analyzes security vulnerabilities
   - **Style Checker**: Verifies code style and conventions
3. **Scores each issue** - 0-100 confidence level
4. **Filters results** - Only shows issues above threshold
5. **Outputs actionable feedback**

## Agents

### `bug-detector`
Focuses on finding bugs:
- Logic errors
- Edge cases
- Null handling
- Race conditions
- Resource leaks

### `security-checker`
Focuses on security:
- Input validation
- Injection vulnerabilities
- Authentication issues
- Sensitive data exposure
- Dependency vulnerabilities

### `style-checker`
Focuses on code quality:
- Naming conventions
- Code organization
- Documentation
- Complexity
- Duplication

## Confidence Scoring

Each issue is rated on a 0-100 scale:

| Score | Meaning |
|-------|---------|
| 0-25 | Low confidence, likely false positive |
| 26-50 | Moderate confidence, worth reviewing |
| 51-75 | High confidence, real issue |
| 76-100 | Very high confidence, needs attention |

**Default threshold: 50** (only shows issues >= 50)

## Output Format

```markdown
## Code Review Results

Found 5 issues (8 filtered as low confidence):

### 🔴 Critical (2)

1. **SQL Injection vulnerability** (confidence: 95)
   Location: src/db.py:45
   Issue: User input directly concatenated into SQL query
   Fix: Use parameterized queries
   
2. **Unhandled exception** (confidence: 88)
   Location: src/api.py:123
   Issue: Network call without try/catch
   Fix: Add error handling

### 🟡 Medium (2)
...

### 🟢 Low (1)
...
```

## Best Practices

1. **Review before committing** - Run `/code-review --git`
2. **Set appropriate threshold** - Lower for critical code
3. **Focus when needed** - Use `--focus security` for sensitive code
4. **Combine with tests** - Code review doesn't replace testing

## Requirements

- AnkaLoop v0.14.0 or later
- Git (for `--git` option)

## Author

AnkaLoop Team
