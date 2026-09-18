"""``TurnEventLike`` declares EXACTLY what ``_record`` reads off the event — derived, not typed out.

WHY A TEST AND NOT A COMMENT. The Protocol sits in a **parameter** position
(``record(event: TurnEventLike)``), so every member it names is a REQUIREMENT on the host that
injects this sink — while ``_record`` reads every field through ``getattr(..., default)`` and needs
none of them. The two facts pull in opposite directions, and nothing on either side made the
disagreement visible: the sink kept working (the runtime path is duck-typed) and the host's type
checker was blind to this package for want of a ``py.typed`` marker.

It had drifted in BOTH directions at once when this test was written, on 2026-09-18:

* ``failover_count`` — declared and read, but the host CUT the field on 2026-09-01 (cogno-host
  #605: a reader with no writer since birth). ``PrometheusMetricsSink`` stopped satisfying the
  host's ``MetricsSink`` protocol that day. Fixed by dropping both the member and the read.
* ``total_tokens`` — declared, never read.
* ``grounding_rule`` / ``grounding_repaired`` / ``provenance_refusals`` — read, never declared.

So the invariant is an EQUALITY, not an inclusion: a member nobody reads is a requirement with no
purpose, and a read nobody declared is a requirement the next reader will not know to keep.

The reads are taken from the AST, never from a hand-written list — a list here would be a third
copy of the same contract, and the third copy is the one that rots.
"""

from __future__ import annotations

import ast
import pathlib

from cogno_observability.sink import TurnEventLike

_SINK = pathlib.Path(__file__).resolve().parent.parent / "cogno_observability" / "sink.py"


def _record_function() -> ast.FunctionDef:
    tree = ast.parse(_SINK.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_record":
            return node
    raise AssertionError("PrometheusMetricsSink._record not found — this test measures nothing")


def _event_reads() -> "tuple[set[str], set[str]]":
    """``(names read off the event, names read WITHOUT a default)``.

    The event parameter is identified by POSITION (the second argument of the method), so renaming
    it does not quietly empty this set. The loop over ``stages`` reads off a different variable and
    is excluded by the same rule — those are the stage sample's fields, not the event's.
    """
    fn = _record_function()
    event_param = fn.args.args[1].arg
    read: "set[str]" = set()
    no_default: "set[str]" = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"):
            continue
        target, attr = node.args[0], node.args[1]
        if not (isinstance(target, ast.Name) and target.id == event_param):
            continue
        assert isinstance(attr, ast.Constant) and isinstance(attr.value, str), \
            "a computed attribute name would make this contract underivable"
        read.add(attr.value)
        if len(node.args) < 3:
            no_default.add(attr.value)
    return read, no_default


def test_the_protocol_declares_exactly_what_the_sink_reads():
    declared = set(TurnEventLike.__annotations__)
    read, _ = _event_reads()
    assert read, "no getattr reads found — the derivation broke, not the contract"
    assert declared - read == set(), (
        "declared but never read — a requirement on every host, for nothing: "
        f"{sorted(declared - read)}")
    assert read - declared == set(), (
        "read but never declared — the next host to drop one of these breaks the sink silently: "
        f"{sorted(read - declared)}")


def test_every_read_still_tolerates_an_absent_field():
    """The defaults are what make a host that lags one field DEGRADE instead of crashing.

    This is the property that decides WHICH SIDE is wrong when the two disagree: the sink cannot
    crash on a missing attribute, so a member declared here is never load-bearing at runtime — it
    is a promise about the host, and a promise the host has stopped keeping must be withdrawn
    here, not worked around there.
    """
    _, no_default = _event_reads()
    assert no_default == set(), (
        f"these reads would raise on a host that does not carry the field: {sorted(no_default)}")


def test_failover_count_stays_gone_until_something_writes_it():
    """The named regression, kept by name because the field is tempting to re-add from the docs.

    ``cogno_failovers_total`` could only ever report 0 — nothing in host/anima/soma/synapse ever
    stamped ``failover_count``. A permanently-zero counter does not read as "unmeasured", it reads
    as "none happened", which is a false statement about the system. Re-add the read AND the
    counter together with a writer, never one of the three alone.
    """
    from cogno_observability.metrics import Metrics
    from prometheus_client import CollectorRegistry

    assert "failover_count" not in TurnEventLike.__annotations__
    assert not hasattr(Metrics(CollectorRegistry()), "failovers_total")
