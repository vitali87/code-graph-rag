from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_rels(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    # (H) Build the graph for `files` and return (caller_qn, rel_type, callee_qn).
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
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


REFERENCES = cs.RelationshipType.REFERENCES.value


def test_callback_kwarg_to_first_party_function_is_referenced(tmp_path: Path) -> None:
    # (H) A nested function passed by keyword to a FIRST-PARTY callee that stores it
    # (H) (never invokes it) must get a REFERENCES edge from the passing scope, or
    # (H) dead-code wrongly flags it (with_brrr_from_cfg/create_context shape).
    files = {
        "brrrlib.py": (
            "def with_brrr_from_cfg(cfg, handlers, create_context=None):\n"
            "    cfg.ctx_factory = create_context\n"
            "    return cfg\n"
        ),
        "worker.py": (
            "from brrrlib import with_brrr_from_cfg\n\n"
            "def _main(cfg, handlers, extra):\n"
            "    def create_context(active_worker, meta):\n"
            "        return (active_worker, extra)\n"
            "    return with_brrr_from_cfg(cfg, handlers, create_context=create_context)\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "worker._main", REFERENCES, "worker._main.create_context")


def test_callback_kwarg_to_constructor_is_referenced(tmp_path: Path) -> None:
    # (H) A nested function passed into a first-party CONSTRUCTOR that stores it as a
    # (H) field must get a REFERENCES edge (AnteriorCodec/create_context shape).
    files = {
        "codec.py": (
            "class Codec:\n"
            "    def __init__(self, create_context=None):\n"
            "        self.create_context = create_context\n"
        ),
        "worker.py": (
            "from codec import Codec\n\n"
            "def get_local(topic, handlers, extra):\n"
            "    def create_context(active_worker, meta):\n"
            "        return (active_worker, extra)\n"
            "    return Codec(create_context=create_context)\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "worker.get_local", REFERENCES, "worker.get_local.create_context")


def test_callback_to_misbound_external_method_is_referenced(tmp_path: Path) -> None:
    # (H) df arrives as an UNTYPED parameter, so the trie binds df.apply by bare name
    # (H) to a same-named first-party method; the passed callback must STILL be
    # (H) referenced from the passing scope (build_suggestion shape). A locally
    # (H) constructed receiver (df = pd.DataFrame(...)) is instead typed external and
    # (H) suppressed outright -- covered in test_external_receiver_misbind.py.
    files = {
        "strategies.py": (
            "class BatchingStrategyBase:\n"
            "    def apply(self, batch):\n"
            "        return batch\n"
        ),
        "report.py": (
            "def write_report(df):\n"
            "    def build_suggestion(row):\n"
            "        return row\n"
            "    df['suggestions'] = df.apply(build_suggestion, axis=1)\n"
            "    return df\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(
        rels, "report.write_report", REFERENCES, "report.write_report.build_suggestion"
    )


def test_plain_argument_is_not_referenced(tmp_path: Path) -> None:
    # (H) Non-callable arguments (locals, literals) must not produce REFERENCES noise.
    files = {
        "lib.py": "def consume(x, y):\n    return x\n",
        "m.py": (
            "from lib import consume\n\n"
            "def use(data):\n"
            "    limit = 3\n"
            "    return consume(data, limit)\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert not any(r == REFERENCES for _, r, _ in rels)


def test_function_assigned_to_local_variable_is_referenced(tmp_path: Path) -> None:
    # (H) A nested function assigned to a local (http_callback =
    # (H) llm_http_task_closure_with_context) is referenced even if never called by
    # (H) name in this scope; the alias may be stored or passed onward for dynamic
    # (H) dispatch, so the assignment itself must keep the function reachable.
    files = {
        "svc.py": (
            "import os\n\n"
            "def get_llm_service(config, http_callback=None):\n"
            "    def llm_http_task_closure_with_context(llm, method, url):\n"
            "        return (config, llm, method, url)\n"
            "    if http_callback:\n"
            "        pass\n"
            "    else:\n"
            "        http_callback = llm_http_task_closure_with_context\n"
            "    return http_callback\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(
        rels,
        "svc.get_llm_service",
        REFERENCES,
        "svc.get_llm_service.llm_http_task_closure_with_context",
    )


def test_function_assigned_to_object_attribute_is_referenced(tmp_path: Path) -> None:
    # (H) A nested function monkeypatched onto an object attribute (mock_client.post
    # (H) = handle_post) is invoked later through that attribute; the assignment must
    # (H) reference it (MockHTTPRouter shape). Its body's self-call then keeps the
    # (H) called method reachable transitively.
    files = {
        "mocking.py": (
            "class MockHTTPRouter:\n"
            "    def _handle_post(self, url, **kwargs):\n"
            "        return url\n"
            "    def create_mock_client(self, mock_client):\n"
            "        async def handle_post(url, **kwargs):\n"
            "            return self._handle_post(url, **kwargs)\n"
            "        mock_client.post = handle_post\n"
            "        return mock_client\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(
        rels,
        "mocking.MockHTTPRouter.create_mock_client",
        REFERENCES,
        "create_mock_client.handle_post",
    )
    assert _has(
        rels,
        "create_mock_client.handle_post",
        cs.RelationshipType.CALLS.value,
        "MockHTTPRouter._handle_post",
    )


def test_monkeypatch_assignment_is_referenced(tmp_path: Path) -> None:
    # (H) A function assigned over a third-party attribute chain
    # (H) (genai.Client.__init__ = _vertex_ai_client_init) is invoked by the
    # (H) patched class later; the assignment must reference it
    # (H) (_vertex_ai_client_init shape).
    files = {
        "helpers.py": (
            "import google.genai as genai_client\n\n"
            "def _vertex_ai_client_init(self, **kwargs):\n"
            "    kwargs.pop('api_key', None)\n"
            "    return None\n\n"
            "def install_patch():\n"
            "    genai_client.Client.__init__ = _vertex_ai_client_init\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(
        rels, "helpers.install_patch", REFERENCES, "helpers._vertex_ai_client_init"
    )


def test_module_level_assignment_in_callless_module_is_referenced(
    tmp_path: Path,
) -> None:
    # (H) A module whose ONLY statement is a first-class function assignment (no call
    # (H) expressions at all) must still emit the Module -> REFERENCES edge; the
    # (H) call-driven pass early-returns on such modules, so the assignment scan has
    # (H) to run before it.
    files = {
        "handlers.py": "def handle_event(evt):\n    return evt\n",
        "registry.py": (
            "from handlers import handle_event\n\nregistry_handler = handle_event\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "registry", REFERENCES, "handlers.handle_event")


def test_annotated_assignment_is_referenced(tmp_path: Path) -> None:
    # (H) An annotated assignment (handler: Callable = handle_event) parses as the
    # (H) same tree-sitter `assignment` node with an extra type child; the walker
    # (H) must reference its RHS exactly like an unannotated one.
    files = {
        "handlers.py": "def handle_event(evt):\n    return evt\n",
        "registry.py": (
            "from typing import Callable\n"
            "from handlers import handle_event\n\n"
            "handler: Callable = handle_event\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "registry", REFERENCES, "handlers.handle_event")


def test_argument_to_call_expression_callee_is_referenced(tmp_path: Path) -> None:
    # (H) `wraps(view_func)(_view_wrapper)` consumes _view_wrapper through the
    # (H) callable the inner call returns. The callee has no extractable name (it
    # (H) is itself a call), but the passed function must still be referenced or
    # (H) dead-code flags every django-style view decorator wrapper.
    files = {
        "deco.py": (
            "from functools import wraps\n\n"
            "def csrf_exempt(view_func):\n"
            "    def _view_wrapper(request):\n"
            "        return view_func(request)\n"
            "    return wraps(view_func)(_view_wrapper)\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "deco.csrf_exempt", REFERENCES, "csrf_exempt._view_wrapper")


def test_ternary_assignment_references_both_methods(tmp_path: Path) -> None:
    # (H) `get_response = self._async if flag else self._sync` binds one of two
    # (H) methods as a value; both are possible referents and must be referenced
    # (H) (django BaseHandler.load_middleware shape), or dead-code flags them.
    files = {
        "handler.py": (
            "def convert(handler):\n"
            "    return handler\n\n"
            "class BaseHandler:\n"
            "    def load_middleware(self, is_async):\n"
            "        get_response = self._async if is_async else self._sync\n"
            "        return convert(get_response)\n\n"
            "    def _async(self, request):\n"
            "        return request\n\n"
            "    def _sync(self, request):\n"
            "        return request\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "BaseHandler.load_middleware", REFERENCES, "BaseHandler._async")
    assert _has(rels, "BaseHandler.load_middleware", REFERENCES, "BaseHandler._sync")


def test_returned_method_attribute_is_referenced(tmp_path: Path) -> None:
    # (H) `return self._get_point_2d` hands the bound method to the caller for
    # (H) later dispatch (django GEOSCoordSeq._point_getter shape); the returning
    # (H) scope must reference it or dead-code flags the whole getter cluster.
    files = {
        "coordseq.py": (
            "class GEOSCoordSeq:\n"
            "    @property\n"
            "    def _point_getter(self):\n"
            "        if self.dims == 3:\n"
            "            return self._get_point_3d\n"
            "        return self._get_point_2d\n\n"
            "    def _get_point_2d(self, index):\n"
            "        return index\n\n"
            "    def _get_point_3d(self, index):\n"
            "        return index\n\n"
            "    def use(self, index):\n"
            "        return self._point_getter(index)\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(
        rels, "GEOSCoordSeq._point_getter", REFERENCES, "GEOSCoordSeq._get_point_2d"
    )
    assert _has(
        rels, "GEOSCoordSeq._point_getter", REFERENCES, "GEOSCoordSeq._get_point_3d"
    )


def test_ternary_condition_is_not_referenced(tmp_path: Path) -> None:
    # (H) The ternary's condition is truthiness-tested, never bound to the LHS,
    # (H) so a callable named there must NOT get an assignment reference.
    files = {
        "picker.py": (
            "def check():\n"
            "    return True\n\n"
            "def first():\n"
            "    return 1\n\n"
            "def second():\n"
            "    return 2\n\n"
            "def pick():\n"
            "    chosen = first if check else second\n"
            "    return chosen()\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "picker.pick", REFERENCES, "picker.first")
    assert _has(rels, "picker.pick", REFERENCES, "picker.second")
    assert not _has(rels, "picker.pick", REFERENCES, "picker.check")


def test_bound_function_argument_is_referenced(tmp_path: Path) -> None:
    # (H) `el.addEventListener("click", handler.bind(this))` hands off `handler`;
    # (H) the .bind call itself resolves to the Function.prototype builtin, so the
    # (H) bound function must be referenced from the passing scope or it reports
    # (H) dead (django admin's inlines.js inlineDeleteHandler).
    files = {
        "inlines.js": (
            "function formset(row) {\n"
            "    const inlineDeleteHandler = function (e1) {\n"
            "        return e1;\n"
            "    };\n"
            '    row.addEventListener("click", inlineDeleteHandler.bind(this));\n'
            "}\n"
            "module.exports = formset;\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(
        rels, "inlines.formset", REFERENCES, "inlines.formset.inlineDeleteHandler"
    ) or _has(rels, "inlines.formset", "CALLS", "inlines.formset.inlineDeleteHandler")


def test_bound_function_assignment_rhs_is_referenced(tmp_path: Path) -> None:
    # (H) `const bound = handler.bind(null)` stores the bound handler for later
    # (H) invocation; the assignment walk must peel .bind like a cast so the
    # (H) underlying function is referenced.
    files = {
        "store.js": (
            "function attach() {\n"
            "    const onSave = function (e) {\n"
            "        return e;\n"
            "    };\n"
            "    const bound = onSave.bind(null);\n"
            "    return bound;\n"
            "}\n"
            "module.exports = attach;\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "store.attach", REFERENCES, "store.attach.onSave")


def test_cast_wrapped_bound_function_argument_is_referenced(tmp_path: Path) -> None:
    # (H) `(handler as any).bind(this)` interleaves a cast INSIDE the bind
    # (H) receiver; a single unwrap pass leaves the cast node behind, so the
    # (H) peel must iterate cast/paren and bind unwraps to a fixpoint. Chained
    # (H) binds (`h.bind(a).bind(b)`) peel the same way.
    files = {
        "wrapped.ts": (
            "function attach(el: { on: (cb: unknown) => void }) {\n"
            "    const onSave = function (e: number) {\n"
            "        return e;\n"
            "    };\n"
            "    el.on((onSave as any).bind(null));\n"
            "    const rebound = onSave.bind(null).bind(null);\n"
            "    return rebound;\n"
            "}\n"
            "export default attach;\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "wrapped.attach", REFERENCES, "wrapped.attach.onSave") or _has(
        rels, "wrapped.attach", "CALLS", "wrapped.attach.onSave"
    )


def test_bound_function_argument_flows_to_callable_param(tmp_path: Path) -> None:
    # (H) A bound function passed to a FIRST-PARTY callee that invokes its
    # (H) parameter must produce the callable-flow CALLS edge (run -> handler),
    # (H) not just the passing scope's REFERENCES edge.
    files = {
        "flow.js": (
            "function run(cb) {\n"
            "    return cb();\n"
            "}\n"
            "function handler() {\n"
            "    return 1;\n"
            "}\n"
            "function main() {\n"
            "    return run(handler.bind(this));\n"
            "}\n"
            "module.exports = main;\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "flow.run", "CALLS", "flow.handler")


def test_bound_function_flows_through_passthrough_param(tmp_path: Path) -> None:
    # (H) The callable-flow fixpoint (outer forwards its param to run) records
    # (H) seeds in _collect_callable_flow, which must peel .bind like the
    # (H) direct-argument path or the propagated CALLS edge is lost.
    files = {
        "flow2.js": (
            "function run(cb) {\n"
            "    return cb();\n"
            "}\n"
            "function outer(cb2) {\n"
            "    return run(cb2);\n"
            "}\n"
            "function handler() {\n"
            "    return 1;\n"
            "}\n"
            "function main() {\n"
            "    return outer(handler.bind(this));\n"
            "}\n"
            "module.exports = main;\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, "flow2.run", "CALLS", "flow2.handler")
