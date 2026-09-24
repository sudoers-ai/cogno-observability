"""``OTelTraceSink`` / ``plan_spans``: one turn → the expected GenAI span tree, with the ledger's tokens.

The in-memory exporter is the only exporter used here: nothing leaves the process.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry.trace import SpanKind, StatusCode

from cogno_observability import OTelTraceSink, plan_spans
from cogno_observability.tracing import OTHER, UNKNOWN_PROVIDER

from tracing_fixtures import NOW_NS, Event, Stage, Tool, in_memory_provider, ledger_rows, \
    ordinary_turn

EMBEDDER = "openai:text-embedding-3-small"


def _emit(event, **kw):
    provider, exporter = in_memory_provider()
    OTelTraceSink(provider, embedding_model=EMBEDDER, clock=lambda: NOW_NS, **kw).record(event)
    return exporter.get_finished_spans()


# ── the tree ──────────────────────────────────────────────────────────────────────────────────
def test_one_turn_is_one_invoke_agent_span_with_every_child_under_it():
    spans = _emit(ordinary_turn())
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 1
    root = roots[0]
    assert root.name == "invoke_agent" and root.kind is SpanKind.INTERNAL
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    children = [s for s in spans if s.parent is not None]
    assert children, "the turn produced no child span: this test measured nothing"
    assert {c.parent.span_id for c in children} == {root.context.span_id}
    assert {c.context.trace_id for c in spans} == {root.context.trace_id}


def test_names_and_kinds_follow_the_genai_conventions():
    spans = _emit(ordinary_turn())
    by_op: "dict[str, list]" = {}
    for s in spans:
        by_op.setdefault(s.attributes["gen_ai.operation.name"], []).append(s)
    assert {s.name for s in by_op["chat"]} == {"chat gpt-4o-mini"}
    assert all(s.kind is SpanKind.CLIENT for s in by_op["chat"] + by_op["embeddings"])
    assert {s.name for s in by_op["embeddings"]} == {"embeddings text-embedding-3-small"}
    assert [s.name for s in by_op["execute_tool"]] == ["execute_tool resolve_date",
                                                       "execute_tool consult_material"]
    assert all(s.kind is SpanKind.INTERNAL for s in by_op["execute_tool"])
    assert all(s.attributes["gen_ai.tool.type"] == "function" for s in by_op["execute_tool"])


def test_one_span_per_ledger_row_with_the_ledgers_tokens_and_cost():
    """The ledger twin: every model span is exactly one row of the host ledger's split, in order,
    with the same tokens and the same cost. The EGO's two attempts are two rows and two spans."""
    event = ordinary_turn()
    rows = ledger_rows(event)
    model_spans = [p for p in plan_spans(event, embedding_model=EMBEDDER, now_ns=NOW_NS)
                   if p.kind in ("chat", "embeddings")]
    assert len(rows) == 8 and len(model_spans) == len(rows)
    for (stage, modality, tin, tout, cost), span in zip(rows, model_spans):
        a = span.attributes
        assert a["cogno.stage"] == stage
        assert span.kind == ("chat" if modality == "llm" else "embeddings")
        assert a["gen_ai.usage.input_tokens"] == tin
        assert a.get("gen_ai.usage.output_tokens", 0) == tout
        assert a.get("cogno.usage.cost_usd") == cost
    assert sum(p.attributes["gen_ai.usage.input_tokens"] for p in model_spans) \
        == sum(r[2] for r in rows) == 10542
    assert sum(p.attributes.get("gen_ai.usage.output_tokens", 0) for p in model_spans) \
        == sum(r[3] for r in rows) == 540
    assert sum(p.attributes["cogno.usage.cost_usd"] for p in model_spans) \
        == pytest.approx(sum(r[4] for r in rows), abs=1e-12)


