"""attach(app) mounts a working /metrics endpoint and instruments HTTP routes."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cogno_observability import PrometheusMetricsSink, attach


def _app() -> FastAPI:
    app = FastAPI()
    attach(app)                       # mounts /metrics + HTTP instrumentation (before routes)

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    return app


def test_metrics_endpoint_exposes_series():
    app = _app()
    # record a business metric onto the global registry the endpoint scrapes
    from types import SimpleNamespace
    PrometheusMetricsSink().record(SimpleNamespace(
        route="EGO", stop_reason="completed", ok=True, blocked=False, cache_hit=False,
        elapsed_ms=500, stages=[], cost_usd=0.0, drift_cumulative=0.0, drift_action="",
        tool_calls=0, tool_failures=0, failover_count=0, correction_count=0, handoff=False,
        error="", tenant_id="acme", total_tokens=0))
    client = TestClient(app)
    client.get("/ping")               # generate an HTTP-layer sample
    body = client.get("/metrics").text
    assert "cogno_turns_total" in body                 # business metric
    assert "http_request" in body or "http_requests" in body   # HTTP RED (instrumentator)


def test_metrics_endpoint_ok_without_http_extra():
    # instrument_http=False must still serve /metrics (business metrics only)
    app = FastAPI()
    attach(app, instrument_http=False)
    body = TestClient(app).get("/metrics").text
    assert "python_info" in body or "cogno_" in body   # a valid exposition page
