# AnkaLoop Server — Dockerfile (with in-place self-restart supervisor)
#
# Build (from the repository root):
#   docker build -f deploy/docker/Dockerfile -t ankaloop .
#
# Run (server on loopback, safe default, no auth required):
#   docker run -it ankaloop
#
# Run server exposed to the host network with authentication (via env vars):
#   docker run -p 8080:8080 \
#       -e ANKA_HOST=0.0.0.0 -e ANKA_API_KEY=your-secret \
#       ankaloop
#
# Run server exposed to the host network with authentication (via config file):
#   docker run -p 8080:8080 -v ./config.toml:/root/.config/ankaloop/config.toml \
#       ankaloop
#
# NOTE: A non-loopback --host (e.g. 0.0.0.0) requires authentication.
#       Use --api-key, ANKA_API_KEY env var, or [server.auth] in config.toml.
#       Custom ports via ANKA_PORT are picked up by the HEALTHCHECK automatically.
#
# The container runs a supervisor that keeps AnkaLoop as a child process, so
# the agent can restart itself in place (/docker:restart or
# /usr/local/bin/restart-ankaloop) WITHOUT the container exiting — container
# filesystem state survives, matching the deploy/vm and deploy/k8s designs.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep \
    git \
    curl \
    ca-certificates \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Copy only files needed to install AnkaLoop. Avoid sending local deployment
# files (for example e2b/env) into the runtime image and keep dependency
# installation cacheable across unrelated repository changes.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install Python dependencies
RUN uv sync --frozen --no-dev --no-editable

# Install the in-place self-restart supervisor + restart helper
COPY deploy/docker/supervisor.sh /usr/local/bin/ankaloop-docker-supervisor
COPY deploy/docker/restart-ankaloop.sh /usr/local/bin/restart-ankaloop
RUN chmod +x /usr/local/bin/ankaloop-docker-supervisor /usr/local/bin/restart-ankaloop

# Create runtime directories
RUN mkdir -p /root/.config/ankaloop /workspace

WORKDIR /workspace

# Health check — hits the server /api/v1/health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s/api/v1/health' % os.environ.get('ANKA_PORT', '8080'))" || exit 1

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/ankaloop-docker-supervisor"]
