# AnkaLoop Plugins

This directory contains official AnkaLoop plugins that extend functionality through custom commands, agents, skills, hooks, and workflows.

## What are AnkaLoop Plugins?

AnkaLoop plugins are extensions that enhance AnkaLoop with:
- **Custom slash commands** - Shortcuts for common tasks
- **Specialized agents** - Pre-configured agents for specific domains
- **Skills** - Knowledge and behavior patterns for agents
- **Hooks** - Event-driven automation and validation
- **Workflows** - Multi-step structured processes

Plugins can be shared across projects and teams, providing consistent tooling and workflows.

## Plugins in This Directory

| Name | Description | Contents |
|------|-------------|----------|
| [code-review](./code-review/) | Multi-agent code review with confidence scoring | **Command:** `/code-review` - Automated code review workflow<br>**Agents:** `code-reviewer`, `security-checker`, `style-checker` |
| [feature-dev](./feature-dev/) | 7-phase structured feature development | **Command:** `/feature-dev` - Guided development workflow<br>**Agents:** `code-explorer`, `code-architect`, `code-reviewer` |
| [security-guidance](./security-guidance/) | Security validation and warnings | **Hook:** PreToolUse - Monitors dangerous patterns<br>**Skill:** Security best practices |

## Installation

1. **Copy plugin to your project:**
   ```bash
   cp -r examples/plugins/feature-dev .ankaloop/plugins/
   ```

2. **Or copy to user-level config:**
   ```bash
   cp -r examples/plugins/feature-dev ~/.config/ankaloop/plugins/
   ```

3. **Use the plugin commands:**
   ```bash
   anka
   AnkaLoop> /feature-dev Add user authentication
   ```

## Plugin Structure

Each plugin follows this standard structure:

```
plugin-name/
├── plugin.json           # Plugin metadata and configuration
├── commands/             # Slash commands (optional)
│   └── command-name.md   # Command definition
├── agents/               # Specialized agents (optional)
│   └── agent-name.yaml   # Agent specification
├── skills/               # Agent skills (optional)
│   └── skill-name/
│       └── SKILL.md      # Skill definition
├── hooks/                # Event handlers (optional)
│   └── hook-name.md      # Hook configuration (simplified format)
└── README.md             # Plugin documentation
```

## Creating Your Own Plugin

### 1. Create plugin directory

```bash
mkdir -p .ankaloop/plugins/my-plugin/{commands,agents,skills,hooks}
```

### 2. Add plugin.json

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "My custom AnkaLoop plugin",
  "author": "Your Name",
  "components": {
    "commands": ["commands/*.md"],
    "agents": ["agents/*.yaml"],
    "skills": ["skills/*/SKILL.md"],
    "hooks": ["hooks/*.md"]
  }
}
```

### 3. Add components

Create commands, agents, skills, or hooks as needed.

### 4. Test your plugin

```bash
anka --plugin .ankaloop/plugins/my-plugin
```

## Plugin Configuration

Plugins can be configured in your project's `.ankaloop/settings.json`:

```json
{
  "plugins": {
    "enabled": ["feature-dev", "code-review"],
    "disabled": ["security-guidance"],
    "paths": [
      ".ankaloop/plugins",
      "~/.config/ankaloop/plugins"
    ]
  }
}
```

## Contributing

When creating plugins:

1. Follow the standard plugin structure
2. Include a comprehensive README.md
3. Add plugin metadata in `plugin.json`
4. Document all commands and agents
5. Provide usage examples
6. Test thoroughly before sharing

## Learn More

- [AnkaLoop Documentation](../../README.md)
- [Commands and Skills Guide](../../docs/skills-and-commands.md)
- [Agent Capabilities](../../docs/phase2-agent-capabilities.md)
- [Hooks Guide](../../docs/hooks.md)
