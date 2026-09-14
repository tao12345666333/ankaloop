#!/usr/bin/env sh
# restart-ankaloop: request an in-place AnkaLoop restart without stopping the
# Docker container. Companion to ankaloop-docker-supervisor. Safe to run
# manually; also invoked by the agent itself through the /docker:restart
# slash command.
set -eu

DELAY_SECONDS="${ANKALOOP_DOCKER_RESTART_DELAY:-1}"
LOG_DIR="${ANKA_LOG_DIR:-${ANKA_WORK_DIR:-/workspace}/logs}"
LOG_FILE="${LOG_DIR}/ankaloop-restart.log"
MARKER="${ANKALOOP_DOCKER_RESTART_MARKER:-/tmp/ankaloop-restart-requested}"
PID_FILE="${ANKALOOP_DOCKER_PID_FILE:-/tmp/ankaloop-server.pid}"

mkdir -p "${LOG_DIR}"

{
    echo "[ankaloop-docker] restart requested at $(date -Is)"
    if [ -n "${ANKALOOP_DOCKER_RESTART_REASON:-}" ]; then
        echo "[ankaloop-docker] reason: ${ANKALOOP_DOCKER_RESTART_REASON}"
    fi

    touch "${MARKER}"

    if [ "${DELAY_SECONDS}" != "0" ]; then
        sleep "${DELAY_SECONDS}"
    fi

    if [ -f "${PID_FILE}" ]; then
        ANKALOOP_PID="$(cat "${PID_FILE}")"
        if [ -n "${ANKALOOP_PID}" ] && kill -0 "${ANKALOOP_PID}" 2>/dev/null; then
            echo "[ankaloop-docker] stopping AnkaLoop pid=${ANKALOOP_PID} (container stays up)"
            kill -TERM "${ANKALOOP_PID}"
            exit 0
        fi
    fi

    # No procps (pkill) in the slim image, so scan /proc as a fallback.
    echo "[ankaloop-docker] pid file missing or stale; falling back to /proc scan"
    for proc in /proc/[0-9]*; do
        pid="${proc#/proc/}"
        if [ "${pid}" = "$$" ]; then
            continue
        fi
        if tr '\0' ' ' < "${proc}/cmdline" 2>/dev/null | grep -q 'anka serve'; then
            echo "[ankaloop-docker] stopping pid=${pid} (container stays up)"
            kill -TERM "${pid}" 2>/dev/null || true
        fi
    done
} >> "${LOG_FILE}" 2>&1
