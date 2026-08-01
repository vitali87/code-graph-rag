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
    # `emitter.call(...)` where `emitter` is a LOCAL binding shadowing a
    # module-level function of the same name must not link to that function:
    # the local holds some other value and the name match is coincidence.
    src = """'use strict'
function emitter () { return 'decoy' }
function main (handlers) {
  const emitter = { call: handlers.onCall }
  emitter.call(1)
  return 2
}
module.exports = { main, emitter }
"""
    cap = _run(tmp_path, src)
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "emitter"))


def test_param_shadowing_function_name_does_not_mislink(tmp_path: Path) -> None:
    # A PARAMETER named like a module-level function carries whatever the
    # caller passed; `.call` through it must not bind the module function.
    src = """'use strict'
function helper (x) { return x + 1 }
function main (helper) {
  helper.call(this, 1)
  return 2
}
module.exports = { main, helper }
"""
    cap = _run(tmp_path, src)
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "helper"))


def test_cross_file_name_collision_does_not_mislink(tmp_path: Path) -> None:
    # A local bound from an untyped expression and `.call`ed must never bind
    # by bare name to a same-named function in an UNRELATED file (the
    # tapable shape: `const hook = compiler.hooks.beforeRun; hook.call(...)`).
    (tmp_path / "other.js").write_text("""'use strict'
function hook (x) { return x }
module.exports = { hook }
""")
    src = """'use strict'
function main (compiler) {
  const hook = compiler.hooks.beforeRun
  hook.call(compiler)
  return 2
}
module.exports = { main }
"""
    cap = _run(tmp_path, src, filename="user.js")
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "hook"))


def test_local_function_receiver_still_links(tmp_path: Path) -> None:
    # A LOCAL arrow invoked via `.call` resolves to the local itself (it is
    # the declared binding), so the shadow guard must not block it.
    src = """'use strict'
function main () {
  const getValue = () => 1
  getValue.call(this)
  return 2
}
module.exports = { main }
"""
    edges = _edges_to_leaf(_run(tmp_path, src), "getValue")
    assert any(f.endswith(".main") for f, _rel in edges)


def test_this_arg_does_not_shift_callback_flow(tmp_path: Path) -> None:
    # `runner.call(this, target, decoy)` passes target as parameter 0 and
    # decoy as parameter 1; mapping the raw argument list positionally would
    # bind `second` (the invoked parameter) to TARGET. No such wrong edge may
    # be emitted, and the real arguments must stay referenced from the caller.
    src = """'use strict'
function runner (first, second) { second(); return first }
function target () { return 1 }
function decoy () { return 2 }
function main () {
  runner.call(this, target, decoy)
  return 3
}
module.exports = { main }
"""
    cap = _run(tmp_path, src)
    assert not any(
        f.endswith(".runner") and rel == str(cs.RelationshipType.CALLS)
        for f, rel in _edges_to_leaf(cap, "target")
    )
    # Both function-valued arguments stay reachable from the call site.
    assert any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "target"))
    assert any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "decoy"))


def test_sibling_nested_function_is_not_visible(tmp_path: Path) -> None:
    # A function nested inside a SIBLING scope is not lexically visible at
    # the call site; the same-file prefix must not stand in for scoping.
    src = """'use strict'
function other () { function helper (x) { return x }; return helper }
function main () {
  helper.call(this, 1)
  return 2
}
module.exports = { main, other }
"""
    cap = _run(tmp_path, src)
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "helper"))


def test_for_of_binding_does_not_mislink(tmp_path: Path) -> None:
    # A loop binding carries collection elements; `.call` through it must not
    # bind the same-named module function (the tapable hook shape as a loop).
    src = """'use strict'
function hook (x) { return x }
function main (compiler) {
  for (const hook of compiler.hooks) {
    hook.call(this, 1)
  }
  return 2
}
module.exports = { main, hook }
"""
    cap = _run(tmp_path, src)
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "hook"))


