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

## Traces: `OTelTraceSink` (OpenTelemetry, GenAI conventions)

The same `record(TurnEvent)` seam can also produce an OpenTelemetry **span tree**:

```
invoke_agent                       (INTERNAL, one per turn)
├── chat {model}                   (CLIENT, one per LLM row of the ledger)
├── embeddings {model}             (CLIENT, one per embedding row of the ledger)
└── execute_tool {tool}            (INTERNAL, one per tool call)
```

Names and attributes follow the OpenTelemetry GenAI semantic conventions at
`SEMCONV_REF` in `cogno_observability/tracing.py`. Those conventions moved to
[`open-telemetry/semantic-conventions-genai`](https://github.com/open-telemetry/semantic-conventions-genai)
and have no tagged release yet, so the reference is the commit `8ffdf568e1` (2026-09-22). Their
status is *Development*, and that commit links the main conventions at v1.44.0. When a name
changes upstream, the change is one row of `SPAN_ATTRIBUTES`.

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # host's choice
from cogno_observability import OTelTraceSink

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=...)))
trace_sink = OTelTraceSink(provider, embedding_model="openai:text-embedding-3-small")
```

**Off by default.** The library reads no environment variable and knows no endpoint. The host
builds the sink only when it has a destination, so "no destination" means "no sink", and then
nothing runs and `opentelemetry` is never imported. Importing this package, and planning spans
with `plan_spans`, never imports it either. `tests/test_tracing_off.py` checks that in a fresh
interpreter, with a control. `pip install "cogno-observability[otel]"` brings the API and the
SDK. The exporter is the host's choice.

**One sink, not a fan-out.** The host has one `metrics_sink`. To run Prometheus and traces
together, the host composes the two sinks. This library ships the trace sink and not the
composition.

### Only metadata, and nothing that identifies the contact

Until the owner decides how long content may be kept (LGPD), a span carries models, tokens,
cost, times and outcome codes. Three guards enforce that, and each one has a test:

1. **The reads.** `INPUT_FIELDS` lists every field the module reads, and
   `tests/test_tracing_reads.py` derives the reads from the module's AST and requires equality.
   The fields that could carry content are never read: the message, prompt and reply, tool
   `arguments`/`result`, the error text, `identity_id` and `session_id` (on WhatsApp the
   identity IS the phone number).
2. **The table.** `SPAN_ATTRIBUTES` below is both the allowlist and the emitter, so an attribute
   with no row cannot be set. None of the spec's Opt-In content attributes has a row, and neither
   has `gen_ai.conversation.id` nor `gen_ai.agent.name`.
3. **The values.** Every string value, and every span name, must be a token: it starts with a
   letter, has no spaces and no `@`, and has no run of ten or more digits (a phone number, even
   behind a prefix like `wa:`). Anything else becomes `_OTHER`. The tenant id is sent
   only when it is a UUID or a token, and is otherwise omitted.

`tests/test_tracing_pii_guard.py` plants invented personal data in every one of those fields
and reads back everything a span can carry.

### Tokens and cost are the ledger's, by construction

A stage sample becomes an LLM row when it has LLM tokens and an embedding row when it has
embedding tokens. That is the split `cogno_host.metering.events_from_context` makes for the
token ledger, so each model span is one ledger row with the same tokens. Adding up
`cogno.usage.cost_usd` over the spans gives the ledger's cost, once the host carries the per-row
cost. The turn's own total is `cogno.turn.cost_usd`, a different key, so it is never counted
twice. **One span per ledger row, not per provider call:** the EGO adds up the calls of its agent
loop into one row per attempt, so an EGO attempt is one span.

This is checked against the real ledger, not against a copy of its rule.
`tests/test_contract.py::test_the_model_spans_are_the_host_ledgers_rows` runs the host's own
`events_from_context` and the host's own `_turn_metrics` over the same context, and requires the
same rows, in the same order, with the same tokens. It auto-skips where `cogno-host` is not
installed, as the rest of that file does.

### Times are never invented

`cogno.timing` says how each interval was obtained:

- `observed`: the host gave `started_at` (epoch seconds) and the duration was measured.
- `anchored`: the duration was measured but there is no start, so the span starts at the turn's
  start. The turn span itself, when it is `anchored`, ends when `record` is called.
- `unmeasured`: there is no duration either, so the span is zero-length at its anchor. That
  includes a turn span whose event carries no `elapsed_ms`: the host's early exits (a disabled
  tenant or contact, a blocked input) record none.

### What the host carries today, and what its wiring adds

The module reads, with defaults, everything in `INPUT_FIELDS`. A `TurnEvent` of today already
drives the sink: turn span, model spans and ledger tokens, with the children `anchored`. The
wiring adds the fields listed in `_AWAITING_HOST_WIRING` (`tests/test_contract.py`):

- `TurnEvent.started_at` and `TurnEvent.tools`, a list of records with the core's
  `ToolExecution` names `tool`/`ok`, plus `elapsed_ms`/`started_at` when the host times the
  call;
- `StageSample.provider`, `served_model`, `cost_usd`, `embedding_cost_usd`, `attempt` and
  `started_at`.

`turn_traces` is not touched. It stays the internal source of truth.

**The limit, stated:** the spans are built after the turn, from the event the host records at its
end. A turn that dies before the host reaches its sink emits no spans. Recording those turns is
the job of the host's failed-turn record (M1), not of this sink.

### The attributes

<!-- span-attributes:start (generated from SPAN_ATTRIBUTES; tests/test_tracing_docs.py pins it) -->
| Key | Spans | Type | Origin | Meaning |
| --- | --- | --- | --- | --- |
| `gen_ai.operation.name` | turn, chat, embeddings, tool | str | semconv | invoke_agent, chat, embeddings or execute_tool |
| `error.type` | turn, tool | str | semconv | only on a failure: a failed turn's exception class, else _OTHER; a failed tool is always _OTHER |
| `gen_ai.provider.name` | chat, embeddings | str | semconv | who served the call; 'unknown' when the host did not say |
| `gen_ai.request.model` | chat, embeddings | str | semconv | the model the host asked for |
| `gen_ai.response.model` | chat, embeddings | str | semconv | the snapshot the provider says answered (when the host carries it) |
| `gen_ai.usage.input_tokens` | chat, embeddings | int | semconv | input tokens of the ledger row (embedding tokens on an embeddings span) |
| `gen_ai.usage.output_tokens` | chat | int | semconv | output tokens of the ledger row |
| `gen_ai.usage.cache_read.input_tokens` | chat | int | semconv | the SUBSET of input tokens the provider served from its cache; omitted when 0 |
| `gen_ai.tool.name` | tool | str | semconv | the tool's name |
| `gen_ai.tool.type` | tool | str | semconv | always 'function': the host executes every tool the EGO calls |
| `cogno.stage` | chat, embeddings | str | cogno | the cognitive stage of the row (noumeno, ner, ego, superego_voice, ...) |
| `cogno.attempt` | chat, embeddings | int | cogno | the correction-loop attempt the row belongs to; omitted when not stamped |
| `cogno.usage.cost_usd` | chat, embeddings | float | cogno | the ledger row's provider cost, when the host carries it |
| `cogno.timing` | turn, chat, embeddings, tool | str | cogno | observed, anchored or unmeasured: how the span's interval was obtained |
| `cogno.tenant.id` | turn | str | cogno | the tenant: only a UUID or a token, otherwise omitted |
| `cogno.turn.route` | turn | str | cogno | the ID stage's route |
| `cogno.turn.stop_reason` | turn | str | cogno | how the turn ended (completed, semantic_cache, input_blocked:..., error, ...) |
| `cogno.turn.ok` | turn | bool | cogno | False when the turn raised |
| `cogno.turn.blocked` | turn | bool | cogno | a guard refused the turn |
| `cogno.turn.cache_hit` | turn | bool | cogno | answered from the semantic cache |
| `cogno.turn.handoff` | turn | bool | cogno | ended in a human handoff |
| `cogno.turn.correction_count` | turn | int | cogno | EGO/SUPEREGO correction retries |
| `cogno.turn.cost_usd` | turn | float | cogno | the meter's cost for the whole turn |
<!-- span-attributes:end -->

`error.type` reports two values. A failed turn sends its exception class when that is a token.
Otherwise, and for every failed tool, it sends the spec's fallback `_OTHER`, because the core
records a tool failure as free text.

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
