"""``PrometheusMetricsSink`` — translate a host ``TurnEvent`` into Prometheus time-series.

This is the concrete implementation of the host's ``MetricsSink`` protocol (``record(event)``).
It is **duck-typed** against the event — it reads attributes off it and never imports
``cogno_host``, so the Ops layer stays decoupled from the product repo (any host emitting the same
``TurnEvent`` shape is observable). The host injects it:

    from cogno_observability import PrometheusMetricsSink, attach
    app = create_pg_app(dsn, auth=admin,
                        metrics_sink=PrometheusMetricsSink(), instrument=attach)
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable

from prometheus_client import CollectorRegistry

from cogno_observability.metrics import Metrics, _get_or_create

log = logging.getLogger("cogno_observability.sink")


@runtime_checkable
class TurnEventLike(Protocol):
    """EXACTLY the event attributes :meth:`PrometheusMetricsSink._record` reads — no more, no less.

    It is not decoration: it sits in a **parameter** position (``record(event: TurnEventLike)``),
    which makes it a REQUIREMENT on every host that wants to inject this sink. A member declared
    here that the host does not carry makes ``PrometheusMetricsSink`` stop satisfying the host's
    own ``MetricsSink`` protocol — the sink is refused by the type checker while working perfectly
    at runtime, because ``_record`` reads through ``getattr(..., default)``.

    That is not hypothetical. ``failover_count`` was declared here and the host CUT the field on
    2026-09-01 (cogno-host #605, "a reader with no writer since birth"): nothing in the stack ever
    stamped it, so the series could only ever be 0, and a flat zero READS as "no failover
    happened" — a false statement, worse than absence. The reasoning applies verbatim on this side
    of the seam, so the read and ``cogno_failovers_total`` left with it. Re-add both WITH a writer,
    never before.

    The list is kept honest by ``tests/test_protocol_matches_the_reads.py``, which derives the
    reads from this module's AST: declaring a member nobody reads, or reading a field nobody
    declared, fails. Before that test the drift ran BOTH ways at once — ``total_tokens`` was
    declared and never read, while ``grounding_rule``/``grounding_repaired``/
    ``provenance_refusals`` were read and never declared.
    """

    tenant_id: str
    route: str
    stop_reason: str
    elapsed_ms: float
    cache_hit: bool
    blocked: bool
    ok: bool
    error: str
    cost_usd: float
    drift_cumulative: float
    drift_action: str
    tool_calls: int
    tool_failures: int
    correction_count: int
    handoff: bool
    grounding_rule: str
    grounding_repaired: bool
    provenance_refusals: int
    stages: list


def _b(v: Any) -> str:
    """Prometheus label values must be strings — normalize a bool to a stable lowercase token."""
    return "true" if bool(v) else "false"


class PrometheusMetricsSink:
    """A ``MetricsSink`` that records each turn to Prometheus counters/histograms.

    ``record`` is **best-effort**: monitoring must never break a turn, so any error while recording
    is swallowed with a warning (the parent did the same). Set ``tenant_label=True`` ONLY on a
    small/bounded fleet — it adds a ``tenant`` label to the turn counter (unbounded tenants would
    explode cardinality; off by default)."""

    def __init__(self, *, registry: Optional[CollectorRegistry] = None,
                 tenant_label: bool = False) -> None:
        self._m = Metrics(registry)
        self._tenant_label = tenant_label
        if tenant_label:
            # A separate, opt-in counter carrying the tenant dimension (kept off the main
            # counters so their cardinality stays flat). ``registry=None`` → the global REGISTRY;
            # get-or-create so a second sink on that registry reuses it instead of crashing.
            from prometheus_client import REGISTRY, Counter
            reg = registry if registry is not None else REGISTRY
            self._turns_by_tenant = _get_or_create(
                reg, "cogno_turns_by_tenant_total", lambda: Counter(
                    "cogno_turns_by_tenant_total", "Turns per tenant (bounded fleets only)",
                    ["tenant", "ok"], registry=reg))

    def record(self, event: TurnEventLike) -> None:
        try:
            self._record(event)
        except Exception as exc:  # noqa: BLE001 — observability must not break the turn
            log.warning("event=metrics_record_failed error=%s", exc)

    def _record(self, e: Any) -> None:
        m = self._m
        route = getattr(e, "route", "") or "none"
        stop_reason = getattr(e, "stop_reason", "") or "completed"
        ok = getattr(e, "ok", True)
        blocked = getattr(e, "blocked", False)
        cache_hit = getattr(e, "cache_hit", False)

        m.turns_total.labels(route=route, stop_reason=stop_reason, ok=_b(ok),
                             blocked=_b(blocked), cache_hit=_b(cache_hit)).inc()
        if self._tenant_label:
            self._turns_by_tenant.labels(tenant=getattr(e, "tenant_id", "") or "unknown",
                                         ok=_b(ok)).inc()

        if not ok:
            m.turn_errors_total.labels(error=getattr(e, "error", "") or "unknown").inc()
        if blocked:
            m.blocked_total.labels(stop_reason=stop_reason).inc()

        elapsed_ms = float(getattr(e, "elapsed_ms", 0.0) or 0.0)
        if elapsed_ms > 0:
            m.turn_duration_seconds.labels(route=route).observe(elapsed_ms / 1000.0)

        # ── per-stage breakdown ──────────────────────────────────────────────────────────
        for s in getattr(e, "stages", None) or []:
            stage = getattr(s, "stage", "") or "unknown"
            model = getattr(s, "model", "") or "unknown"
            s_ms = float(getattr(s, "elapsed_ms", 0.0) or 0.0)
            if s_ms > 0:
                m.stage_duration_seconds.labels(stage=stage).observe(s_ms / 1000.0)
            tin = int(getattr(s, "tokens_in", 0) or 0)
            tout = int(getattr(s, "tokens_out", 0) or 0)
            temb = int(getattr(s, "embedding_tokens", 0) or 0)
            if tin:
                m.stage_tokens_total.labels(stage=stage, model=model, direction="in").inc(tin)
            if tout:
                m.stage_tokens_total.labels(stage=stage, model=model, direction="out").inc(tout)
            if temb:
                m.stage_tokens_total.labels(stage=stage, model=model,
                                            direction="embedding").inc(temb)

        cost = float(getattr(e, "cost_usd", 0.0) or 0.0)
        if cost > 0:
            m.cost_usd_total.inc(cost)

        # ── quality / reliability ────────────────────────────────────────────────────────
        drift = float(getattr(e, "drift_cumulative", 0.0) or 0.0)
        if drift > 0:
            m.drift_score.observe(drift)
        action = getattr(e, "drift_action", "") or ""
        if action:
            m.drift_actions_total.labels(action=action).inc()

        tool_calls = int(getattr(e, "tool_calls", 0) or 0)
        if tool_calls:
            m.tool_calls_total.inc(tool_calls)
        tool_failures = int(getattr(e, "tool_failures", 0) or 0)
        if tool_failures:
            m.tool_failures_total.inc(tool_failures)
        corrections = int(getattr(e, "correction_count", 0) or 0)
        if corrections:
            m.self_corrections_total.inc(corrections)
        if getattr(e, "handoff", False):
            m.handoffs_total.inc()
        # protection nets (see cogno-host grounding.py / provenance.py)
        grounding_rule = getattr(e, "grounding_rule", "") or ""
        if grounding_rule:
            m.grounding_rewrites_total.labels(
                rule=grounding_rule,
                repaired=_b(getattr(e, "grounding_repaired", False))).inc()
        provenance_refusals = int(getattr(e, "provenance_refusals", 0) or 0)
        if provenance_refusals:
            m.provenance_refusals_total.inc(provenance_refusals)