def test_catch_binding_does_not_mislink(tmp_path: Path) -> None:
    src = """'use strict'
function err () { return 'decoy' }
function main (fn) {
  try { fn() } catch (err) { err.call(this, 1) }
  return 2
}
module.exports = { main, err }
"""
    cap = _run(tmp_path, src)
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "err"))


def test_nested_callback_param_does_not_mislink(tmp_path: Path) -> None:
    # Calls inside an anonymous callback bubble to the enclosing named
    # caller, but the callback's OWN parameter still shadows the module
    # function at the call site.
    src = """'use strict'
function handler (x) { return x }
function main (list) {
  list.forEach(function (handler) { handler.call(this, 1) })
  return 2
}
module.exports = { main, handler }
"""
    cap = _run(tmp_path, src)
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "handler"))


def test_module_level_callback_shadow_does_not_mislink(tmp_path: Path) -> None:
    # A module-level anonymous callback whose local rebinds the name must not
    # emit a module-scope edge to the same-named module function.
    src = """'use strict'
function hook (x) { return x }
function setup (cb) { return cb }
setup(() => {
  const hook = { call: (n) => n }
  hook.call(1)
})
module.exports = { hook, setup }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls for _f, rel in _edges_to_leaf(cap, "hook"))


def test_duplicate_declarations_suppress(tmp_path: Path) -> None:
    # Two same-named hoisted declarations: the name has two introductions, so
    # which one the call hits is unknowable to the unique-binding rule; no
    # edge may be invented (the shadowed first declaration IS dead code, and
    # an edge onto either variant could falsely revive it).
    src = """'use strict'
function helper (x) { return x + 1 }
function helper (x) { return x + 2 }
function main () {
  helper.call(this, 1)
  return 2
}
module.exports = { main }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and f.endswith(".main") for f, rel in _edges_to_leaf(cap, "helper")
    )


def test_variant_caller_local_arrow_still_links(tmp_path: Path) -> None:
    # A DUPLICATE caller (registered as a @line variant) still resolves its
    # own local arrow receiver by span; the feature must not switch off for
    # variant callers.
    src = """'use strict'
function main () { return 3 }
function main () {
  const getValue = () => 1
  getValue.call(this)
  return 2
}
module.exports = { main }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert any(rel == calls for _f, rel in _edges_to_leaf(cap, "getValue"))


def test_ts_cast_receiver_links(tmp_path: Path) -> None:
    # A cast-wrapped receiver (`(helper as any).call(...)`) peels to the
    # identifier and resolves like the plain form.
    src = """export function helper (x: number): number { return x + 1 }
export function main (): number {
  (helper as any).call(this, 1)
  return 2
}
"""
    cap = _run(tmp_path, src, filename="a.ts")
    edges = _edges_to_leaf(cap, "helper")
    assert any(f.endswith(".main") for f, _rel in edges)


def _self_loops(cap: _Capture) -> set[str]:
    return {
        str(frm)
        for frm, rel, to in cap.rels
        if str(frm) == str(to) and rel == str(cs.RelationshipType.CALLS)
    }


def test_c_style_for_var_receiver_does_not_mislink(tmp_path: Path) -> None:
    # The C-style loop variable is the binding at the call site, not the
    # module function; `for_statement` introduces it outside any block.
    src = """'use strict'
function hook (x) { return x + 1 }
function main (list) {
  for (var hook = list.head; hook; hook = hook.next) { hook.call(this, 1) }
  return 2
}
module.exports = { main, hook }
"""
    cap = _run(tmp_path, src)
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "hook"))


def test_switch_case_binding_does_not_mislink(tmp_path: Path) -> None:
    # A lexical declaration inside a switch case shadows the module function;
    # the call through it must emit nothing.
    src = """'use strict'
function helper (x) { return x + 1 }
function main (o, x) {
  switch (x) {
    case 1:
      const helper = o.thing
      helper.call(this, 1)
      break
  }
  return 2
}
module.exports = { main, helper }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and f.endswith(".main") for f, rel in _edges_to_leaf(cap, "helper")
    )


