"""What ``tracing.py`` READS off the host's objects is exactly ``INPUT_FIELDS``, derived from its AST.

WHY THIS IS THE PRIVACY GUARD THAT MATTERS MOST. The table and the value guard decide what may be
WRITTEN to a span. This file checks the step before that: a field this module never reads cannot
reach a span through any code path, including one a later edit adds. The fields that could carry
content (the message, the prompt, the reply, tool arguments and results, the error message, the
contact's ``identity_id``/``session_id``) are therefore held out of the READS, not only out of the
table.

It is the same rule as ``test_protocol_matches_the_reads.py``, which checks the Prometheus sink:
the reads are taken from the AST, never from a hand-written list. A list here would be a third
copy of the contract, and the third copy is the one that rots.
"""

from __future__ import annotations

import ast
import pathlib

from cogno_observability.tracing import INPUT_FIELDS

_MODULE = pathlib.Path(__file__).resolve().parent.parent / "cogno_observability" / "tracing.py"

#: The source each of the host's lists feeds, keyed by the ``TurnEvent`` field it comes from.
_LIST_SOURCE = {"stages": "stage", "tools": "tool"}

#: Fields a host object really carries and this module must never read. They are listed by name
#: because they are the reason this test exists.
_NEVER_READ = {"text", "message", "messages", "prompt", "reply", "response", "arguments",
               "result", "output", "identity_id", "session_id", "user_id", "phone", "email",
               "system_fingerprint", "recent", "trace"}


def _tree() -> ast.Module:
    return ast.parse(_MODULE.read_text(encoding="utf-8"))


def _source_names() -> "dict[str, str]":
    """``{variable name: source}`` DERIVED from ``plan_spans``, not assumed. The event is its first
    parameter, and a stage or tool is whatever a ``for`` binds while iterating over
    ``getattr(event, "stages"/"tools", ...)``. So renaming a variable does not quietly empty the
    checks below."""
    fn = next(n for n in ast.walk(_tree())
              if isinstance(n, ast.FunctionDef) and n.name == "plan_spans")
    event = fn.args.args[0].arg
    names = {event: "turn"}
    for loop in (n for n in ast.walk(fn) if isinstance(n, ast.For)):
        for call in (n for n in ast.walk(loop.iter) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "getattr"):
            target, attr = call.args[0], call.args[1]
            if isinstance(target, ast.Name) and target.id == event \
                    and isinstance(attr, ast.Constant) and attr.value in _LIST_SOURCE:
                assert isinstance(loop.target, ast.Name), ast.unparse(loop)
                names[loop.target.id] = _LIST_SOURCE[attr.value]
    assert set(names.values()) == {"turn", "stage", "tool"}, \
        f"the derivation found {names}: it broke, not the contract"
    return names


def _getattr_calls():
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "getattr":
            yield node


def _reads() -> "dict[str, set[str]]":
    source_of = _source_names()
    out: "dict[str, set[str]]" = {}
    for node in _getattr_calls():
        target, attr = node.args[0], node.args[1]
        assert isinstance(target, ast.Name), ast.unparse(node)
        assert target.id in source_of, (
            f"a getattr on {target.id!r}, which is not one of the host's objects this module "
            f"may read: {ast.unparse(node)}")
        assert isinstance(attr, ast.Constant) and isinstance(attr.value, str), \
            "a computed attribute name would make the reads underivable"
        assert len(node.args) == 3, f"a read with no default raises on a lagging host: " \
                                    f"{ast.unparse(node)}"
        out.setdefault(source_of[target.id], set()).add(attr.value)
    return out


def _plain_reads() -> "set[tuple[str, str]]":
    """``(source, field)`` for every PLAIN read (``call.error``) off one of the host's objects."""
    source_of = _source_names()
    return {(source_of[n.value.id], n.attr) for n in ast.walk(_tree())
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id in source_of}


def test_the_reads_are_exactly_the_declared_input_fields():
    reads = _reads()
    assert reads, "no getattr found: the derivation broke, not the contract"
    declared = {source: set(names) for source, names in INPUT_FIELDS.items()}
    assert reads == declared, {
        "read but not declared": {k: sorted(reads.get(k, set()) - declared.get(k, set()))
                                  for k in declared},
        "declared but never read": {k: sorted(declared.get(k, set()) - reads.get(k, set()))
                                    for k in declared}}


def test_no_content_or_contact_field_is_read_by_any_route():
    """Held for getattr AND for a plain ``obj.field``, since either one reads."""
    tree = _tree()
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    getattrs = {n.args[1].value for n in _getattr_calls()}
    declared = {name for names in INPUT_FIELDS.values() for name in names}
    assert (attrs | getattrs | declared) & _NEVER_READ == set(), \
        sorted((attrs | getattrs | declared) & _NEVER_READ)


def test_every_read_goes_through_getattr_so_the_equality_above_sees_it():
    """A plain ``call.error`` would be a read that ``getattr`` derivation cannot see, and it
    would also raise on a host that does not carry the field. So there are none."""
    assert _plain_reads() == set(), sorted(_plain_reads())


def test_the_error_read_is_the_turns_class_name_never_a_tools_message():
    """``error`` is read off the TURN only. There it is the host's ``type(exc).__name__``, and it
    still goes through the value guard. A tool's ``error`` is free text, so it is not read.
    Measured on the AST, both routes, and not on the declared table, which cannot contradict
    itself."""
    reads = _reads()
    plain = _plain_reads()
    assert "error" in reads["turn"], "the turn's error read is gone: this test measured nothing"
    for source in ("stage", "tool"):
        assert "error" not in reads.get(source, set())
        assert (source, "error") not in plain


def test_the_library_reads_no_environment():
    """*Libs emit, host configures*: the destination, and whether there is one, is the host's."""
    tree = _tree()
    imported = {alias.name.split(".")[0] for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom)) for alias in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module}
    assert imported, "no import found: the derivation broke"
    assert "os" not in imported
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} \
        | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert names & {"environ", "getenv"} == set()
