# Follow-up to issue #2538: an anonymous class's qn was built from EVERY
# callable written around it, while its DEFINES parent is the innermost
# callable's registered node. Those disagree whenever that callable is
# registered under a shorter path: a named function declared inside a method
# registers as `Box.inner` (no `run`), and a closure inside another closure
# skips the outer one. The class then sat at `Box.run.inner.anonymous_9_23`
# under a parent `Box.inner`, so walking up from the class by name and
# walking up by DEFINES reached different nodes.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import NodeLabel, RelationshipType
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
    get_relationships,
)

_PROJECT = "php_anon"
_MODULE = f"{_PROJECT}.Box"
_BOX = f"{_MODULE}.Box"

# A named function declared in a method; its anonymous class (row 9, col 23)
# has a method calling a sibling and a closure (row 12, col 25).
_NAMED_IN_METHOD = """\
<?php
namespace App;

class Box
{
    public function run(): object
    {
        function inner(): object
        {
            return new class {
                public function go(): int
                {
                    $f = function () { return 1; };
                    return $this->ping() + $f();
                }

                public function ping(): int { return 2; }
            };
        }
        return inner();
    }
}
"""

_NAMED_IN_CLOSURE = """\
<?php
namespace App;

class Box
{
    public function run(): object
    {
        $make = function () {
            function viaClosure(): object
            {
                return new class {};
            }
            return viaClosure();
        };
        return $make();
    }
}
"""

_CLOSURE_IN_CLOSURE = """\
<?php
namespace App;

class Box
{
    public function run(): object
    {
        $outer = function () {
            $inner = function () {
                return new class {};
            };
            return $inner();
        };
        return $outer();
    }
}
"""

_ARROW_IN_CLOSURE = """\
<?php
namespace App;

class Box
{
    public function run(): object
    {
        $outer = function () {
            $make = fn() => new class {};
            return $make();
        };
        return $outer();
    }
}
"""

_NAMED_IN_FUNCTION = """\
<?php
namespace App;

function outer(): object
{
    function deeper(): object
    {
        return new class {};
    }
    return deeper();
}
"""

# Negative cases: an anonymous class directly in a method (row 7, col 19),
# directly in a closure in a method (row 9, col 23), and at file scope (row
# 15, col 11), plus a closure in the first one's method (row 10, col 21).
_UNCHANGED = """\
<?php
namespace App;

class Box
{
    public function run(): object
    {
        return new class {
            public function go(): int
            {
                $f = function () { return 1; };
                return $f();
            }
        };
    }

    public function wrap(): object
    {
        $h = function () {
            return new class {};
        };
        return $h();
    }
}

$top = new class {};
"""


def _index(temp_repo: Path, mock_ingestor: MagicMock, source: str) -> None:
    project = temp_repo / _PROJECT
    project.mkdir(exist_ok=True)
    (project / "Box.php").write_text(source, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="php")


def _parents(mock_ingestor: MagicMock, label: NodeLabel) -> dict[str, tuple[str, str]]:
    # Every node of `label`, keyed by qn, to its DEFINES parent (label, qn).
    return {
        c.args[2][2]: (str(c.args[0][0]), c.args[0][2])
        for c in get_relationships(mock_ingestor, RelationshipType.DEFINES)
        if str(c.args[2][0]) == label
    }


def _assert_parent_is_prefix(
    mock_ingestor: MagicMock, label: NodeLabel, qn: str
) -> tuple[str, str]:
    parents = _parents(mock_ingestor, label)
    assert qn in parents, f"no DEFINES edge into {qn}: {parents}"
    parent_label, parent_qn = parents[qn]
    assert qn.rsplit(".", 1)[0] == parent_qn, (
        f"{qn} is DEFINED by {parent_label} {parent_qn}, not by its qn prefix"
    )
    assert parent_qn in get_node_names(mock_ingestor, parent_label)
    return parent_label, parent_qn


