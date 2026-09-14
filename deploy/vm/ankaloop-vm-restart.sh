#!/usr/bin/env bash
# ankaloop-vm-restart: request an in-place AnkaLoop restart without a systemd
# restart. Companion to ankaloop-vm-supervisor. Safe to run manually; also
# invoked by the agent itself through the /vm:restart slash command.
set -u

RUNTIME_DIR="${ANKALOOP_RUNTIME_DIR:-/opt/ankaloop/run}"
MARKER="${RUNTIME_DIR}/restart-requested"
PID_FILE="${RUNTIME_DIR}/ankaloop.pid"
DELAY="${ANKALOOP_VM_RESTART_DELAY:-1}"
LOG_FILE="${ANKALOOP_WORK_DIR:-/opt/ankaloop/data}/logs/vm-restart.log"

mkdir -p "${RUNTIME_DIR}" "$(dirname "${LOG_FILE}")"

{
    echo "[ankaloop-vm] restart requested at $(date -Is)"
    [ -n "${ANKALOOP_VM_RESTART_REASON:-}" ] && echo "[ankaloop-vm] reason: ${ANKALOOP_VM_RESTART_REASON}"

    : > "${MARKER}"
    [ "${DELAY}" != "0" ] && sleep "${DELAY}"

    if [ -f "${PID_FILE}" ]; then
        pid="$(cat "${PID_FILE}")"
        if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
            echo "[ankaloop-vm] stopping AnkaLoop pid=${pid} (service stays up)"
            kill -TERM "${pid}"
            exit 0
        fi
    fi

    echo "[ankaloop-vm] pid file missing/stale; signalling supervisor via SIGUSR1"
    systemctl kill --signal=SIGUSR1 ankaloop.service
} >> "${LOG_FILE}" 2>&1
