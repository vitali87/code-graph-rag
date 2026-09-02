"""`value.Name()` must not resolve to a class named `Name` (#1641).

Two resolution paths reach a class by discarding the receiver and looking the
trailing name up alone -- `_try_resolve_module_method` within a module and
`_try_resolve_via_trie` across modules. The emit site then records INSTANTIATES
for any callee that resolved to a Class, so a method call became a construction.

Measured on gin-gonic/gin before the fix: all 82 INSTANTIATES edges targeted a
type whose name is also a method name, and every count tracked method calls
rather than literals (`errors.Error` 27 edges against 35 `.Error()` calls and 6
genuine literals). The spurious path also emitted CALLS to `__init__` in Python,
so the caller appeared to run the constructor.

Not a Go defect: Python reproduces it identically, which is why the guard sits
in the shared resolver rather than in a Go path. JS/TS and Dart never
reproduced because each already carries its own version of this rule.

The controls matter as much as the defect. A receiver that names a CLASS
(`Outer.Inner()`) or a MODULE (`mod_a.Error()`) is a genuine construction, and
a fix phrased as "a dotted call never constructs" would silently delete both.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import NodeLabel, RelationshipType
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_nodes,
    get_qualified_names,
    get_relationships,
)


def _targets(mock_ingestor: MagicMock, rel: RelationshipType) -> set[tuple[str, str]]:
    return {
        (call[0][0][2], call[0][2][2])
        for call in get_relationships(mock_ingestor, rel.value)
    }


def _instantiations(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return _targets(mock_ingestor, RelationshipType.INSTANTIATES)


@pytest.fixture
def go_project(temp_repo: Path) -> Path:
    project = temp_repo / "govalue"
    project.mkdir()
    (project / "go.mod").write_text(
        encoding="utf-8", data="module govalue\n\ngo 1.22\n"
    )
    # `Error` is both a first-party struct and the stdlib error interface's
    # method. `describe` calls .Error() on a stdlib error and constructs
    # nothing; `useHelper` is the control -- a real receiver method call, which
    # Go's row in the language table explicitly claims ("receiver methods with
    # cross-file binding") and which must keep resolving to the METHOD.
    (project / "m.go").write_text(
        encoding="utf-8",
        data="""package m

import "errors"

type Error struct {
\tMsg string
}

type Box struct{}

func (b Box) helper() int {
\treturn 1
}

func useHelper(b Box) int {
\treturn b.helper()
}

func describe() string {
\terr := errors.New("failed")
\treturn err.Error()
}
""",
    )
    return project


@pytest.fixture
def python_project(temp_repo: Path) -> Path:
    project = temp_repo / "pyvalue"
    project.mkdir()
    (project / "mod_a.py").write_text(
        encoding="utf-8",
        data="""
class Error:
    def __init__(self, msg):
        self.msg = msg

    def Error(self):
        return self.msg


class Outer:
    class Inner:
        def __init__(self, v):
            self.v = v


instance = Error("x")


def helper():
    return 1
