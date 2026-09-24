"""The PII guard: no content, and nothing that identifies the contact, reaches any span.

The spans are metadata only until the owner decides how long content may be kept (LGPD). This
file fills EVERY field that could carry content or identify a contact with invented personal data:
the session and identity (an ``identity_id`` shaped like a WhatsApp phone number, which is what it
is on that channel), tool arguments, results and error text, an exception message, and a model,
stage and tool NAME that are really an e-mail address or a phone number. It then reads back
everything a span can carry: the attributes (keys and values), the name, the status description,
the events and the links.

Three properties, each asserted on the exported spans and not on a description of them:

1. every emitted key is a row of :data:`SPAN_ATTRIBUTES` (the allowlist is DERIVED from the
   table, never typed here);
2. none of the spec's Opt-In content attributes, and none that identify the conversation or the
   agent, is emitted;
3. no planted value, and no fragment of one, appears anywhere.
"""

from __future__ import annotations

import pytest

from cogno_observability import ALLOWED_ATTRIBUTE_KEYS, SPAN_ATTRIBUTES, OTelTraceSink

from tracing_fixtures import NOW_NS, Event, Stage, Tool, in_memory_provider

#: The phone number the contact's ``identity_id`` IS on WhatsApp (invented).
PHONE = "5511987654321"
EMAIL = "ana.teste@example.com"
#: Every planted value, and the fragments of them that must not leak either.
PLANTED = (PHONE, "98765-4321", "+55 11", EMAIL, "example.com", "Ana Teste", "Rua das Flores",
           "R$ 1.250,00", "123.456.789-09")

#: Content and identity attributes of the GenAI conventions (SEMCONV_REF), written out BY NAME
#: from the spec: the Opt-In content attributes, the ones the spec flags as sensitive, and the
#: identifiers of the conversation and of the agent. This is the one list typed out in this
#: file, because it is the spec's list and not ours. It checks that the table never gains a row
#: for any of them.
SPEC_CONTENT_OR_IDENTITY = frozenset({
    "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
    "gen_ai.tool.definitions", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result",
    "gen_ai.tool.description", "gen_ai.prompt.variable", "gen_ai.memory.query.text",
    "gen_ai.memory.records", "gen_ai.conversation.id", "gen_ai.agent.name",
    "gen_ai.agent.description", "gen_ai.agent.id", "gen_ai.tool.call.id", "gen_ai.response.id",
    "gen_ai.request.previous_response.id", "gen_ai.data_source.id",
})


def _laden_turn() -> Event:
    return Event(
        tenant_id=PHONE,                                  # a tenant id with a phone's shape
        session_id=f"whatsapp:{PHONE}", identity_id=PHONE,
        route="EGO", stop_reason="completed", ok=False,
        error=f"ValueError: could not notify {EMAIL} at +55 11 98765-4321",
        elapsed_ms=900.0, cost_usd=0.001,
        stages=[
            Stage("ner", model="gpt-4o-mini", provider="openai", tokens_in=10, tokens_out=2),
            Stage(EMAIL, model=EMAIL, provider=PHONE, tokens_in=3, tokens_out=1,
                  served_model="Ana Teste"),
            Stage(PHONE, model=f"+55 11 {PHONE}", embedding_tokens=4),
            # a phone behind a letter prefix: a token by shape, refused by the digit-run rule
            Stage("ner", model=f"wa:{PHONE}", provider=f"wa:{PHONE}", tokens_in=1),
        ],
        tools=[
            Tool("notify_user", ok=True,
                 arguments={"to": EMAIL, "phone": PHONE,
                            "text": "Olá Ana Teste, o pagamento de R$ 1.250,00 foi confirmado"},
                 result=f"sent to {EMAIL}; CPF 123.456.789-09; Rua das Flores, 10"),
            Tool(f"send to {EMAIL}", ok=False, error=f"{PHONE} rejected"),
            Tool(PHONE, ok=False, error="Rua das Flores"),
            Tool(f"wa:{PHONE}", ok=True),
        ],
    )


@pytest.fixture
def spans():
    provider, exporter = in_memory_provider()
    OTelTraceSink(provider, embedding_model="openai:text-embedding-3-small",
                  clock=lambda: NOW_NS).record(_laden_turn())
    out = exporter.get_finished_spans()
    # Prove the condition happened before reading the verdict: a turn with every kind of span,
    # the bad names included. An empty export would pass everything below.
    ops = sorted(s.attributes["gen_ai.operation.name"] for s in out)
    assert ops == ["chat", "chat", "chat", "embeddings", "execute_tool", "execute_tool",
                   "execute_tool", "execute_tool", "invoke_agent"], ops
    return out


def _everything_a_span_carries(span) -> "list[str]":
    texts = [span.name, str(span.status.description or "")]
    for key, value in (span.attributes or {}).items():
        texts += [key, str(value)]
    for ev in span.events:
        texts += [ev.name, str(dict(ev.attributes or {}))]
    for link in span.links:
        texts.append(str(dict(link.attributes or {})))
    return texts


def test_every_emitted_key_is_a_row_of_the_one_table(spans):
    assert ALLOWED_ATTRIBUTE_KEYS == {a.key for a in SPAN_ATTRIBUTES}
    emitted = {k for s in spans for k in (s.attributes or {})}
    assert emitted, "no attribute emitted: this test measured nothing"
    assert emitted <= ALLOWED_ATTRIBUTE_KEYS, sorted(emitted - ALLOWED_ATTRIBUTE_KEYS)


def test_no_content_or_identity_attribute_is_emitted_and_the_table_has_none(spans):
    emitted = {k for s in spans for k in (s.attributes or {})}
    assert emitted & SPEC_CONTENT_OR_IDENTITY == set()
    assert ALLOWED_ATTRIBUTE_KEYS & SPEC_CONTENT_OR_IDENTITY == set()


def test_no_planted_value_reaches_any_span(spans):
    leaks = [(s.name, frag) for s in spans for text in _everything_a_span_carries(s)
             for frag in PLANTED if frag in text]
    assert leaks == []


def test_the_contacts_phone_shaped_identity_never_appears(spans):
    """The consultant's twin: on WhatsApp the ``identity_id`` IS the phone number. It must not
    appear in any emitted attribute, and here it was planted into the tenant id, a stage, a
    provider, a model and a tool name, besides the identity and session themselves."""
    assert not any(PHONE in text for s in spans for text in _everything_a_span_carries(s))
    root = [s for s in spans if s.parent is None][0]
    assert "cogno.tenant.id" not in root.attributes      # digits only → omitted, not _OTHER


def test_the_bad_names_became_the_fallback_not_a_guess(spans):
    names = sorted(s.name for s in spans)
    assert "execute_tool _OTHER" in names and "chat _OTHER" in names
    assert "execute_tool notify_user" in names and "chat gpt-4o-mini" in names


def test_the_control_an_ordinary_turn_is_not_emptied_by_the_guard():
    """Control: the guard removes values that are not tokens, and keeps everything that is."""
    provider, exporter = in_memory_provider()
    OTelTraceSink(provider, clock=lambda: NOW_NS).record(Event(
        tenant_id="acme", route="EGO", stop_reason="completed",
        stages=[Stage("ner", model="gpt-4o-mini", provider="openai", tokens_in=10,
                      tokens_out=2)],
        tools=[Tool("resolve_date")]))
    values = [str(v) for s in exporter.get_finished_spans() for v in s.attributes.values()]
    assert "_OTHER" not in values
    assert {"acme", "EGO", "completed", "ner", "gpt-4o-mini", "openai", "resolve_date"} \
        <= set(values)
