# AnkaLoop Prompt Templates System

AnkaLoop 使用模板化的 system prompt 系统，支持：

- **结构化的 Prompt 区块**：`<critical_rules>`, `<workflow>`, `<editing_files>` 等
- **模型特定优化**：为 Claude 提供专门的 prompt（扩展思考指导）
- **动态环境注入**：工作目录、Git 状态、日期时间等
- **Skills 和 Memory 文件集成**
- **用户语言匹配**：自动使用与用户相同的语言回复
- **并行工具调用**：强调并行执行以提高效率
- **KISS 原则**：保持简单，避免过度复杂化

## 设计参考

本系统参考了多个优秀项目的 Prompt 设计：
- **Crush** (Charmbracelet): 结构化 XML 标签、工作流程、编辑规则
- **OpenCode** (SST): 模型特定优化、示例驱动的指导
- **Kimi CLI** (Moonshot): 压缩优先级、语言匹配、KISS 原则

## 目录结构

```
src/ankaloop/prompts/
├── __init__.py          # 模块导出
├── manager.py           # PromptManager 和 PromptContext 类
└── templates/           # Prompt 模板文件
    ├── coder.md             # 默认 coder 模板（最完整）
    ├── coder_anthropic.md   # Claude 优化模板（扩展思考）
    ├── explorer.md          # Explorer 子代理模板
    ├── planner.md           # Planner 子代理模板
    ├── initialize.md        # AGENTS.md 生成模板
    └── compact.md           # 上下文压缩模板
```

## 使用方法

### 基本用法

```python
from ankaloop.prompts import PromptContext, get_prompt_manager

# 创建上下文
context = PromptContext.from_environment(
    working_dir="/path/to/project",
    model_name="claude-sonnet-4-20250514",
    available_tools=["read_file", "grep", "bash"],
    skills_xml="<skills>...</skills>",
    memory_files=[{"path": "AGENTS.md", "content": "..."}],
)

# 获取渲染后的 prompt
pm = get_prompt_manager()
prompt = pm.get_system_prompt(context, template_name="coder")
```

### 模型自动检测

系统会根据模型名称自动检测模型家族并使用对应的模板：

| 模型名称包含 | 检测为 | 使用模板 |
|-------------|--------|----------|
| claude, anthropic | ANTHROPIC | coder_anthropic.md |
| gpt, o1, o3 | OPENAI | coder.md (默认) |
| gemini, palm | GEMINI | coder_gemini.md |
| qwen | QWEN | coder.md |
| deepseek | DEEPSEEK | coder.md |

### 获取子代理 Prompt

```python
from ankaloop.agent_spec import get_subagent_spec

# 获取 explorer 子代理配置
explorer_spec = get_subagent_spec(
    template_name="explorer",
    working_dir="/path/to/project",
    available_tools=["read_file", "grep"],
)
```

### 列出可用模板

```python
from ankaloop.agent_spec import list_available_templates

templates = list_available_templates()
# ['coder', 'coder_anthropic', 'coder_gemini', 'explorer', 'initialize', 'planner']
```

## 模板变量

模板支持以下变量：

| 变量 | 描述 |
|------|------|
| `${working_dir}` | 当前工作目录 |
| `${platform}` | 操作系统 (linux/darwin/windows) |
| `${date}` | 当前日期 |
| `${time}` | 当前时间 |
| `${is_git_repo}` | 是否是 git 仓库 |
| `${git_branch}` | 当前 git 分支 |
| `${git_status}` | Git 状态摘要 |
| `${git_recent_commits}` | 最近的 commit |
| `${tools_list}` | 可用工具列表 |
| `${skills_section}` | Skills XML 内容 |
| `${memory_section}` | Memory 文件内容 |

## 条件区块

模板支持条件渲染：

```markdown
{{if is_git_repo}}
Git branch: ${git_branch}
{{end}}

{{if skills}}
${skills_section}
{{end}}

{{if memory}}
${memory_section}
{{end}}
```

## 自定义模板

1. 在 `src/ankaloop/prompts/templates/` 目录下创建新模板文件
2. 使用 `.md` 或 `.txt` 扩展名
3. 模板会自动被发现和加载

示例：创建 `templates/reviewer.md` 后可以这样使用：

```python
prompt = pm.get_system_prompt(context, template_name="reviewer")
```

## Prompt 结构最佳实践

参考 Crush 和 OpenCode 的 prompt 设计，建议包含以下区块：

1. **`<critical_rules>`** - 最重要的规则，优先级最高
2. **`<communication_style>`** - 输出风格指南
3. **`<workflow>`** - 工作流程指南
4. **`<decision_making>`** - 自主决策指南
5. **`<editing_files>`** - 文件编辑指南
6. **`<error_handling>`** - 错误处理指南
7. **`<tool_usage>`** - 工具使用指南
8. **`<env>`** - 环境信息（动态注入）
9. **`<available_tools>`** - 可用工具列表