def test_switch_case_unshadowed_call_links(tmp_path: Path) -> None:
    # The positive twin: with no shadowing binding anywhere, a call inside a
    # switch case links to the module function.
    src = """'use strict'
function helper (x) { return x + 1 }
function main (x) {
  switch (x) {
    case 1: {
      helper.call(this, 1)
      break
    }
  }
  return 2
}
module.exports = { main, helper }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert any(
        rel == calls and f.endswith(".main") for f, rel in _edges_to_leaf(cap, "helper")
    )


def test_try_block_var_hoists_to_function_scope(tmp_path: Path) -> None:
    # `try { var hook = ... } catch {}` binds `hook` for the WHOLE function;
    # the call after the try must not bind the module function.
    src = """'use strict'
function hook (x) { return x + 1 }
function main (list) {
  try { var hook = list.resolve() } catch (e) { }
  hook.call(this, 1)
  return 2
}
module.exports = { main, hook }
"""
    cap = _run(tmp_path, src)
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "hook"))


def test_sibling_block_var_arrow_links(tmp_path: Path) -> None:
    # The mirror case: a var-bound arrow in a nested block hoists to function
    # scope, so the call after the block resolves to the arrow.
    src = """'use strict'
function main (cond) {
  if (cond) { var doIt = () => 1 }
  doIt.call(this)
  return 2
}
module.exports = { main }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert any(rel == calls for _f, rel in _edges_to_leaf(cap, "doIt"))


def test_object_method_shadowing_name_calls_module_function(tmp_path: Path) -> None:
    # An object method's NAME does not bind in its own body; the call inside
    # it resolves to the module function, never to a phantom self-loop.
    src = """'use strict'
function helper (x) { return x + 1 }
const o = { helper (x) { helper.call(this, 1); return x } }
module.exports = { o, helper }
"""
    cap = _run(tmp_path, src)
    assert not _self_loops(cap)
    calls = str(cs.RelationshipType.CALLS)
    assert any(rel == calls and str(to) == "p.a.helper" for _frm, rel, to in cap.rels)


def test_class_method_named_like_function_calls_module_function(
    tmp_path: Path,
) -> None:
    src = """'use strict'
function helper (x) { return x + 1 }
class C {
  helper (x) { helper.call(this, 1); return x }
}
module.exports = { C, helper }
"""
    cap = _run(tmp_path, src)
    assert not _self_loops(cap)
    calls = str(cs.RelationshipType.CALLS)
    assert any(rel == calls and str(to) == "p.a.helper" for _frm, rel, to in cap.rels)


def test_named_function_expression_param_shadows_self_name(tmp_path: Path) -> None:
    # A parameter sharing the function expression's own name wins inside the
    # body, so the call must not bind the expression to itself.
    src = """'use strict'
const f = function helper (helper) { helper.call(this, 1); return 2 }
module.exports = { f }
"""
    cap = _run(tmp_path, src)
    assert not _self_loops(cap)


def test_named_function_expression_self_recursion_links(tmp_path: Path) -> None:
    # Without a shadowing param, the self-name IS the binding: genuine
    # `.call` self-recursion keeps the function reachable.
    src = """'use strict'
const f = function helper () { helper.call(this) }
module.exports = { f }
"""
    cap = _run(tmp_path, src)
    assert _self_loops(cap)


def test_for_of_var_receiver_does_not_mislink(tmp_path: Path) -> None:
    # `for (var hook of ...)` is function-scoped: after the loop the name is
    # the loop binding, never the module function.
    src = """'use strict'
function hook (x) { return x + 1 }
function main (list) {
  for (var hook of list.hooks) { }
  hook.call(this, 1)
  return 2
}
module.exports = { main, hook }
"""
    cap = _run(tmp_path, src)
    assert not any(f.endswith(".main") for f, _rel in _edges_to_leaf(cap, "hook"))


