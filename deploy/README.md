# Deploy — Grafana over Prometheus (metrics) + Loki (logs)

One Grafana login to watch the Cogno host **live**: metrics from Prometheus, logs from Loki.
This stack **watches an existing cogno-host** — it does not run the host itself.

## 1. Turn observability on in the host

The host stays prometheus-agnostic; two env vars light it up (the app factory auto-wires
`cogno-observability` when the package is installed):

```bash
pip install cogno-observability                 # in the host's environment
export COGNO_ENABLE_METRICS=1                    # exposes /metrics + wires the PrometheusMetricsSink
export COGNO_LOG_FORMAT=json                     # one JSON log object per line (for Loki)
# then start the host as usual (uvicorn cogno_host...:app). /metrics is now served.
```

Get the host's JSON logs into `./logs` (Promtail tails `./logs/*.log`):

- **Host as a bare process:** redirect stdout →
  `uvicorn ... 2>&1 | tee -a deploy/logs/cogno-host.log`
- **Host in Docker:** point its log file / a volume at `deploy/logs`, or swap Promtail's
  `__path__` for the Docker container-logs path.

## 2. Start the monitoring stack

```bash
cd deploy
cp .env.example .env         # set GF_ADMIN_PASSWORD
docker compose up -d
```

## 3. Log in

**http://<server>:3000** — user `admin`, password from `.env` (`GF_ADMIN_PASSWORD`).

Datasources (Prometheus + Loki) and dashboards are auto-provisioned. You'll find:

- **Cogno — Pipeline Overview** — metrics (turns/s, error ratio, p95 latency, cost, per-stage
  tokens, reliability ratios). Refreshes every 10–30s.
- **Cogno — Live Logs** — the log stream (5s refresh) + log volume by level + error ratio. Filter
  by `level`; drill into any tenant with LogQL `{job="cogno-host"} | json | tenant="acme"`.
- **Explore → Loki → Live** — true tail (near-real-time) if you want a raw firehose.

## Pointing at your host

- **Metrics target:** edit `prometheus.yml` (`host.docker.internal:8000` → your host's address, or
  its compose service name if you run the host in the same network).
- **Alerts:** `../ops/alerts/cogno_rules.yml` is loaded into Prometheus; wire an Alertmanager for
  paging (not included here).

## Notes

- **Cardinality:** `tenant`/`identity`/`session` are **never** Prometheus/Loki *labels* (they'd
  blow up cardinality) — they live inside the JSON log line, queried with `| json | field="…"`.
- This is the **operator** surface (cross-tenant fleet). It is distinct from the tenant-facing
  Radar Tokens in cogno-ui (per-tenant, from the billing ledger) — link Grafana from your admin
  nav if you want it one click away, but keep it out of tenant scope.
