#!/usr/bin/env bash
# ankaloop-vm-supervisor: run AnkaLoop as a supervised child so it can be
# restarted in place (same machine, same service, no systemd restart) — the
# same supervisor design as the deploy/k8s and deploy/docker setups.
#
# Restart triggers:
#   1. /usr/local/bin/ankaloop-vm-restart touches $MARKER and SIGTERMs the child.
#   2. SIGUSR1 to this process (e.g. systemctl kill --signal=SIGUSR1 ankaloop.service).
# When the child exits and $MARKER exists, the loop starts it again.
# When the child exits WITHOUT the marker (crash), the supervisor exits with
# the same status and systemd (Restart=always) takes over.
set -u

REPO_DIR="${ANKALOOP_REPO_DIR:-/opt/ankaloop/repo}"
VENV_DIR="${ANKALOOP_VENV_DIR:-/opt/ankaloop/venv}"
WORK_DIR="${ANKALOOP_WORK_DIR:-/opt/ankaloop/data}"
RUNTIME_DIR="${ANKALOOP_RUNTIME_DIR:-/opt/ankaloop/run}"
HOST="${ANKALOOP_VM_HOST:-127.0.0.1}"
PORT="${ANKALOOP_VM_PORT:-8080}"
MIN_UPTIME="${ANKALOOP_VM_MIN_UPTIME:-0}"

MARKER="${RUNTIME_DIR}/restart-requested"
PID_FILE="${RUNTIME_DIR}/ankaloop.pid"
ANKA_BIN="${VENV_DIR}/bin/anka"

mkdir -p "${RUNTIME_DIR}" "${WORK_DIR}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-${WORK_DIR}/.config}"

log() { echo "[ankaloop-vm] $*"; }

install_restart_command() {
    local commands_dir="${WORK_DIR}/.ankaloop/commands/vm"
    local command_file="${commands_dir}/restart.toml"
    mkdir -p "${commands_dir}"
    cat > "${command_file}" <<'EOF_COMMAND'
description = "Restart AnkaLoop in place on this VM"

prompt = """
Restart AnkaLoop on this VM so recent code changes take effect.

Use the bash tool to run:

ANKALOOP_VM_RESTART_REASON="requested from /vm:restart" /usr/local/bin/ankaloop-vm-restart

After the command is scheduled, tell the user that AnkaLoop is restarting in place
on the same VM. The systemd service does not restart.

User note:
{{args}}
"""
EOF_COMMAND
}

sync_repo() {
    # Install/update the package from the repo checkout into the venv before
    # every (re)start, so `git pull` + in-place restart picks up new code.
    log "syncing ankaloop package from ${REPO_DIR}"
    (cd "${REPO_DIR}" && uv pip install --python "${VENV_DIR}/bin/python" --no-editable '.[telegram]')
}

child_pid=""
restart_pending=0

on_usr1() {
    restart_pending=1
    : > "${MARKER}"
    if [ -n "${child_pid}" ] && kill -0 "${child_pid}" 2>/dev/null; then
        kill -TERM "${child_pid}" 2>/dev/null
    fi
}

on_term() {
    rm -f "${MARKER}" "${PID_FILE}"
    if [ -n "${child_pid}" ] && kill -0 "${child_pid}" 2>/dev/null; then
        kill -TERM "${child_pid}" 2>/dev/null
        wait "${child_pid}" 2>/dev/null
    fi
    exit 143
}

trap on_usr1 USR1
trap on_term TERM INT

rm -f "${MARKER}" "${PID_FILE}"
install_restart_command

while true; do
    if ! sync_repo; then
        log "uv sync failed; retrying in 10s (service stays up)"
        sleep 10
        continue
    fi

    cmd=("${ANKA_BIN}" serve --host "${HOST}" --port "${PORT}" --work-dir "${WORK_DIR}")
    if [ -n "${ANKA_TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${ANKA_TELEGRAM_ALLOWED_USERS:-}" ]; then
        cmd+=(--telegram)
    else
        log "Telegram disabled (missing ANKA_TELEGRAM_BOT_TOKEN or ANKA_TELEGRAM_ALLOWED_USERS)"
    fi

    log "starting AnkaLoop on ${HOST}:${PORT}"
    started_at=$(date +%s)
    "${cmd[@]}" &
    child_pid=$!
    printf '%s\n' "${child_pid}" > "${PID_FILE}"

    wait "${child_pid}"
    status=$?

    rm -f "${PID_FILE}"
    child_pid=""

    if [ -f "${MARKER}" ]; then
        rm -f "${MARKER}"
        log "restart requested; starting AnkaLoop again in place"
        restart_pending=0
        continue
    fi

    uptime=$(( $(date +%s) - started_at ))
    log "AnkaLoop exited with status ${status} after ${uptime}s (no restart requested)"

    if [ "${MIN_UPTIME}" != "0" ] && [ "${uptime}" -lt "${MIN_UPTIME}" ]; then
        log "uptime < ANKALOOP_VM_MIN_UPTIME=${MIN_UPTIME}s; exiting so systemd reports the failure"
        exit "${status}"
    fi

    log "restarting child inside supervisor (crash loop guard disabled or uptime OK)"
    sleep 1
done
