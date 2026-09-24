"""Cross-repo contract: the host's real ``TurnEvent`` drives ``PrometheusMetricsSink``.

The sink duck-types the event, so a silent field rename on the host would degrade a metric to 0
with no failing test. This test closes that gap by importing the **real** ``cogno_host.TurnEvent``
and asserting (a) it satisfies the ``TurnEventLike`` shape the sink relies on, and (b) a real event
actually populates the series end-to-end. Auto-skips if cogno-host isn't installed (this repo stays
standalone)."""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from cogno_observability import PrometheusMetricsSink
from cogno_observability.sink import TurnEventLike

cogno_host = pytest.importorskip("cogno_host", reason="cogno-host not installed")


def test_real_turnevent_satisfies_sink_shape():
    # runtime_checkable Protocol → isinstance verifies every attribute the sink reads exists.
    e = cogno_host.TurnEvent(tenant_id="acme", session_id="s1")
    assert isinstance(e, TurnEventLike)


def test_real_turnevent_drives_the_series_end_to_end():
    reg = CollectorRegistry()
    sink = PrometheusMetricsSink(registry=reg)
    e = cogno_host.TurnEvent(
        tenant_id="acme", session_id="s1", route="EGO", stop_reason="completed",
        total_tokens=15, elapsed_ms=1200.0, cost_usd=0.002, drift_cumulative=0.3,
        drift_action="warn", tool_calls=1, handoff=False,
        stages=[cogno_host.StageSample(stage="ner", model="mistral",
                                       tokens_in=10, tokens_out=5, elapsed_ms=200.0)])
    sink.record(e)

    assert reg.get_sample_value("cogno_turns_total", {
        "route": "EGO", "stop_reason": "completed", "ok": "true",
        "blocked": "false", "cache_hit": "false"}) == 1.0
    assert reg.get_sample_value("cogno_stage_tokens_total",
                                {"stage": "ner", "model": "mistral", "direction": "in"}) == 10.0
    assert reg.get_sample_value("cogno_drift_actions_total", {"action": "warn"}) == 1.0
    assert reg.get_sample_value("cogno_turn_duration_seconds_count", {"route": "EGO"}) == 1.0


def test_sink_reads_every_field_the_host_provides():
    # Belt-and-suspenders: every attribute named in TurnEventLike is present on the host DTO, so
    # the getattr defaults in the sink are a safety net, never the actual data path.
    e = cogno_host.TurnEvent(tenant_id="acme", session_id="s1")
    for field in TurnEventLike.__annotations__:
        assert hasattr(e, field), f"host TurnEvent is missing '{field}' the sink reads"


# ── the trace sink: what the host carries TODAY, and what its wiring still has to add ──────────
#: The fields ``tracing.py`` reads that the host does not carry yet. They are read with defaults,
#: so today a real ``TurnEvent`` produces spans without them: no tool spans, no row cost, and
#: children ``anchored`` at the turn's start. This set is the host wiring's to-do list, written
#: where a test can check it. A field that leaves this set because the host added it keeps
#: passing (the check is an inclusion). A field the host REMOVES fails, which is the drift that
#: #7 was.
_AWAITING_HOST_WIRING = {
    "turn": {"started_at", "tools"},
    "stage": {"provider", "served_model", "cost_usd", "embedding_cost_usd", "attempt",
              "started_at"},
}


def test_the_host_carries_every_traced_field_but_the_ones_its_wiring_will_add():
    from cogno_observability.tracing import INPUT_FIELDS

    turn = set(cogno_host.TurnEvent.__dataclass_fields__)
    stage = set(cogno_host.StageSample.__dataclass_fields__)
    assert set(INPUT_FIELDS["turn"]) - _AWAITING_HOST_WIRING["turn"] <= turn, \
        sorted(set(INPUT_FIELDS["turn"]) - _AWAITING_HOST_WIRING["turn"] - turn)
    assert set(INPUT_FIELDS["stage"]) - _AWAITING_HOST_WIRING["stage"] <= stage, \
        sorted(set(INPUT_FIELDS["stage"]) - _AWAITING_HOST_WIRING["stage"] - stage)


