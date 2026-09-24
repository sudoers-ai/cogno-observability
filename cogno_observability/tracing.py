"""``OTelTraceSink`` — one host ``TurnEvent`` → an OpenTelemetry span tree, in the GenAI conventions.

**What it emits.** One ``invoke_agent`` span per turn and, beneath it, one span per MODEL row and
one per TOOL call. Names and attributes follow the OpenTelemetry GenAI semantic conventions at
:data:`SEMCONV_REF`. Those conventions have left the main ``semantic-conventions`` repository and
have **no tagged release yet**, so the reference is a commit and not a version number. Their
status is *Development*. When a name changes upstream, the change is ONE line in
:data:`SPAN_ATTRIBUTES`.

**Only metadata.** Until the owner decides how long content may be kept (LGPD), a span carries
models, tokens, cost, times and outcome codes, and nothing else. Nothing about the contact goes
out, because this module never READS the fields that could carry it: the message, the prompt,
the reply, tool arguments, tool results, error MESSAGES, ``identity_id`` and ``session_id``. That
is a property of the reads, and ``tests/test_tracing_reads.py`` derives them from this module's
AST. Two guards sit on top of it. (1) :data:`SPAN_ATTRIBUTES` is both the allowlist and the
emitter, because the emitter is a loop over that table. An attribute with no row cannot be set,
and the span names are built from values that went through (2). (2) Every string value must be a
TOKEN (:func:`_token`): it starts with a letter, has no spaces and no ``@``, and has no run of
ten or more digits. A sentence, an e-mail address or a phone number becomes ``_OTHER``. None of the spec's Opt-In content
attributes (``gen_ai.input.messages``, ``gen_ai.tool.call.arguments`` and the rest) has a row.
``tests/test_tracing_pii_guard.py`` checks that by name.

**When it runs.** AFTER the turn, from the event the host already hands its metrics sink.
Emitting then, rather than as live spans, means the spans are built from the same per-stage
records the host's token ledger is built from (``cogno_host.metering.events_from_context``: one
LLM row when a stage has LLM tokens, one embedding row when it has embedding tokens). So "the
tokens on the spans are the ledger's tokens" holds by construction, and wiring it into the host
is one more sink. **The limit this buys, stated rather than implied:** a turn that dies before
the host reaches its sink emits no spans at all. Recording those turns is M1's job, not this
module's.

**One span per ledger ROW, not per provider call.** The EGO adds up the several model calls of
its agent loop into ONE ``StageMetrics`` per attempt, so for the EGO "one span per model call"
means one span per ATTEMPT. The ledger has the same shape, so the two agree. A finer grain would
need a finer ledger first.

**Times are never invented.** A ``StageSample`` carries no start instant, and the core's
``ToolExecution`` carries no duration. So each child span says how its interval was obtained, in
``cogno.timing``:

* ``observed``: the host gave a ``started_at`` (epoch seconds) and the duration was measured;
* ``anchored``: the duration was measured, but there is no start, so the span starts at the
  turn's start;
* ``unmeasured``: there is no duration either (a tool the host did not time, or the second span
  cut from a stage sample whose one duration went to the first). The span is zero-length at its
  anchor.

The turn span is ``observed`` when the event carries ``started_at``. Otherwise it is
``anchored``: it ENDS at the instant :meth:`OTelTraceSink.record` is called, which the host does
at the very end of the turn, and it lasts ``elapsed_ms``.

**Off by default, and free when off.** Importing this package, and running :func:`plan_spans`,
imports nothing from ``opentelemetry``. The API is imported when an :class:`OTelTraceSink` is
BUILT, and a host builds one only when it has a ``TracerProvider`` with somewhere to send the
spans. This library reads no environment variable and knows no endpoint: *libs emit, host
configures*. The SDK and the exporter are the host's choice, through the optional extra
``cogno-observability[otel]``.
"""

from __future__ import annotations

import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

if TYPE_CHECKING:   # pragma: no cover — typing only; the runtime import is lazy
    from opentelemetry.trace import TracerProvider

log = logging.getLogger("cogno_observability.tracing")

#: The conventions this module follows. There is no release number: the GenAI conventions moved
#: to their own repository and have not been tagged. That commit's pages link the main conventions
#: at v1.44.0, and every GenAI attribute used here is at stability *Development*.
SEMCONV_REF = ("open-telemetry/semantic-conventions-genai@8ffdf568e1 (2026-09-22, "
               "status Development; links semantic-conventions v1.44.0)")

