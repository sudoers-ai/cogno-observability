"""
Minimal host wiring for cogno-observability: turn per-turn TurnEvents into Prometheus series and
expose them on /metrics.

Nothing here needs a real cogno-host — a tiny stand-in TurnEvent (duck-typed) drives the sink, so
the example runs standalone:  python examples/host_min.py

It shows the two seams a host supplies:
  1. a MetricsSink the host calls record(event) on once per turn,
  2. the instrument(app) hook that mounts /metrics on the ASGI app.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, generate_latest

from cogno_observability import PrometheusMetricsSink


@dataclass
class StageSample:                      # the shape the host's TurnEvent.stages carries
    stage: str
    model: str = "mistral"
    tokens_in: int = 0
    tokens_out: int = 0
    embedding_tokens: int = 0
    elapsed_ms: float = 0.0


@dataclass
class TurnEvent:                         # a stand-in for the host's per-turn DTO (duck-typed)
    route: str = "EGO"
    stop_reason: str = "completed"
    ok: bool = True
    error: str = ""
    blocked: bool = False
    cache_hit: bool = False
    elapsed_ms: float = 0.0
    cost_usd: float = 0.0
    drift_cumulative: float = 0.0
    drift_action: str = ""
    tool_calls: int = 0
    tool_failures: int = 0
    failover_count: int = 0
    correction_count: int = 0
    handoff: bool = False
    tenant_id: str = "acme"
    total_tokens: int = 0
    stages: list = field(default_factory=list)


def main() -> None:
    # A private registry keeps the example self-contained (a real host uses the default one, which
    # attach()'s /metrics endpoint serves).
    registry = CollectorRegistry()
    sink = PrometheusMetricsSink(registry=registry)

    # ── the host records one event per turn ──────────────────────────────────────────────
    sink.record(TurnEvent(route="EGO", elapsed_ms=1450, cost_usd=0.002, tool_calls=1,
                          stages=[StageSample("ner", tokens_in=120, tokens_out=40, elapsed_ms=210),
                                  StageSample("ego", tokens_in=300, tokens_out=90, elapsed_ms=900)]))
    sink.record(TurnEvent(route="SUPEREGO", stop_reason="pii_blocked", blocked=True))
    sink.record(TurnEvent(ok=False, error="MCPDispatchError", stop_reason="error"))

    # ── what /metrics would expose (a real host mounts this via attach(app)) ─────────────
    print(generate_latest(registry).decode())


if __name__ == "__main__":
    main()