def test_the_tool_fields_are_the_cores_own_names():
    """``tool``/``ok`` are read under the names of the core's ``ToolExecution``, so the host can
    hand the executions over as they are. Everything else on that record (``arguments``,
    ``result``, the ``error`` text) is content and is never read."""
    types = pytest.importorskip("cogno_anima.types", reason="cogno-anima not installed")
    from cogno_observability.tracing import INPUT_FIELDS

    fields = set(types.ToolExecution.model_fields)
    assert set(INPUT_FIELDS["tool"]) - {"elapsed_ms", "started_at"} <= fields


def test_a_real_turnevent_drives_the_trace_sink_end_to_end():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from cogno_observability import OTelTraceSink

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    e = cogno_host.TurnEvent(
        tenant_id="acme", session_id="s1", identity_id="5511900000000", route="EGO",
        stop_reason="completed", elapsed_ms=1200.0, cost_usd=0.002,
        stages=[cogno_host.StageSample(stage="ner", model="gpt-4o-mini", tokens_in=10,
                                       tokens_out=5, cached_tokens=4, elapsed_ms=200.0),
                cogno_host.StageSample(stage="id", model="heuristic", embedding_tokens=7)])
    OTelTraceSink(provider, clock=lambda: 10**18).record(e)
    spans = {s.attributes["gen_ai.operation.name"]: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"invoke_agent", "chat", "embeddings"}
    assert spans["chat"].attributes["gen_ai.usage.input_tokens"] == 10
    assert spans["chat"].attributes["gen_ai.usage.output_tokens"] == 5
    assert spans["chat"].attributes["gen_ai.usage.cache_read.input_tokens"] == 4
    assert spans["embeddings"].attributes["gen_ai.usage.input_tokens"] == 7
    assert not any("5511900000000" in str(v) for s in spans.values()
                   for v in s.attributes.values())


def test_the_model_spans_are_the_host_ledgers_rows():
    """The ledger twin against the REAL ledger: the host's own ``events_from_context`` over a
    context, and this library's spans over the ``TurnEvent`` the host's own ``_turn_metrics``
    builds from that same context. The two must be the same rows, in the same order, with the
    same tokens. If the host changes how it splits a stage into ledger rows, this fails here
    instead of leaving the spans quietly disagreeing with the invoice."""
    metering = pytest.importorskip("cogno_host.metering")
    service = pytest.importorskip("cogno_host.service")
    types = pytest.importorskip("cogno_anima.types")
    from cogno_meter import PriceBook

    from cogno_observability import plan_spans

    turn_metrics = getattr(service, "_turn_metrics", None)
    assert turn_metrics is not None, "cogno_host.service._turn_metrics moved: re-point this twin"

    def sm(stage, model, **kw):
        return types.StageMetrics(stage=stage, model=model, elapsed_ms=10.0, **kw)

    ctx = types.PipelineContext(user_input="-", retry_metrics=[
        sm("noumeno", "gpt-4o-mini", tokens_in=900, tokens_out=60, embedding_tokens=30),
        sm("ner", "gpt-4o-mini", tokens_in=1200, tokens_out=150),
        sm("id", "heuristic", tokens_in=0, tokens_out=0, embedding_tokens=12),
        sm("ego", "gpt-4o-mini", tokens_in=2500, tokens_out=80, cached_tokens=2048),
        sm("superego_voice", "gpt-4o-mini", tokens_in=1500, tokens_out=120),
    ])
    ledger = metering.events_from_context(ctx, tenant_id="acme", period="2026-09",
                                          book=PriceBook.default())
    event = cogno_host.TurnEvent(tenant_id="acme", session_id="s1",
                                 stages=turn_metrics(ctx)["stages"])
    spans = [p for p in plan_spans(event, now_ns=10**18) if p.kind in ("chat", "embeddings")]
    assert len(ledger) == 6, "the ledger split changed shape: re-read events_from_context"
    assert [(e.stage, "chat" if e.modality == "llm" else "embeddings", e.tokens_in, e.tokens_out)
            for e in ledger] == \
        [(p.attributes["cogno.stage"], p.kind, p.attributes["gen_ai.usage.input_tokens"],
          p.attributes.get("gen_ai.usage.output_tokens", 0)) for p in spans]
