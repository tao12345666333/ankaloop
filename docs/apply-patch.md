# Apply Patch Tool

The `apply_patch` tool provides a diff-based file patching system inspired by [OpenAI Codex's apply_patch](https://github.com/openai/codex). It is the **recommended way to edit files** in AnkaLoop, offering precision, efficiency, and batch operation support.

## Why Use Apply Patch?

### Token Efficiency

| Scenario | write_file | apply_patch |
|----------|------------|-------------|
| Fix 1 line in 1000-line file | ~1000 tokens | ~20 tokens |
| Modify 3 locations | ~3000 tokens | ~60 tokens |
| Create + modify files | ~2000 tokens | ~100 tokens |

### Precision

- **Context anchors**: Use `@@` headers to locate exact positions
- **Multi-level anchors**: Combine class and method names for unique matching
- **Context verification**: 3 lines of context before/after ensure correct placement

### Batch Operations

One patch can:
- Create multiple files
- Update multiple files
- Delete files
- Rename files

## Patch Format

```
*** Begin Patch
[one or more file operations]
*** End Patch
```

### Operation Types

#### Add File

```
*** Add File: path/to/new_file.py
+#!/usr/bin/env python3
+"""New module."""
+
+def hello():
+    return "Hello, World!"
```

- Every line must start with `+`
- Path must be relative (never absolute)

#### Delete File

```
*** Delete File: path/to/obsolete.py
```

- No content follows the header

#### Update File

```
*** Update File: path/to/existing.py
@@ class Calculator
@@ def subtract(a, b):
     """Subtract b from a."""
-    return a + b
+    return a - b
```

Components:
- **`@@` anchors**: Optional headers to locate the code block
- **Context lines**: Prefixed with space (` `)
- **Deletions**: Prefixed with minus (`-`)
- **Additions**: Prefixed with plus (`+`)

#### Rename + Update File

```
*** Update File: old/path/module.py
*** Move to: new/path/module.py
@@ def old_function():
-    pass
+    return True
```

## Context Guidelines

### Using Anchors

Anchors help locate the exact position in the file:

```
*** Update File: src/app.py
@@ class UserService
@@ def authenticate(user):
     if not user.password:
-        return False
+        raise AuthError("Password required")
```

Multiple `@@` lines narrow down the search:
1. Find `class UserService`
2. Within it, find `def authenticate(user)`
3. Match the context lines

### Context Lines

Always include 3 lines of context:

```
*** Update File: src/utils.py
@@ def process():
     for item in items:        # Context line 1
         result = transform()   # Context line 2
         if result:             # Context line 3
-            cache[id] = result # Line to delete
+            cache[id] = copy(result)  # Line to add
         log(result)            # Context line 1 (after)
         count += 1             # Context line 2 (after)
     return count               # Context line 3 (after)
```

## Full Example

```
*** Begin Patch

*** Add File: tests/test_calculator.py
+import pytest
+from calculator import Calculator
+
+def test_subtract():
+    calc = Calculator()
+    assert calc.subtract(5, 3) == 2

*** Update File: src/calculator.py
@@ class Calculator:
@@ def subtract(self, a, b):
     """Subtract b from a."""
-    return a + b  # Bug: was adding
+    return a - b

*** Update File: README.md
@@ ## Usage
 
-Run `python calculator.py` to start.
+Run `python -m calculator` to start.
+
+## Testing
+
+```bash
+pytest tests/
+```

*** Delete File: src/deprecated_calc.py

*** End Patch
```

## Error Handling

### Parse Errors

If the patch format is invalid:
```
Patch parse error: No '*** Begin Patch' found. Check the patch format.
```

### Apply Errors

If context doesn't match:
```
Patch apply error: Could not find match for hunk in src/app.py. 
Looking for: ['    if not user:', '        return False']...
```

**Common causes**:
- File was modified after you read it
- Not enough context lines
- Missing or incorrect `@@` anchors

## Best Practices

1. **Always read the file first** before creating a patch
2. **Use descriptive anchors** like `@@ class ClassName` and `@@ def method_name():`
3. **Include 3 lines of context** before and after each change
4. **Group related changes** in a single patch
5. **Use relative paths** - never absolute paths
6. **Test complex patches** on a copy first

## Configuration

The `apply_patch` tool is enabled by default. To disable it, modify your config:

```toml
# ~/.config/ankaloop/config.toml
[chat]
# Note: Currently there's no specific toggle for apply_patch
# It's always available when tools are enabled
```

## Comparison with write_file

| Feature | write_file | apply_patch |
|---------|------------|-------------|
| Create files | ✅ | ✅ |
| Delete files | ❌ | ✅ |
| Rename files | ❌ | ✅ |
| Precise edits | ❌ (overwrites) | ✅ (context-aware) |
| Batch operations | ❌ | ✅ |
| Token efficiency | Low | High |
| Error detection | None | Rich |

**When to use `write_file`**: Creating small new files (config files, scripts < 20 lines).

**When to use `apply_patch`**: All other file modifications, especially editing existing files.

## API Reference

### Tool Specification

```json
{
  "name": "apply_patch",
  "description": "Apply diff-based patches to files...",
  "parameters": {
    "type": "object",
    "properties": {
      "patch": {
        "type": "string",
        "description": "The patch content..."
      }
    },
    "required": ["patch"]
  }
}
```

### Python API

```python
from ankaloop.apply_patch import apply_patch_text, PatchParser, PatchApplier

# Simple usage
changes = apply_patch_text(patch_text)

# Advanced usage
parser = PatchParser()
patch = parser.parse(patch_text)

applier = PatchApplier(base_dir=Path("/project"))
changes = applier.apply(patch)

for change in changes:
    print(f"{change['type']}: {change['path']}")
```