#: The span kinds this module emits.
TURN, CHAT, EMBEDDINGS, TOOL = "turn", "chat", "embeddings", "tool"
SPAN_KINDS = (TURN, CHAT, EMBEDDINGS, TOOL)

#: ``gen_ai.operation.name`` for each span kind: the spec's well-known values.
_OPERATION = {TURN: "invoke_agent", CHAT: "chat", EMBEDDINGS: "embeddings", TOOL: "execute_tool"}

#: The value for ``error.type`` when the error has no class name we may send. It is the spec's own
#: fallback, and it is the only value this module sends for a failed TOOL, because the core
#: records a failure as free text (see ``ToolExecution.error``) and free text is not metadata.
OTHER = "_OTHER"

#: A backend's prefix as the host writes it (``openai:gpt-4o-mini``) → ``gen_ai.provider.name``.
#: The spec's well-known value when one exists, and the prefix itself otherwise (the spec allows a
#: custom value). ``mistral`` is deliberately absent: here ``mistral:latest`` names an Ollama
#: model, and reading its prefix as a provider would call a local model a cloud one.
_PROVIDERS: Mapping[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "groq": "groq",
    "gemini": "gcp.gemini",
    "bedrock": "aws.bedrock",
    "deepseek": "deepseek",
    "moonshot": "moonshot_ai",
    "kimi": "moonshot_ai",
    "xai": "x_ai",
    "grok": "x_ai",
    "ollama": "ollama",
    "openrouter": "openrouter",
    "together": "together",
    "fireworks": "fireworks",
}
#: ``gen_ai.provider.name`` is REQUIRED on model spans, and a bare model name (``qwen3:8b``,
#: ``gpt-4o-mini``) does not say who served it. We send "unknown" when we do not know, because a
#: guessed provider would be a false statement.
UNKNOWN_PROVIDER = "unknown"

# ── the value guard ───────────────────────────────────────────────────────────────────────────
# A token starts with a letter, so a bare phone number is not one, and has no spaces, so a
# sentence is not one. It has no "@" either, so an e-mail address is not one. The other
# characters are the ones model and tool names really use (``us.anthropic.claude-3-haiku-v1:0``,
# ``accounts/fireworks/models/x``).
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]{0,127}")
# ...and no run of TEN or more digits, which is the shape of a phone number (11 digits for a
# Brazilian mobile, 13 with the country code) or of an unformatted national id, even behind a
# letter prefix such as ``wa:5511...``. A model snapshot date is 8 digits
# (``claude-3-haiku-20240307``), so real names pass.
_LONG_DIGITS_RE = re.compile(r"[0-9]{10,}")


