# AnkaLoop on Kubernetes

Manifests to run AnkaLoop as a single-replica Deployment using the
`ghcr.io/tao12345666333/ankaloop:gmi-latest` image. LLM provider settings
(base URL, API key, model) are passed purely through environment variables,
same as the GMI AgentBox flow.

## Self-restart without stopping the pod

The same supervisor design as `deploy/vm` and `deploy/docker`:

```text
tini (PID 1)
└── /scripts/supervisor.sh        <- from ConfigMap, replaces image entrypoint
    └── /usr/local/bin/gmi-entrypoint   <- stock gmi entrypoint, runs as a child
        └── anka serve [--telegram]
```

- The agent can run `/k8s:restart` (slash command installed into
  `/workspace/.ankaloop/commands/k8s/restart.toml` on first boot), which
  executes `/scripts/restart-ankaloop.sh`: it touches
  `/tmp/ankaloop-restart-requested` and SIGTERMs the AnkaLoop process.
- `supervisor.sh` sees the marker and starts AnkaLoop again **without the
  container ever exiting** — the pod keeps running, `restartCount` stays
  unchanged, and container-local state (e.g. anything installed outside
  `/workspace`) survives.
- If AnkaLoop exits **without** the marker (a real crash), the supervisor
  exits too and Kubernetes restarts the container as usual.
- Manual restart from outside: `kubectl -n ankaloop exec deploy/ankaloop -- /scripts/restart-ankaloop.sh`
- Set env `ANKALOOP_K8S_REINSTALL_ON_RESTART=1` on the Deployment to make
  `restart-ankaloop.sh` run `uv sync --reinstall-package ankaloop` in `/app`
  before the restart.

## Files

| File | Purpose |
| --- | --- |
| `namespace.yaml` | `ankaloop` namespace. |
| `configmap-scripts.yaml` | `supervisor.sh` + `restart-ankaloop.sh`, mounted at `/scripts`. |
| `deployment.yaml` | Single replica, `Recreate` strategy, no resource requests/limits, probes on `/api/v1/health`. `/workspace` is an `emptyDir` — no persistence; sessions, memory, config, and logs live only as long as the pod. |
| `secret.example.yaml` | Template for the `ankaloop-env` Secret (copy to `secret.yaml`, which is gitignored). |
| `kustomization.yaml` | `kubectl apply -k` entrypoint (Secret intentionally excluded). |

## Deploy

```bash
# 1. Create the Secret with real values (never commit it)
cp deploy/k8s/secret.example.yaml deploy/k8s/secret.yaml
$EDITOR deploy/k8s/secret.yaml

# 2. Apply everything
kubectl apply -k deploy/k8s/
kubectl apply -f deploy/k8s/secret.yaml

# 3. Watch it come up
kubectl -n ankaloop rollout status deployment/ankaloop
kubectl -n ankaloop logs -f deployment/ankaloop
```

Required Secret keys: `OPENAI_API_KEY` (or `GMI_MAAS_API_KEY`) and
`ANKA_CHAT_MODEL` (or `GMI_MODELS`). Optional: `ANKA_OPENAI_BASE` /
`GMI_MAAS_BASE_URL`, `ANKA_TELEGRAM_BOT_TOKEN` +
`ANKA_TELEGRAM_ALLOWED_USERS` to enable the Telegram polling bot.

## Access

No Service is created on purpose. Reach the API directly through the pod:

```bash
kubectl -n ankaloop port-forward deployment/ankaloop 8080:8080
curl -fsS http://localhost:8080/api/v1/health
curl -fsS http://localhost:8080/api/v1/info
```

If you later expose the API through a Service/Ingress, put an authenticating
gateway in front — AnkaLoop's gmi configuration ships with
`server.auth.enabled = false`.

## Persistence (optional)

`/workspace` is an `emptyDir` by default: sessions, memory, generated config,
skills, and logs are lost when the pod is replaced (in-place self-restart via
`/k8s:restart` does NOT wipe them, since the container keeps running).

If you need the data to survive pod rescheduling, create and manage your own
PVC — it is intentionally not included in this directory:

```bash
# 1. Create a PVC yourself, e.g.:
kubectl -n ankaloop apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ankaloop-workspace
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 10Gi
EOF
```

Then point the `workspace` volume in `deployment.yaml` at it:

```yaml
        - name: workspace
          persistentVolumeClaim:
            claimName: ankaloop-workspace
```

The `Recreate` strategy already matches ReadWriteOnce volumes.

## Notes

- `imagePullPolicy: Always` + the moving `gmi-latest` tag means a pod
  reschedule picks up the newest image. Pin `image:` to `gmi-<version>` if
  you prefer stability.
- Liveness probe is intentionally tolerant (`failureThreshold: 12`) so a
  self-restart does not make Kubernetes kill the container mid-restart.
