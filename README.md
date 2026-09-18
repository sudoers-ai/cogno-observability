# cogno-observability

**SRE/Ops monitoring for the [Cogno](https://github.com/sudoers-ai/cogno-anima) stack** — a Prometheus metrics sink + `/metrics` wiring + Grafana dashboards/alerts, plus a one-login deploy stack for metrics **and** live logs.

Where [`cogno-anima`](https://github.com/sudoers-ai/cogno-anima) is the *mind* and [`cogno-homeo`](https://github.com/sudoers-ai/cogno-homeo) keeps the calls *alive*, `cogno-observability` is the **operator's eyes** on the running fleet — how many turns, how fast, how much they cost, where they fail. It attaches to the host from *outside* its cognition, so the host never imports `prometheus_client`.

> Status: **alpha** — metrics sink + `/metrics` wiring + deploy stack + unit/contract suite in place.

SRE/Ops monitoring is the *operator-facing* pillar — cross-tenant fleet health — and is
deliberately **separate** from two other consumers of the same turn data:

| Surface | Audience | This repo? |
|---|---|---|
| **Fleet monitoring** (Prometheus/Grafana, cross-tenant) | operator / SRE | ✅ **yes** |
| **Radar Tokens** (per-tenant usage, from the `token_ledger`) | the tenant/customer | ❌ cogno-ui |
| **Billing / BudgetGuard** (quota, spend) | the platform | ❌ cogno-host + cogno-meter |

## Why a separate repo

The house rule is **libs emit, host configures** — and the host stays **prometheus-agnostic**. It
emits one `TurnEvent` per turn to an injected `MetricsSink` and exposes an `instrument(app)` hook;
it never imports `prometheus_client`. This package fills both seams, so the Prometheus dependency,
the metric catalog, and the dashboards live here — swappable, versioned independently, and absent
from a deployment that doesn't want them.

## Logs ≠ metrics

Two different pipelines. **Logs** (host `configure_logging` → stdout → Loki/ELK) answer *"what
happened in this turn?"* and may carry high-cardinality `tenant`/`identity`/`session`. **Metrics**
(this repo) answer *"how is the fleet doing?"* — aggregate series with **low-cardinality labels
only**. `session_id`/`identity_id` are **never** Prometheus labels (they'd blow up the TSDB); use
logs for per-turn tracing. `tenant` is opt-in and capped (`PrometheusMetricsSink(tenant_label=True)`).

## Install

```bash
pip install cogno-observability            # core (prometheus-client)
pip install "cogno-observability[http]"    # + per-route HTTP RED metrics (fastapi instrumentator)
```

## Wire it into the host

```python
from cogno_host.api.pg_app import create_pg_app
from cogno_observability import PrometheusMetricsSink, attach

app = create_pg_app(
    dsn, auth=admin,
    metrics_sink=PrometheusMetricsSink(),   # per-turn TurnEvent → Prometheus series
    instrument=attach,                      # mounts /metrics + HTTP instrumentation
)
```

With neither argument the host runs exactly as before (no-op sink, no `/metrics`).

## Metrics

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `cogno_turns_total` | counter | route, stop_reason, ok, blocked, cache_hit | turn outcomes |
| `cogno_turn_errors_total` | counter | error | unhandled exceptions (by class) |
| `cogno_turn_duration_seconds` | histogram | route | end-to-end latency |
| `cogno_stage_duration_seconds` | histogram | stage | per cognitive-stage latency |
| `cogno_stage_tokens_total` | counter | stage, model, direction | tokens (in/out/embedding) |
| `cogno_cost_usd_total` | counter | — | provider spend (0 for local) |
| `cogno_drift_score` | histogram | — | cumulative drift distribution |
| `cogno_drift_actions_total` | counter | action | drift-triggered actions |
| `cogno_tool_calls_total` / `_failures_total` | counter | — | EGO tool calls / failures |
| `cogno_self_corrections_total`, `cogno_handoffs_total` | counter | — | reliability |
| `cogno_blocked_total` | counter | stop_reason | safety/quota/PII blocks |

Plus HTTP RED metrics (`http_request_*`) from the instrumentator.

## Multi-worker

Each uvicorn/gunicorn worker has its own registry. Set `PROMETHEUS_MULTIPROC_DIR` to a shared
(empty at boot) dir and `attach` will reconcile across workers via `MultiProcessCollector` at scrape
time. Without it, single-process mode is used.

## Ops assets (`ops/`)

- `alerts/cogno_rules.yml` — Prometheus recording + alerting rules.
- `dashboards/cogno_overview.json` — Grafana dashboard (import, pick your Prometheus datasource).
- `dashboards/cogno_logs.json` — Grafana dashboard for live logs (Loki).
- `prometheus-scrape.example.yml` — scrape config / ServiceMonitor example.
- `SLOs.md` — starting SLOs + a metric→runbook map.

## Deploy — one Grafana login for metrics + logs

`deploy/` is a docker-compose stack (Prometheus + Loki + Promtail + Grafana) that watches an
existing host and gives you **one Grafana login** to follow it live — metrics and near-real-time
logs on the same pane. See [`deploy/README.md`](deploy/README.md).

## Host integration

The host exposes two seams (`metrics_sink=` + `instrument(app)`) and stays prometheus-agnostic;
this library fills them. See [`docs/HOST_INTEGRATION.md`](docs/HOST_INTEGRATION.md).

## The Cogno ecosystem

`cogno-observability` is one organ of **[Cogno](https://github.com/sudoers-ai)** — a family of
small, composable, Apache-2.0 libraries that together form a complete conversational-agent
platform. Each library owns a single concern and stays infra-agnostic; a **host** assembles them
into a running agent:

![The Cogno ecosystem](docs/assets/cogno-ecosystem.svg)

The open-source libraries are the organs; the **host is the body** that joins them. Our reference
host — `cogno-host`, with its `cogno-ui` dashboard — is the private product layer, but it holds no
special powers: everything it does rides on the public seams documented in each library's
`docs/HOST_INTEGRATION.md`.
