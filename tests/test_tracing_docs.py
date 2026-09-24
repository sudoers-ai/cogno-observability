"""The attribute table in ``docs/HOST_INTEGRATION.md`` IS ``SPAN_ATTRIBUTES``, row for row.

The docs keep a copy so that a reader does not have to open the code. A copy nobody checks
drifts, so this test renders the table from the code and requires the docs block to match it
exactly, in both directions: a row the code gained, and a row the docs kept after the code
dropped it, both fail.
"""

from __future__ import annotations

import pathlib

from cogno_observability.tracing import SPAN_ATTRIBUTES, SPAN_KINDS

_DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "HOST_INTEGRATION.md"
_START = "<!-- span-attributes:start"
_END = "<!-- span-attributes:end -->"


def _rendered() -> "list[str]":
    rows = ["| Key | Spans | Type | Origin | Meaning |", "| --- | --- | --- | --- | --- |"]
    for a in SPAN_ATTRIBUTES:
        spans = ", ".join(k for k in SPAN_KINDS if k in a.spans)
        rows.append(f"| `{a.key}` | {spans} | {a.value_type.__name__} | {a.origin} | "
                    f"{a.meaning} |")
    return rows


def _documented() -> "list[str]":
    text = _DOC.read_text(encoding="utf-8")
    assert text.count(_START) == 1 and text.count(_END) == 1, "the docs block markers moved"
    block = text.split(_START, 1)[1].split(_END, 1)[0]
    return [line for line in block.splitlines()[1:] if line.strip()]


def test_the_docs_table_is_the_code_table():
    documented = _documented()
    assert len(documented) > 2, "the docs block is empty: this test measured nothing"
    assert documented == _rendered()


def test_no_meaning_breaks_the_markdown_table():
    """A ``|`` inside a meaning splits the row into extra columns when the docs are rendered."""
    assert [a.key for a in SPAN_ATTRIBUTES if "|" in a.meaning] == []
