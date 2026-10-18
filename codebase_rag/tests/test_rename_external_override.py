"""`rename` refuses to silently stop an override of something outside the project.

Renaming `Worker(threading.Thread).run`, `TestCase.setUp`, `__str__` or a
Java `@Override toString()` rewrote "1 site" and reported success: the
method then overrode nothing, so the framework, the interpreter or the JDK
never called it again (or Java no longer compiled), and no call site showed
why (issue #3226).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import RenameRefused, rename
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

_PY = """\
import threading
import unittest


class Worker(threading.Thread):
    def run(self):
        return 1


class Money:
    def __str__(self):
        return "m"

    def amount(self):
        return 1

    def __total(self):
        return 2


class T(unittest.TestCase):
    def setUp(self):
        self.x = 1

    def test_one(self):
        self.assertEqual(self.x, 1)


class Shape:
    def area(self):
        return 0


class Circle(Shape):
    def area(self):
        return 3


def main():
    Worker().start()
    return str(Money()) + str(Circle().area())
"""
_JAVA = """\
public class Task implements Runnable {
    @Override
    public void run() {}

    @Override
    public String toString() { return "t"; }

    public void plain() {}
}

class Sub extends Task {
    @Override
    public void plain() {}
}
"""
_TS = (
    "export class A extends Error {\n"
    '  override toString(): string { return "a"; }\n'
    "  label(): string { return this.toString(); }\n"
    "}\n"
)


@pytest.fixture
def py_graph(temp_repo: Path, mock_ingestor: MagicMock) -> RecordedGraph:
    _write(temp_repo, "w.py", _PY)
    return _index(temp_repo, mock_ingestor)


@pytest.fixture
def java_graph(temp_repo: Path, mock_ingestor: MagicMock) -> RecordedGraph:
    if cs.SupportedLanguage.JAVA not in load_parsers()[0]:
        pytest.skip("java parser not available")
    _write(temp_repo, "Task.java", _JAVA)
    return _index(temp_repo, mock_ingestor)


def _plan(root: Path, graph: RecordedGraph, member: str, new: str, **kwargs: object):
    return rename(
        root,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.{member}",
        new,
        dry_run=True,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        ("w.Worker.run", "overrides a method outside the project"),
        ("w.T.setUp", "overrides a method outside the project"),
        ("w.Money.__str__", "is the Python protocol method __str__"),
    ],
    ids=["thread-run", "testcase-setup", "dunder"],
)
def test_a_python_override_of_an_outside_method_is_refused(
    temp_repo: Path, py_graph: RecordedGraph, member: str, reason: str
) -> None:
    before = (temp_repo / "w.py").read_text()
    with pytest.raises(RenameRefused) as refused:
        _plan(temp_repo, py_graph, member, "renamed")
    assert reason in str(refused.value), refused.value
    assert cs.MCPParamName.ALLOW_EXTERNAL_OVERRIDE in str(refused.value)
    assert (temp_repo / "w.py").read_text() == before


@pytest.mark.parametrize("member", ["Task.Task.run()", "Task.Task.toString()"])
def test_a_java_override_of_a_jdk_method_is_refused(
    temp_repo: Path, java_graph: RecordedGraph, member: str
) -> None:
    with pytest.raises(RenameRefused) as refused:
        _plan(temp_repo, java_graph, member, "renamed")
    assert "is marked @Override but overrides no method in the project" in str(
        refused.value
    ), refused.value


def test_a_ts_override_modifier_without_a_project_base_is_refused(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(temp_repo, "a.ts", _TS)
    graph = _index(temp_repo, mock_ingestor)
    with pytest.raises(RenameRefused) as refused:
        _plan(temp_repo, graph, "a.A.toString", "show")
    assert "is marked override" in str(refused.value), refused.value


def test_the_cli_spelling_of_the_opt_in_is_named(
    temp_repo: Path, py_graph: RecordedGraph
) -> None:
    with pytest.raises(RenameRefused) as refused:
        _plan(
            temp_repo,
            py_graph,
            "w.Worker.run",
            "execute",
            external_override_opt_in=cs.RENAME_CLI_ALLOW_EXTERNAL_OVERRIDE,
        )
    assert "--allow-external-override" in str(refused.value)


def test_the_opt_in_renames_it_deliberately(
    temp_repo: Path, py_graph: RecordedGraph
) -> None:
    report = _plan(
        temp_repo, py_graph, "w.Worker.run", "execute", allow_external_override=True
    )
    assert "+    def execute(self):" in report.diff, report.diff


@pytest.mark.parametrize(
    "member",
    ["w.Money.amount", "w.Money.__total", "w.Shape.area", "w.Circle.area"],
    ids=["plain-method", "name-mangled-private", "project-base", "project-override"],
)
def test_project_methods_still_rename(
    temp_repo: Path, py_graph: RecordedGraph, member: str
) -> None:
    # Negatives: an ordinary method, a `__private` (not `__dunder__`) one,
    # and an override hierarchy wholly inside the project (renamed together).
    report = _plan(temp_repo, py_graph, member, "renamed")
    assert report.diff, report


def test_a_java_override_of_a_project_method_still_renames(
    temp_repo: Path, java_graph: RecordedGraph
) -> None:
    # Negative: `Sub.plain()` carries @Override, and its base is in the
    # project, so the hierarchy renames together.
    report = _plan(temp_repo, java_graph, "Task.Sub.plain()", "simple")
    assert "public void simple()" in report.diff, report.diff
    assert set(report.hierarchy) == {
        f"{java_graph.project}.Task.Sub.plain()",
        f"{java_graph.project}.Task.Task.plain()",
    }


# `Task.run()` overrides the project's `Base.run()`, and `Worker.run()` the
# project interface's `Local.run()`; both classes also implement Runnable,
# whose `run()` the rename would leave unimplemented.
_JAVA_MIXED = {
    "Local.java": "public interface Local {\n    void run();\n}\n",
    "Base.java": "public class Base {\n    public void run() {}\n}\n",
    "Task.java": (
        "public class Task extends Base implements Runnable {\n"
        "    @Override\n    public void run() {}\n}\n"
    ),
    "Worker.java": (
        "public class Worker implements Local, Runnable {\n"
        "    @Override\n    public void run() {}\n}\n"
    ),
    "Plain.java": (
        "public class Plain extends Base {\n    @Override\n    public void run() {}\n}\n"
    ),
}


@pytest.fixture
def java_mixed(temp_repo: Path, mock_ingestor: MagicMock) -> RecordedGraph:
    if cs.SupportedLanguage.JAVA not in load_parsers()[0]:
        pytest.skip("java parser not available")
    for rel, text in _JAVA_MIXED.items():
        _write(temp_repo, rel, text)
    return _index(temp_repo, mock_ingestor)


@pytest.mark.parametrize(
    "member",
    ["Task.Task.run()", "Base.Base.run()", "Worker.Worker.run()", "Local.Local.run()"],
    ids=["override", "its-project-base", "implementation", "its-project-interface"],
)
def test_a_project_base_does_not_hide_an_outside_contract(
    temp_repo: Path, java_mixed: RecordedGraph, member: str
) -> None:
    with pytest.raises(RenameRefused) as refused:
        _plan(temp_repo, java_mixed, member, "execute")
    assert "java.lang.Runnable" in str(refused.value), refused.value


def test_an_override_whose_class_has_no_outside_supertype_still_renames(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: `Plain` extends only the project's `Base`, so its @Override
    # is the project base's and the hierarchy renames together.
    if cs.SupportedLanguage.JAVA not in load_parsers()[0]:
        pytest.skip("java parser not available")
    for rel in ("Base.java", "Plain.java"):
        _write(temp_repo, rel, _JAVA_MIXED[rel])
    graph = _index(temp_repo, mock_ingestor)
    report = _plan(temp_repo, graph, "Plain.Plain.run()", "execute")
    assert "public void execute()" in report.diff, report.diff


_PY_DUNDER_NAMES = """\
class __Widget__:
    pass


def __str__():
    return "module function"


def __getattr__(name):
    return name


def use():
    return __str__() + str(__Widget__())
"""


@pytest.mark.parametrize(
    "member", ["d.__Widget__", "d.__str__"], ids=["class", "module-function"]
)
def test_a_dunder_name_that_python_never_dispatches_still_renames(
    temp_repo: Path, mock_ingestor: MagicMock, member: str
) -> None:
    # Negative: only a method (or a module hook) is called by the
    # interpreter under its dunder name.
    _write(temp_repo, "d.py", _PY_DUNDER_NAMES)
    graph = _index(temp_repo, mock_ingestor)
    report = _plan(temp_repo, graph, member, "renamed")
    assert report.diff, report


def test_a_module_getattr_hook_is_refused(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PEP 562: the interpreter calls a module's `__getattr__` for a missing
    # attribute, so it stays a protocol name at module level.
    _write(temp_repo, "d.py", _PY_DUNDER_NAMES)
    graph = _index(temp_repo, mock_ingestor)
    with pytest.raises(RenameRefused) as refused:
        _plan(temp_repo, graph, "d.__getattr__", "lookup")
    assert "is the Python protocol method __getattr__" in str(refused.value)
