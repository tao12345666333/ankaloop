# AnkaLoop image prepared for GMI Cloud AgentBox.
#
# Build from the repository root:
#   docker build -f deploy/gmi/Dockerfile -t ankaloop-gmi:latest .

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}" \
    HOME=/workspace \
    XDG_CONFIG_HOME=/workspace/.config \
    PORT=8080 \
    ANKA_WORK_DIR=/workspace

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    ripgrep \
    tini \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable --extra telegram

COPY deploy/gmi/gmi-entrypoint.sh /usr/local/bin/gmi-entrypoint
RUN chmod +x /usr/local/bin/gmi-entrypoint \
    && mkdir -p /workspace/.config/ankaloop

WORKDIR /workspace

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/v1/health')" || exit 1

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/gmi-entrypoint"]