def test_the_ego_attempts_stay_apart_and_cached_tokens_are_a_subset():
    spans = [p for p in plan_spans(ordinary_turn(), now_ns=NOW_NS)
             if p.attributes.get("cogno.stage") == "ego"]
    assert [p.attributes["cogno.attempt"] for p in spans] == [1, 2]
    assert [p.attributes["gen_ai.usage.cache_read.input_tokens"] for p in spans] == [2048, 2432]
    for p in spans:
        assert p.attributes["gen_ai.usage.cache_read.input_tokens"] \
            <= p.attributes["gen_ai.usage.input_tokens"]


def test_cached_zero_and_attempt_zero_are_omitted_not_sent_as_facts():
    """0 means "unknown or none" on the host. Sending 0 would claim "none"."""
    (turn, chat) = plan_spans(Event(stages=[Stage("ner", model="m", tokens_in=5)]),
                              now_ns=NOW_NS)
    assert "gen_ai.usage.cache_read.input_tokens" not in chat.attributes
    assert "cogno.attempt" not in chat.attributes
    assert "cogno.usage.cost_usd" not in chat.attributes


def test_a_stage_with_no_tokens_is_not_a_model_call():
    plan = plan_spans(Event(stages=[Stage("id", model="heuristic", elapsed_ms=3.0)]),
                      now_ns=NOW_NS)
    assert [p.kind for p in plan] == ["turn"]


# ── provider and model ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("provider,model,want_provider,want_model", [
    ("openai", "gpt-4o-mini", "openai", "gpt-4o-mini"),
    ("", "openai:gpt-4o-mini", "openai", "gpt-4o-mini"),
    ("", "gemini:gemini-2.0-flash", "gcp.gemini", "gemini-2.0-flash"),
    ("bedrock", "us.anthropic.claude-3-haiku-20240307-v1:0", "aws.bedrock",
     "us.anthropic.claude-3-haiku-20240307-v1:0"),
    ("", "qwen3:8b", UNKNOWN_PROVIDER, "qwen3:8b"),            # a bare Ollama name says nothing
    ("", "mistral:latest", UNKNOWN_PROVIDER, "mistral:latest"),  # an Ollama model, not a provider
    ("ollama", "qwen3:8b", "ollama", "qwen3:8b"),
])
def test_provider_is_what_the_host_said_or_a_known_prefix_or_unknown(
        provider, model, want_provider, want_model):
    (_, chat) = plan_spans(Event(stages=[Stage("ner", model=model, provider=provider,
                                               tokens_in=1)]), now_ns=NOW_NS)
    assert chat.attributes["gen_ai.provider.name"] == want_provider
    assert chat.attributes["gen_ai.request.model"] == want_model


def test_served_model_is_the_response_model():
    plan = plan_spans(ordinary_turn(), now_ns=NOW_NS)
    voice = [p for p in plan if p.attributes.get("cogno.stage") == "superego_voice"][0]
    assert voice.attributes["gen_ai.response.model"] == "gpt-4o-mini-2024-07-18"
    assert voice.attributes["gen_ai.request.model"] == "gpt-4o-mini"


def test_an_embedder_the_host_did_not_name_is_an_embeddings_span_without_a_model():
    (_, emb) = plan_spans(Event(stages=[Stage("id", embedding_tokens=7)]), now_ns=NOW_NS)
    assert emb.name == "embeddings"
    assert "gen_ai.request.model" not in emb.attributes
    assert emb.attributes["gen_ai.provider.name"] == UNKNOWN_PROVIDER


# ── times ─────────────────────────────────────────────────────────────────────────────────────
def test_the_turn_ends_at_the_record_instant_and_lasts_elapsed_ms():
    (turn,) = plan_spans(Event(elapsed_ms=2000.0), now_ns=NOW_NS)
    assert (turn.start_ns, turn.end_ns) == (NOW_NS - 2_000_000_000, NOW_NS)
    assert turn.attributes["cogno.timing"] == "anchored"


