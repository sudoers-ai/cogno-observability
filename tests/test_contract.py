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
