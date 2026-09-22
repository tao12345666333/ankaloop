# DevOps Security Workflow Example

This workflow demonstrates how DevOps and Security agents can collaborate to create secure, production-ready infrastructure and deployment pipelines.

> **Illustrative example:** The generated configurations, compliance language, and
> production-readiness statements below are hypothetical workflow output. They are not audited,
> certified, or evidence that AnkaLoop itself meets a compliance or production benchmark.

## Workflow Overview

```
User Request → DevOps Engineer Agent → Security Auditor Agent → Documentation Writer Agent → Final Result
```

## Step-by-Step Workflow

### 1. Initial Request (User → DevOps Engineer)

**User Input:**
```
"I need to set up a complete CI/CD pipeline for my web application with Docker containerization, Kubernetes deployment, automated testing, and comprehensive security measures. The pipeline should include security scanning, vulnerability assessment, and compliance monitoring."
```

**DevOps Engineer Agent Tasks:**
- Design the overall infrastructure architecture
- Create Dockerfile and container configuration
- Set up Kubernetes deployment manifests
- Configure CI/CD pipeline (GitHub Actions/GitLab CI)
- Implement automated testing integration
- Set up monitoring and logging infrastructure
- Create backup and disaster recovery procedures

### 2. Security Review (DevOps Engineer → Security Auditor)

**Delegation Prompt:**
```
"Please perform a comprehensive security audit of the CI/CD pipeline and infrastructure I've designed. Check for security vulnerabilities, compliance issues, and provide recommendations for hardening the deployment."
```

**Security Auditor Agent Tasks:**
- Analyze container security and image vulnerabilities
- Review Kubernetes security configurations
- Assess CI/CD pipeline security
- Check network security and firewall rules
- Validate secrets management and encryption
- Review access control and permissions
- Provide security hardening recommendations

### 3. Security Implementation (DevOps Engineer)

**Implementation Tasks:**
- Apply security recommendations from the audit
- Implement security scanning in the pipeline
- Configure network policies and firewalls
- Set up secrets management (HashiCorp Vault/AWS Secrets Manager)
- Implement security monitoring and alerting
- Configure compliance checks and reporting

### 4. Documentation Creation (DevOps Engineer → Documentation Writer)

**Delegation Prompt:**
```
"Please create comprehensive documentation for the secure CI/CD pipeline and infrastructure, including setup guides, security procedures, and operational runbooks."
```

**Documentation Writer Agent Tasks:**
- Create infrastructure documentation
- Write deployment and setup guides
- Document security procedures and policies
- Create operational runbooks
- Write troubleshooting guides
- Create compliance documentation

## Example Implementation

### DevOps Engineer Agent Configuration

```yaml
name: devops-engineer
description: "DevOps specialist with security focus"
mode: "primary"
system_prompt: |
  You are a DevOps Engineer agent specializing in secure infrastructure:
  1. Design scalable and secure infrastructure
  2. Implement proper CI/CD pipelines with security integration
  3. Use Infrastructure as Code principles
  4. Implement proper monitoring and observability
  5. Prioritize security in all configurations
  6. Delegate security reviews to security-auditor agent
  7. Delegate documentation to documentation-writer agent
tools: ["read_file", "write_file", "apply_patch", "bash", "grep", "think"]
can_delegate: true
max_steps: 40
```

### Sample Infrastructure Code

#### Dockerfile with Security Best Practices

```dockerfile
# Multi-stage build for security and size optimization
FROM python:3.11-slim as builder

# Set security-focused environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Install dependencies with security checks
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install safety bandit

# Production stage
FROM python:3.11-slim as production

# Copy only necessary files
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Set secure working directory
WORKDIR /app
COPY . .

# Change ownership to non-root user
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
```

#### Kubernetes Deployment with Security

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: secure-app
  labels:
    app: secure-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: secure-app
  template:
    metadata:
      labels:
        app: secure-app
    spec:
      # Security context
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 2000
      containers:
      - name: app
        image: myapp:latest
        imagePullPolicy: Always
        # Container security context
        securityContext:
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities:
            drop:
            - ALL
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        # Probes
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5
        # Environment variables from secrets
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: app-secrets
              key: database-url
        - name: API_KEY
          valueFrom:
            secretKeyRef:
              name: app-secrets
              key: api-key
---
# Network policy for security
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: secure-app-netpol
spec:
  podSelector:
    matchLabels:
      app: secure-app
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          name: ingress-nginx
    ports:
    - protocol: TCP
      port: 8000
  egress:
  - to: []
    ports:
    - protocol: TCP
      port: 443  # HTTPS only
    - protocol: TCP
      port: 53   # DNS
