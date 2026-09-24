"""Off by default, and free when off: nothing from ``opentelemetry`` is imported until a sink is BUILT.

The exporter is optional (the ``[otel]`` extra) and this library adds no hard dependency. So a
host that never builds an :class:`OTelTraceSink` must not import the OpenTelemetry API or SDK.
That includes importing this package, using the Prometheus sink, and planning a turn's spans.

Measured in a FRESH interpreter, because the test process has already imported the SDK for the
other tests and its ``sys.modules`` would answer nothing.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent

_PROBE = r"""
import json, sys
from dataclasses import dataclass, field

import cogno_observability
from cogno_observability import PrometheusMetricsSink, plan_spans
from prometheus_client import CollectorRegistry


@dataclass
class Stage:
    stage: str = "ner"
    model: str = "gpt-4o-mini"
    tokens_in: int = 10
    tokens_out: int = 2


@dataclass
class Event:
    route: str = "EGO"
    stop_reason: str = "completed"
    elapsed_ms: float = 100.0
    stages: list = field(default_factory=lambda: [Stage()])


PrometheusMetricsSink(registry=CollectorRegistry()).record(Event())
plan = plan_spans(Event(), now_ns=1)
before_build = sorted(m for m in sys.modules if m.split(".")[0] == "opentelemetry")
if sys.argv[1] == "build":
    from opentelemetry.sdk.trace import TracerProvider
    cogno_observability.OTelTraceSink(TracerProvider())
after_build = sorted(m for m in sys.modules if m.split(".")[0] == "opentelemetry")
print(json.dumps({"planned": len(plan), "before": before_build, "after": after_build}))
"""


def _probe(mode: str) -> dict:
    out = subprocess.run([sys.executable, "-c", _PROBE, mode], cwd=_ROOT,
                         capture_output=True, text=True, timeout=120,
                         env={"PYTHONPATH": str(_ROOT), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_importing_recording_and_planning_import_no_opentelemetry():
    got = _probe("off")
    assert got["planned"] == 2, "the probe planned nothing: this test measured nothing"
    assert got["before"] == [] and got["after"] == []


def test_the_control_building_a_sink_does_import_it():
    """Control: the probe can see an OpenTelemetry import when there is one. Without this, an
    empty list above could mean the probe was blind."""
    got = _probe("build")
    assert got["before"] == []
    assert "opentelemetry.trace" in got["after"]
