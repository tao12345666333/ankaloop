# Feature Development Plugin

Comprehensive feature development workflow with a structured 7-phase approach.

## Overview

The Feature Development Plugin provides a systematic approach to building new features. Instead of jumping straight into code, it guides you through understanding the codebase, asking clarifying questions, designing architecture, and ensuring quality—resulting in better-designed features that integrate seamlessly with your existing code.

## Philosophy

Building features requires more than just writing code. You need to:
- **Understand the codebase** before making changes
- **Ask questions** to clarify ambiguous requirements
- **Design thoughtfully** before implementing
- **Review for quality** after building

This plugin embeds these practices into a structured workflow.

## Command: `/feature-dev`

Launches a guided feature development workflow with 7 distinct phases.

**Usage:**
```bash
/feature-dev Add user authentication with OAuth
```

Or simply:
```bash
/feature-dev
```

The command will guide you through the entire process interactively.

## The 7-Phase Workflow

### Phase 1: Discovery
**Goal**: Understand what needs to be built

- Clarifies the feature request
- Identifies constraints and requirements
- Summarizes understanding and confirms with you

### Phase 2: Codebase Exploration
**Goal**: Understand relevant existing code and patterns

- Explores similar features in the codebase
- Maps architecture and abstractions
- Identifies key files and patterns to follow

### Phase 3: Clarifying Questions
**Goal**: Fill in gaps and resolve ambiguities

- Reviews findings and feature request
- Asks about edge cases, error handling, integration points
- **Waits for your answers before proceeding**

### Phase 4: Architecture Design
**Goal**: Design multiple implementation approaches

- Designs 2-3 different approaches with trade-offs:
  - **Minimal changes**: Smallest change, maximum reuse
  - **Clean architecture**: Maintainability, elegant abstractions
  - **Pragmatic balance**: Speed + quality
- Presents comparison with recommendation
- **Asks which approach you prefer**

### Phase 5: Implementation
**Goal**: Build the feature

- **Waits for explicit approval**
- Implements following chosen architecture
- Follows codebase conventions strictly

### Phase 6: Quality Review
**Goal**: Review the implementation

- Reviews code for bugs and quality issues
- Checks project guideline compliance
- Suggests improvements

### Phase 7: Summary
**Goal**: Document and wrap up

- Generates implementation summary
- Lists files changed
- Provides testing suggestions

## Agents

### `code-explorer`
**Purpose**: Deeply analyzes existing codebase features

**Focus areas:**
- Entry points and call chains
- Data flow and transformations
- Architecture layers and patterns
- Dependencies and integrations

### `code-architect`
**Purpose**: Designs feature architectures

**Focus areas:**
- Codebase pattern analysis
- Architecture decisions
- Component design
- Implementation roadmap

### `code-reviewer`
**Purpose**: Reviews code for quality

**Focus areas:**
- Project guideline compliance
- Bug detection
- Code quality issues
- Best practice suggestions

## Usage Examples

### Full workflow (recommended):
```bash
/feature-dev Add user authentication with OAuth support for Google and GitHub
```

### With an existing branch:
```bash
/feature-dev Continue implementing the payment integration from the design phase
```

## Best Practices

1. **Be specific** in your feature request
2. **Answer questions thoroughly** in Phase 3
3. **Choose architecture wisely** - consider long-term maintainability
4. **Review changes** before committing

## Requirements

- AnkaLoop v0.6.0 or later
- Access to the codebase you're developing

## Author

AnkaLoop Team
