"""A function used as a collection value binds the declaration in scope (#3199).

`return { AwaitExpression: validate }` inside eslint's `create(context)`, with
`function validate` declared a few lines above, bound by name to another,
unimported module's `validate` (heuristic): the rule's implementation was
reported dead and an unrelated function gained callers. A call or callback
argument in the same position already resolved through the enclosing scope.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

_JS_SOURCE = (
    "function validate(config) { return !!config; }\n"
    "function check(config) { return config; }\n"
    "function noop() {}\n"
    "module.exports = { validate, check, noop };\n"
)
_PY_SOURCE = (
    "def validate(config):\n    return bool(config)\n\n\n"
    "def check(config):\n    return config\n"
)

# (file, source, caller, {local callee names}) per collection shape.
_CASES = {
    "js-object-value": (
        "lib/rule.js",
        "module.exports = {\n  create(context) {\n"
        "    function validate(node) { return node; }\n"
        "    return { AwaitExpression: validate, ForOfStatement: validate };\n"
        "  },\n};\n",
        "lib.rule.create",
        {"validate"},
    ),
    "js-array-element": (
        "lib/arr.js",
        "function arr() {\n  function validate(n) { return n; }\n"
        "  const handlers = [validate];\n  return handlers;\n}\n"
        "module.exports = { arr };\n",
        "lib.arr.arr",
        {"validate"},
    ),
    "js-ternary-value": (
        "lib/tern.js",
        "function tern(c) {\n  function check(n) { return n; }\n"
        "  function noop() {}\n  return { K: c ? check : noop };\n}\n"
        "module.exports = { tern };\n",
        "lib.tern.tern",
        {"check", "noop"},
    ),
    "ts-object-value": (
        "lib/rule.ts",
        "export function create(context: unknown) {\n"
        "  function validate(node: unknown) { return node; }\n"
        "  return { AwaitExpression: validate };\n}\n",
        "lib.rule.create",
        {"validate"},
    ),
    "py-dict-value": (
        "py/rule.py",
        "def create(context):\n    def validate(node):\n        return node\n\n"
        '    return {"AwaitExpression": validate}\n',
        "py.rule.create",
        {"validate"},
    ),
    "py-list-element": (
        "py/lst.py",
        "def lst():\n    def check(node):\n        return node\n\n    return [check]\n",
        "py.lst.lst",
        {"check"},
    ),
}


def _index(repo: Path, files: dict[str, str], mock: MagicMock) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(repo, mock)


def _edges(repo: Path, mock: MagicMock, caller: str) -> dict[tuple[str, str], str]:
    # (relationship, callee) -> resolution, for edges out of `caller`.
    prefix = f"{repo.name}."
    out: dict[tuple[str, str], str] = {}
    for c in mock.ensure_relationship_batch.call_args_list:
        rel = str(c.args[1])
        if rel not in (
            cs.RelationshipType.CALLS.value,
            cs.RelationshipType.REFERENCES.value,
        ):
            continue
        if str(c.args[0][2]).removeprefix(prefix) != caller:
            continue
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        out[(rel, str(c.args[2][2]).removeprefix(prefix))] = str(
            props.get(cs.KEY_RESOLUTION)
        )
    return out


@pytest.mark.parametrize("case", list(_CASES))
def test_a_collection_value_binds_the_function_declared_in_scope(
    temp_repo: Path, mock_ingestor: MagicMock, case: str
) -> None:
    rel, text, caller, locals_ = _CASES[case]
    files = {"lib/source.js": _JS_SOURCE, "py/source.py": _PY_SOURCE, rel: text}
    _index(temp_repo, files, mock_ingestor)

    edges = _edges(temp_repo, mock_ingestor, caller)
    targets = {callee for _rel, callee in edges}
    assert targets == {f"{caller}.{name}" for name in locals_}, edges
    assert set(edges.values()) == {cs.EdgeResolution.EXACT}, edges


def test_a_sibling_functions_local_does_not_capture_the_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the scope chain is lexical. `b` sees the module's own
    # `validate`, not the one declared inside its sibling `a`.
    text = (
        "function validate(x) { return x; }\n"
        "function a() {\n  function validate(n) { return n; }\n  return validate;\n}\n"
        "function b() { return { K: validate }; }\n"
        "module.exports = { a, b };\n"
    )
    _index(temp_repo, {"lib/source.js": _JS_SOURCE, "lib/pair.js": text}, mock_ingestor)

    edges = _edges(temp_repo, mock_ingestor, "lib.pair.b")
    assert {callee for _rel, callee in edges} == {"lib.pair.validate"}, edges


@pytest.mark.parametrize(
    ("rel", "text", "caller", "target"),
    [
        (
            "lib/imp.js",
            "const { validate } = require('./source');\n"
            "function create() { return { K: validate }; }\n"
            "module.exports = { create };\n",
            "lib.imp.create",
            "lib.source.validate",
        ),
        (
            "py/imp.py",
            "from .source import validate\n\n\n"
            'def create():\n    return {"k": validate}\n',
            "py.imp.create",
            "py.source.validate",
        ),
    ],
    ids=["js-require", "py-import"],
)
def test_an_imported_function_in_a_collection_still_binds_its_module(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    rel: str,
    text: str,
    caller: str,
    target: str,
) -> None:
    # Negative: with nothing declared in scope, the import still decides.
    files = {"lib/source.js": _JS_SOURCE, "py/source.py": _PY_SOURCE, rel: text}
    (temp_repo / "py").mkdir(parents=True, exist_ok=True)
    (temp_repo / "py/__init__.py").write_text("", encoding="utf-8")
    _index(temp_repo, files, mock_ingestor)

    edges = _edges(temp_repo, mock_ingestor, caller)
    assert {callee for _rel, callee in edges} == {target}, edges


def test_a_python_local_value_shadowing_a_function_is_not_a_reference(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A local bound to a non-function value hides the module function of the
    # same name: the table holds that value, so no edge, as for a call.
    text = (
        "from .source import validate\n\n\n"
        'def create(context):\n    validate = context.rule\n    return {"k": validate}\n'
    )
    (temp_repo / "py").mkdir(parents=True, exist_ok=True)
    (temp_repo / "py/__init__.py").write_text("", encoding="utf-8")
    _index(
        temp_repo,
        {"py/source.py": _PY_SOURCE, "py/shadow.py": text},
        mock_ingestor,
    )

    edges = _edges(temp_repo, mock_ingestor, "py.shadow.create")
    assert "py.source.validate" not in {callee for _rel, callee in edges}, edges