def _token(value: Any) -> Optional[str]:
    """``value`` as a token that may be sent, ``_OTHER`` when it is not one, ``None`` when blank.

    This check is the reason a string attribute cannot carry a sentence, and it is applied to
    EVERY string the table emits and to every span name (which is built from those strings).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not _TOKEN_RE.fullmatch(text) or _LONG_DIGITS_RE.search(text):
        return OTHER
    return text


def _tenant(value: Any) -> Optional[str]:
    """The tenant id, only when it is a UUID or a token. Otherwise it is omitted, not ``_OTHER``.

    The tenant may be named (it is the operator's customer, not the contact). A UUID can start
    with a digit, so it gets its own shape. Anything else, including a string of digits that could
    be a phone number, is dropped. Sending ``_OTHER`` for it would only add noise to a filter.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 36:
        try:
            return str(uuid.UUID(text))
        except ValueError:
            pass
    token = _token(text)
    return None if token in (None, OTHER) else token


# ── the input contract: EVERYTHING this module reads, and nothing else ────────────────────────
#: The duck-typed reads, by source. ``tests/test_tracing_reads.py`` derives the reads from this
#: module's AST and requires EQUALITY with this table. A read that is not listed here, or a listed
#: field nobody reads, fails that test. That is the guard that matters for privacy: the fields
#: that could carry content (``text``, ``arguments``, ``result``, the error MESSAGE,
#: ``identity_id``, ``session_id``) are absent from this table, so nothing in this module reads
#: them. Every read has a default, so a host that does not carry a field yet degrades instead of
#: raising.
INPUT_FIELDS: Mapping[str, tuple[str, ...]] = {
    # the host's ``TurnEvent``: all present today except ``started_at`` and ``tools``
    "turn": ("tenant_id", "route", "stop_reason", "ok", "error", "blocked", "cache_hit",
             "handoff", "correction_count", "cost_usd", "elapsed_ms", "started_at", "stages",
             "tools"),
    # one item of ``TurnEvent.stages`` (``StageSample``): the first seven are present today
    "stage": ("stage", "model", "tokens_in", "tokens_out", "embedding_tokens", "cached_tokens",
              "elapsed_ms", "provider", "served_model", "cost_usd", "embedding_cost_usd",
              "attempt", "started_at"),
    # one item of ``TurnEvent.tools``: ``tool``/``ok`` are the core's ``ToolExecution`` names
    "tool": ("tool", "ok", "elapsed_ms", "started_at"),
}


# ── normalised rows: what the table reads ─────────────────────────────────────────────────────
@dataclass
class _Row:
    """One span-to-be, as plain values. Every reader in :data:`SPAN_ATTRIBUTES` reads THIS, never
    the host's objects. So the host's objects are read only in the normalisers below, which are
    the reads ``INPUT_FIELDS`` lists."""

    kind: str
    timing: str = "anchored"
    error_type: Optional[str] = None
    # turn
    tenant: Optional[str] = None
    route: Optional[str] = None
    stop_reason: Optional[str] = None
    ok: Optional[bool] = None
    blocked: Optional[bool] = None
    cache_hit: Optional[bool] = None
    handoff: Optional[bool] = None
    corrections: Optional[int] = None
    turn_cost_usd: Optional[float] = None
    # model
    stage: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    served_model: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cached_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    attempt: Optional[int] = None
    # tool
    tool: Optional[str] = None


@dataclass(frozen=True)
class SpanAttribute:
    """One row of THE table: an attribute key, the span kinds that carry it, and its reader."""

    key: str
    spans: "frozenset[str]"
    value_type: type
    origin: str                    # "semconv" (the GenAI conventions) | "cogno" (this library)
    read: Callable[[_Row], Any]    # ``None`` → the attribute is omitted on that span
    meaning: str


def _row(key: str, spans: "tuple[str, ...]", typ: type, origin: str,
         read: Callable[[_Row], Any], meaning: str) -> SpanAttribute:
    return SpanAttribute(key, frozenset(spans), typ, origin, read, meaning)


_MODEL = (CHAT, EMBEDDINGS)

#: THE table. It is the allowlist AND the emitter (:func:`_attributes` is a loop over it), so an
#: attribute with no row here cannot be emitted. One row per KEY, so a rename upstream is one line.
#: ``cogno.*`` holds what the conventions do not define. They define no COST attribute, and
#: ``gen_ai.*`` is reserved for the conventions themselves. The TURN's cost and the ROW costs are
#: two different keys, so that adding ``cogno.usage.cost_usd`` over every span gives the ledger's
#: total, and ``cogno.turn.cost_usd`` is never counted twice into it.
SPAN_ATTRIBUTES: "tuple[SpanAttribute, ...]" = (
    _row("gen_ai.operation.name", SPAN_KINDS, str, "semconv", lambda r: _OPERATION[r.kind],
         "invoke_agent, chat, embeddings or execute_tool"),
    _row("error.type", (TURN, TOOL), str, "semconv", lambda r: r.error_type,
         "only on a failure: a failed turn's exception class, else _OTHER; a failed tool is "
         "always _OTHER"),
    _row("gen_ai.provider.name", _MODEL, str, "semconv", lambda r: r.provider,
         "who served the call; 'unknown' when the host did not say"),
    _row("gen_ai.request.model", _MODEL, str, "semconv", lambda r: r.model,
         "the model the host asked for"),
    _row("gen_ai.response.model", _MODEL, str, "semconv", lambda r: r.served_model,
         "the snapshot the provider says answered (when the host carries it)"),
    _row("gen_ai.usage.input_tokens", _MODEL, int, "semconv", lambda r: r.tokens_in,
         "input tokens of the ledger row (embedding tokens on an embeddings span)"),
    _row("gen_ai.usage.output_tokens", (CHAT,), int, "semconv", lambda r: r.tokens_out,
         "output tokens of the ledger row"),
    _row("gen_ai.usage.cache_read.input_tokens", (CHAT,), int, "semconv",
         lambda r: r.cached_tokens or None,
         "the SUBSET of input tokens the provider served from its cache; omitted when 0"),
    _row("gen_ai.tool.name", (TOOL,), str, "semconv", lambda r: r.tool, "the tool's name"),
    _row("gen_ai.tool.type", (TOOL,), str, "semconv", lambda r: "function",
         "always 'function': the host executes every tool the EGO calls"),
    _row("cogno.stage", _MODEL, str, "cogno", lambda r: r.stage,
         "the cognitive stage of the row (noumeno, ner, ego, superego_voice, ...)"),
    _row("cogno.attempt", _MODEL, int, "cogno", lambda r: r.attempt or None,
         "the correction-loop attempt the row belongs to; omitted when not stamped"),
    _row("cogno.usage.cost_usd", _MODEL, float, "cogno", lambda r: r.cost_usd,
         "the ledger row's provider cost, when the host carries it"),
    _row("cogno.timing", SPAN_KINDS, str, "cogno", lambda r: r.timing,
         "observed, anchored or unmeasured: how the span's interval was obtained"),
    _row("cogno.tenant.id", (TURN,), str, "cogno", lambda r: r.tenant,
         "the tenant: only a UUID or a token, otherwise omitted"),
    _row("cogno.turn.route", (TURN,), str, "cogno", lambda r: r.route, "the ID stage's route"),
    _row("cogno.turn.stop_reason", (TURN,), str, "cogno", lambda r: r.stop_reason,
         "how the turn ended (completed, semantic_cache, input_blocked:..., error, ...)"),
    _row("cogno.turn.ok", (TURN,), bool, "cogno", lambda r: r.ok,
         "False when the turn raised"),
    _row("cogno.turn.blocked", (TURN,), bool, "cogno", lambda r: r.blocked,
         "a guard refused the turn"),
    _row("cogno.turn.cache_hit", (TURN,), bool, "cogno", lambda r: r.cache_hit,
         "answered from the semantic cache"),
    _row("cogno.turn.handoff", (TURN,), bool, "cogno", lambda r: r.handoff,
         "ended in a human handoff"),
    _row("cogno.turn.correction_count", (TURN,), int, "cogno", lambda r: r.corrections,
         "EGO/SUPEREGO correction retries"),
    _row("cogno.turn.cost_usd", (TURN,), float, "cogno", lambda r: r.turn_cost_usd,
         "the meter's cost for the whole turn"),
)

#: The keys that may ever appear on a span. Derived from the table, never written out by hand.
ALLOWED_ATTRIBUTE_KEYS = frozenset(a.key for a in SPAN_ATTRIBUTES)


def _coerce(typ: type, value: Any) -> Any:
    """The table's declared type, or ``None`` (omitted). A value that does not fit is dropped,
    never sent in a shape the table did not declare."""
    if value is None:
        return None
    if typ is str:
        return _token(value)
    if typ is bool:
        return bool(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if typ is int else number


def _attributes(row: _Row) -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for attr in SPAN_ATTRIBUTES:
        if row.kind not in attr.spans:
            continue
        value = _coerce(attr.value_type, attr.read(row))
        if value is not None:
            out[attr.key] = value
    return out


# ── the plan: a pure description of the spans, with no OpenTelemetry import ──────────────────
@dataclass
class PlannedSpan:
    """A span to emit: everything :class:`OTelTraceSink` hands the tracer, as plain values."""

    kind: str
    name: str
    start_ns: int
    end_ns: int
    attributes: "dict[str, Any]" = field(default_factory=dict)
    error: bool = False


def _int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _provider_and_model(provider: Any, model: Any) -> "tuple[str, Optional[str]]":
    """``(gen_ai.provider.name, gen_ai.request.model)``: the host's ``provider`` if given, else a
    known ``provider:`` prefix on the model (which is then stripped), else ``unknown``."""
    name = str(model or "").strip()
    declared = str(provider or "").strip().lower()
    if ":" in name:
        prefix, rest = name.split(":", 1)
        if prefix.lower() in _PROVIDERS:
            declared = declared or prefix.lower()
            name = rest
    resolved = _PROVIDERS.get(declared, declared) if declared else UNKNOWN_PROVIDER
    return resolved, (name or None)


def _span_name(kind: str, subject: Optional[str]) -> str:
    """``{gen_ai.operation.name} {subject}``, as the conventions name each span. ``subject`` has
    already been through the value guard, so a name is never built from free text."""
    op = _OPERATION[kind]
    token = _token(subject) if subject else None
    return f"{op} {token}" if token else op


def _epoch_ns(seconds: Any) -> Optional[int]:
    """Epoch seconds (a float, as ``time.time()`` gives) → integer nanoseconds, or ``None``.

    Split into whole and fractional parts first. ``int(s * 1e9)`` loses about 256 ns at today's
    epoch, because the product has more significant digits than a float holds."""
    value = _float(seconds)
    if value is None or value <= 0:
        return None
    whole = int(value)
    return whole * 1_000_000_000 + round((value - whole) * 1e9)


def _interval(anchor_ns: int, started_at: Any, elapsed_ms: Any,
              *, measured: bool = True) -> "tuple[int, int, str]":
    """``(start_ns, end_ns, cogno.timing)``. The host's own instant when it gave one; the anchor
    otherwise. A duration that was not measured is zero, and the span says so."""
    start = _epoch_ns(started_at)
    ms = _float(elapsed_ms) if measured else None
    duration = int(ms * 1e6) if ms is not None and ms > 0 else None
    if duration is None:
        return (start if start is not None else anchor_ns), \
               (start if start is not None else anchor_ns), "unmeasured"
    if start is None:
        return anchor_ns, anchor_ns + duration, "anchored"
    return start, start + duration, "observed"


def plan_spans(event: Any, *, embedding_model: str = "", now_ns: Optional[int] = None) \
        -> "list[PlannedSpan]":
    """Describe the spans for one turn, with no OpenTelemetry import. The turn span comes first.

    Pure. ``now_ns`` is the instant the turn ENDED (the host records its event at the end of the
    turn); it defaults to the wall clock and is injectable so that a test's window does not
    depend on the day it runs. ``embedding_model`` names the deployment's embedder (for example
    ``openai:text-embedding-3-small``), the same way the host's ledger does, because a stage
    sample does not carry it.
    """
    end_ns = int(now_ns if now_ns is not None else time.time_ns())

    # ── the turn ──
    elapsed = _float(getattr(event, "elapsed_ms", 0.0)) or 0.0
    observed_start = _epoch_ns(getattr(event, "started_at", None))
    if observed_start is not None:
        t_start = observed_start
        t_end, t_timing = t_start + int(max(elapsed, 0.0) * 1e6), "observed"
    else:
        t_start, t_end, t_timing = end_ns - int(max(elapsed, 0.0) * 1e6), end_ns, "anchored"
    ok = bool(getattr(event, "ok", True))
    error_class = _token(getattr(event, "error", "")) if not ok else None
    turn = _Row(
        kind=TURN, timing=t_timing,
        error_type=None if ok else (error_class or OTHER),
        tenant=_tenant(getattr(event, "tenant_id", "")),
        route=getattr(event, "route", "") or None,
        stop_reason=getattr(event, "stop_reason", "") or None,
        ok=ok,
        blocked=bool(getattr(event, "blocked", False)),
        cache_hit=bool(getattr(event, "cache_hit", False)),
        handoff=bool(getattr(event, "handoff", False)),
        corrections=_int(getattr(event, "correction_count", 0)),
        turn_cost_usd=_float(getattr(event, "cost_usd", None)),
    )
    plan = [PlannedSpan(TURN, _span_name(TURN, None), t_start, t_end, _attributes(turn),
                        error=not ok)]

    # ── the model rows: the ledger's split, one stage sample → up to two rows ──
    emb_provider, emb_model = _provider_and_model("", embedding_model)
    for sample in getattr(event, "stages", None) or []:
        stage = getattr(sample, "stage", "") or None
        attempt = _int(getattr(sample, "attempt", 0))
        started = getattr(sample, "started_at", None)
        elapsed_ms = getattr(sample, "elapsed_ms", 0.0)
        tin = _int(getattr(sample, "tokens_in", 0))
        tout = _int(getattr(sample, "tokens_out", 0))
        temb = _int(getattr(sample, "embedding_tokens", 0))
        duration_spent = False
        if tin or tout:
            provider, model = _provider_and_model(getattr(sample, "provider", ""),
                                                  getattr(sample, "model", ""))
            start, stop, timing = _interval(t_start, started, elapsed_ms)
            duration_spent = True
            row = _Row(kind=CHAT, timing=timing, stage=stage, provider=provider, model=model,
                       served_model=getattr(sample, "served_model", "") or None,
                       tokens_in=tin, tokens_out=tout,
                       cached_tokens=_int(getattr(sample, "cached_tokens", 0)),
                       cost_usd=_float(getattr(sample, "cost_usd", None)), attempt=attempt)
            plan.append(PlannedSpan(CHAT, _span_name(CHAT, model), start, stop,
                                    _attributes(row)))
        if temb:
            start, stop, timing = _interval(t_start, started, elapsed_ms,
                                            measured=not duration_spent)
            row = _Row(kind=EMBEDDINGS, timing=timing, stage=stage, provider=emb_provider,
                       model=emb_model, tokens_in=temb,
                       cost_usd=_float(getattr(sample, "embedding_cost_usd", None)),
                       attempt=attempt)
            plan.append(PlannedSpan(EMBEDDINGS, _span_name(EMBEDDINGS, emb_model), start, stop,
                                    _attributes(row)))

    # ── the tools ──
    for call in getattr(event, "tools", None) or []:
        tool_ok = bool(getattr(call, "ok", True))
        start, stop, timing = _interval(t_start, getattr(call, "started_at", None),
                                        getattr(call, "elapsed_ms", None))
        row = _Row(kind=TOOL, timing=timing, tool=getattr(call, "tool", "") or None,
                   error_type=None if tool_ok else OTHER)
        plan.append(PlannedSpan(TOOL, _span_name(TOOL, row.tool), start, stop,
                                _attributes(row), error=not tool_ok))
    return plan


# ── the sink ──────────────────────────────────────────────────────────────────────────────────
class OTelTraceSink:
    """A ``MetricsSink`` (``record(event)``) that emits each turn as OpenTelemetry spans.

    ``tracer_provider`` is REQUIRED, and it is the host's: an SDK ``TracerProvider`` with the
    exporter the deployment chose. The host builds this sink only when it has somewhere to send
    the spans. So "no destination configured" means "no sink", and then nothing here is imported
    or run. This is the only place the ``opentelemetry`` API is imported, and it happens when the
    sink is built.

    ``record`` is **best-effort**: observability must never break a turn. A failure is logged at
    WARNING with its exception CLASS only, because an exception's message can carry the value
    that caused it.
    """

    def __init__(self, tracer_provider: "TracerProvider", *, embedding_model: str = "",
                 clock: Callable[[], int] = time.time_ns) -> None:
        try:
            from opentelemetry import trace
            from opentelemetry.trace import SpanKind, Status, StatusCode
        except ImportError as exc:   # pragma: no cover — exercised only without the extra
            raise ImportError("OTelTraceSink needs the OpenTelemetry API: "
                              "pip install 'cogno-observability[otel]'") from exc
        from cogno_observability import __version__

        self._trace = trace
        self._error_status = Status(StatusCode.ERROR)
        self._kinds = {TURN: SpanKind.INTERNAL, CHAT: SpanKind.CLIENT,
                       EMBEDDINGS: SpanKind.CLIENT, TOOL: SpanKind.INTERNAL}
        self._tracer = tracer_provider.get_tracer("cogno_observability", __version__)
        self._embedding_model = embedding_model
        self._clock = clock

    def record(self, event: Any) -> None:
        try:
            self._emit(plan_spans(event, embedding_model=self._embedding_model,
                                  now_ns=self._clock()))
        except Exception as exc:  # noqa: BLE001 — observability must not break the turn
            log.warning("event=trace_record_failed error=%s", type(exc).__name__)

    def _emit(self, plan: "list[PlannedSpan]") -> None:
        root, children = plan[0], plan[1:]
        turn = self._tracer.start_span(root.name, kind=self._kinds[TURN],
                                       start_time=root.start_ns, attributes=root.attributes)
        parent = self._trace.set_span_in_context(turn)
        try:
            for child in children:
                span = self._tracer.start_span(child.name, context=parent,
                                               kind=self._kinds[child.kind],
                                               start_time=child.start_ns,
                                               attributes=child.attributes)
                if child.error:
                    span.set_status(self._error_status)
                span.end(end_time=child.end_ns)
        finally:
            if root.error:
                turn.set_status(self._error_status)
            turn.end(end_time=root.end_ns)
