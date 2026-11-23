"""The reference graphs' line format, pinned (docs/reports/PHASE1_SEAMS.md).

`codebase_rag.canonical_graph` writes the recorded reference graphs, and the
Rust rewrite's parity checks compare both engines' graphs in it. A change to
the format would make every recorded graph differ from the next check, so
the exact lines of one small export are pinned here.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag.canonical_graph import canonical
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
