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

#: The loop variable (or parameter) that holds each source object in ``plan_spans``.
_SOURCE_OF = {"event": "turn", "sample": "stage", "call": "tool"}

#: Fields a host object really carries and this module must never read. They are listed by name
#: because they are the reason this test exists.
_NEVER_READ = {"text", "message", "messages", "prompt", "reply", "response", "arguments",
               "result", "output", "identity_id", "session_id", "user_id", "phone", "email",
               "system_fingerprint", "recent", "trace"}


def _tree() -> ast.Module:
    return ast.parse(_MODULE.read_text(encoding="utf-8"))


def _getattr_calls():
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "getattr":
            yield node


def _reads() -> "dict[str, set[str]]":
    out: "dict[str, set[str]]" = {}
    for node in _getattr_calls():
        target, attr = node.args[0], node.args[1]
        assert isinstance(target, ast.Name), ast.unparse(node)
        assert target.id in _SOURCE_OF, (
            f"a getattr on {target.id!r}, which is not one of the host's objects this module "
            f"may read: {ast.unparse(node)}")
        assert isinstance(attr, ast.Constant) and isinstance(attr.value, str), \
            "a computed attribute name would make the reads underivable"
        assert len(node.args) == 3, f"a read with no default raises on a lagging host: " \
                                    f"{ast.unparse(node)}"
        out.setdefault(_SOURCE_OF[target.id], set()).add(attr.value)
    return out


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


def test_the_error_read_is_the_turns_class_name_never_a_tools_message():
    """``error`` is read off the TURN only. There it is the host's ``type(exc).__name__``, and it
    still goes through the value guard. A tool's ``error`` is free text, so it is not read."""
    assert "error" in INPUT_FIELDS["turn"]
    assert "error" not in INPUT_FIELDS["tool"] and "error" not in INPUT_FIELDS["stage"]


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
