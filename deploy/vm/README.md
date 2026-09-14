# AnkaLoop on a bare VM (no Docker)

Deploy AnkaLoop from the public repo (`https://github.com/tao12345666333/ankaloop`)
directly onto a Linux server, running on a uv-managed venv with the same
in-place self-restart ability as the `deploy/k8s` setup.

## Layout on the server

| Path | Purpose |
| --- | --- |
| `/opt/ankaloop/repo` | Git clone of the ankaloop repo. `git pull` + self-restart picks up new code. |
| `/opt/ankaloop/venv` | uv virtualenv (created with `uv venv --python 3.12`). |
| `/opt/ankaloop/data` | `ANKALOOP_WORK_DIR`: logs, generated commands; `XDG_CONFIG_HOME` is `/opt/ankaloop/data/.config`, so `config.toml`, sessions, memory, and Telegram state live under `/opt/ankaloop/data/.config/ankaloop`. |
| `/opt/ankaloop/run` | Runtime control files only (supervisor PID + in-place restart marker). |
| `/etc/ankaloop/ankaloop.env` | Runtime configuration (chmod 600): LLM base URL, API key, model, optional Telegram settings. |
| `/usr/local/bin/ankaloop-vm-supervisor` | Supervisor loop (systemd `ExecStart`). |
| `/usr/local/bin/ankaloop-vm-restart` | In-place restart helper (`/vm:restart` or manual). |
| `/etc/systemd/system/ankaloop.service` | systemd unit; binds 127.0.0.1:8080. |

## Self-restart without stopping the service

Mirrors the K8s (`deploy/k8s`) and Docker (`deploy/docker`) designs:

```text
systemd (Restart=always)          <- only acts when the supervisor exits
└── ankaloop-vm-supervisor        <- bash loop, long-running
    └── anka serve --telegram     <- the actual server, a CHILD process
```

- In-place restart (no systemd restart, code checkout/venv/state survive):
  - from inside the agent: `/vm:restart` (slash command installed by the
    supervisor on every start at `$ANKALOOP_WORK_DIR/.ankaloop/commands/vm/restart.toml`)
  - from a shell: `ankaloop-vm-restart` (or `systemctl kill --signal=SIGUSR1 ankaloop.service`)
  - flow: marker file `/opt/ankaloop/run/restart-requested` is touched, the child
    gets SIGTERM, the supervisor sees the marker and immediately re-execs
    `anka serve`. systemd never notices.
- Real crash (no marker): the supervisor exits with the child's status and
  systemd restarts the service (`Restart=always`).
- Upgrade flow: `cd /opt/ankaloop/repo && git pull && ankaloop-vm-restart` — the
  supervisor runs `uv pip install --no-editable '.[telegram]'` before
  every start, so the new code is installed into the venv automatically.

## Deploy (from your workstation)

`install-vm.sh` installs its sibling scripts and the systemd unit from its
own directory, so copy the whole `deploy/vm/` payload (not just the installer)
to the server.

```bash
# 1. Create the env file locally (never commit it)
cp deploy/vm/ankaloop.env.example deploy/vm/ankaloop.env
#    or reuse values from your deploy/k8s secret (same keys)
$EDITOR deploy/vm/ankaloop.env

# 2. Copy the deploy/vm payload + your env to the server
ssh root@<server> 'mkdir -p /tmp/ankaloop-vm'
scp deploy/vm/install-vm.sh \
    deploy/vm/ankaloop-vm-supervisor.sh \
    deploy/vm/ankaloop-vm-restart.sh \
    deploy/vm/ankaloop.service \
    deploy/vm/ankaloop.env \
    root@<server>:/tmp/ankaloop-vm/

# 3. Run the installer on the server (idempotent; re-run to update/redeploy)
ssh root@<server> 'bash /tmp/ankaloop-vm/install-vm.sh /tmp/ankaloop-vm/ankaloop.env && rm -rf /tmp/ankaloop-vm'

# 4. Verify
ssh root@<server> 'systemctl status ankaloop --no-pager; curl -fsS http://127.0.0.1:8080/api/v1/health'
```

`install-vm.sh` is idempotent; re-running it updates scripts/unit and redeploys.

## Day-2 operations

| Task | Command |
| --- | --- |
| Logs | `journalctl -u ankaloop -f` |
| In-place restart | `ssh root@<server> ankaloop-vm-restart` |
| Upgrade to latest main | `ssh root@<server> 'cd /opt/ankaloop/repo && git pull && ankaloop-vm-restart'` |
| Full service restart | `systemctl restart ankaloop` |
| Change config | edit `/etc/ankaloop/ankaloop.env`, then `systemctl restart ankaloop` (env is only read at process start) |
| Edit repo code manually | `cd /opt/ankaloop/repo` … then `ankaloop-vm-restart` |

## Notes / security

- All AnkaLoop state lives under `/opt/ankaloop` on the root ext4 filesystem
  and survives reboots: repo/venv, work dir + config (`/opt/ankaloop/data`),
  and the runtime control dir (`/opt/ankaloop/run`). Secrets are at
  `/etc/ankaloop/ankaloop.env`. Nothing is on tmpfs — `/run` is not used at
  all — so a reboot loses nothing.
- The server binds to `127.0.0.1` (override with `ANKALOOP_VM_HOST` in
  `/etc/ankaloop/ankaloop.env`). AnkaLoop's HTTP API has no built-in auth in
  this configuration, so if you expose it publicly put an authenticating
  reverse proxy in front. Telegram uses outbound long polling — no inbound
  port is needed for the bot.
- `/etc/ankaloop/ankaloop.env` holds the API key and Telegram token: mode
  0600, never commit it to git.
- The venv pins Python 3.12 via `uv venv --python 3.12` (uv installs it on
  first use), matching the Docker images, instead of using the OS Python.
- Before switching the supervisor loop on, verify the unit logs show the
  server healthy for ~2 minutes (`journalctl -u ankaloop --since -2m`).
  Otherwise enable the safeguard `Environment=ANKALOOP_VM_MIN_UPTIME=30` in
  the unit so a crash-looping child can't be hidden by the supervisor.
