# AnkaLoop Examples

This directory contains ready-to-use examples that demonstrate the various capabilities of AnkaLoop. These examples are designed to help you get started quickly and understand how to leverage different features of the system.

## 📁 Directory Structure

```
examples/
├── agents/          # Agent specification examples
├── commands/        # Slash command examples
├── skills/          # Skill definitions for specialized knowledge
├── hooks/           # Hook configurations for automation
├── workflows/       # Multi-agent workflow examples
├── plugins/         # Complete plugin packages (commands + agents + hooks + skills)
└── README.md        # This file
```

## 🤖 Agents (`agents/`)

Agent specifications define the behavior, capabilities, and system prompts for different types of AI agents. These examples show various agent configurations for different use cases.

### Available Agent Examples

| Agent | Description | Mode | Use Case |
|-------|-------------|------|----------|
| `web-developer.yaml` | Web development specialist | Primary | HTML, CSS, JavaScript, React, Vue, etc. |
| `data-scientist.yaml` | Data science and ML expert | Primary | Data analysis, machine learning, scientific computing |
| `devops-engineer.yaml` | DevOps and infrastructure specialist | Primary | CI/CD, containers, cloud, automation |
| `security-auditor.yaml` | Security analysis and vulnerability assessment | Subagent | Security audits, compliance, vulnerability scanning |
| `documentation-writer.yaml` | Technical documentation specialist | Subagent | API docs, user guides, documentation creation |

### How to Use Agents

1. **Copy an agent spec to your project:**
   ```bash
   cp examples/agents/web-developer.yaml ~/.config/ankaloop/agents/
   ```

2. **Use the agent with AnkaLoop:**
   ```bash
   anka --agent web-developer
   ```

3. **Or use it directly from the examples directory:**
   ```bash
   anka --agent examples/agents/web-developer.yaml
   ```

### Creating Custom Agents

To create your own agent:

1. Copy an existing agent specification as a template
2. Modify the `name`, `description`, and `system_prompt`
3. Adjust the `tools` list to include/exclude specific tools
4. Set `max_steps` for the agent's execution limit
5. Configure `can_delegate` for multi-agent workflows

## ⚡ Commands (`commands/`)

Slash commands provide quick shortcuts for common tasks. These examples demonstrate various command patterns and use cases.

### Available Command Examples

#### General Development Commands
- `/explain` - Explain code from a file or selection
- `/debug` - Debug code and analyze errors
- `/refactor` - Refactor code for better quality
- `/test` - Generate or improve tests
- `/optimize` - Optimize code for performance
- `/migrate` - Migrate code between frameworks/versions

#### Namespaced Commands
- `/docker:build` - Docker build optimization
- `/database:query` - Database query optimization
- `/security:audit` - Security audit and vulnerability assessment

### How to Use Commands

1. **Copy commands to your configuration:**
   ```bash
   # User-level commands
   cp examples/commands/*.toml ~/.config/ankaloop/commands/

   # Project-level commands
   mkdir -p .ankaloop/commands
   cp examples/commands/*.toml .ankaloop/commands/
   ```

2. **Use commands in AnkaLoop:**
   ```
   AnkaLoop> /debug "My function is not working"
   AnkaLoop> /docker:build "Optimize my Dockerfile"
   ```

### Creating Custom Commands

Create a `.toml` file with the following structure:

```toml
description = "Brief description of the command"

prompt = """
The prompt template that will be submitted to the agent.
Use {{args}} to include user arguments.

Provide clear instructions and expected output format.
"""
```

## 🎯 Skills (`skills/`)

Skills provide specialized knowledge and behavior patterns to agents. These examples show how to create skills for different domains and expertise areas.

### Available Skill Examples

| Skill | Description | Domain |
|-------|-------------|--------|
| `code-review` | Code review guidelines and best practices | General |
| `python-expert` | Python development expertise | Python |
| `react-developer` | React development patterns and best practices | Web Development |
| `ai-engineer` | AI/ML development and MLOps practices | Artificial Intelligence |
| `mobile-developer` | Mobile app development for iOS/Android | Mobile Development |

### How to Use Skills

1. **Copy skills to your configuration:**
   ```bash
   # User-level skills
   cp -r examples/skills/* ~/.config/ankaloop/skills/

   # Project-level skills
   mkdir -p .ankaloop/skills
   cp -r examples/skills/* .ankaloop/skills/
   ```

2. **Activate skills in your agent or during conversation:**
   ```
   AnkaLoop> Activate the react-developer skill
   AnkaLoop> I need help with React hooks
   ```

### Creating Custom Skills

Create a skill directory with a `SKILL.md` file:

```markdown
---
name: your-skill-name
description: Brief description of what this skill provides
---

# Your Skill Name

Detailed documentation about the skill, including:
- Domain expertise
- Best practices
- Code examples
- Common patterns
- Troubleshooting guides
```

## 🪝 Hooks (`hooks/`)

Hooks provide automation and validation capabilities that run at various points in the AnkaLoop workflow. These examples demonstrate different hook configurations for common automation needs.

### Available Hook Examples

