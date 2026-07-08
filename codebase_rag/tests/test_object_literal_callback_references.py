from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

REFERENCES = cs.RelationshipType.REFERENCES.value


def _run_rels(
    tmp_path: Path, files: dict[str, str], lang_key: str
) -> set[tuple[str, str, str]]:
    # (H) Build the graph for `files` and return (caller_qn, rel_type, callee_qn).
    parsers, queries = load_parsers()
    if lang_key not in parsers:
        pytest.skip(f"{lang_key} parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def _has(
    rels: set[tuple[str, str, str]], caller_suffix: str, rel: str, callee_suffix: str
) -> bool:
    return any(
        a.endswith(caller_suffix) and r == rel and b.endswith(callee_suffix)
        for a, r, b in rels
    )


def _function_qns(tmp_path: Path, files: dict[str, str], lang_key: str) -> set[str]:
    # (H) Build the graph and return the qualified names of all FUNCTION nodes.
    parsers, queries = load_parsers()
    if lang_key not in parsers:
        pytest.skip(f"{lang_key} parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return {
        c.args[1][cs.KEY_QUALIFIED_NAME]
        for c in mock.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.FUNCTION
    }


def test_use_mutation_variable_not_registered_as_function(tmp_path: Path) -> None:
    # (H) `const mutation = useMutation({...})` binds a call_expression, not a
    # (H) function. The inner object-literal arrows (mutationFn/onSuccess) must NOT
    # (H) climb past the pair/call up to the `mutation` declarator and register a
    # (H) bogus FUNCTION node named after the variable -- that phantom node has no
    # (H) incoming edge and reports as dead code (~27 of the template's remaining
    # (H) false positives).
    files = {
        "AddUser.tsx": (
            "import { useMutation } from '@tanstack/react-query'\n\n\n"
            "const AddUser = () => {\n"
            "  const mutation = useMutation({\n"
            "    mutationFn: (d) => save(d),\n"
            "    onSuccess: () => { reset() },\n"
            "  })\n"
            "  return mutation\n"
            "}\n\n\n"
            "function save(d) { return d }\n"
            "function reset() {}\n"
            "export default AddUser\n"
        ),
    }
    fns = _function_qns(tmp_path, files, "typescript")
    assert not any(qn.split(".")[-1].split("@")[0] == "mutation" for qn in fns), (
        f"variable `mutation` wrongly registered as a function; fns={fns}"
    )


def test_inline_arrow_in_component_not_named_after_component(tmp_path: Path) -> None:
    # (H) An inline arrow inside an arrow-const component's JSX (an onClick handler)
    # (H) has no declarator of its own, so climbing ancestors for a name must STOP at
    # (H) the component's function-body boundary -- otherwise it reaches the
    # (H) `const Appearance = () =>` declarator and registers as
    # (H) `module.Appearance.Appearance` (a double-segment phantom with no incoming
    # (H) edge = dead code, and it orphans the real inline handlers from #616).
    files = {
        "widget.tsx": (
            "export const Panel = () => {\n"
            "  return (\n"
            "    <Menu>\n"
            "      <Item onClick={() => setTheme('light')}>L</Item>\n"
            "      <Item onClick={() => setTheme('dark')}>D</Item>\n"
            "    </Menu>\n"
            "  )\n"
            "}\n\n\n"
            "function setTheme(x) {}\n"
        ),
    }
    fns = _function_qns(tmp_path, files, "tsx")
    assert not any(qn.endswith(".Panel.Panel") for qn in fns), (
        f"component arrow double-registered under itself; fns={fns}"
    )


