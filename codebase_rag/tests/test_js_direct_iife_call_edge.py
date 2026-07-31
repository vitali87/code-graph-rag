# An unparenthesised direct IIFE (`const a = function () {...}()`) mints an
# `iife_direct_R_C` node on the definition side; the call side must produce
# the same name and resolve it, or every such node is an orphan reported
# dead. Applies equally to generator IIFEs (issue #994 follow-on).
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []
        self.nodes: list[str] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        if qn := properties.get(cs.KEY_QUALIFIED_NAME):
            self.nodes.append(str(qn))

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec[2], str(rel_type), to_spec[2]))

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _run(tmp_path: Path, src: str) -> _Capture:
    (tmp_path / "a.js").write_text(src)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return cap


def _assert_single_direct_iife_called(cap: _Capture) -> None:
    iife_nodes = [n for n in cap.nodes if cs.PREFIX_IIFE_DIRECT in n]
    assert len(iife_nodes) == 1
    assert any(
        rel == str(cs.RelationshipType.CALLS) and str(to) == iife_nodes[0]
        for _frm, rel, to in cap.rels
    )


def test_direct_function_iife_gets_module_call_edge(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const b = function () { return helper(2) }()
module.exports = { b }
"""
    _assert_single_direct_iife_called(_run(tmp_path, src))


def test_direct_generator_iife_gets_module_call_edge(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const a = function* () { yield helper(1) }()
module.exports = { a }
"""
    _assert_single_direct_iife_called(_run(tmp_path, src))


def _assert_named_iife_called(cap: _Capture, leaf: str) -> None:
    # A NAMED IIFE registers under its own name, not a positional iife_*
    # name; the call side must agree or the node and everything only it
    # calls report dead.
    named = [n for n in cap.nodes if n.endswith(f".{leaf}")]
    assert len(named) == 1
    assert any(
        rel == str(cs.RelationshipType.CALLS) and str(to) == named[0]
        for _frm, rel, to in cap.rels
    )


def test_named_direct_generator_iife_gets_call_edge(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const b = function* gnamed () { yield helper(2) }()
module.exports = { b }
"""
    _assert_named_iife_called(_run(tmp_path, src), "gnamed")


def test_named_parenthesized_generator_iife_gets_call_edge(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const b = (function* pnamed () { yield helper(2) })()
module.exports = { b }
"""
    _assert_named_iife_called(_run(tmp_path, src), "pnamed")


def test_named_direct_function_iife_gets_call_edge(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const b = function named () { return helper(2) }()
module.exports = { b }
"""
    _assert_named_iife_called(_run(tmp_path, src), "named")


def test_named_parenthesized_function_iife_gets_call_edge(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const b = (function pnamed () { return helper(2) })()
module.exports = { b }
"""
    _assert_named_iife_called(_run(tmp_path, src), "pnamed")


def _assert_single_anonymous_called(cap: _Capture) -> None:
    anon = [n for n in cap.nodes if cs.PREFIX_ANONYMOUS in n]
    assert len(anon) == 1
    assert any(
        rel == str(cs.RelationshipType.CALLS) and str(to) == anon[0]
        for _frm, rel, to in cap.rels
    )


def test_sequence_generator_iife_gets_call_edge(tmp_path: Path) -> None:
    # The comma operator yields its LAST operand; `(0, fn)()` invokes fn
    # (the indirect-this idiom). The function registers as an anonymous
    # node, so the call side must resolve it by span or it orphans.
    src = """'use strict'
function helper (x) { return x }
const a = (0, function* () { yield helper(1) })()
module.exports = { a }
"""
    _assert_single_anonymous_called(_run(tmp_path, src))


def test_sequence_function_iife_gets_call_edge(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const b = (0, function () { return helper(2) })()
module.exports = { b }
"""
    _assert_single_anonymous_called(_run(tmp_path, src))


def test_double_paren_named_generator_iife_gets_call_edge(tmp_path: Path) -> None:
    # Wrappers NEST; the callee peel must run to a fixpoint or one extra
    # pair of parens orphans the function and everything only it calls.
    src = """'use strict'
function helper (x) { return x }
const b = ((function* gg () { yield helper(1) }))()
module.exports = { b }
"""
    _assert_named_iife_called(_run(tmp_path, src), "gg")


def test_double_paren_anonymous_iife_gets_call_edge(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const a = ((function* () { yield helper(1) }))()
const b = ((function () { return helper(2) }))()
module.exports = { a, b }
"""
    cap = _run(tmp_path, src)
    anon = [n for n in cap.nodes if cs.PREFIX_ANONYMOUS in n]
    assert len(anon) == 2
    calls = str(cs.RelationshipType.CALLS)
    for node in anon:
        assert any(rel == calls and str(to) == node for _frm, rel, to in cap.rels)


def test_nested_sequence_paren_iifes_get_call_edges(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x }
const a = (0, (function* () { yield helper(1) }))()
const b = ((0, function* () { yield helper(2) }))()
module.exports = { a, b }
"""
    cap = _run(tmp_path, src)
    anon = [n for n in cap.nodes if cs.PREFIX_ANONYMOUS in n]
    assert len(anon) == 2
    calls = str(cs.RelationshipType.CALLS)
    for node in anon:
        assert any(rel == calls and str(to) == node for _frm, rel, to in cap.rels)


def _has_inbound(cap: _Capture, node_qn: str) -> bool:
    return any(
        str(to) == node_qn and rel != str(cs.RelationshipType.DEFINES)
        for _frm, rel, to in cap.rels
    )


def test_ternary_callee_function_operands_are_referenced(tmp_path: Path) -> None:
    # A branch-valued callee may invoke ANY of its inline function operands;
    # each must be referenced or the operand (and everything only it calls)
    # reports dead (issue #1002 family).
    src = """'use strict'
function onlyHere (x) { return x }
const k = 1
const a = (k ? function* gt () { yield onlyHere(1) } : null)()
module.exports = { a }
"""
    cap = _run(tmp_path, src)
    named = [n for n in cap.nodes if n.endswith(".gt")]
    assert len(named) == 1
    assert _has_inbound(cap, named[0])


def test_logical_callee_function_operands_are_referenced(tmp_path: Path) -> None:
    src = """'use strict'
function onlyHere (x) { return x }
const b = (null || function () { return onlyHere(2) })()
module.exports = { b }
"""
    cap = _run(tmp_path, src)
    anon = [n for n in cap.nodes if cs.PREFIX_ANONYMOUS in n]
    assert len(anon) == 1
    assert _has_inbound(cap, anon[0])


def test_ts_cast_wrapped_iife_gets_call_edge(tmp_path: Path) -> None:
    # TS cast wrappers are value-transparent; `(fn as any)()` invokes fn.
    src = """function helper (x: number) { return x }
const b = (function* () { yield helper(1) } as any)()
export { b }
"""
    (tmp_path / "a.ts").write_text(src)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    _assert_single_anonymous_called(cap)