def test_an_early_exit_with_no_duration_is_an_unmeasured_turn_not_a_measured_zero():
    """The host's early exits (a disabled tenant, a blocked input) call its ``_done`` with no
    ``elapsed``, so ``elapsed_ms`` is 0 there."""
    (turn,) = plan_spans(Event(blocked=True, stop_reason="identity_disabled"), now_ns=NOW_NS)
    assert turn.start_ns == turn.end_ns == NOW_NS
    assert turn.attributes["cogno.timing"] == "unmeasured"


def test_a_turn_with_started_at_is_observed():
    (turn,) = plan_spans(Event(elapsed_ms=1500.0, started_at=1_600_000_000.25), now_ns=NOW_NS)
    assert turn.start_ns == 1_600_000_000_250_000_000
    assert turn.end_ns == turn.start_ns + 1_500_000_000
    assert turn.attributes["cogno.timing"] == "observed"


def test_children_are_anchored_at_the_turn_start_never_given_an_invented_position():
    plan = plan_spans(ordinary_turn(), embedding_model=EMBEDDER, now_ns=NOW_NS)
    turn, children = plan[0], plan[1:]
    ner = [p for p in children if p.attributes.get("cogno.stage") == "ner"][0]
    assert ner.start_ns == turn.start_ns
    assert ner.end_ns - ner.start_ns == 820_000_000
    assert ner.attributes["cogno.timing"] == "anchored"


def test_the_second_span_of_a_sample_is_unmeasured_its_one_duration_went_to_the_first():
    plan = plan_spans(ordinary_turn(), embedding_model=EMBEDDER, now_ns=NOW_NS)
    noumeno = [p for p in plan if p.attributes.get("cogno.stage") == "noumeno"]
    assert [p.kind for p in noumeno] == ["chat", "embeddings"]
    chat, emb = noumeno
    assert chat.end_ns - chat.start_ns == 610_000_000
    assert chat.attributes["cogno.timing"] == "anchored"
    assert emb.end_ns == emb.start_ns and emb.attributes["cogno.timing"] == "unmeasured"
    # an embedding-only stage keeps its own duration
    ident = [p for p in plan if p.attributes.get("cogno.stage") == "id"][0]
    assert ident.end_ns - ident.start_ns == 40_000_000
    assert ident.attributes["cogno.timing"] == "anchored"


def test_a_host_instant_makes_a_child_observed():
    stage = Stage("ner", model="m", tokens_in=5, elapsed_ms=100.0, started_at=1_600_000_001.0)
    tool = Tool("resolve_date", elapsed_ms=12.0, started_at=1_600_000_002.0)
    (_, chat, call) = plan_spans(Event(stages=[stage], tools=[tool]), now_ns=NOW_NS)
    assert (chat.start_ns, chat.end_ns) == (1_600_000_001_000_000_000, 1_600_000_001_100_000_000)
    assert chat.attributes["cogno.timing"] == "observed"
    assert call.end_ns - call.start_ns == 12_000_000
    assert call.attributes["cogno.timing"] == "observed"


def test_a_tool_nobody_timed_is_unmeasured_and_zero_length():
    (turn, call) = plan_spans(Event(elapsed_ms=1000.0, tools=[Tool("resolve_date")]),
                              now_ns=NOW_NS)
    assert call.start_ns == call.end_ns == turn.start_ns
    assert call.attributes["cogno.timing"] == "unmeasured"


def test_exported_spans_carry_the_planned_intervals():
    spans = _emit(ordinary_turn())
    plan = plan_spans(ordinary_turn(), embedding_model=EMBEDDER, now_ns=NOW_NS)
    assert sorted((s.name, s.start_time, s.end_time) for s in spans) \
        == sorted((p.name, p.start_ns, p.end_ns) for p in plan)