@pytest.mark.parametrize(
    ("source", "class_qn", "parent"),
    [
        (
            _NAMED_IN_METHOD,
            f"{_BOX}.inner.anonymous_9_23",
            (NodeLabel.FUNCTION, f"{_BOX}.inner"),
        ),
        (
            _NAMED_IN_CLOSURE,
            f"{_BOX}.viaClosure.anonymous_10_27",
            (NodeLabel.FUNCTION, f"{_BOX}.viaClosure"),
        ),
        (
            _CLOSURE_IN_CLOSURE,
            f"{_BOX}.run.anonymous_8_21.anonymous_9_27",
            (NodeLabel.FUNCTION, f"{_BOX}.run.anonymous_8_21"),
        ),
        (
            _ARROW_IN_CLOSURE,
            f"{_BOX}.run.anonymous_8_20.anonymous_8_32",
            (NodeLabel.FUNCTION, f"{_BOX}.run.anonymous_8_20"),
        ),
        (
            _NAMED_IN_FUNCTION,
            f"{_MODULE}.deeper.anonymous_7_19",
            (NodeLabel.FUNCTION, f"{_MODULE}.deeper"),
        ),
    ],
    ids=[
        "named-function-in-method",
        "named-function-in-closure",
        "closure-in-closure",
        "arrow-in-closure",
        "named-function-in-function",
    ],
)
def test_anonymous_class_is_defined_by_its_qn_prefix(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    source: str,
    class_qn: str,
    parent: tuple[NodeLabel, str],
) -> None:
    _index(temp_repo, mock_ingestor, source)

    (anonymous,) = (
        qn
        for qn in get_node_names(mock_ingestor, NodeLabel.CLASS)
        if qn.rsplit(".", 1)[-1].startswith("anonymous_")
    )
    found_parent = _assert_parent_is_prefix(mock_ingestor, NodeLabel.CLASS, anonymous)
    assert (anonymous, found_parent) == (class_qn, (str(parent[0]), parent[1]))


def test_members_of_the_moved_class_follow_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The class's methods, the closure in one of them and the call between
    # the methods all sit under the class's new qn, so no edge dangles.
    _index(temp_repo, mock_ingestor, _NAMED_IN_METHOD)

    anon = f"{_BOX}.inner.anonymous_9_23"
    methods = get_node_names(mock_ingestor, NodeLabel.METHOD)
    assert {f"{anon}.go", f"{anon}.ping"} <= methods
    closure = f"{anon}.go.anonymous_12_25"
    assert closure in get_node_names(mock_ingestor, NodeLabel.FUNCTION)
    assert _assert_parent_is_prefix(mock_ingestor, NodeLabel.FUNCTION, closure) == (
        str(NodeLabel.METHOD),
        f"{anon}.go",
    )
    calls = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, RelationshipType.CALLS)
    }
    assert (f"{anon}.go", f"{anon}.ping") in calls


# Negative tests: classes whose qn already matched their parent keep today's
# names and parents.


@pytest.mark.parametrize(
    ("label", "qn", "parent"),
    [
        (
            NodeLabel.CLASS,
            f"{_BOX}.run.anonymous_7_19",
            (NodeLabel.METHOD, f"{_BOX}.run"),
        ),
        (
            NodeLabel.CLASS,
            f"{_BOX}.wrap.anonymous_18_13.anonymous_19_23",
            (NodeLabel.FUNCTION, f"{_BOX}.wrap.anonymous_18_13"),
        ),
        (
            NodeLabel.CLASS,
            f"{_MODULE}.anonymous_25_11",
            (NodeLabel.MODULE, _MODULE),
        ),
        (
            NodeLabel.FUNCTION,
            f"{_BOX}.run.anonymous_7_19.go.anonymous_10_21",
            (NodeLabel.METHOD, f"{_BOX}.run.anonymous_7_19.go"),
        ),
    ],
    ids=["in-method", "in-closure-in-method", "file-scope", "closure-in-its-method"],
)
def test_already_consistent_names_and_parents_are_unchanged(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    label: NodeLabel,
    qn: str,
    parent: tuple[NodeLabel, str],
) -> None:
    _index(temp_repo, mock_ingestor, _UNCHANGED)

    assert qn in get_node_names(mock_ingestor, label)
    assert _parents(mock_ingestor, label)[qn] == (str(parent[0]), parent[1])


def test_nested_named_function_keeps_its_own_qn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Only the class moves: the function it is written in stays registered
    # where it was, under the method's class.
    _index(temp_repo, mock_ingestor, _NAMED_IN_METHOD)

    functions = get_node_names(mock_ingestor, NodeLabel.FUNCTION)
    assert f"{_BOX}.inner" in functions
    assert _parents(mock_ingestor, NodeLabel.FUNCTION)[f"{_BOX}.inner"] == (
        str(NodeLabel.METHOD),
        f"{_BOX}.run",
    )
