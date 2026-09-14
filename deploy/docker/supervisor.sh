#!/usr/bin/env sh
# ankaloop-docker-supervisor: run AnkaLoop as a supervised child so it can be
# restarted in place WITHOUT the container exiting — the same supervisor
# design as deploy/vm and deploy/k8s.
#
# Restart trigger: /usr/local/bin/restart-ankaloop (also invoked by the agent
# through the /docker:restart slash command) touches $MARKER and SIGTERMs the
# child. When the child exits and $MARKER exists, the loop starts it again.
# When the child exits WITHOUT the marker (crash), the supervisor exits with
# the same status and Docker's restart policy (e.g. `unless-stopped`) takes
# over.
set -eu

WORK_DIR="${ANKA_WORK_DIR:-/workspace}"
MARKER="${ANKALOOP_DOCKER_RESTART_MARKER:-/tmp/ankaloop-restart-requested}"
PID_FILE="${ANKALOOP_DOCKER_PID_FILE:-/tmp/ankaloop-server.pid}"
HOST="${ANKA_HOST:-127.0.0.1}"
PORT="${ANKA_PORT:-8080}"

install_restart_command() {
    if [ "${ANKALOOP_DOCKER_INSTALL_RESTART_COMMAND:-1}" = "0" ]; then
        return 0
    fi
    commands_dir="${WORK_DIR}/.ankaloop/commands/docker"
    command_file="${commands_dir}/restart.toml"
    mkdir -p "${commands_dir}"
    if [ -f "${command_file}" ]; then
        echo "[ankaloop-docker] keeping existing restart command at ${command_file}"
        return 0
    fi
    cat > "${command_file}" <<'EOF_COMMAND'
description = "Restart AnkaLoop inside the current Docker container"

prompt = """
Restart AnkaLoop in this Docker container so recent changes take effect.

Use the bash tool to run:

ANKALOOP_DOCKER_RESTART_REASON="requested from /docker:restart" /usr/local/bin/restart-ankaloop

After the command is scheduled, tell the user that AnkaLoop is restarting in the
same container. Do not stop or recreate the container; it keeps running.

User note:
{{args}}
"""
EOF_COMMAND
    echo "[ankaloop-docker] installed restart command at ${command_file}"
}

child_pid=""
terminate_child() {
    if [ -n "${child_pid}" ] && kill -0 "${child_pid}" 2>/dev/null; then
        kill -TERM "${child_pid}" 2>/dev/null || true
        wait "${child_pid}" 2>/dev/null || true
    fi
    rm -f "${PID_FILE}"
    exit 143
}

trap terminate_child TERM INT
rm -f "${MARKER}" "${PID_FILE}"
install_restart_command

while true; do
    set -- serve --host "${HOST}" --port "${PORT}" --work-dir "${WORK_DIR}"
    if [ -n "${ANKA_TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${ANKA_TELEGRAM_ALLOWED_USERS:-}" ]; then
        set -- "$@" --telegram
    else
        echo "[ankaloop-docker] Telegram disabled (missing ANKA_TELEGRAM_BOT_TOKEN or ANKA_TELEGRAM_ALLOWED_USERS)"
    fi

    echo "[ankaloop-docker] starting AnkaLoop on ${HOST}:${PORT}"
    anka "$@" &
    child_pid=$!
    printf '%s\n' "${child_pid}" > "${PID_FILE}"

    set +e
    wait "${child_pid}"
    status=$?
    set -e

    rm -f "${PID_FILE}"
    child_pid=""

    if [ -f "${MARKER}" ]; then
        rm -f "${MARKER}"
        echo "[ankaloop-docker] restart requested; starting AnkaLoop again (container stays up)"
        continue
    fi

    echo "[ankaloop-docker] AnkaLoop exited with status ${status} and no restart was requested"
    echo "[ankaloop-docker] letting the container exit so Docker restarts it"
    exit "${status}"
done
