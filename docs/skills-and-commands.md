# Skills and Slash Commands

AnkaLoop supports two powerful extension mechanisms inspired by Gemini CLI:

1. **Skills** - Reusable knowledge or behavior definitions that can be activated to provide specialized capabilities
2. **Slash Commands** - Custom command shortcuts that can be invoked using the `/command` syntax

## Skills

Skills are markdown files with YAML frontmatter that define reusable knowledge or behavior. When activated, their content is injected into the agent's system prompt, giving the agent specialized capabilities.

### Skill Structure

Skills are stored in directories containing a `SKILL.md` file:

```
~/.config/ankaloop/skills/
└── code-review/
    └── SKILL.md

~/.agents/skills/           # Home agent skills
└── personal-helper/
    └── SKILL.md

.ankaloop/skills/              # Project-level skills
└── my-project-skill/
    └── SKILL.md
```

### SKILL.md Format

```markdown
---
name: code-review
description: Provides expert code review guidelines and best practices
---

# Code Review Skill

When reviewing code, follow these best practices:
...
```

The frontmatter must include:
- `name`: The skill name (used for activation)
- `description`: A brief description shown in listings

### Discovery Locations

Discovery uses increasing precedence. Later locations override earlier same-name skills.

1. **Built-in skills** bundled with AnkaLoop
2. **User skills**: `~/.config/ankaloop/skills/<skill-name>/SKILL.md`
3. **Home agent skills**: `~/.agents/skills/<skill-name>/SKILL.md`
4. **Project skills**: `.ankaloop/skills/<skill-name>/SKILL.md`

### Using Skills

In the CLI interactive mode:

```bash
# List available skills
/skills list

# Activate a skill
/skills activate code-review

# Deactivate a skill
/skills deactivate code-review

# Show skill content
/skills show code-review
```

Active skills are indicated with a ⭐ in the listing.

### Programmatic Usage

```python
from ankaloop import get_skill_manager

# Get the skill manager
sm = get_skill_manager()

# Discover skills
sm.discover_skills()

# List skills
for skill in sm.get_all_skills():
    print(f"{skill.name}: {skill.description}")

# Activate a skill
sm.activate_skill("code-review")

# Get active skills content (for injection into prompts)
content = sm.get_active_skills_content()
```

## Slash Commands

Slash commands are custom shortcuts defined as TOML files. They allow you to save frequently used prompts and execute them with simple command invocations.

### Command Structure

Commands are `.toml` files stored in command directories:

```
~/.config/ankaloop/commands/
├── explain.toml
├── review.toml
└── git/
    ├── commit.toml
    └── log.toml

.ankaloop/commands/           # Project-level commands
└── deploy.toml
```

### Command File Format

```toml
# Simple command
description = "Explain code in detail"
prompt = """
Please explain the following code:

{{args}}
"""
```

Required:
- `prompt`: The prompt text to submit

Optional:
- `description`: Shown in `/help` listings

### Naming Convention

The command name is derived from the file path:
- `explain.toml` → `/explain`
- `git/commit.toml` → `/git:commit`
- `git/log.toml` → `/git:log`

### Argument Handling

#### Using `{{args}}`
When your prompt contains `{{args}}`, it's replaced with the text after the command:

```toml
prompt = "Explain {{args}} in simple terms"
```

Using `/explain Python generators` sends: "Explain Python generators in simple terms"

#### Default Behavior
If no `{{args}}` is present, the full command is appended to the prompt.

### Shell Injection `!{...}`

Execute shell commands and inject their output:

```toml
description = "Generate commit message from staged changes"
prompt = """
Generate a commit message for:

```diff
!{git diff --staged}
```
"""
```

Arguments inside shell commands are automatically shell-escaped:
```toml
prompt = "Search results for {{args}}: !{grep -r {{args}} .}"
```

### File Injection `@{...}`

Inject file contents:

```toml
prompt = """
Review this file:

@{{{args}}}

Provide suggestions for improvement.
"""
```

Using `/review src/main.py` injects the content of `src/main.py`.

### Discovery Locations

1. **User commands**: `~/.config/ankaloop/commands/`
2. **Project commands**: `.ankaloop/commands/` (takes precedence)

### Built-in Commands

AnkaLoop provides several built-in commands:

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/skills` | Manage skills |
| `/clear` | Clear conversation history |
| `/info` | Show session information |
| `/exit` | Exit the chat session |

### Programmatic Usage

```python
from ankaloop import get_command_manager

# Get the command manager
cm = get_command_manager()

# Discover commands
cm.discover_commands()

# List commands
for cmd in cm.get_all_commands():
    print(f"/{cmd.name}: {cmd.description}")

# Parse input
command, args = cm.parse_input("/git:commit")
if command:
    result = cm.execute_command(command, args)
```

## Examples

The `examples/` directory contains sample skills and commands:

```
examples/
├── skills/
│   ├── code-review/
│   │   └── SKILL.md
│   └── python-expert/
│       └── SKILL.md
└── commands/
    ├── explain.toml
    ├── review.toml
    └── git/
        ├── commit.toml
        └── log.toml
```

To use these examples, copy them to your config directory:

```bash
# Copy skills
cp -r examples/skills/* ~/.config/ankaloop/skills/

# Copy commands
cp -r examples/commands/* ~/.config/ankaloop/commands/
```

## Best Practices

### Skills
1. Keep skills focused on a specific domain
2. Provide clear, actionable guidelines
3. Use markdown formatting for readability
4. Test skills with various prompts

### Commands
1. Use descriptive names
2. Provide helpful descriptions
3. Use namespacing (subdirectories) for related commands
4. Test shell commands before using in production
5. Be cautious with `!{...}` - commands are executed on your system

## Security Considerations

- Shell commands in `!{...}` are executed on your local system
- User input in `{{args}}` is shell-escaped when used in `!{...}`
- Review custom commands before using them
- Project-level commands can override user-level commands
