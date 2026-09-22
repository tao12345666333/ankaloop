# Web Development Workflow Example

This workflow demonstrates how multiple agents can collaborate on a web development project, with each agent handling their specialized tasks.

## Workflow Overview

```
User Request → Web Developer Agent → Security Auditor Agent → Documentation Writer Agent → Final Result
```

## Step-by-Step Workflow

### 1. Initial Request (User → Web Developer)

**User Input:**
```
"I need to create a secure user authentication system for my React web application. It should include login, registration, password reset, and proper session management. Please also ensure it follows security best practices and include comprehensive documentation."
```

**Web Developer Agent Tasks:**
- Design the authentication system architecture
- Create React components for login, registration, and password reset
- Implement secure API endpoints
- Set up session management with JWT tokens
- Configure proper routing and protected routes
- Implement form validation and error handling

### 2. Security Review (Web Developer → Security Auditor)

**Delegation Prompt:**
```
"Please review the authentication system I've created and perform a comprehensive security audit. Check for OWASP Top 10 vulnerabilities, authentication bypasses, and any security issues. Provide specific recommendations for improvements."
```

**Security Auditor Agent Tasks:**
- Analyze authentication flow for security vulnerabilities
- Check for SQL injection, XSS, and CSRF protection
- Review password policies and hashing implementation
- Validate session management and token security
- Assess API endpoint security
- Provide detailed security recommendations

### 3. Documentation Creation (Web Developer → Documentation Writer)

**Delegation Prompt:**
```
"Please create comprehensive documentation for the authentication system I've developed. Include API documentation, user guides, setup instructions, and security considerations."
```

**Documentation Writer Agent Tasks:**
- Create API documentation with OpenAPI/Swagger specs
- Write user guides for authentication features
- Document setup and configuration procedures
- Create security best practices guide
- Generate code examples and tutorials
- Create troubleshooting documentation

### 4. Integration and Finalization (Web Developer)

**Final Tasks:**
- Implement security recommendations from the audit
- Integrate documentation into the project
- Perform final testing and validation
- Create deployment configuration
- Generate final project summary

## Example Implementation

### Web Developer Agent Configuration

```yaml
name: web-developer
description: "Specialized agent for web development with security awareness"
mode: "primary"
system_prompt: |
  You are a Web Developer agent specializing in secure web applications.
  When working on authentication systems:
  1. Always prioritize security best practices
  2. Use modern authentication standards (OAuth 2.0, JWT)
  3. Implement proper input validation and sanitization
  4. Use HTTPS for all API communications
  5. Implement proper session management
  6. Delegate security reviews to the security-auditor agent
  7. Delegate documentation tasks to the documentation-writer agent
tools: ["read_file", "write_file", "apply_patch", "bash", "grep", "think"]
can_delegate: true
max_steps: 50
```

### Security Auditor Agent Configuration

```yaml
name: security-auditor
description: "Security specialist for vulnerability assessment"
mode: "subagent"
system_prompt: |
  You are a Security Auditor agent. Perform comprehensive security analysis:
  1. Check OWASP Top 10 vulnerabilities
  2. Validate authentication and authorization
  3. Review input validation and output encoding
  4. Assess session management security
  5. Provide actionable remediation steps
tools: ["read_file", "grep", "think"]
exclude_tools: ["write_file", "apply_patch", "bash"]
can_delegate: false
max_steps: 30
```

### Documentation Writer Agent Configuration

```yaml
name: documentation-writer
description: "Technical documentation specialist"
mode: "subagent"
system_prompt: |
  You are a Documentation Writer agent. Create comprehensive documentation:
  1. API documentation with clear examples
  2. User guides and tutorials
  3. Setup and configuration instructions
  4. Security best practices documentation
  5. Troubleshooting guides
tools: ["read_file", "write_file", "grep", "think"]
can_delegate: false
max_steps: 25
```

## Sample Conversation Flow

```
User: I need to create a secure user authentication system for my React web application...

Web Developer: I'll help you create a secure authentication system. Let me start by designing the architecture and implementing the core components.

[Web Developer creates authentication components and API endpoints]

Web Developer: I've created the basic authentication system. Now I'll delegate a security review to ensure everything meets security standards.

Web Developer uses the `task` tool with `agent_type="explorer"` and `description="Please review the authentication system I've created and perform a comprehensive security audit..."`

Security Auditor: I'll perform a thorough security audit of your authentication system...

[Security Auditor analyzes the code and provides recommendations]

Web Developer: Thank you for the security review. I'll implement the recommended improvements and then create comprehensive documentation.

Web Developer uses the `task` tool with `agent_type="focused_coder"` and `description="Please create comprehensive documentation for the authentication system..."`

Documentation Writer: I'll create comprehensive documentation for your authentication system...

[Documentation Writer creates API docs, user guides, etc.]

Web Developer: Perfect! I've now integrated the security improvements and documentation. Your secure authentication system is ready for deployment.
```

## Benefits of This Workflow

1. **Specialization**: Each agent focuses on their area of expertise
2. **Security First**: Built-in security review process
3. **Comprehensive Documentation**: Automatic documentation generation
4. **Quality Assurance**: Multiple validation points
5. **Efficiency**: Parallel processing of specialized tasks

## Customization Options

- **Add Testing Agent**: Include automated testing in the workflow
- **Add DevOps Agent**: Include deployment and infrastructure setup
- **Add UI/UX Agent**: Include user experience optimization
- **Add Performance Agent**: Include performance optimization

## Usage Instructions

1. **Start the Web Developer agent:**
   ```bash
   anka --agent examples/agents/web-developer.yaml
   ```

2. **Make sure other agents are available:**
   ```bash
   cp examples/agents/security-auditor.yaml ~/.config/ankaloop/agents/
   cp examples/agents/documentation-writer.yaml ~/.config/ankaloop/agents/
   ```

3. **Submit your request and let the agents collaborate!**

   Delegation happens through the built-in `task` tool when the active agent has `can_delegate: true`.

This workflow demonstrates the power of AnkaLoop's multi-agent system for complex development tasks.