def test_arrow_const_with_inner_arrow_is_callable(tmp_path: Path) -> None:
    # (H) An exported arrow-const whose body contains an inner arrow (`.map(w => ...)`)
    # (H) must register as `utils.getInitials` (single segment) so a cross-module
    # (H) call resolves. The inner arrow must not climb to the getInitials declarator
    # (H) and push the real function to `utils.getInitials.getInitials`, which no call
    # (H) site matches (the util then reports as dead despite being called).
    files = {
        "utils.ts": (
            "export const getInitials = (name) => {\n"
            "  return name.split(' ').map((w) => w[0]).join('')\n"
            "}\n"
        ),
        "user.tsx": (
            "import { getInitials } from './utils'\n\n"
            "export function User() {\n"
            "  return <div>{getInitials('x')}</div>\n"
            "}\n"
        ),
    }
    fns = _function_qns(tmp_path, files, "tsx")
    assert not any(qn.endswith(".getInitials.getInitials") for qn in fns), (
        f"inner arrow mis-named after the getInitials const; fns={fns}"
    )
    # (H) The real function keeps the single-segment name a call site resolves to.
    assert any(qn.endswith(".utils.getInitials") for qn in fns), (
        f"getInitials not registered at the expected single-segment qn; fns={fns}"
    )


def test_function_expression_assigned_to_property_is_referenced(
    tmp_path: Path,
) -> None:
    # (H) `OpenAPI.TOKEN = async () => {...}` stores a function on an object property
    # (H) for a library to invoke later (the openapi-ts token provider); the arrow is
    # (H) anonymous with no incoming edge. The assigning scope must reference it or it
    # (H) reports as dead (the last frontend false positive on the template).
    files = {
        "main.tsx": (
            "import { OpenAPI } from './client'\n\n"
            "OpenAPI.TOKEN = async () => {\n"
            "  return getToken()\n"
            "}\n\n\n"
            "function getToken() {\n"
            "  return ''\n"
            "}\n"
        ),
        "client.ts": "export const OpenAPI = { TOKEN: '' }\n",
    }
    rels = _run_rels(tmp_path, files, "tsx")
    refs = {b for a, r, b in rels if r == REFERENCES and a.endswith(".main")}
    assert any(".main.anonymous_" in b for b in refs), (
        f"function-expression assigned to a property not referenced; refs={refs}"
    )


