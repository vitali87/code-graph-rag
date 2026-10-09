"""`cgr index` carries what the graph carries (issue #3206).

The protobuf writer keeps a property only when the label's message declares
a field of that name, and `codec/schema.proto` had fallen behind: an
Interface, Enum or Type kept no line span, docstring or `is_exported`, and
Functions, Methods and Classes lost `path`, `modifiers`, `start_col`,
`positional_params` and more. `diff-index` could not see a type that moved,
changed its docstring or stopped being exported.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import codec.schema_pb2 as pb
from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.services.graph_diff import diff_indexes
from codebase_rag.services.protobuf_service import LABEL_TO_ONEOF_FIELD
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.tests.test_protobuf_node_property_parity import _NOT_EXPORTED

runner = CliRunner()

_SHAPES = """\
/** A drawable shape. */
export interface Shape {
  area(): number;
}
/** Supported colours. */
export enum Colour {
  Red,
  Blue,
}
/** A point in 2D. */
export type Point = { x: number; y: number };
export class Circle implements Shape {
  constructor(private r: number) {}
  area(): number {
    return Math.PI * this.r * this.r;
  }
}
"""
_EDITED = "\n\n/** Anything with an area. */\n" + _SHAPES.split("\n", 1)[1].replace(
    "export interface Shape", "interface Shape"
).replace("/** A point in 2D. */", "/** A point in 3D space. */").replace(
    "export type Point", "type Point"
)
_POLYGLOT = {
    "w.py": (
        "import threading\n\n\nclass W(threading.Thread):\n"
        '    """doc"""\n\n    @property\n    def p(self):\n        return 1\n\n'
        "    def run(self):\n        return helper(1)\n\n\n"
        "def helper(a, b=2):\n    return a\n"
    ),
    "T.java": "public class T implements Runnable {\n  @Override public void run() {}\n}\n",
    "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n',
    "src/lib.rs": (
        "pub fn f() -> u32 { 1 }\nmacro_rules! m { () => {} }\n"
        "pub enum E { A }\npub struct S;\nimpl S { pub fn g(&self) {} }\n"
    ),
    "main.go": "package main\n\ntype I interface{ M() }\n\nfunc main() {}\n",
    "C.cs": 'namespace N { public class C { public override string ToString() { return ""; } } }\n',
    "u.c": "union U { int a; float b; };\nstatic int h(void) { return 0; }\n",
    "shapes.ts": _SHAPES,
}


def _index(repo: Path, out: Path) -> dict[str, object]:
    result = runner.invoke(app, ["index", "--repo-path", str(repo), "-o", str(out)])
    assert result.exit_code == 0, result.output
    index = pb.GraphCodeIndex.FromString((out / cs.PROTOBUF_INDEX_FILE).read_bytes())
    payloads: dict[str, object] = {}
    for node in index.nodes:
        kind = node.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF)
        payload = getattr(node, kind)
        if hasattr(payload, "qualified_name"):
            payloads[payload.qualified_name.split(".", 1)[-1]] = payload
    return payloads


@pytest.fixture
def shapes(tmp_path: Path) -> Path:
    repo = tmp_path / "shapes"
    repo.mkdir()
    (repo / "shapes.ts").write_text(_SHAPES, encoding="utf-8")
    return repo


@pytest.mark.parametrize(
    ("qn", "span", "docstring"),
    [
        ("shapes.Shape", (2, 4), "A drawable shape."),
        ("shapes.Colour", (6, 9), "Supported colours."),
        ("shapes.Point", (11, 11), "A point in 2D."),
    ],
    ids=["interface", "enum", "type-alias"],
)
def test_a_type_keeps_its_span_docstring_and_export(
    shapes: Path, tmp_path: Path, qn: str, span: tuple[int, int], docstring: str
) -> None:
    payload = _index(shapes, tmp_path / "idx")[qn]
    assert (payload.start_line, payload.end_line) == span, payload
    assert docstring in payload.docstring, payload
    assert payload.is_exported is True, payload
    assert payload.path == "shapes.ts", payload


def test_diff_index_sees_a_type_that_moved_or_stopped_being_exported(
    shapes: Path, tmp_path: Path
) -> None:
    _index(shapes, tmp_path / "idx1")
    (shapes / "shapes.ts").write_text(_EDITED, encoding="utf-8")
    _index(shapes, tmp_path / "idx2")
    changed = diff_indexes(tmp_path / "idx1", tmp_path / "idx2")["nodes"]["changed"]
    by_name = {
        key.split("::")[1].rsplit(".", 1)[-1]: delta for key, delta in changed.items()
    }
    assert {"Shape", "Colour", "Point"} <= set(by_name), sorted(changed)
    assert by_name["Shape"]["is_exported"] == {"old": True, "new": False}
    assert by_name["Point"]["is_exported"] == {"old": True, "new": False}
    assert by_name["Colour"]["start_line"] == {"old": 6, "new": 8}


def test_methods_and_functions_keep_their_signature_facts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "poly"
    for rel, text in _POLYGLOT.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    payloads = _index(repo, tmp_path / "idx")
    run = payloads["w.W.run"]
    assert run.overrides_external is True and run.path == "w.py", run
    assert payloads["w.W.p"].is_property is True
    assert list(payloads["w.helper"].positional_params) == ["a", "b"]
    assert "static" in payloads["u.h"].modifiers
    assert payloads["src.lib.m"].is_macro is True


def test_every_property_a_run_writes_is_exported_or_listed(tmp_path: Path) -> None:
    # Default-deny on the WRITER side: the schema parity test checks what
    # NODE_SCHEMAS declares, this checks what an index run actually writes,
    # so a property a parser starts writing fails here until the proto (or
    # the closed `_NOT_EXPORTED` list) names it.
    repo = tmp_path / "poly"
    for rel, text in _POLYGLOT.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(repo, mock)
    dropped: set[str] = set()
    for call in mock.ensure_node_batch.call_args_list:
        label, props = str(call.args[0]), call.args[1]
        oneof = LABEL_TO_ONEOF_FIELD[cs.NodeLabel(label)]
        message = pb.Node.DESCRIPTOR.fields_by_name[oneof].message_type
        fields = {field.name for field in message.fields}
        allowed = _NOT_EXPORTED.get(label, frozenset())
        dropped |= {
            f"{label}.{key}"
            for key, value in props.items()
            if value is not None and key not in fields and key not in allowed
        }
    assert not dropped, sorted(dropped)