# ── outcomes ──────────────────────────────────────────────────────────────────────────────────
def test_a_failed_turn_is_an_error_span_with_the_exception_class():
    """The host's failure path records ``TurnEvent(ok=False, error=<class>, stop_reason=error)``
    with no stages. It must be visible, not an absence."""
    spans = _emit(Event(ok=False, error="ReadTimeout", stop_reason="error", elapsed_ms=120000.0))
    (turn,) = spans
    assert turn.status.status_code is StatusCode.ERROR
    assert turn.status.description is None
    assert turn.attributes["error.type"] == "ReadTimeout"
    assert turn.attributes["cogno.turn.ok"] is False
    assert turn.attributes["cogno.turn.stop_reason"] == "error"


def test_an_error_that_is_not_a_class_name_is_sent_as_other():
    (turn,) = plan_spans(Event(ok=False, error="could not reach the server"), now_ns=NOW_NS)
    assert turn.attributes["error.type"] == OTHER


def test_an_ok_turn_has_no_error_type_and_an_unset_status():
    spans = _emit(ordinary_turn())
    root = [s for s in spans if s.parent is None][0]
    assert "error.type" not in root.attributes
    assert root.status.status_code is StatusCode.UNSET


def test_a_failed_tool_is_an_error_span_with_the_fallback_error_type():
    spans = _emit(Event(tools=[Tool("book_slot", ok=False, error="slot taken")]))
    (call,) = [s for s in spans if s.parent is not None]
    assert call.status.status_code is StatusCode.ERROR
    assert call.attributes["error.type"] == OTHER


def test_turn_outcome_codes_ride_on_the_turn_span():
    (turn,) = plan_spans(Event(route="SUPEREGO", stop_reason="input_blocked:crisis",
                               blocked=True, cache_hit=False, handoff=True, correction_count=2,
                               cost_usd=0.0031), now_ns=NOW_NS)
    a = turn.attributes
    assert a["cogno.turn.route"] == "SUPEREGO"
    assert a["cogno.turn.stop_reason"] == "input_blocked:crisis"
    assert (a["cogno.turn.blocked"], a["cogno.turn.cache_hit"], a["cogno.turn.handoff"]) \
        == (True, False, True)
    assert a["cogno.turn.correction_count"] == 2
    assert a["cogno.turn.cost_usd"] == 0.0031
    assert a["cogno.tenant.id"] == "acme"


def test_the_turn_cost_is_a_different_key_from_the_row_cost():
    """So adding ``cogno.usage.cost_usd`` over every span gives the ledger's total, and the
    turn's own total is not counted twice into it."""
    plan = plan_spans(ordinary_turn(), now_ns=NOW_NS)
    assert "cogno.usage.cost_usd" not in plan[0].attributes
    assert all("cogno.turn.cost_usd" not in p.attributes for p in plan[1:])


# ── best-effort ───────────────────────────────────────────────────────────────────────────────
def test_record_never_raises_and_logs_only_the_exception_class(caplog):
    class Exploding:
        @property
        def stages(self):
            raise RuntimeError("ana.teste@example.com is in this message")

    provider, exporter = in_memory_provider()
    sink = OTelTraceSink(provider, clock=lambda: NOW_NS)
    with caplog.at_level(logging.WARNING, logger="cogno_observability.tracing"):
        sink.record(Exploding())
    assert "event=trace_record_failed error=RuntimeError" in caplog.text
    assert "example.com" not in caplog.text
    assert exporter.get_finished_spans() == ()


def test_the_turn_span_is_ended_even_when_a_child_fails(monkeypatch):
    provider, exporter = in_memory_provider()
    sink = OTelTraceSink(provider, clock=lambda: NOW_NS)
    real = sink._tracer.start_span
    calls = {"n": 0}

    def flaky(name, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("child failed")
        return real(name, **kw)

    monkeypatch.setattr(sink._tracer, "start_span", flaky)
    sink.record(ordinary_turn())
    assert calls["n"] == 2, "the child path was never reached: this test measured nothing"
    assert [s.name for s in exporter.get_finished_spans()] == ["invoke_agent"]