def test_block_function_declaration_var_redeclared_suppresses(
    tmp_path: Path,
) -> None:
    # A local function declaration plus a same-named `var` in a nested block:
    # the runtime value after the try is the var's; no edge may bind the
    # declaration.
    src = """'use strict'
function main (list) {
  function hook (x) { return x + 1 }
  try { var hook = list.resolve() } catch (e) { }
  hook.call(this, 1)
  return 2
}
module.exports = { main }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls for _f, rel in _edges_to_leaf(cap, "hook"))


def test_module_block_var_redeclaration_suppresses(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x + 1 }
{ var helper = globalThis.pick }
helper.call(this, 1)
module.exports = { helper }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls for _f, rel in _edges_to_leaf(cap, "helper"))


def test_class_static_block_var_shadow_suppresses(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x + 1 }
class C {
  static {
    if (globalThis.cond) { var helper = globalThis.pick }
    helper.call(this, 1)
  }
}
module.exports = { C, helper }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls for _f, rel in _edges_to_leaf(cap, "helper"))


def test_generator_function_expression_receiver_links(tmp_path: Path) -> None:
    # A `const gen = function* () {...}` receiver registers like any inline
    # function (issue #994) and the `.call` resolves to it by span.
    src = """'use strict'
function main () {
  const gen = function* () { yield 1 }
  gen.call(this)
  return 2
}
module.exports = { main }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert any(rel == calls for _f, rel in _edges_to_leaf(cap, "gen"))


def test_export_const_arrow_receiver_links(tmp_path: Path) -> None:
    # The modern ESM idiom: an exported const arrow invoked only via `.call`
    # must link; the export_statement wrapper must not shrink the binding's
    # container to the single statement.
    src = """export const helper = (x: number): number => x + 1
export function main (): number {
  helper.call(this, 1)
  return 2
}
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert any(
        rel == calls and f.endswith(".main") for f, rel in _edges_to_leaf(cap, "helper")
    )


def test_default_param_value_reference_does_not_suppress(tmp_path: Path) -> None:
    # `wrap (cb = helper)` READS helper as a default value; it introduces
    # `cb`, not `helper`, so the genuine `.call` elsewhere keeps its edge.
    src = """'use strict'
function helper (x) { return x }
function wrap (cb = helper) { return cb }
function main () {
  helper.call(this, 1)
  return 2
}
module.exports = { main, wrap, helper }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert any(
        rel == calls and f.endswith(".main") for f, rel in _edges_to_leaf(cap, "helper")
    )


def test_ts_typeof_param_type_does_not_suppress(tmp_path: Path) -> None:
    # A `typeof helper` TYPE QUERY in a parameter annotation is a use of the
    # name, not an introduction.
    src = """export function helper (x: number): number { return x }
export function wrap (cb: typeof helper): void { void cb }
export function main (): void {
  helper.call(this, 1)
}
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert any(
        rel == calls and f.endswith(".main") for f, rel in _edges_to_leaf(cap, "helper")
    )


def test_sibling_function_local_does_not_suppress(tmp_path: Path) -> None:
    # The fastify lib/route.js shape: a `const route = ...` inside a SIBLING
    # function can never be in scope at the call site, so it must not
    # suppress the edge onto the module-level `route`.
    src = """'use strict'
function prepareRoute (opts) {
  route.call(this, { opts })
}
function findRoute (router) {
  const route = router.find()
  return route
}
function route (o) { return o }
module.exports = { prepareRoute, findRoute }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert any(
        rel == calls and f.endswith(".prepareRoute")
        for f, rel in _edges_to_leaf(cap, "route")
    )