| Hook Configuration | Description | Use Case |
|-------------------|-------------|----------|
| `hooks.toml` | Basic hook configuration example | General automation |
| `automated-testing.toml` | Testing and quality assurance hooks | CI/CD integration |
| `security-validation.toml` | Security scanning and compliance | Security workflows |

### Hook Types

- **PreToolUse**: Run before tool execution
- **PostToolUse**: Run after tool execution
- **UserPromptSubmit**: Run when user submits a prompt
- **SessionStart**: Run when a new session begins
- **SessionEnd**: Run when a session ends

### How to Use Hooks

1. **Copy hook configuration:**
   ```bash
   cp examples/hooks/automated-testing.toml ~/.config/ankaloop/hooks.toml
   # or for project-specific hooks
   cp examples/hooks/automated-testing.toml .ankaloop/hooks.toml
   ```

2. **Make hook scripts executable:**
   ```bash
   chmod +x examples/hooks/scripts/*.sh
   ```

3. **The hooks will automatically run** based on their configuration

### Creating Custom Hooks

Create a `hooks.toml` file with your desired hook configurations:

```toml
[hooks.PreToolUse]
[[hooks.PreToolUse.handlers]]
matcher = "write_file"
type = "command"
command = "./your-script.sh"
timeout = 30
enabled = true
```

## 🔄 Multi-Agent Workflows

AnkaLoop supports multi-agent workflows where agents can delegate tasks to specialized subagents. Here are some example workflows:

### Example 1: Web Development with Security Review
```bash
# Start with web-developer agent
anka --agent web-developer

# In the conversation, the agent can delegate to security-auditor:
AnkaLoop> "Create a login form and have it security reviewed"
```

### Example 2: Data Science with Documentation
```bash
# Use data-scientist agent
anka --agent data-scientist

# The agent can delegate to documentation-writer:
AnkaLoop> "Analyze this dataset and create comprehensive documentation"
```

### Example 3: DevOps with Security
```bash
# Use devops-engineer agent
anka --agent devops-engineer

# The agent can delegate to security-auditor:
AnkaLoop> "Set up a CI/CD pipeline and ensure it meets security standards"
```

## 🛠️ Setup and Configuration

### Quick Setup

1. **Initialize AnkaLoop configuration:**
   ```bash
   anka init
   ```

2. **Copy examples to your configuration:**
   ```bash
   # Copy all examples
   cp -r examples/agents/* ~/.config/ankaloop/agents/
   cp -r examples/commands/* ~/.config/ankaloop/commands/
   cp -r examples/skills/* ~/.config/ankaloop/skills/
   cp examples/hooks/*.toml ~/.config/ankaloop/
   ```

3. **Start using AnkaLoop with examples:**
   ```bash
   anka --agent web-developer
   ```

### Project-Specific Configuration

For project-specific configurations, create a `.ankaloop` directory in your project root:

```bash
mkdir -p .ankaloop/{agents,commands,skills}
cp -r examples/agents/* .ankaloop/agents/
cp -r examples/commands/* .ankaloop/commands/
cp -r examples/skills/* .ankaloop/skills/
cp examples/hooks.toml .ankaloop/
```

## 📚 Best Practices

### Agent Configuration
- Keep system prompts focused and specific
- Use appropriate tool sets for each agent type
- Set reasonable `max_steps` limits
- Enable delegation only for primary agents

### Command Design
- Use descriptive names and clear prompts
- Include expected output formats
- Handle edge cases in prompts
- Use namespacing for related commands

### Skill Development
- Focus on specific domains or expertise areas
- Include practical code examples
- Provide troubleshooting guidance
- Keep documentation up-to-date

### Hook Implementation
- Use appropriate timeouts for hook scripts
- Handle errors gracefully
- Log important events for debugging
- Test hooks thoroughly before deployment

## 🤝 Contributing

To contribute new examples:

1. **Fork the repository**
2. **Create a new branch** for your examples
3. **Add your examples** to the appropriate directories
4. **Update this README** with documentation for your examples
5. **Submit a pull request**

### Example Guidelines

- **Agents**: Should be complete and ready-to-use
- **Commands**: Should handle common use cases
- **Skills**: Should provide comprehensive domain knowledge
- **Hooks**: Should be robust and well-tested

## 📖 Additional Resources

- [AnkaLoop Main Documentation](../README.md)
- [Agent Capabilities](../docs/design/phase2-agent-capabilities.md)
- [Commands and Skills Guide](../docs/skills-and-commands.md)
- [Hooks System Guide](../docs/hooks.md)
- [Project Structure](../docs/PROJECT_STRUCTURE.md)

## 🐛 Troubleshooting

### Common Issues

1. **Agent not found**: Ensure the agent file is in the correct directory
2. **Command not working**: Check the TOML syntax and file placement
3. **Skill not loading**: Verify the SKILL.md format and directory structure
4. **Hooks not running**: Check permissions and script paths

### Getting Help

- Check the [AnkaLoop documentation](../README.md)
- Review the setup and notes in the [main documentation](../README.md)
- Open an issue on the GitHub repository

---

Happy coding with AnkaLoop! 🚀
