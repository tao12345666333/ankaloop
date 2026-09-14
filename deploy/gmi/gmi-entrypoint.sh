#!/usr/bin/env sh
set -eu

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
WORK_DIR="${ANKA_WORK_DIR:-/workspace}"
GMI_BASE="${GMI_MAAS_BASE_URL:-https://api.gmi-serving.com}"

case "$GMI_BASE" in
    */v1) OPENAI_BASE="${GMI_BASE%/}" ;;
    *) OPENAI_BASE="${GMI_BASE%/}/v1" ;;
esac

export ANKA_OPENAI_BASE="${ANKA_OPENAI_BASE:-$OPENAI_BASE}"

if [ -n "${GMI_MAAS_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
    export OPENAI_API_KEY="$GMI_MAAS_API_KEY"
fi

if [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "GMI_MAAS_API_KEY or OPENAI_API_KEY is required" >&2
    exit 1
fi

RAW_MODEL="${ANKA_CHAT_MODEL:-${GMI_MODELS:-}}"
if [ -z "$RAW_MODEL" ]; then
    echo "GMI_MODELS or ANKA_CHAT_MODEL is required" >&2
    exit 1
fi
case "$RAW_MODEL" in
    *,*) MODEL="${RAW_MODEL%%,*}" ;;
    *) MODEL="$RAW_MODEL" ;;
esac
export ANKA_CHAT_MODEL="$MODEL"

CONFIG_ROOT="${XDG_CONFIG_HOME:-/root/.config}"
CONFIG_DIR="$CONFIG_ROOT/ankaloop"
CONFIG_FILE="$CONFIG_DIR/config.toml"
mkdir -p "$CONFIG_DIR" "$WORK_DIR"

if [ ! -f "$CONFIG_FILE" ] || [ "${ANKA_GMI_REWRITE_CONFIG:-1}" = "1" ]; then
    cat > "$CONFIG_FILE" <<EOF
[server]
host = "$HOST"
port = $PORT
session_timeout_minutes = 1440
max_sessions = 100
work_dir = "$WORK_DIR"
default_agent = "default"

[server.auth]
enabled = false

[server.cors]
enabled = true
allow_origins = ["*"]
allow_methods = ["*"]
allow_headers = ["*"]
allow_credentials = false

[chat]
api_type = "openai"
base_url = "$ANKA_OPENAI_BASE"
model = "$MODEL"
mcp_tools_enabled = true
write_tool_enabled = true
edit_tool_enabled = true
tool_loop_limit = 300
bash_tool_limit = 100
default_max_lines = 400

[context]
progressive_tools = true
progressive_skills = true
EOF
fi

set -- serve --host "$HOST" --port "$PORT" --work-dir "$WORK_DIR"

if [ -n "${ANKA_TELEGRAM_BOT_TOKEN:-}" ]; then
    if [ -z "${ANKA_TELEGRAM_ALLOWED_USERS:-}" ]; then
        echo "ANKA_TELEGRAM_ALLOWED_USERS is required when Telegram is enabled" >&2
        exit 1
    fi
    anka "$@" &
    server_pid=$!
    anka telegram start --work-dir "$WORK_DIR" &
    telegram_pid=$!

    stop_children() {
        kill "$server_pid" "$telegram_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
        wait "$telegram_pid" 2>/dev/null || true
    }
    trap 'stop_children; exit 143' INT TERM

    while kill -0 "$server_pid" 2>/dev/null && kill -0 "$telegram_pid" 2>/dev/null; do
        sleep 2
    done

    if ! kill -0 "$server_pid" 2>/dev/null; then
        if wait "$server_pid"; then status=0; else status=$?; fi
    else
        if wait "$telegram_pid"; then status=0; else status=$?; fi
    fi
    if [ "$status" -eq 0 ]; then
        status=1
    fi
    stop_children
    exit "$status"
fi

exec anka "$@"