def test_static_block_var_not_visible_outside(tmp_path: Path) -> None:
    # A `var` inside a class static block scopes to the block; a site outside
    # must not receive an edge onto it (the name is unbound there).
    src = """'use strict'
class C {
  static { var doIt = () => 1 }
}
function main () {
  doIt.call(this)
  return 2
}
module.exports = { main, C }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls for _f, rel in _edges_to_leaf(cap, "doIt"))


def test_destructuring_reassignment_suppresses(tmp_path: Path) -> None:
    # `({ helper } = ...)` and `[helper] = ...` rebind the name to arbitrary
    # values; the declared function is no longer what the site invokes.
    src = """'use strict'
function helper (x) { return x + 1 }
;({ helper } = require('./m'))
function main () {
  helper.call(this, 1)
  return 2
}
module.exports = { main }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls for _f, rel in _edges_to_leaf(cap, "helper"))


def test_array_destructuring_reassignment_suppresses(tmp_path: Path) -> None:
    src = """'use strict'
function helper (x) { return x + 1 }
function main (list) {
  ;[helper] = list
  helper.call(this, 1)
  return 2
}
module.exports = { main }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls for _f, rel in _edges_to_leaf(cap, "helper"))


def test_with_statement_poisons_file(tmp_path: Path) -> None:
    # `with (obj)` makes every name dynamically resolvable through obj; no
    # receiver in the file can be trusted.
    src = """function helper (x) { return x + 1 }
function main (obj) {
  with (obj) { helper.call(this, 1) }
  return 2
}
module.exports = { main }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls for _f, rel in _edges_to_leaf(cap, "helper"))


def test_switch_case_function_declaration_shadow_suppresses(tmp_path: Path) -> None:
    # A function declaration under one case is live across the WHOLE switch
    # body; a call in another case hits the inner one, so no edge may bind
    # the outer namesake.
    src = """'use strict'
function helper (x) { return 'outer' }
function main (x) {
  switch (x) {
    case 1:
      function helper (y) { return 'inner' }
      break
    case 2:
      helper.call(this, 1)
      break
  }
  return 2
}
module.exports = { main, helper }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(to) == "p.a.helper" for _frm, rel, to in cap.rels
    )


def test_sloppy_block_function_declaration_shadow_suppresses(
    tmp_path: Path,
) -> None:
    # Annex B: in a non-strict file a block-level function declaration is
    # function-scoped; after the block the name is the inner function or
    # undefined, never the outer one.
    src = """function h2 (x) { return 'outer' }
function main (cond) {
  if (cond) { function h2 (y) { return 'inner' } }
  return h2.call(this, 1)
}
module.exports = { main, h2 }
"""
    cap = _run(tmp_path, src)
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls and str(to) == "p.a.h2" for _frm, rel, to in cap.rels)


def test_ts_enum_shadow_suppresses(tmp_path: Path) -> None:
    # A local TS enum is a VALUE binding (it compiles to a var); a call
    # through the shadowed name must not bind the outer function.
    src = """export function helper (x: number): number { return x + 1 }
export function main (): void {
  enum helper { A, B }
  helper.call(this, 1)
}
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls for _f, rel in _edges_to_leaf(cap, "helper"))


def test_ts_import_require_shadow_suppresses(tmp_path: Path) -> None:
    # `import helper = require('./other')` inside a namespace binds a value
    # name that shadows the outer function for sites in that namespace.
    (tmp_path / "other.ts").write_text("export function o (): number { return 1 }\n")
    src = """export function helper (x: number): number { return x + 1 }
namespace N {
  import helper = require('./other')
  export function main (): void {
    helper.call(this, 1)
  }
}
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(to) == "p.a.helper" for _frm, rel, to in cap.rels
    )


def test_reused_updater_does_not_use_stale_bindings(tmp_path: Path) -> None:
    # A reused updater (watch mode, a second run) re-parses files; the
    # binding index from the FIRST parse must not resolve spans against the
    # new registry, or the edge lands on whatever now sits at the old
    # line/column.
    (tmp_path / "a.js").write_text("""'use strict'