```

#### CI/CD Pipeline with Security (GitHub Actions)

```yaml
name: Secure CI/CD Pipeline

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  security-scan:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4

    - name: Run Trivy vulnerability scanner
      uses: aquasecurity/trivy-action@master
      with:
        scan-type: 'fs'
        scan-ref: '.'
        format: 'sarif'
        output: 'trivy-results.sarif'

    - name: Upload Trivy scan results
      uses: github/codeql-action/upload-sarif@v2
      with:
        sarif_file: 'trivy-results.sarif'

  build-and-test:
    runs-on: ubuntu-latest
    needs: security-scan
    steps:
    - uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'

    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install -r requirements.txt
        pip install pytest pytest-cov bandit safety

    - name: Run security tests
      run: |
        bandit -r . -f json -o bandit-report.json
        safety check --json --output safety-report.json

    - name: Run unit tests
      run: |
        pytest --cov=. --cov-report=xml tests/

    - name: Build Docker image
      run: |
        docker build -t myapp:${{ github.sha }} .
        docker tag myapp:${{ github.sha }} myapp:latest

    - name: Run container security scan
      run: |
        docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
          aquasec/trivy image --format json --output trivy-container.json myapp:latest

  deploy:
    runs-on: ubuntu-latest
    needs: build-and-test
    if: github.ref == 'refs/heads/main'
    steps:
    - uses: actions/checkout@v4

    - name: Configure kubectl
      run: |
        echo "${{ secrets.KUBE_CONFIG }}" | base64 -d > kubeconfig
        export KUBECONFIG=kubeconfig

    - name: Deploy to Kubernetes
      run: |
        kubectl set image deployment/secure-app app=myapp:${{ github.sha }}
        kubectl rollout status deployment/secure-app

    - name: Run post-deployment security check
      run: |
        kubectl get pods -l app=secure-app
        kubectl exec -it deployment/secure-app -- security-check
```

## Sample Conversation Flow

```
User: I need to set up a complete CI/CD pipeline for my web application with Docker containerization...

DevOps Engineer: I'll help you create a secure, production-ready CI/CD pipeline with all the security measures you need. Let me start by designing the infrastructure and creating the necessary configurations.

[DevOps Engineer creates Dockerfile, Kubernetes manifests, CI/CD pipeline]

DevOps Engineer: I've created the basic infrastructure with security considerations. Now I'll delegate a comprehensive security audit to ensure everything meets security standards.

DevOps Engineer uses the `task` tool with `agent_type="explorer"` and `description="Please perform a comprehensive security audit of the CI/CD pipeline and infrastructure I've designed..."`

Security Auditor: I'll perform a thorough security audit of your infrastructure and pipeline.

[Security Auditor analyzes configurations, identifies vulnerabilities, provides recommendations]

DevOps Engineer: Thank you for the security recommendations. I'll implement all the suggested security measures and then create comprehensive documentation.

[DevOps Engineer implements security hardening]

DevOps Engineer uses the `task` tool with `agent_type="focused_coder"` and `description="Please create comprehensive documentation for the secure CI/CD pipeline and infrastructure..."`

Documentation Writer: I'll create thorough documentation for your secure infrastructure.

[Documentation Writer creates setup guides, security procedures, runbooks]

DevOps Engineer: Perfect! Your secure CI/CD pipeline is now complete with all security measures implemented and comprehensive documentation.
```

## Security Features Implemented

### Container Security
- Non-root user execution
- Read-only filesystem where possible
- Minimal base images
- Security scanning in CI/CD
- Signed images

### Kubernetes Security
- Network policies
- Pod security policies
- RBAC configuration
- Secrets management
- Resource limits

### CI/CD Security
- Secret scanning
- Dependency vulnerability scanning
- Container image scanning
- Security testing integration
- Compliance checks

### Monitoring and Logging
- Security event logging
- Anomaly detection
- Compliance reporting
- Audit trails
- Incident response procedures

## Workflow Benefits

1. **Security First**: Built-in security at every layer
2. **Compliance-oriented example**: Illustrates checks that still require expert validation
3. **Deployment-oriented example**: Illustrates scalable infrastructure patterns
4. **Comprehensive Documentation**: Complete operational guides
5. **Automated Security**: Continuous security validation

## Usage Instructions

1. **Start the DevOps Engineer agent:**
   ```bash
   anka --agent examples/agents/devops-engineer.yaml
   ```

2. **Ensure supporting agents are available:**
   ```bash
   cp examples/agents/security-auditor.yaml ~/.config/ankaloop/agents/
   cp examples/agents/documentation-writer.yaml ~/.config/ankaloop/agents/
   ```

3. **Provide your application requirements and security needs**

   Delegation happens through the built-in `task` tool when the active agent has `can_delegate: true`.

This workflow demonstrates how AnkaLoop can handle complex DevOps projects with comprehensive security integration through specialized agent collaboration.
