# Cogno SLOs & runbook pointers

Starting SLOs for the pipeline. Tune windows/targets to your traffic; the alert rules in
`ops/alerts/cogno_rules.yml` are phrased against the same recording rules.

| SLO | Target | Metric (recording rule) | Alert |
|---|---|---|---|
| **Availability** | turns are served | `cogno:turns:rate5m > 0` | `CognoNoTraffic` |
| **Success rate** | ≥ 99% turns without unhandled error | `1 - cogno:error_ratio:rate5m` | `CognoHighErrorRate` (>5%) |
| **Latency** | p95 turn ≤ 30s | `cogno:turn_latency_p95:5m` | `CognoLatencyP95High` |
| **Tool health** | tool-failure ratio ≤ 20% | `cogno:tool_failure_ratio:rate5m` | `CognoToolFailureSpike` |
| **Quality** | human-handoff ratio ≤ 10% | `cogno:handoff_ratio:rate5m` | `CognoHandoffSpike` |
| **Abuse/guardrails** | block ratio ≤ 15% | `cogno:block_ratio:rate5m` | `CognoBlockRateHigh` |
| **Cost** | spend ≤ budget/h | `cogno:cost_usd:rate1h` | `CognoCostBurnHigh` |

## Error budget

With a 99% success SLO over 30 days the budget is ~7.2h of "erroring". Track burn with
`sum(increase(cogno_turn_errors_total[30d])) / sum(increase(cogno_turns_total[30d]))`.

## Runbook pointers (metric → where to look next)

- **error rate ↑** → `sum(rate(cogno_turn_errors_total[5m])) by (error)` to see the exception class,
  then the host/lib ERROR logs in Loki filtered by `tenant`/`session` (the high-cardinality trace
  keys live in logs, not metrics — that's the split).
- **latency ↑** → `cogno_stage_duration_seconds` by stage. EGO high → agent-loop / model provider;
  NER/NOUMENO high → the JSON model; ID ~0 (heuristic).
- **tool failures ↑** → the MCP module / dispatcher, not the model. Check the vertical's server logs.
- **handoffs ↑** → judge-reject or grounding-rewrite exhaustion → a quality regression (persona,
  model routing, or a prompt change). Cross-check `cogno_self_corrections_total`.
- **cost burn ↑** → a runaway EGO loop (`cogno_self_corrections_total`, interrupted turns) or a
  tenant on a premium model; cross-check the per-tenant `token_ledger` (billing) in cogno-ui.

> Metrics tell you **that** something is wrong and **which class**; the per-turn **why** is in the
> logs (Loki) keyed by tenant/session, and the per-tenant **spend** is in the billing ledger. Three
> separate surfaces — don't try to make Prometheus answer all three.