function helper () { return 1 }
function main () { helper.call(this) }
module.exports = { main }
""")
    parsers, queries = load_parsers()
    cap = _Capture()
    updater = GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    (tmp_path / "a.js").write_text("""'use strict'
function decoy () { return 9 }
function main () { const helper = require('./z'); helper.call(this) }
module.exports = { main }
""")
    cap.rels.clear()
    updater.run(force=True)
    assert not any(
        str(to).endswith(".decoy") and rel == str(cs.RelationshipType.CALLS)
        for _frm, rel, to in cap.rels
    )


def test_ts_namespace_function_not_visible_outside(tmp_path: Path) -> None:
    # A function declared inside a namespace is scoped to it; a site outside
    # must not link to it, while a site inside must.
    src = """namespace N {
  function helper (): string { return 'inner' }
  export function innerUse (): string { return '' + helper.call(this) }
}
export function outerUse (): void {
  helper.call(this, 1)
}
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    outer = {
        (str(frm), str(to))
        for frm, rel, to in cap.rels
        if rel == calls and str(frm).endswith(".outerUse")
    }
    assert not outer
    assert any(
        rel == calls and str(frm).endswith(".innerUse")
        for frm, rel, to in cap.rels
        if str(to).endswith(".helper")
    )


def test_ts_namespace_import_alias_shadow_suppresses(tmp_path: Path) -> None:
    # `import helper = Other.thing` inside a namespace binds a value name.
    src = """function helper (): string { return 'outer' }
namespace Other {
  export const thing = { call: (x: unknown) => x }
}
namespace N {
  import helper = Other.thing
  export function use (): void {
    helper.call(this)
  }
}
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(to) == "p.a.helper" for _frm, rel, to in cap.rels
    )


def test_ts_default_param_value_does_not_suppress(tmp_path: Path) -> None:
    # The TS twin of the JS default-value test, with the default in the SAME
    # function as the site: `main (cb: any = helper)` reads helper; it
    # introduces only cb, so the call inside main keeps its edge.
    src = """export function helper (): number { return 1 }
export function main (cb: any = helper): void {
  helper.call(this)
  void cb
}
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert any(
        rel == calls and f.endswith(".main") for f, rel in _edges_to_leaf(cap, "helper")
    )


def test_ts_merged_namespace_member_shadow_suppresses(tmp_path: Path) -> None:
    # Declaration merging: exported members of every `namespace N` block
    # share one scope, so a site in block 2 sees block 1's helper, and the
    # file-level namesake must not receive the edge.
    src = """function helper (): number { return 1 }
namespace N { export function helper (): number { return 2 } }
namespace N { export function use (): number { return helper.call(this) } }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(to) == "p.a.helper" for _frm, rel, to in cap.rels
    )


def test_ts_merged_namespace_member_links_without_namesake(tmp_path: Path) -> None:
    # The same merging with no outer namesake: the block-1 member is the
    # unique binding and the block-2 call links to it.
    src = """namespace N { export function helper (): number { return 2 } }
namespace N { export function use (): number { return helper.call(this) } }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert any(
        rel == calls and str(frm).endswith(".use")
        for frm, rel, to in cap.rels
        if str(to).rsplit(cs.SEPARATOR_DOT, 1)[-1] == "helper"
    )


def test_merged_namespace_ambient_namesake_no_edge_outside(tmp_path: Path) -> None:
    # At file scope the name is the ambient declaration; the merged
    # namespace's member is visible only inside N's blocks, so the outer
    # site must emit nothing at all.
    src = """declare function helper (): number
namespace N { export function helper (): number { return 2 } }
namespace N { export function other (): void {} }
export function outerUse (): void { helper.call(this) }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(frm).endswith(".outerUse") for frm, rel, _to in cap.rels
    )


def test_merged_namespace_nonexported_member_stays_private(tmp_path: Path) -> None:
    # A NON-exported member is visible only in its own block, not in the
    # namespace's other blocks and never outside.
    src = """namespace N {
  function helper (): number { return 1 }
  export function inner (): void { helper.call(this) }
}
namespace N { export function other (): void {} }
export function outerUse (): void { helper.call(this) }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(frm).endswith(".outerUse") for frm, rel, _to in cap.rels
    )
    # Inside its own block the member still resolves.
    assert any(
        rel == calls and str(frm).endswith(".inner")
        for frm, rel, to in cap.rels
        if str(to).rsplit(cs.SEPARATOR_DOT, 1)[-1] == "helper"
    )


