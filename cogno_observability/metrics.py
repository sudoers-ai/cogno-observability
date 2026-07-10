"""Prometheus metric definitions for the Cogno pipeline (business/SRE layer).

These live in a **separate Ops repo** (not cogno-host, not cogno-ui): the host stays
prometheus-agnostic and only emits a ``TurnEvent`` per turn to an injected ``MetricsSink``; this
module is where those events become time-series scraped into Grafana.

**Cardinality discipline (the cardinal rule of Prometheus):** every label here is a *bounded*
set — ``route`` (~4), ``stop_reason`` (~8), ``stage`` (~6), ``model`` (the deployment's catalog),
``direction`` (3), ``action`` (4), ``error`` (exception class names). High-cardinality identifiers
(``session_id`` / ``identity_id``) are **deliberately absent** — they belong in logs (Loki), never
as labels, or they blow up the TSDB. ``tenant`` is opt-in and capped (see :class:`sink` config)
because a large multi-tenant fleet is still unbounded.

Adapted from the parent monolith's ``cogno/core/metrics.py``, but fed off the decomposed host's
``TurnEvent`` DTO instead of reaching into a ``PipelineContext`` (whose field names differ).
"""

from __future__ import annotations

from typing import Any, Callable

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram


def _get_or_create(reg: CollectorRegistry, name: str, factory: Callable[[], Any]) -> Any:
    """Create a metric, or return the one already registered under ``name`` on ``reg``.

    prometheus_client raises ``ValueError: Duplicated timeseries`` if a name is registered twice on
    the same registry, so constructing a second ``Metrics``/``PrometheusMetricsSink`` on the default
    ``REGISTRY`` (gunicorn --preload + fork, an app factory reused across tests, a hot reload) would
    crash at boot. Reuse the existing collector instead — two sinks on one registry then share the
    same series, which is exactly right for a process-global registry."""
    try:
        return factory()
    except ValueError:
        existing = getattr(reg, "_names_to_collectors", {}).get(name)
        if existing is None:
            raise
        return existing


# Latency buckets (seconds) tuned for LLM turns: sub-second cache hits → multi-minute agent loops.
_TURN_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300)
_STAGE_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)
_DRIFT_BUCKETS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


class Metrics:
    """The Cogno metric set, bound to one registry (default: the global one).

    Held on an instance rather than module globals so tests get a fresh registry each time and a
    multiprocess deployment can pass its own. All names are prefixed ``cogno_`` and carry a HELP
    string (Grafana/alerts reference them by name)."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        # NB: prometheus_client treats ``registry=None`` as "register nowhere" (not "the default"),
        # so resolve None to the global REGISTRY explicitly.
        reg = registry if registry is not None else REGISTRY

        def counter(name: str, doc: str, labels: list[str] | None = None) -> Any:
            return _get_or_create(reg, name, lambda: Counter(
                name, doc, labels or [], registry=reg))

        def histogram(name: str, doc: str, buckets: tuple,
                      labels: list[str] | None = None) -> Any:
            return _get_or_create(reg, name, lambda: Histogram(
                name, doc, labels or [], buckets=buckets, registry=reg))

        # ── turn outcomes ────────────────────────────────────────────────────────────────
        self.turns_total = counter(
            "cogno_turns_total", "Pipeline turns by outcome",
            ["route", "stop_reason", "ok", "blocked", "cache_hit"])
        self.turn_errors_total = counter(
            "cogno_turn_errors_total", "Turns that raised an unhandled exception", ["error"])
        self.turn_duration_seconds = histogram(
            "cogno_turn_duration_seconds", "End-to-end turn latency", _TURN_BUCKETS, ["route"])

        # ── per-stage cost ───────────────────────────────────────────────────────────────
        self.stage_duration_seconds = histogram(
            "cogno_stage_duration_seconds", "Per cognitive-stage latency", _STAGE_BUCKETS, ["stage"])
        self.stage_tokens_total = counter(
            "cogno_stage_tokens_total", "Tokens consumed per stage/model",
            ["stage", "model", "direction"])  # direction: in|out|embedding
        self.cost_usd_total = counter(
            "cogno_cost_usd_total", "Provider cost in USD (0 for local models)")

        # ── quality / reliability signals ────────────────────────────────────────────────
        self.drift_score = histogram(
            "cogno_drift_score", "Cumulative drift score distribution", _DRIFT_BUCKETS)
        self.drift_actions_total = counter(
            "cogno_drift_actions_total", "Drift-triggered actions",
            ["action"])  # none|warn|ask_user|self_correct
        self.tool_calls_total = counter(
            "cogno_tool_calls_total", "EGO tool executions")
        self.tool_failures_total = counter(
            "cogno_tool_failures_total", "EGO tool executions that returned ok=False")
        self.failovers_total = counter(
            "cogno_failovers_total", "LLM backend failover events")
        self.self_corrections_total = counter(
            "cogno_self_corrections_total", "EGO↔SUPEREGO correction retries")
        self.handoffs_total = counter(
            "cogno_handoffs_total", "Turns escalated to a human handoff")
        self.blocked_total = counter(
            "cogno_blocked_total", "Turns blocked (safety/quota/PII)", ["stop_reason"])
