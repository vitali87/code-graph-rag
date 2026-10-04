"""Issue #2929: a bare Python name reaches no function its module cannot see.

A bare name inside a function is looked up in its locals, the enclosing
functions, its module's globals and imports, then the builtins. The
resolver's last resort, a project-wide search by name, bound every bare call
nothing else resolved to any same-named module-level function in the
project: on rich, 234 builtin `print(...)` calls in modules that never
import it became heuristic callers of `rich.print`, and `cgr rename` refused
to rename it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

LIB = """\
from .progress import wrap as _wrap


def print(*objects, sep=" ", end="\\n"):
    return objects


def open(path, mode="r"):
    return path


def sorted(items):
    return items


def shout():
    return print("hey")


def unbound():
    return track()


def namespaced():
    return emit("x")
"""

PROGRESS = """\
def wrap(x):
    return x


def track():
    return 1
"""

APP = """\
import json


def main():
    print("starting")
    with open("config.json") as fh:
        data = json.load(fh)
    return sorted(data)


def handled(log):
    try:
        return 1
    except ValueError as error:
        log(error)
"""

SCRIPTS = """\
def report(rows):
    for row in rows:
        print(row)
"""

# In `lib/tools/`, a directory with no `__init__.py`.
OUTPUT = """\
def emit(x):
    return x
"""

OTHER = """\
def error():
    return 1
"""

ALIASED = """\
from lib import print as lprint


def aliased():
    return lprint("x")
"""

STARRED = """\
from lib import *


def starred():
    return print("x")
"""

QUALIFIED = """\
import lib


def qualified():
    return lib.print("x")
"""


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("bltn") / "bltn"
    _write(root, "lib/__init__.py", LIB)
    _write(root, "lib/progress.py", PROGRESS)
    _write(root, "lib/tools/output.py", OUTPUT)
    _write(root, "app.py", APP)
    _write(root, "scripts.py", SCRIPTS)
    _write(root, "other.py", OTHER)
    _write(root, "aliased.py", ALIASED)
    _write(root, "starred.py", STARRED)
    _write(root, "qualified.py", QUALIFIED)
    return _index(root, MagicMock())


def _targets(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel in ("CALLS", "REFERENCES") and src == f"{prefix}{caller}"
    }


@pytest.mark.parametrize(
    "caller",
    ["app.main", "scripts.report", "lib.unbound", "lib.namespaced"],
    ids=[
        "builtins",
        "builtin-in-loop",
        "package-submodule",
        "module-under-a-plain-directory",
    ],
)
def test_a_bare_name_its_module_cannot_see_binds_nothing_elsewhere(
    graph: RecordedGraph, caller: str
) -> None:
    # `print`, `open`, `sorted` are builtins there; `track` and `emit` are
    # in submodules `lib/__init__.py` never imports.
    assert _targets(graph, caller) == {}


# Negative: what must not change.


@pytest.mark.parametrize(
    ("caller", "target"),
    [
        ("lib.shout", "lib.print"),
        ("aliased.aliased", "lib.print"),
        ("starred.starred", "lib.print"),
        ("qualified.qualified", "lib.print"),
    ],
    ids=["own-module", "aliased-import", "star-import", "module-import"],
)
def test_a_name_the_module_binds_still_resolves(
    graph: RecordedGraph, caller: str, target: str
) -> None:
    assert target in _targets(graph, caller)


def test_an_except_variable_passed_on_names_no_function(
    graph: RecordedGraph,
) -> None:
    assert "other.error" not in _targets(graph, "app.handled")