""",
    )
    (project / "mod_b.py").write_text(
        encoding="utf-8",
        data='''
import mod_a


def describe(err):
    """`err` is a parameter: a VALUE receiver, constructs nothing."""
    return err.Error()


def build_via_module():
    """A MODULE receiver: a genuine construction."""
    return mod_a.Error("boom")
''',
    )
    (project / "mod_c.py").write_text(
        encoding="utf-8",
        data='''
from mod_a import Outer


def build_nested():
    """A CLASS receiver: also a genuine construction."""
    return Outer.Inner(1)
''',
    )
    # An import brings in VALUES as well as namespaces, so "the receiver head is
    # in the import map" is not the same question as "the receiver is a
    # namespace". Both kinds live here.
    (project / "mod_d.py").write_text(
        encoding="utf-8",
        data='''
import mod_a
from mod_a import Error
from mod_a import instance
from mod_a import helper


def describe_imported_instance():
    """Receiver is an imported INSTANCE -- a value."""
    return instance.Error()


def describe_imported_function():
    """Receiver is an imported FUNCTION -- also a value."""
    return helper.Error()


def build_via_imported_module():
    """Receiver is an imported MODULE -- a namespace."""
    return mod_a.Error("boom")


def build_via_imported_class():
    """Receiver is an imported CLASS -- a namespace for nested construction."""
    return Outer.Inner(2)


from mod_a import Outer  # noqa: E402  (kept last so the fixture reads in order)
''',
    )
    return project


class TestGo:
    def test_method_call_named_like_a_type_does_not_instantiate(
        self, go_project: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(go_project, mock_ingestor)
        edges = _instantiations(mock_ingestor)
        assert ("govalue.m.describe", "govalue.m.Error") not in edges

    def test_a_real_receiver_method_call_still_resolves(
        self, go_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # The control for the claimed feature. Without it, a guard that simply
        # refused every dotted call would pass the test above.
        create_and_run_updater(go_project, mock_ingestor)
        calls = _targets(mock_ingestor, RelationshipType.CALLS)
        assert ("govalue.m.useHelper", "govalue.m.Box.helper") in calls


class TestPython:
    def test_method_call_named_like_a_class_does_not_instantiate(
        self, python_project: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(python_project, mock_ingestor)
        edges = _instantiations(mock_ingestor)
        assert ("pyvalue.mod_b.describe", "pyvalue.mod_a.Error") not in edges

    def test_the_spurious_constructor_call_goes_too(
        self, python_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # The bad resolution also produced CALLS -> __init__, so the caller
        # looked like it ran the constructor. Asserted separately because
        # dropping only the INSTANTIATES edge would leave this behind.
        create_and_run_updater(python_project, mock_ingestor)
        calls = _targets(mock_ingestor, RelationshipType.CALLS)
        assert ("pyvalue.mod_b.describe", "pyvalue.mod_a.Error.__init__") not in calls

    def test_a_module_receiver_still_constructs(
        self, python_project: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(python_project, mock_ingestor)
        edges = _instantiations(mock_ingestor)
        assert ("pyvalue.mod_b.build_via_module", "pyvalue.mod_a.Error") in edges

    def test_a_class_receiver_still_constructs_a_nested_class(
        self, python_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Outer.Inner()` has a receiver, and it IS an instantiation. This is
        # the case a "dotted calls never construct" rule would delete.
        create_and_run_updater(python_project, mock_ingestor)
        edges = _instantiations(mock_ingestor)
        assert ("pyvalue.mod_c.build_nested", "pyvalue.mod_a.Outer.Inner") in edges


class TestImportedReceiverKind:
    """An import map entry says a name was imported, not what KIND it is.

    `from mod_a import instance` and `import mod_a` both put a name in the same
    map, so a guard that accepts any imported head lets the spurious edge back
    in through the import door. The target's node kind is what separates them.
    """

    def test_an_imported_instance_does_not_construct(
        self, python_project: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(python_project, mock_ingestor)
        edges = _instantiations(mock_ingestor)
        assert (
            "pyvalue.mod_d.describe_imported_instance",
            "pyvalue.mod_a.Error",
        ) not in edges

    def test_an_imported_function_does_not_construct(
        self, python_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # A function is a value too. An import-kind check that special-cased
        # only instances would pass the test above and still be wrong.
        create_and_run_updater(python_project, mock_ingestor)
        edges = _instantiations(mock_ingestor)
        assert (
            "pyvalue.mod_d.describe_imported_function",
            "pyvalue.mod_a.Error",
        ) not in edges

    def test_an_imported_module_still_constructs(
        self, python_project: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(python_project, mock_ingestor)
        edges = _instantiations(mock_ingestor)
        assert (
            "pyvalue.mod_d.build_via_imported_module",
            "pyvalue.mod_a.Error",
        ) in edges

    def test_an_imported_class_still_constructs_its_nested_class(
        self, python_project: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(python_project, mock_ingestor)
        edges = _instantiations(mock_ingestor)
        assert (
            "pyvalue.mod_d.build_via_imported_class",
            "pyvalue.mod_a.Outer.Inner",
        ) in edges


class TestFixtureIsLive:
    def test_the_class_node_still_exists(
        self, python_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # Guards against the whole fixture silently failing to parse, which
        # would make every "not in" assertion above pass vacuously.
        create_and_run_updater(python_project, mock_ingestor)
        classes = get_qualified_names(get_nodes(mock_ingestor, NodeLabel.CLASS.value))
        assert "pyvalue.mod_a.Error" in classes
        assert "pyvalue.mod_a.Outer.Inner" in classes
