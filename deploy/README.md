# AnkaLoop deployment

One directory per deployment target. Each mode is self-contained; pick the one
that matches where you want AnkaLoop to run.

| Directory | Target | Self-restart in place | Process model |
| --- | --- | --- | --- |
| `docker/` | Docker / Docker Compose | Yes (`/docker:restart`) | tini → supervisor → `anka serve` |
| `vm/` | Bare Linux VM (systemd, no Docker) | Yes (`/vm:restart`) | systemd → supervisor → `anka serve` |
| `k8s/` | Kubernetes | Yes (`/k8s:restart`) | tini → supervisor (ConfigMap) → gmi-entrypoint → `anka serve` |
| `gmi/` | GMI Cloud AgentBox | Platform-managed | gmi-entrypoint → `anka serve` |

## Self-restart (in-place)

`docker/`, `vm/`, and `k8s/` share the same supervisor design: AnkaLoop runs as
a child process, and a marker file + SIGTERM restarts it **without** the
container/pod/service exiting. This means:

- The agent can update its own code (`git pull` on vm, package reinstall on
  k8s) and restart itself to pick up changes.
- Container-local state (installed packages, scratch files) survives the
  restart because the container/pod never exits.
- A real crash (no restart marker) still bubbles up to systemd / Docker /
  Kubernetes, which restarts the workload as usual.

## Secrets

Nothing under `deploy/` contains real credentials. All examples use
placeholders. Copy `*.example` files to a gitignored local file and fill in
real values:

- `deploy/k8s/secret.example.yaml` → `deploy/k8s/secret.yaml` (gitignored)
- `deploy/vm/ankaloop.env.example` → `deploy/vm/ankaloop.env` (gitignored)

See each directory's README for details.
