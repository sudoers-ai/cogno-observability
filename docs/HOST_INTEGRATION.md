# Host integration

`cogno-observability` is the **operator-facing** monitoring organ: it turns a host's per-turn
`TurnEvent`s into Prometheus series and mounts `/metrics`, so Grafana can watch the fleet. It plugs
into the host through **two seams the host already exposes** — the host itself stays
prometheus-agnostic (it never imports `prometheus_client`).

## The seams

A host that follows the Cogno metrics contract exposes:

1. a **`MetricsSink`** injection point — something it calls `record(TurnEvent)` on once per turn;
2. an **`instrument(app)`** hook — a callable run right after the ASGI app is built.

This library fills both:

```python
from cogno_observability import PrometheusMetricsSink, attach

app = create_pg_app(
    dsn, auth=admin,
    metrics_sink=PrometheusMetricsSink(),   # per-turn TurnEvent → Prometheus series
    instrument=attach,                      # mounts /metrics + HTTP RED instrumentation
)
```

With neither argument the host runs exactly as before (no-op sink, no `/metrics`).

### Or: one env var

The reference host (`cogno-host`) auto-wires this library when it is installed and
`COGNO_ENABLE_METRICS=1` is set (a lazy, optional import — no hard dependency):

```bash
pip install cogno-observability
export COGNO_ENABLE_METRICS=1        # /metrics + the sink
export COGNO_METRICS_TENANT_LABEL=1  # optional: add a (capped) tenant label
export COGNO_LOG_FORMAT=json         # JSON logs (for Loki)
```

## The `TurnEvent` contract

`PrometheusMetricsSink.record` **duck-types** the event (reads attributes, no `cogno_host`
import), so any host emitting this shape is observable. The fields it reads: `route`,
`stop_reason`, `ok`, `error`, `blocked`, `cache_hit`, `elapsed_ms`, `cost_usd`,
`drift_cumulative`, `drift_action`, `tool_calls`, `tool_failures`, `correction_count`,
`handoff`, `grounding_rule`, `grounding_repaired`, `provenance_refusals`, and `stages` (a
list of `{stage, model, tokens_in, tokens_out, embedding_tokens, elapsed_ms}`).

That list is not maintained by hand on either side: `TurnEventLike` in `sink.py` declares it,
`tests/test_protocol_matches_the_reads.py` holds the declaration byte-for-byte against the
`getattr` reads in `_record`, and `tests/test_contract.py` holds it against the host's real
`TurnEvent`. `failover_count` used to be on this line; the host cut the field (cogno-host
#605) because nothing ever wrote it, and this side followed.

## Cardinality rule

`tenant` / `identity` / `session` are **never** Prometheus labels (unbounded → they'd blow up the
TSDB). They belong in **logs** (Loki), queried with `| json | tenant="…"`. `tenant` as a metric
label is opt-in and capped (`tenant_label=True`) — only for a small, bounded fleet.

## Deploy

`deploy/` has a one-command docker-compose stack (Prometheus + Loki + Promtail + Grafana) that
watches an existing host and gives you one Grafana login for metrics **and** live logs. See
`deploy/README.md`.

## Not this library's job (host / other organs)

Per-tenant usage the **customer** sees (Radar Tokens) is `cogno-ui` reading the billing ledger —
a different surface from fleet monitoring. Alertmanager paging, log shipping, and the Grafana
deployment topology are the operator's infra; this library provides the metrics, the `/metrics`
endpoint, and starter dashboards/rules.
