# Changelog

## Unreleased

### Added

- **`OTelTraceSink`**, an optional OpenTelemetry trace sink on the same `record(TurnEvent)` seam:
  one `invoke_agent` span per turn, one `chat`/`embeddings` span per row of the host's token ledger
  and one `execute_tool` span per tool call, in the GenAI semantic conventions
  (`open-telemetry/semantic-conventions-genai@8ffdf568e1`, 2026-09-22, status Development; see
  `SEMCONV_REF`). It sends metadata only: the fields that could carry content or identify the
  contact are never read (`tests/test_tracing_reads.py`, from the AST). `SPAN_ATTRIBUTES` is the
  allowlist and the emitter, and every string value is a token or `_OTHER`. A tool's name, the one
  value the model produces, is sent only when the host marks the call `in_catalog=True`. Times are never
  invented (`cogno.timing`). Also `plan_spans`, a pure description of the same spans with no
  OpenTelemetry import.
- Extra **`[otel]`** (`opentelemetry-api`, `opentelemetry-sdk`). Nothing imports `opentelemetry`
  until a sink is built, and the library reads no environment.

### Known limits

- A turn that dies before the host reaches its sink emits no spans (the failed-turn record is the
  host's).
- One span per ledger row: the EGO's agent loop is one row, and one span, per attempt.
- Until the host wiring adds them, there are no tool spans and no per-row cost, and the children
  are `anchored` at the turn's start (see `_AWAITING_HOST_WIRING` in `tests/test_contract.py`).

## 0.1.0 — 2026-07-25

First public release on PyPI.

SRE/Ops monitoring for the Cogno stack — Prometheus metrics sink + /metrics wiring + Grafana dashboards/alerts. Cross-tenant fleet health (NOT the tenant-facing Radar Tokens; NOT billing).
