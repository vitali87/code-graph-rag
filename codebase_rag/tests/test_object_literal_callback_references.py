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
