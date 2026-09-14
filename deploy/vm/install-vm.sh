#!/usr/bin/env bash
# Install or update AnkaLoop on a bare VM (Ubuntu/Debian) without Docker.
# Usage: bash install-vm.sh [path-to-ankaloop.env]
# Idempotent: safe to re-run; it updates the repo, venv, scripts, and unit.
set -euo pipefail

REPO_URL="${ANKALOOP_REPO_URL:-https://github.com/tao12345666333/ankaloop.git}"
REPO_DIR=/opt/ankaloop/repo
VENV_DIR=/opt/ankaloop/venv
WORK_DIR=/opt/ankaloop/data
RUNTIME_DIR=/opt/ankaloop/run
ENV_FILE=/etc/ankaloop/ankaloop.env
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_SRC="${1:-${SELF_DIR}/ankaloop.env}"

if [ "$(id -u)" -ne 0 ]; then
    echo "install-vm.sh must run as root" >&2
    exit 1
fi

echo "==> [1/7] Install prerequisites (git, uv)"
export DEBIAN_FRONTEND=noninteractive
if ! command -v git >/dev/null; then
    apt-get update -qq && apt-get install -y -qq git
fi
if ! command -v uv >/dev/null 2>&1; then
    if [ -x "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    else
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
fi
UV_BIN="$(command -v uv)"
# Do not create a self-referential symlink when uv already lives in
# /usr/local/bin (e.g. from a previous run of this script).
if [ "${UV_BIN}" != "/usr/local/bin/uv" ]; then
    ln -sf "${UV_BIN}" /usr/local/bin/uv
fi

echo "==> [2/7] Clone/update repo at ${REPO_DIR}"
mkdir -p /opt/ankaloop
if [ -d "${REPO_DIR}/.git" ]; then
    git -C "${REPO_DIR}" fetch --prune origin
    git -C "${REPO_DIR}" reset --hard origin/main
else
    git clone --depth=50 "${REPO_URL}" "${REPO_DIR}"
fi

echo "==> [3/7] Create venv (python 3.12) and install ankaloop"
uv venv --clear --python 3.12 "${VENV_DIR}"
# Install the project non-editably into the standalone venv. `uv sync` skips
# the root package when the venv is not the active project venv, so install
# it explicitly; supervisor re-syncs deps on every start via the same command.
(cd "${REPO_DIR}" && uv pip install --python "${VENV_DIR}/bin/python" --no-editable '.[telegram]')

echo "==> [4/7] Install supervisor/restart scripts and systemd unit"
install -m 0755 "${SELF_DIR}/ankaloop-vm-supervisor.sh" /usr/local/bin/ankaloop-vm-supervisor
install -m 0755 "${SELF_DIR}/ankaloop-vm-restart.sh" /usr/local/bin/ankaloop-vm-restart
install -m 0644 "${SELF_DIR}/ankaloop.service" /etc/systemd/system/ankaloop.service

echo "==> [5/7] Install runtime config"
# Stop any running instance so the install below captures a quiet snapshot
# rather than racing a live server.
if systemctl is-active --quiet ankaloop.service; then
    echo "    stopping ankaloop.service"
    systemctl stop ankaloop.service
fi
mkdir -p /etc/ankaloop "${WORK_DIR}" "${RUNTIME_DIR}"
if [ -f "${ENV_SRC}" ]; then
    install -m 0600 "${ENV_SRC}" "${ENV_FILE}"
    echo "    installed ${ENV_FILE} (0600)"
elif [ -f "${ENV_FILE}" ]; then
    echo "    keeping existing ${ENV_FILE}"
else
    echo "ERROR: no env file at ${ENV_SRC} or ${ENV_FILE}" >&2
    echo "Copy deploy/vm/ankaloop.env.example, fill it in, and re-run." >&2
    exit 1
fi

echo "==> [6/7] Enable and (re)start ankaloop.service"
systemctl daemon-reload
systemctl enable ankaloop.service
systemctl restart ankaloop.service

echo "==> [7/7] Wait for health endpoint"
for i in $(seq 1 30); do
    if curl -fsS -m 2 http://127.0.0.1:8080/api/v1/health >/dev/null 2>&1; then
        echo "    AnkaLoop is healthy on 127.0.0.1:8080"
        systemctl --no-pager --full status ankaloop.service | head -5
        exit 0
    fi
    sleep 2
done
echo "ERROR: health check did not pass in 60s; recent logs:" >&2
journalctl -u ankaloop --no-pager -n 30 >&2
exit 1
