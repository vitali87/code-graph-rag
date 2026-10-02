"""Issue #2640: a dotted `--decorator-root` roots the decorator it names.

Decorators read from the graph were reduced to their last dotted segment, but
the user's values were only lowercased, so `--decorator-root
registry.register` (the documented form) compared `register` with
`registry.register` and never matched: the decorated functions and everything
only they call stayed in the report, with no warning.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import cli
from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import dead_code_from_graph
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

P = "dead"
_FUNCTION = cs.NodeLabel.FUNCTION.value
_CALLS = cs.RelationshipType.CALLS.value

# name -> decorators, as the parser stores them.
DECORATED = {
    "_handler": ["@registry.register"],
    "_with_args": ["@registry.register(priority=1)"],
    "_deeper": ["@app.registry.register"],
    "_other": ["@other.register"],
    "_lookalike": ["@myregistry.register"],
    "_celery": ["@celery_app.task"],
    "_plain": [],
}
CALLS = [("_handler", "_used_by_handler")]
NAMES = [*DECORATED, "_used_by_handler"]


def _qn(name: str) -> str:
    return f"{P}.app.{name}"


def _nodes() -> dict[tuple[str, str], PropertyDict]:
    nodes: dict[tuple[str, str], PropertyDict] = {}
    for name in NAMES:
        props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: _qn(name),
            cs.KEY_NAME: name,
            cs.KEY_PATH: "app.py",
        }
        if decorators := DECORATED.get(name):
            props[cs.KEY_DECORATORS] = decorators
        nodes[(_FUNCTION, _qn(name))] = props
    return nodes


def _rels() -> list[tuple[str, str, str, str, str]]:
    return [(_FUNCTION, _qn(a), _CALLS, _FUNCTION, _qn(b)) for a, b in CALLS]


def _rooted(*decorator_roots: str) -> set[str]:
    config = cli._dead_code_config(True, False, [], list(decorator_roots))
    dead = dead_code_from_graph(_nodes(), _rels(), f"{P}.", config)
    baseline = dead_code_from_graph(
        _nodes(), _rels(), f"{P}.", cli._dead_code_config(True, False, [], [])
    )
    return {name for name in NAMES if _qn(name) in baseline - dead}


def test_the_documented_dotted_form_roots_the_decorated_function() -> None:
    assert _rooted("registry.register") == {
        "_handler",
        "_used_by_handler",
        "_with_args",
        "_deeper",
    }


@pytest.mark.parametrize(
    "written", ["@registry.register", "registry.register()", "Registry.Register"]
)
def test_the_value_is_read_the_way_decorators_are(written: str) -> None:
    assert "_handler" in _rooted(written)


def test_a_dotted_celery_task_root_matches_its_own_decorator() -> None:
    # `task` is built in, so `_celery` is already a root: the dotted form
    # must at least not stop it being one.
    assert _rooted("celery_app.task") == set()


def _ingestor() -> MagicMock:
    nodes: list[ResultRow] = [
        {
            cs.KEY_LABEL: _FUNCTION,
            cs.KEY_NAME: name,
            cs.KEY_QUALIFIED_NAME: _qn(name),
            cs.KEY_PATH: "app.py",
            cs.KEY_DECORATORS: DECORATED.get(name, []),
            cs.KEY_START_LINE: 1,
            cs.KEY_END_LINE: 2,
        }
        for name in NAMES
    ]
    rels: list[ResultRow] = [
        {
            cs.KEY_FROM_LABEL: _FUNCTION,
            cs.KEY_FROM_QN: _qn(a),
            cs.KEY_REL_TYPE: _CALLS,
            cs.KEY_TO_LABEL: _FUNCTION,
            cs.KEY_TO_QN: _qn(b),
        }
        for a, b in CALLS
    ]

    def _fetch(
        query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return nodes
        return rels if query == cq.CYPHER_DEAD_CODE_RELS else []

    mock = MagicMock()
    mock.list_projects.return_value = [P]
    mock.fetch_all.side_effect = _fetch
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def test_the_cli_roots_the_documented_form() -> None:
    with patch("codebase_rag.cli.connect_memgraph", return_value=_ingestor()):
        result = CliRunner().invoke(
            cli.app,
            [
                "dead-code",
                "-n",
                P,
                "--format",
                "json",
                "--decorator-root",
                "registry.register",
            ],
        )

    assert result.exit_code == 0, result.output
    reported = {row[cs.KEY_NAME] for row in json.loads(result.output)}
    assert reported.isdisjoint({"_handler", "_used_by_handler"})


# Negative: what must not change.


def test_a_dotted_root_does_not_root_another_receivers_decorator() -> None:
    rooted = _rooted("registry.register")

    assert "_other" not in rooted
    assert "_lookalike" not in rooted


def test_a_bare_root_still_matches_the_last_segment_anywhere() -> None:
    assert _rooted("register") == {
        "_handler",
        "_used_by_handler",
        "_with_args",
        "_deeper",
        "_other",
        "_lookalike",
    }


def test_no_user_root_leaves_the_built_in_set_alone() -> None:
    config = cli._dead_code_config(True, False, [], [])
    dead = dead_code_from_graph(_nodes(), _rels(), f"{P}.", config)

    assert _qn("_celery") not in dead
    assert _qn("_handler") in dead
