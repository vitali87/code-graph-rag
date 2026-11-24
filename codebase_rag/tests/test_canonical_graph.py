"""The reference graphs' line format, pinned (docs/reports/PHASE1_SEAMS.md).

`codebase_rag.canonical_graph` writes the recorded reference graphs, and the
Rust rewrite's parity checks compare both engines' graphs in it. A change to
the format would make every recorded graph differ from the next check, so
the exact lines of one small export are pinned here. The same checks compare
the two engines' hand-off files, by the rules the last tests pin.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.canonical_graph import (
    canonical,
    difference_in_order,
    handoff_difference,
    read_handoff,
)
from codebase_rag.types_defs import ExportRecord

ROOT = Path("/work/checkout/demo")

EXPORT: ExportRecord = {
    "nodes": [
        {
            "node_id": 9,
            "labels": ["Project"],
            "properties": {
                "name": "demo",
                "root_path": "/work/checkout/demo",
                "last_synced_at": "2026-11-18T13:14:10+00:00",
            },
        },
        {
            "node_id": 4,
            "labels": ["File"],
            "properties": {
                "absolute_path": "/work/checkout/demo/app.py",
                "path": "app.py",
                "name": "app.py",
            },
        },
        {
            "node_id": 2,
            "labels": ["Function"],
            "properties": {
                "qualified_name": "demo.app.main",
                "name": "main",
                "docstring": "Grüße",
            },
        },
        {
            "node_id": 7,
            "labels": ["Method", "Decorated"],
            "properties": {
                "qualified_name": "demo.app.C.run",
                "name": "run",
                "sources": ["/work/checkout/demo/deco.py", "elsewhere"],
            },
        },
    ],
    "relationships": [
        {"from_id": 9, "to_id": 4, "type": "CONTAINS_FILE", "properties": {}},
        {
            "from_id": 4,
            "to_id": 2,
            "type": "DEFINES",
            "properties": {"at": "/work/checkout/demo/app.py"},
        },
        {"from_id": 2, "to_id": 7, "type": "CALLS", "properties": {"line": 2}},
        {"from_id": 2, "to_id": 7, "type": "CALLS", "properties": {"line": 2}},
    ],
}


def test_a_small_export_reads_as_these_lines() -> None:
    # Sorted, edges (`E`) before nodes (`N`); a node named by its sorted
    # labels and the key its first keyed label has; the checkout as <root>,
    # in keys, values and lists; the sync time dropped; JSON with sorted
    # keys, no spaces and no ASCII escaping; the repeated edge kept twice.
    assert canonical(EXPORT, ROOT) == [
        'E\tCALLS\tFunction\t"demo.app.main"\tDecorated:Method\t"demo.app.C.run"\t'
        '{"line":2}',
        'E\tCALLS\tFunction\t"demo.app.main"\tDecorated:Method\t"demo.app.C.run"\t'
        '{"line":2}',
        'E\tCONTAINS_FILE\tProject\t"demo"\tFile\t"<root>/app.py"\t{}',
        'E\tDEFINES\tFile\t"<root>/app.py"\tFunction\t"demo.app.main"\t'
        '{"at":"<root>/app.py"}',
        'N\tDecorated:Method\t"demo.app.C.run"\t'
        '{"name":"run","sources":["<root>/deco.py","elsewhere"]}',
        'N\tFile\t"<root>/app.py"\t{"name":"app.py","path":"app.py"}',
        'N\tFunction\t"demo.app.main"\t{"docstring":"Grüße","name":"main"}',
        'N\tProject\t"demo"\t{"root_path":"<root>"}',
    ]


FILES_HEADER = "# cgr handoff files.txt 1"
INDEX_HEADER = "# cgr handoff index.txt 1"
A_GO = 'file\ta.go\t{"language":"go"}'
B_GO = 'file\tb.go\t{"language":"go"}'
MODULE = 'module\tdemo.a\t{"language":"go","path":"a.go"}'
DEFINITION = 'definition\tdemo.a.f\t{"label":"Function"}'


def test_the_file_plan_is_compared_in_order() -> None:
    # Its order is data: pass 2 registers files in walk order, and the
    # order decides which of two same-stem files claims a module's name.
    python = [FILES_HEADER, A_GO, B_GO, ""]
    rust = [FILES_HEADER, B_GO, A_GO, ""]

    # `b.go` moved before `a.go`: Rust has it where Python has nothing, and
    # Python has it where Rust has nothing.
    assert handoff_difference(cs.HandoffFile.FILES, python, rust) == [
        f"+ {B_GO}",
        f"- {B_GO}",
    ]
    assert handoff_difference(cs.HandoffFile.FILES, python, python) == []


def test_the_project_index_is_compared_as_a_multiset() -> None:
    python = [INDEX_HEADER, DEFINITION, MODULE, ""]

    assert (
        handoff_difference(
            cs.HandoffFile.INDEX, python, [INDEX_HEADER, MODULE, DEFINITION, ""]
        )
        == []
    )
    assert handoff_difference(
        cs.HandoffFile.INDEX, python, [INDEX_HEADER, MODULE, ""]
    ) == [f"- {DEFINITION}"]
    assert handoff_difference(cs.HandoffFile.INDEX, python, [*python, DEFINITION]) == [
        f"+ {DEFINITION}"
    ]


def test_two_format_versions_are_compared_by_their_headers_alone() -> None:
    # Each engine writes the version it implements; lines of two versions
    # cannot be compared, so the difference names both.
    for file, header in (
        (cs.HandoffFile.FILES, FILES_HEADER),
        (cs.HandoffFile.INDEX, INDEX_HEADER),
    ):
        newer = header.replace(" 1", " 2")

        assert handoff_difference(file, [header, A_GO, ""], [newer, B_GO, ""]) == [
            f"- {header}",
            f"+ {newer}",
        ]


def test_a_missing_file_differs_from_any_written_one() -> None:
    assert handoff_difference(cs.HandoffFile.INDEX, [INDEX_HEADER, MODULE, ""], []) == [
        f"- {INDEX_HEADER}"
    ]
    assert handoff_difference(cs.HandoffFile.INDEX, [], []) == []


def test_a_difference_in_order_keeps_each_sides_lines_where_they_differ() -> None:
    assert difference_in_order(["a", "b", "c"], ["a", "x", "c", "d"]) == [
        "- b",
        "+ x",
        "+ d",
    ]
    assert difference_in_order(["a", "b"], ["a", "b"]) == []


def test_a_hand_off_reads_back_byte_for_byte(tmp_path: Path) -> None:
    # Split on `\n` alone, so any byte that differs shows in some line: a
    # `\r` before it, a missing last line end, a U+2028 inside a value.
    written = (
        f'{INDEX_HEADER}\ndefinition\tdemo.a.f\t{{"docstring":"one\u2028two"}}\n'
    ).encode()
    path = tmp_path / "index.txt"
    path.write_bytes(written)

    lines = read_handoff(path)

    assert lines == [
        INDEX_HEADER,
        'definition\tdemo.a.f\t{"docstring":"one\u2028two"}',
        "",
    ]
    assert "\n".join(lines).encode() == written
    for other in (written.replace(b"\n", b"\r\n"), written.removesuffix(b"\n")):
        path.write_bytes(other)
        assert read_handoff(path) != lines
