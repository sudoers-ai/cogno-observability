"""cogno-observability — SRE/Ops monitoring for the Cogno stack.

The **operator-facing** half of observability (cross-tenant fleet health), separate from the
**tenant-facing** Radar Tokens in cogno-ui and from billing. The host stays prometheus-agnostic
and exposes two seams; this package fills them:

* :class:`PrometheusMetricsSink` — implements the host's ``MetricsSink`` (``record(TurnEvent)``),
  turning each turn into Prometheus time-series.
* :func:`attach` — the ``instrument(app)`` hook that mounts ``/metrics`` + HTTP RED metrics.

    from cogno_observability import PrometheusMetricsSink, attach
    app = create_pg_app(dsn, auth=admin, metrics_sink=PrometheusMetricsSink(), instrument=attach)

Grafana dashboards + Prometheus alert/recording rules live under ``ops/``.
"""

from cogno_observability.instrument import attach
from cogno_observability.metrics import Metrics
from cogno_observability.sink import PrometheusMetricsSink, TurnEventLike

__all__ = ["PrometheusMetricsSink", "attach", "Metrics", "TurnEventLike"]
__version__ = "0.1.0"
