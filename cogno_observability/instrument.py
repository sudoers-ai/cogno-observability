"""``attach(app)`` — the host's ``instrument`` hook: mount ``/metrics`` + HTTP RED instrumentation.

Passed to ``create_pg_app(..., instrument=attach)``. The host calls it right after building the
FastAPI app (before routers), so it wraps every route. Keeping this here — not in the host — is
the whole point of the split: the host never imports ``prometheus_client``.

**Multiprocess (N uvicorn/gunicorn workers).** Each worker has its own in-process registry, so a
naive ``/metrics`` would report only the scraped worker's slice. When ``PROMETHEUS_MULTIPROC_DIR``
is set, prometheus_client writes counters to that shared dir and the exposition endpoint reconciles
across workers via ``MultiProcessCollector``. Set the env var and ensure the dir exists/empty at
boot (per the prometheus_client docs). Without it, single-process mode is used (dev / one worker).
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("cogno_observability.instrument")

_EXCLUDED = ("/metrics", "/health")


def attach(app: Any, *, metrics_path: str = "/metrics", instrument_http: bool = True) -> None:
    """Mount ``metrics_path`` on ``app`` and (optionally) add per-route HTTP RED metrics.

    Best-effort and idempotent-ish: a missing optional dep degrades gracefully (business metrics
    still work; only the HTTP layer is skipped)."""
    if instrument_http:
        try:
            from prometheus_fastapi_instrumentator import Instrumentator
            Instrumentator(
                should_group_status_codes=False,
                should_ignore_untemplated=True,
                excluded_handlers=list(_EXCLUDED),
            ).instrument(app)   # records default http_request_* to the global REGISTRY
            log.info("event=http_instrumented")
        except ImportError:
            log.warning("event=http_instrument_skipped reason=prometheus-fastapi-instrumentator "
                        "not installed (pip install 'cogno-observability[http]')")

    app.mount(metrics_path, _metrics_asgi_app())
    log.info("event=metrics_mounted path=%s multiprocess=%s",
             metrics_path, bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR")))


def _metrics_asgi_app() -> Any:
    """Build the ``/metrics`` ASGI app — a multiprocess-reconciling registry when configured,
    else the default global registry."""
    from prometheus_client import make_asgi_app
    mpdir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if mpdir:
        # prometheus_client requires the dir to EXIST and be emptied at process start (stale files
        # from a previous run would be summed into the exposition). We can't empty it here safely
        # (sibling workers may already be writing), but we can catch the common misconfiguration.
        if not os.path.isdir(mpdir):
            log.warning("event=multiproc_dir_missing dir=%s — /metrics will under-report; create it "
                        "and empty it at boot (per PROMETHEUS_MULTIPROC_DIR docs)", mpdir)
        from prometheus_client import CollectorRegistry, multiprocess
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return make_asgi_app(registry=registry)
    return make_asgi_app()
