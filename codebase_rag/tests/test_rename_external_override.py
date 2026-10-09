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