def test_returned_cleanup_closure_is_referenced(tmp_path: Path) -> None:
    # (H) A useEffect cleanup (`return () => unsubscribe()`) is a function the hook
    # (H) hands back for React to invoke; it registers as an anonymous node under the
    # (H) enclosing hook (the effect arrow is anonymous, so it nests one level up) but
    # (H) has no incoming edge. The scope that returns it must reference it or every
    # (H) effect cleanup reports as dead.
    files = {
        "hook.tsx": (
            "export function useThing() {\n"
            "  useEffect(() => {\n"
            "    subscribe(handler)\n"
            "    return () => unsubscribe(handler)\n"
            "  }, [])\n"
            "}\n\n\n"
            "function subscribe(f) {}\n"
            "function unsubscribe(f) {}\n"
            "function handler() {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    refs = {b for a, r, b in rels if r == REFERENCES and a.endswith("hook.useThing")}
    assert any(".hook.useThing.anonymous_" in b for b in refs), (
        f"returned cleanup closure not referenced; refs={refs}"
    )


def test_object_value_bound_function_is_referenced(tmp_path: Path) -> None:
    # (H) `onError: handleError.bind(showToast)` hands the bound function
    # (H) `handleError` to the mutation config; the `.bind(...)` call resolves to the
    # (H) Function.prototype builtin, so without unwrapping it the real `handleError`
    # (H) gets no incoming edge and reports as dead (and drags down the private
    # (H) helper it calls). The enclosing scope must reference the bound function.
    files = {
        "utils.ts": (
            "function extractErrorMessage(e) { return e.message }\n"
            "export const handleError = function (e) {\n"
            "  return extractErrorMessage(e)\n"
            "}\n"
        ),
        "comp.tsx": (
            "import { handleError } from './utils'\n\n"
            "export function AddItem() {\n"
            "  return doMutation({ onError: handleError.bind(showToast) })\n"
            "}\n\n\n"
            "function doMutation(o) { return o }\n"
            "function showToast(m) {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    edges = {(r, b) for a, r, b in rels if a.endswith("comp.AddItem")}
    assert any(b.endswith("utils.handleError") for _r, b in edges), (
        f"bound function handleError not referenced; edges={edges}"
    )


def test_object_literal_inline_arrow_is_referenced(tmp_path: Path) -> None:
    # (H) useMutation({ mutationFn: () => {}, onSuccess: () => {} }) registers each
    # (H) inline arrow as its own node (AddUser.mutationFn / AddUser.onSuccess); the
    # (H) library invokes them, so the enclosing scope must REFERENCE them or every
    # (H) TanStack Query callback reports as dead (the dominant remaining gap on the
    # (H) FastAPI full-stack template).
    files = {
        "AddUser.tsx": (
            "import { useMutation } from '@tanstack/react-query'\n\n\n"
            "export function AddUser() {\n"
            "  const mutation = useMutation({\n"
            "    mutationFn: (data) => save(data),\n"
            "    onSuccess: () => { reset() },\n"
            "  })\n"
            "  return mutation\n"
            "}\n\n\n"
            "function save(d) { return d }\n"
            "function reset() {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    assert _has(rels, "AddUser.AddUser", REFERENCES, "AddUser.AddUser.mutationFn")
    assert _has(rels, "AddUser.AddUser", REFERENCES, "AddUser.AddUser.onSuccess")


def test_object_literal_inline_function_expr_is_referenced(tmp_path: Path) -> None:
    # (H) A classic function expression as an object value is the same first-class
    # (H) value handoff and must also be referenced.
    files = {
        "config.ts": (
            "export function build() {\n"
            "  register({\n"
            "    handler: function () { run() },\n"
            "  })\n"
            "}\n\n\n"
            "function register(o) { return o }\n"
            "function run() {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    assert _has(rels, "config.build", REFERENCES, "config.build.handler")


def test_arrow_const_component_object_callbacks_referenced(tmp_path: Path) -> None:
    # (H) The real FastAPI-template shape: the component is an arrow bound to a const
    # (H) (const AddUser = () => {...}), and useMutation callbacks live inside it. The
    # (H) definition pass must nest those object-arrows under the component
    # (H) (module.AddUser.mutationFn), matching the component's own qn and the call
    # (H) pass, so the REFERENCES edge connects; otherwise every TanStack callback in
    # (H) an arrow-const component (the whole template) stays dead.
    files = {
        "AddUser.tsx": (
            "import { useMutation } from '@tanstack/react-query'\n\n\n"
            "const AddUser = () => {\n"
            "  const mutation = useMutation({\n"
            "    mutationFn: (d) => save(d),\n"
            "    onSuccess: () => { reset() },\n"
            "  })\n"
            "  return mutation\n"
            "}\n\n\n"
            "function save(d) { return d }\n"
            "function reset() {}\n"
            "export default AddUser\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    assert _has(rels, "AddUser.AddUser", REFERENCES, "AddUser.AddUser.mutationFn")
    assert _has(rels, "AddUser.AddUser", REFERENCES, "AddUser.AddUser.onSuccess")


def test_object_literal_string_key_inline_arrow_is_referenced(tmp_path: Path) -> None:
    # (H) A string-literal key ({'onSuccess': () => {}}) has no property name, so the
    # (H) inline arrow registers as scope.anonymous_<row>_<col>, not scope.onSuccess.
    # (H) The reference must target the actual registered (anonymous) node by the
    # (H) value's position, or the callback still reports as dead.
    files = {
        "widget.tsx": (
            "export function Widget() {\n"
            "  register({\n"
            "    'onSuccess': () => { done() },\n"
            "  })\n"
            "}\n\n\n"
            "function register(o) { return o }\n"
            "function done() {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    refs = {b for a, r, b in rels if r == REFERENCES and a.endswith("widget.Widget")}
    assert any(".widget.Widget.anonymous_" in b for b in refs), (
        f"no anonymous ref emitted; refs={refs}"
    )


def test_inline_arrow_call_argument_is_referenced(tmp_path: Path) -> None:
    # (H) An arrow passed DIRECTLY as a call argument (useCallback(() => {}),
    # (H) setTimeout(() => {}), arr.map(x => ...)) registers as an anonymous node in
    # (H) the enclosing scope but has no incoming edge -- the call consumes it, so
    # (H) the scope must REFERENCE it or every inline callback reports as dead (the
    # (H) dominant remaining false-positive class on the FastAPI template).
    files = {
        "hook.tsx": (
            "export function useCopyToClipboard() {\n"
            "  const copy = useCallback(async (text) => {\n"
            "    return write(text)\n"
            "  }, [])\n"
            "  return copy\n"
            "}\n\n\n"
            "function write(t) { return t }\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    # (H) An external callee (useCallback) gets the historical CALLS edge, a
    # (H) first-party one REFERENCES; either keeps the callback reachable.
    edges = {b for a, _r, b in rels if a.endswith("hook.useCopyToClipboard")}
    assert any(".hook.useCopyToClipboard.anonymous_" in b for b in edges), (
        f"inline call-argument arrow not referenced; edges={edges}"
    )


def test_inline_arrow_new_expression_argument_is_referenced(tmp_path: Path) -> None:
    # (H) A Promise executor (`new Promise((resolve, reject) => {...})`) is an inline
    # (H) arrow handed to a constructor. JS/TS never treated `new X(...)` as a call
    # (H) node, so the executor got no incoming edge and reported as dead (the
    # (H) openapi-ts CancelablePromise/request plumbing is built on this pattern).
    # (H) Constructing must reference the inline callback the same way a call does.
    files = {
        "make.ts": (
            "export function make() {\n"
            "  return new Promise((resolve, reject) => { resolve(go()) })\n"
            "}\n\n\n"
            "function go() { return 1 }\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    # (H) A DEFINES edge always exists; the executor must gain a CALLS/REFERENCES edge.
    defines = cs.RelationshipType.DEFINES.value
    edges = {b for a, r, b in rels if a.endswith("make.make") and r != defines}
    assert any(".make.make.anonymous_" in b for b in edges), (
        f"inline new-expression executor arrow not referenced; edges={edges}"
    )


def test_new_first_party_class_records_instantiation(tmp_path: Path) -> None:
    # (H) Adding new_expression to the JS/TS call query also wires the previously
    # (H) missing INSTANTIATES edge for `new Foo()` to a first-party class.
    files = {
        "app.ts": (
            "class Widget {\n"
            "  constructor() {}\n"
            "}\n\n\n"
            "export function build() {\n"
            "  return new Widget()\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    instantiates = cs.RelationshipType.INSTANTIATES.value
    assert _has(rels, "app.build", instantiates, "app.Widget"), (
        f"new Widget() did not record INSTANTIATES; rels={rels}"
    )


def test_inline_callback_in_nested_arrow_const_is_referenced(tmp_path: Path) -> None:
    # (H) An inline `.forEach(v => ...)` inside a NESTED arrow-const (encodePair inside
    # (H) getQueryString) reported as dead: the call pass built the caller qn from the
    # (H) ancestor arrow-const's missing `name` field, dropping the outer segment
    # (H) (request.encodePair instead of request.getQueryString.encodePair), so the
    # (H) inline-arg candidate never matched the registered node. The whole openapi-ts
    # (H) query-string encoder is this shape.
    files = {
        "request.ts": (
            "export const getQueryString = (params) => {\n"
            "  const encodePair = (key, value) => {\n"
            "    if (Array.isArray(value)) {\n"
            "      value.forEach(v => encodePair(key, v))\n"
            "    }\n"
            "  }\n"
            "  Object.entries(params).forEach(([key, value]) => encodePair(key, value))\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    # (H) A DEFINES edge always exists; require a CALLS/REFERENCES edge to the inner
    # (H) arrow nested under the fully-qualified encodePair (request.getQueryString.
    # (H) encodePair.anonymous_*), which only matches once the caller qn keeps the
    # (H) outer getQueryString segment.
    defines = cs.RelationshipType.DEFINES.value
    edges = {
        b
        for a, r, b in rels
        if a.endswith("getQueryString.encodePair") and r != defines
    }
    assert any(".getQueryString.encodePair.anonymous_" in b for b in edges), (
        f"inline forEach arrow in nested arrow-const not referenced; edges={edges}"
    )


def test_promise_executor_in_constructor_is_referenced(tmp_path: Path) -> None:
    # (H) The openapi-ts CancelablePromise shape: a class constructor builds
    # (H) `this.promise = new Promise((resolve, reject) => {...})`. The executor arrow
    # (H) is anonymous and nested inside the constructor, so its qn must keep the full
    # (H) class.constructor path (not flatten to module.anonymous) for the constructor's
    # (H) CALLS edge to connect; otherwise the executor is orphaned and reports dead.
    files = {
        "CancelablePromise.ts": (
            "export class CancelablePromise {\n"
            "  constructor(executor) {\n"
            "    this.promise = new Promise((resolve, reject) => {\n"
            "      run(resolve)\n"
            "    })\n"
            "  }\n"
            "}\n\n\n"
            "function run(f) {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    defines = cs.RelationshipType.DEFINES.value
    edges = {
        b
        for a, r, b in rels
        if a.endswith("CancelablePromise.constructor") and r != defines
    }
    assert any(".CancelablePromise.constructor.anonymous_" in b for b in edges), (
        f"promise executor in constructor not referenced; edges={edges}"
    )


def test_defineproperty_getter_in_executor_is_referenced(tmp_path: Path) -> None:
    # (H) A getter descriptor (`Object.defineProperty(x, 'y', {get: () => ...})`) sits
    # (H) inside an anonymous Promise-executor arrow that gets no caller pass of its own;
    # (H) its calls bubble to the enclosing constructor. The constructor's collection
    # (H) walk must therefore descend into the unowned executor and reference the getter,
    # (H) or every defineProperty getter/setter reports as dead.
    files = {
        "CancelablePromise.ts": (
            "export class CancelablePromise {\n"
            "  constructor(executor) {\n"
            "    this.promise = new Promise((resolve, reject) => {\n"
            "      const onCancel = (h) => { track(h) }\n"
            "      Object.defineProperty(onCancel, 'isResolved', {\n"
            "        get: () => this._isResolved,\n"
            "      })\n"
            "    })\n"
            "  }\n"
            "}\n\n\n"
            "function track(h) {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    refs = {
        b
        for a, r, b in rels
        if r == REFERENCES and a.endswith("CancelablePromise.constructor")
    }
    assert any(
        ".CancelablePromise.constructor." in b and b.rsplit(".", 1)[-1] == "get"
        for b in refs
    ), f"defineProperty getter in executor not referenced; refs={refs}"


def test_inline_arrow_call_argument_function_expr_is_referenced(
    tmp_path: Path,
) -> None:
    # (H) A classic function expression passed directly as a call argument is the
    # (H) same first-class handoff and must also be referenced.
    files = {
        "timer.ts": (
            "export function start() {\n"
            "  schedule(function () { tick() })\n"
            "}\n\n\n"
            "function schedule(cb) { return cb }\n"
            "function tick() {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    # (H) schedule is first-party, so the handoff records as REFERENCES.
    refs = {b for a, r, b in rels if r == REFERENCES and a.endswith("timer.start")}
    assert any(".timer.start.anonymous_" in b for b in refs), (
        f"inline call-argument function expression not referenced; refs={refs}"
    )