def test_merged_namespace_nested_namespace_stays_narrow(tmp_path: Path) -> None:
    # A member of a NON-merged inner namespace M must not leak file-wide
    # just because the outer N is merged.
    src = """namespace N {
  export namespace M { export function helper (): number { return 1 } }
}
namespace N { export function other (): void {} }
export function outerUse (): void { helper.call(this) }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(frm).endswith(".outerUse") for frm, rel, _to in cap.rels
    )


def test_distinct_namespaces_sharing_leaf_name_do_not_merge(tmp_path: Path) -> None:
    # `A.N` and top-level `N` are different namespaces; sharing the leaf
    # name must not replicate A.N's members into N.
    src = """declare function helper (): number
namespace A { export namespace N { export function helper (): number { return 2 } } }
namespace N { export function use (): number { return helper.call(this) } }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls for _f, rel in _edges_to_leaf(cap, "helper"))


def test_sibling_inner_namespaces_do_not_merge(tmp_path: Path) -> None:
    # `Foo.Utils` and `Bar.Utils` are distinct; a call in Bar.Utils to a
    # file-level function must not bind Foo.Utils' member.
    src = """function log (m: string): void { }
namespace Foo { export namespace Utils { export function log (m: string): void { } } }
namespace Bar { export namespace Utils { export function run (): void { log.call(this, 'x') } } }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(rel == calls and "Foo" in str(to) for _frm, rel, to in cap.rels)
    # The file-level log is the unique relevant binding at the site.
    assert any(rel == calls and str(to) == "p.a.log" for _frm, rel, to in cap.rels)


def test_merged_namespace_exported_const_links(tmp_path: Path) -> None:
    # The declarator replication path: an exported const arrow in block 1 is
    # visible in block 2.
    src = """namespace N { export const helper = (): number => 1 }
namespace N { export function use (): number { return helper.call(this) } }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert any(
        rel == calls and str(frm).endswith(".use")
        for frm, rel, to in cap.rels
        if str(to).rsplit(cs.SEPARATOR_DOT, 1)[-1] == "helper"
    )


def test_nested_namespace_merges_across_parent_blocks(tmp_path: Path) -> None:
    # Two inner `N` blocks under two `A` blocks are the SAME namespace A.N;
    # block 2's call must not fall through to the file-level namesake.
    src = """function helper (): number { return 1 }
namespace A { export namespace N { export function helper (): number { return 2 } } }
namespace A { export namespace N { export function use (): number { return helper.call(this) } } }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(to) == "p.a.helper" for _frm, rel, to in cap.rels
    )


def test_mixed_spelling_namespace_merges(tmp_path: Path) -> None:
    # `namespace A { export namespace N }` and `namespace A.N` are the same
    # namespace; the dotted spelling must join the group.
    src = """function helper (): number { return 1 }
namespace A { export namespace N { export function helper (): number { return 2 } } }
namespace A.N { export function use (): number { return helper.call(this) } }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(to) == "p.a.helper" for _frm, rel, to in cap.rels
    )


def test_depth_three_namespace_merges(tmp_path: Path) -> None:
    src = """function helper (): number { return 1 }
namespace A { export namespace B { export namespace N { export function helper (): number { return 2 } } } }
namespace A { export namespace B { export namespace N { export function use (): number { return helper.call(this) } } } }
"""
    cap = _run(tmp_path, src, filename="a.ts")
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(to) == "p.a.helper" for _frm, rel, to in cap.rels
    )
