"""Unit tests for PrometheusMetricsSink — TurnEvent → Prometheus series (fresh registry each)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from prometheus_client import CollectorRegistry

from cogno_observability import PrometheusMetricsSink


# ── minimal stand-ins for the host's TurnEvent / StageSample (duck-typed, no cogno_host dep) ──
@dataclass
class Stage:
    stage: str
    model: str = "fake"
    tokens_in: int = 0
    tokens_out: int = 0
    embedding_tokens: int = 0
    elapsed_ms: float = 0.0


@dataclass
class Event:
    tenant_id: str = "acme"
    route: str = "EGO"
    stop_reason: str = "completed"
    total_tokens: int = 0
    elapsed_ms: float = 0.0
    cache_hit: bool = False
    blocked: bool = False
    ok: bool = True
    error: str = ""
    cost_usd: float = 0.0
    drift_cumulative: float = 0.0
    drift_action: str = ""
    tool_calls: int = 0
    tool_failures: int = 0
    correction_count: int = 0
    handoff: bool = False
    grounding_rule: str = ""
    grounding_repaired: bool = False
    provenance_refusals: int = 0
    stages: list = field(default_factory=list)


@pytest.fixture
def reg():
    return CollectorRegistry()


def test_turn_counter_and_latency(reg):
    sink = PrometheusMetricsSink(registry=reg)
    sink.record(Event(route="EGO", stop_reason="completed", elapsed_ms=1500))
    v = reg.get_sample_value("cogno_turns_total", {
        "route": "EGO", "stop_reason": "completed", "ok": "true",
        "blocked": "false", "cache_hit": "false"})
    assert v == 1.0
    assert reg.get_sample_value("cogno_turn_duration_seconds_count", {"route": "EGO"}) == 1.0


def test_failed_turn_increments_errors(reg):
    sink = PrometheusMetricsSink(registry=reg)
    sink.record(Event(ok=False, error="MCPDispatchError", stop_reason="error"))
    assert reg.get_sample_value("cogno_turn_errors_total", {"error": "MCPDispatchError"}) == 1.0


def test_stage_tokens_by_model_and_direction(reg):
    sink = PrometheusMetricsSink(registry=reg)
    sink.record(Event(stages=[
        Stage("ner", model="mistral", tokens_in=100, tokens_out=40, elapsed_ms=200),
        Stage("id", model="heuristic", embedding_tokens=12),
    ]))
    assert reg.get_sample_value("cogno_stage_tokens_total",
                                {"stage": "ner", "model": "mistral", "direction": "in"}) == 100.0
    assert reg.get_sample_value("cogno_stage_tokens_total",
                                {"stage": "ner", "model": "mistral", "direction": "out"}) == 40.0
    assert reg.get_sample_value("cogno_stage_tokens_total",
                                {"stage": "id", "model": "heuristic",
                                 "direction": "embedding"}) == 12.0
    assert reg.get_sample_value("cogno_stage_duration_seconds_count", {"stage": "ner"}) == 1.0


def test_cost_drift_tools_reliability(reg):
    sink = PrometheusMetricsSink(registry=reg)
    sink.record(Event(cost_usd=0.0123, drift_cumulative=0.42, drift_action="warn",
                      tool_calls=3, tool_failures=1,
                      correction_count=1, handoff=True))
    assert reg.get_sample_value("cogno_cost_usd_total") == pytest.approx(0.0123)
    assert reg.get_sample_value("cogno_drift_actions_total", {"action": "warn"}) == 1.0
    assert reg.get_sample_value("cogno_drift_score_count") == 1.0
    assert reg.get_sample_value("cogno_tool_calls_total") == 3.0
    assert reg.get_sample_value("cogno_tool_failures_total") == 1.0
    assert reg.get_sample_value("cogno_self_corrections_total") == 1.0
    assert reg.get_sample_value("cogno_handoffs_total") == 1.0


def test_blocked_turn_counted(reg):
    sink = PrometheusMetricsSink(registry=reg)
    sink.record(Event(blocked=True, stop_reason="quota_exceeded"))
    assert reg.get_sample_value("cogno_blocked_total", {"stop_reason": "quota_exceeded"}) == 1.0


def test_record_never_raises_on_garbage(reg):
    sink = PrometheusMetricsSink(registry=reg)
    sink.record(object())            # missing every attribute → swallowed, no exception


def test_tenant_label_opt_in(reg):
    sink = PrometheusMetricsSink(registry=reg, tenant_label=True)
    sink.record(Event(tenant_id="acme"))
    assert reg.get_sample_value("cogno_turns_by_tenant_total",
                                {"tenant": "acme", "ok": "true"}) == 1.0


def test_two_sinks_on_same_registry_share_series(reg):
    # A second sink on the same registry must not crash (Duplicated timeseries) — it reuses the
    # existing collectors, so counts from both accumulate onto one series.
    s1 = PrometheusMetricsSink(registry=reg)
    s2 = PrometheusMetricsSink(registry=reg)          # must NOT raise
    s1.record(Event(route="EGO", stop_reason="completed"))
    s2.record(Event(route="EGO", stop_reason="completed"))
    assert reg.get_sample_value("cogno_turns_total", {
        "route": "EGO", "stop_reason": "completed", "ok": "true",
        "blocked": "false", "cache_hit": "false"}) == 2.0


def test_two_default_registry_sinks_do_not_crash():
    # The real-world footgun: PrometheusMetricsSink() twice on the global REGISTRY.
    PrometheusMetricsSink()
    PrometheusMetricsSink()                            # must NOT raise


def test_protection_net_counters(reg):
    # grounding rewrite (rule + repaired) and provenance refusals land as counters; a clean
    # turn (empty rule, zero refusals) emits neither series.
    sink = PrometheusMetricsSink(registry=reg)
    sink.record(Event(grounding_rule="performative_without_commit", grounding_repaired=True,
                      provenance_refusals=2))
    sink.record(Event())                                     # clean turn → no net samples
    assert reg.get_sample_value("cogno_grounding_rewrites_total", {
        "rule": "performative_without_commit", "repaired": "true"}) == 1.0
    assert reg.get_sample_value("cogno_provenance_refusals_total") == 2.0
