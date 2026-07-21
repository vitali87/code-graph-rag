from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_rels(
    tmp_path: Path, files: dict[str, str], lang_key: str
) -> set[tuple[str, str, str]]:
    # Build the graph for `files` and return (caller_qn, rel_type, callee_qn).
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


REFERENCES = cs.RelationshipType.REFERENCES.value


def test_js_function_assigned_to_const_is_referenced(tmp_path: Path) -> None:
    # `const cb = handler` binds a first-class function to a local for later
    # dynamic dispatch; the assignment must reference it exactly like the
    # Python `http_callback = fn` shape or dead-code wrongly flags handler.
    files = {
        "m.js": (
            "function handler(evt) { return evt; }\n\n"
            "function setup() {\n"
            "    const cb = handler;\n"
            "    return cb;\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "javascript")
    assert _has(rels, "m.setup", REFERENCES, "m.handler")


def test_js_function_assigned_to_object_attribute_is_referenced(
    tmp_path: Path,
) -> None:
    # A nested function monkeypatched onto an object attribute
    # (client.post = handlePost) is invoked later through that attribute; the
    # assignment_expression must reference it (MockHTTPRouter shape in JS).
    files = {
        "m.js": (
            "function createMockClient(client) {\n"
            "    async function handlePost(url) { return url; }\n"
            "    client.post = handlePost;\n"
            "    return client;\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "javascript")
    assert _has(rels, "m.createMockClient", REFERENCES, "handlePost")


def test_js_module_exports_assignment_in_callless_module_is_referenced(
    tmp_path: Path,
) -> None:
    # `module.exports.run = run` in a file with NO call expressions must still
    # emit the Module -> REFERENCES edge; the call-driven pass early-returns on
    # such modules, so the assignment scan has to run before it.
    files = {
        "m.js": ("function run() { return 1; }\n\nmodule.exports.run = run;\n"),
    }
    rels = _run_rels(tmp_path, files, "javascript")
    assert _has(rels, "m", REFERENCES, "m.run")


def test_js_imported_function_assigned_at_module_scope_is_referenced(
    tmp_path: Path,
) -> None:
    # A function defined in one file, imported and re-bound in another
    # (registryHandler = handleEvent) must reference the ORIGIN function.
    files = {
        "handlers.js": "export function handleEvent(evt) { return evt; }\n",
        "registry.js": (
            "import { handleEvent } from './handlers.js';\n\n"
            "const registryHandler = handleEvent;\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "javascript")
    assert _has(rels, "registry", REFERENCES, "handlers.handleEvent")


def test_ts_annotated_const_assignment_is_referenced(tmp_path: Path) -> None:
    # A TS annotated declarator (const handler: Handler = handleEvent) carries
    # an extra type child; the walker must reference its value exactly like an
    # unannotated one.
    files = {
        "m.ts": (
            "type Handler = (evt: string) => string;\n\n"
            "function handleEvent(evt: string): string { return evt; }\n\n"
            "const handler: Handler = handleEvent;\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    assert _has(rels, "m", REFERENCES, "m.handleEvent")


def test_js_plain_value_assignments_are_not_referenced(tmp_path: Path) -> None:
    # Non-callable RHS values (literals, plain locals) must not produce
    # REFERENCES noise.
    files = {
        "m.js": (
            "function setup() {\n"
            "    const limit = 3;\n"
            "    const name = 'x';\n"
            "    const copy = limit;\n"
            "    return copy;\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "javascript")
    assert not any(r == REFERENCES for _, r, _ in rels)


def test_ts_as_cast_assignment_references_target(tmp_path: Path) -> None:
    # `export const persist = persistImpl as unknown as Persist` aliases the real
    # implementation through TS `as` casts. The cast expression is transparent for
    # reference resolution, so the module must reference persistImpl or it (and
    # everything only it reaches) reports as dead -- the zustand middleware
    # pattern where the public export is a cast of the internal impl.
    files = {
        "mw.ts": (
            "const persistImpl = (config) => build(config)\n"
            "export const persist = persistImpl as unknown as Persist\n"
            "function build(c) { return c }\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    assert _has(rels, "mw", REFERENCES, "mw.persistImpl")


def test_ts_non_null_cast_assignment_references_target(tmp_path: Path) -> None:
    # A non-null assertion (`handler!`) is also a transparent wrapper.
    files = {
        "m.ts": (
            "function handleEvent() { return 1 }\n"
            "const cb = handleEvent!\n"
            "export function use() { return cb }\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    assert _has(rels, "m", REFERENCES, "m.handleEvent")


def test_ts_cast_object_value_is_referenced(tmp_path: Path) -> None:
    # A cast function in a collection value (`{ onEvent: handler as any }`) must
    # still be referenced -- the cast wrapper is unwrapped in the value-ref path.
    files = {
        "m.ts": (
            "function handler() { return 1 }\n"
            "export function reg() { register({ onEvent: handler as any }) }\n"
            "function register(o: unknown) { return o }\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "typescript")
    # Collection-value handoffs record as CALLS; the point is the cast is unwrapped
    # so `handler` is reached at all.
    assert any(a.endswith("m.reg") and b.endswith("m.handler") for a, _r, b in rels), (
        "cast object value not referenced"
    )
