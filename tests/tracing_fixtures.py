"""Stand-ins for the host's ``TurnEvent``/``StageSample`` and for a tool record, for the tracing tests.

Duck-typed, with no ``cogno_host`` import: the same approach ``test_sink.py`` takes. They carry
TODAY's host fields plus the ones the host wiring will add (``started_at``, ``tools``, and the
per-row ``provider``/``served_model``/``cost_usd``/``embedding_cost_usd``/``attempt``). They ALSO
carry the content fields a real host object has (``session_id``, ``identity_id``, tool
``arguments``/``result``/``error``). ``tracing.py`` must never read those, and the PII guard test
fills them with personal data to prove it. Every name, address and number below is invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

#: The instant the turn ENDS in every test. Injected, so that no result depends on the day the
#: suite runs.
NOW_NS = 1_700_000_000_000_000_000


@dataclass
class Stage:
    stage: str
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    embedding_tokens: int = 0
    cached_tokens: int = 0
    elapsed_ms: float = 0.0
    # to be added by the host wiring
    provider: str = ""
    served_model: str = ""
    cost_usd: Optional[float] = None
    embedding_cost_usd: Optional[float] = None
    attempt: int = 0
    started_at: Optional[float] = None


@dataclass
class Tool:
    tool: str
    ok: bool = True
    elapsed_ms: Optional[float] = None
    started_at: Optional[float] = None
    # content the core's ToolExecution carries, which tracing.py must never read
    arguments: dict = field(default_factory=dict)
    result: str = ""
    error: Optional[str] = None


@dataclass
class Event:
    tenant_id: str = "acme"
    session_id: str = "s1"
    identity_id: str = ""
    route: str = "EGO"
    stop_reason: str = "completed"
    total_tokens: int = 0
    elapsed_ms: float = 0.0
    cache_hit: bool = False
    blocked: bool = False
    ok: bool = True
    error: str = ""
    cost_usd: float = 0.0
    correction_count: int = 0
    handoff: bool = False
    stages: list = field(default_factory=list)
    # to be added by the host wiring
    started_at: Optional[float] = None
    tools: list = field(default_factory=list)


def ordinary_turn() -> Event:
    """A plausible turn: two stages with LLM and embedding tokens, a stage with embedding tokens
    only (the ID heuristic), two EGO attempts, the judge, the voice and two tools."""
    return Event(
        tenant_id="3f2b8c1e-0d4a-4e8b-9a61-7c5d2e9f0a13", route="EGO", stop_reason="completed",
        elapsed_ms=4200.0, cost_usd=0.00412, correction_count=1,
        stages=[
            Stage("noumeno", model="gpt-4o-mini", provider="openai", tokens_in=900,
                  tokens_out=60, embedding_tokens=30, elapsed_ms=610.0, cost_usd=0.000171,
                  embedding_cost_usd=0.0000006),
            Stage("ner", model="gpt-4o-mini", provider="openai", tokens_in=1200, tokens_out=150,
                  elapsed_ms=820.0, cost_usd=0.00027),
            Stage("id", model="heuristic", embedding_tokens=12, elapsed_ms=40.0,
                  embedding_cost_usd=0.00000024),
            Stage("ego", model="gpt-4o-mini", provider="openai", tokens_in=2500,
                  tokens_out=80, cached_tokens=2048, elapsed_ms=1300.0, cost_usd=0.000261,
                  attempt=1),
            Stage("ego", model="gpt-4o-mini", provider="openai", tokens_in=2600,
                  tokens_out=90, cached_tokens=2432, elapsed_ms=900.0, cost_usd=0.000219,
                  attempt=2),
            Stage("superego_judge", model="gpt-4o-mini", provider="openai", tokens_in=1800,
                  tokens_out=40, elapsed_ms=300.0, cost_usd=0.000294, attempt=2),
            Stage("superego_voice", model="gpt-4o-mini", provider="openai", tokens_in=1500,
                  tokens_out=120, elapsed_ms=230.0, cost_usd=0.000297,
                  served_model="gpt-4o-mini-2024-07-18"),
        ],
        tools=[Tool("resolve_date", ok=True), Tool("consult_material", ok=True)],
    )


def ledger_rows(event: Event) -> "list[tuple[str, str, int, int, Optional[float]]]":
    """The host ledger's split of the same stages (``cogno_host.metering.events_from_context``):
    one LLM row when a stage has LLM tokens, one embedding row when it has embedding tokens.
    Returns ``(stage, modality, tokens_in, tokens_out, cost_usd)``."""
    rows = []
    for s in event.stages:
        if s.tokens_in or s.tokens_out:
            rows.append((s.stage, "llm", s.tokens_in, s.tokens_out, s.cost_usd))
        if s.embedding_tokens:
            rows.append((s.stage, "embedding", s.embedding_tokens, 0, s.embedding_cost_usd))
    return rows


def in_memory_provider() -> "tuple[TracerProvider, InMemorySpanExporter]":
    """An SDK provider that sends to memory: the only exporter these tests use."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter
