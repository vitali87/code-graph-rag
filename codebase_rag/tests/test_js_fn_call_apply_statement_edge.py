# A locally-known function invoked as `fn.call(this, ...)` or `fn.apply(this,
# ...)` in statement position must record a CALLS edge to `fn`; the receiver is
# a plain identifier resolving to a function, so `.call` is unambiguously
# Function.prototype.call. Value positions (`return fn.call(...)`, `x =
# fn.call(...)`) already link via the bound-callable peel, which is why only
# statement-position invocations report their targets dead. Found dogfooding
# fastify (`multipleBindings.call(this, ...)` in lib/server.js, the
# plugin-utils trio, `_addHook.call(this, ...)`). Issue #988.
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


def _edges_to_leaf(cap: _Capture, leaf: str) -> set[tuple[str, str]]:
    return {
        (str(frm), rel)
        for frm, rel, to in cap.rels
        if str(to).rsplit(cs.SEPARATOR_DOT, 1)[-1] == leaf
        and rel != str(cs.RelationshipType.DEFINES)
    }


def test_statement_fn_call_links_to_function(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x + 1 }
function main () {
  helper.call(this, 1)
  return 2
}
module.exports = { main }
"""
    edges = _edges_to_leaf(_run(tmp_path, src), "helper")
    assert any(f.endswith(".main") for f, _rel in edges)


def test_statement_fn_apply_links_to_function(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x + 1 }
function main () {
  helper.apply(this, [1])
  return 2
}
module.exports = { main }
"""
    edges = _edges_to_leaf(_run(tmp_path, src), "helper")
    assert any(f.endswith(".main") for f, _rel in edges)


def test_fn_call_inside_nested_member_arrow_links(tmp_path: Path) -> None:
    # The fastify lib/server.js shape: the `.call` sits inside an arrow
    # assigned to an options member, itself inside another function.
    src = """'use strict'
function helper (x, cb) { cb(x) }
function main (opts, cb) {
  opts.cb = (err, address) => {
    helper.call(this, err, () => { cb(null, address) })
  }
}
module.exports = { main }
"""
    assert _edges_to_leaf(_run(tmp_path, src), "helper")


def test_real_method_named_call_still_links_to_method(tmp_path: Path) -> None:
    # A genuine method named `call` on a typed receiver must keep linking to
    # the METHOD; the receiver is not a function, so the Function.prototype
    # fallback must not fire, and no edge may target the decoy function
    # sharing the receiver's name.
    src = """'use strict'
function inv () { return 'decoy' }
class Invoker {
  call (x) { return x }
}
function main () {
  const invoker = new Invoker()
  invoker.call(1)
  return 2
}
module.exports = { main, Invoker, inv }
"""
    cap = _run(tmp_path, src)
    call_edges = _edges_to_leaf(cap, "call")
    assert any(f.endswith(".main") for f, _rel in call_edges)
    # The decoy function `inv` gains no edge from main; the module export
    # shorthand legitimately references it, so only the caller is asserted.
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "inv"))


def test_object_receiver_named_like_function_does_not_mislink(tmp_path: Path) -> None:
    # `emitter.call(...)` where `emitter` is a plain object local must not
    # link to an unrelated module-level function named `emitter`.
    src = """'use strict'
function emitter () { return 'decoy' }
function main (handlers) {
  const holder = { call: handlers.onCall }
  holder.call(1)
  return 2
}
module.exports = { main, emitter }
"""
    cap = _run(tmp_path, src)
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "emitter"))
