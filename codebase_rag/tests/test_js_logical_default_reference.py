# A function referenced as a fallback operand of a logical default in an
# assignment (`x = a || fn`, chained `x = a || b || fn`, `x = a ?? fn`) flows
# as the assigned VALUE, so it must be referenced or dead-code falsely flags
# it. Found dogfooding fastify: `options.clientErrorHandler =
# options.clientErrorHandler || defaultClientErrorHandler` (fastify.js) and
# the chained `this.schemaErrorFormatter = a || b || defaultSchemaErrorFormatter`
# (lib/context.js). Issue #990.
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

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

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


def _run(tmp_path: Path, src: str, filename: str = "a.js") -> _Capture:
    (tmp_path / filename).write_text(src)
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


def _referenced_from(cap: _Capture, caller_leaf: str, target_leaf: str) -> bool:
    return any(
        str(frm).rsplit(cs.SEPARATOR_DOT, 1)[-1] == caller_leaf
        and str(to).rsplit(cs.SEPARATOR_DOT, 1)[-1] == target_leaf
        and rel == str(cs.RelationshipType.REFERENCES)
        for frm, rel, to in cap.rels
    )


def test_or_default_assignment_references_fallback(tmp_path: Path) -> None:
    src = """'use strict'
function fallback () { return 1 }
function main (options) {
  options.handler = options.handler || fallback
  return options
}
module.exports = { main }
"""
    assert _referenced_from(_run(tmp_path, src), "main", "fallback")


def test_chained_or_default_references_last_operand(tmp_path: Path) -> None:
    # The fastify lib/context.js shape: a multi-line chain whose LAST operand
    # is the module-level default.
    src = """'use strict'
function defaultFormatter (errors) { return String(errors) }
function Context (opts, server) {
  this.schemaErrorFormatter =
    opts.schemaErrorFormatter ||
    server.schemaErrorFormatter ||
    defaultFormatter
}
module.exports = { Context }
"""
    assert _referenced_from(_run(tmp_path, src), "Context", "defaultFormatter")


def test_nullish_default_references_fallback(tmp_path: Path) -> None:
    src = """'use strict'
function fallback () { return 1 }
function main (options) {
  const handler = options.handler ?? fallback
  return handler
}
module.exports = { main }
"""
    assert _referenced_from(_run(tmp_path, src), "main", "fallback")


def test_and_operand_references_fallback(tmp_path: Path) -> None:
    # `a && fn` can evaluate TO fn; the assigned value may be the function.
    src = """'use strict'
function fallback () { return 1 }
function main (options) {
  const handler = options.enabled && fallback
  return handler
}
module.exports = { main }
"""
    assert _referenced_from(_run(tmp_path, src), "main", "fallback")


def test_comparison_operand_is_not_referenced(tmp_path: Path) -> None:
    # `x === fn` only tests identity; the assigned boolean never IS the
    # function, so no reference may be emitted for non-logical operators.
    src = """'use strict'
function fallback () { return 1 }
function main (options) {
  const isDefault = options.handler === fallback
  return isDefault
}
module.exports = { main }
"""
    assert not _referenced_from(_run(tmp_path, src), "main", "fallback")


def test_or_default_inline_function_still_references(tmp_path: Path) -> None:
    # The already-working express shape (`done = done || function (err) {}`)
    # must keep its reference.
    src = """'use strict'
function main (done) {
  done = done || function fallbackDone (err) { return err }
  return done
}
module.exports = { main }
"""
    assert _referenced_from(_run(tmp_path, src), "main", "fallbackDone")


def test_go_boolean_operands_are_not_referenced(tmp_path: Path) -> None:
    # Go spells boolean `||` with the same binary_expression node and
    # operator field; a bool named like a method must not revive it.
    src = """package m

type T struct{}

func (t T) valid() bool { return true }

func Run(valid bool, other bool) bool {
\tres := valid || other
\treturn res
}
"""
    cap = _run(tmp_path, src, filename="a.go")
    assert not any(
        str(to).rsplit(cs.SEPARATOR_DOT, 1)[-1] == "valid"
        and rel == str(cs.RelationshipType.REFERENCES)
        for _frm, rel, to in cap.rels
    )


def test_and_left_operand_is_not_referenced(tmp_path: Path) -> None:
    # A function value is always truthy, so the LEFT operand of `&&` can
    # never be the expression's result; referencing it would falsely revive.
    src = """'use strict'
function fallback () { return 1 }
function main (options) {
  const handler = fallback && options.other
  return handler
}
module.exports = { main }
"""
    assert not _referenced_from(_run(tmp_path, src), "main", "fallback")
