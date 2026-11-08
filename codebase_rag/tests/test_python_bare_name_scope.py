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

from pathlib import Path
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

# An import binds the name only to what it names (bot review on PR #2968):
# an external package's `get`, or a star import of a module without
# `print`, leaves the project's same-named functions out.
EXTERNAL = """\
from requests import get


def fetch():
    return get("u")
"""

GETTER = """\
def get(x):
    return x
"""

STAR_OTHER = """\
from lib.progress import *


def starred_other():
    return print("x")
"""

QUALIFIED = """\
import lib


def qualified():
    return lib.print("x")
"""

# `shout` here is `lib._wrap`, which `lib` re-exports from `lib.progress`;
# `lib.shout` only shares the local name (bot review on PR #2968).
ALIASED_REEXPORT = """\
from lib import _wrap as shout


def wrapped():
    return shout(1)
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
    _write(root, "external.py", EXTERNAL)
    _write(root, "getter.py", GETTER)
    _write(root, "star_other.py", STAR_OTHER)
    _write(root, "aliased_reexport.py", ALIASED_REEXPORT)
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
    [
        "app.main",
        "scripts.report",
        "lib.unbound",
        "lib.namespaced",
        "external.fetch",
        "star_other.starred_other",
    ],
    ids=[
        "builtins",
        "builtin-in-loop",
        "package-submodule",
        "module-under-a-plain-directory",
        "imported-from-an-external-package",
        "star-import-of-a-module-without-it",
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


def test_an_aliased_import_names_no_sibling_sharing_the_alias(
    graph: RecordedGraph,
) -> None:
    assert "lib.shout" not in _targets(graph, "aliased_reexport.wrapped")


# A name a module passes on (bot review on PR #2968): each hop of the chain
# is a module the name really comes from, so a call through it binds the
# definition at its end, wherever that sits, and a name no hop binds stays
# unbound. The name search finds it, so the edge is heuristic; a re-export
# that renames the function is left to the Jedi frontend
# (test_python_frontend.py).

IMPL = "def helper():\n    return 1\n"

OTHER_API = "def other():\n    return 2\n"

# `log` is bound at run time to the closure the factory returns.
BUILD = """\
def _make_logger():
    def log(msg):
        return msg

    return log


log = _make_logger()
"""


def _app(imports: str, call: str) -> str:
    return f"{imports}\n\n\ndef main():\n    return {call}\n"


def _index_files(root: Path, files: dict[str, str]) -> RecordedGraph:
    for rel, text in files.items():
        _write(root, rel, text)
    return _index(root, MagicMock())


@pytest.mark.parametrize(
    ("files", "target"),
    [
        (
            {
                "impl.py": IMPL,
                "api.py": "from impl import helper\n",
                "app.py": _app("from api import *", "helper()"),
            },
            "impl.helper",
        ),
        (
            {
                "impl.py": IMPL,
                "api.py": "from impl import *\n",
                "app.py": _app("from api import *", "helper()"),
            },
            "impl.helper",
        ),
        (
            {
                "impl.py": IMPL,
                "api.py": "from impl import helper\n",
                "app.py": _app("from api import helper", "helper()"),
            },
            "impl.helper",
        ),
        (
            {
                "impl.py": IMPL,
                "api.py": "from impl import *\n",
                "app.py": _app("from api import helper", "helper()"),
            },
            "impl.helper",
        ),
        (
            {
                "impl.py": IMPL,
                "mid.py": "from impl import helper\n",
                "api.py": "from mid import *\n",
                "app.py": _app("from api import helper", "helper()"),
            },
            "impl.helper",
        ),
        (
            {
                "pkg/__init__.py": "from .sub import helper\n",
                "pkg/sub/__init__.py": "from .impl import helper\n",
                "pkg/sub/impl.py": IMPL,
                "app.py": _app("from pkg import helper", "helper()"),
            },
            "pkg.sub.impl.helper",
        ),
        (
            {
                "pkg/__init__.py": "from .sub import *\n",
                "pkg/sub/__init__.py": "from .impl import *\n",
                "pkg/sub/impl.py": IMPL,
                "app.py": _app("from pkg import *", "helper()"),
            },
            "pkg.sub.impl.helper",
        ),
        (
            {
                "build.py": BUILD,
                "api.py": "from build import log\n",
                "app.py": _app("from api import log", "log('x')"),
            },
            "build._make_logger.log",
        ),
    ],
    ids=[
        "star-then-named",
        "star-then-star",
        "named-then-named",
        "named-then-star",
        "named-then-star-then-named",
        "nested-packages",
        "nested-packages-through-stars",
        "run-time-binding-passed-on",
    ],
)
def test_a_name_passed_on_binds_the_definition_it_reaches(
    tmp_path: Path, files: dict[str, str], target: str
) -> None:
    graph = _index_files(tmp_path / "rx", files)
    assert _targets(graph, "app.main") == {target: "heuristic"}


@pytest.mark.parametrize(
    "files",
    [
        {
            "impl.py": IMPL,
            "api.py": OTHER_API,
            "app.py": _app("from api import *", "helper()"),
        },
        {
            "impl.py": IMPL,
            "api.py": f"from impl import helper\n\n\n{OTHER_API}",
            "app.py": _app("from api import other", "helper()"),
        },
        {
            "impl.py": IMPL,
            "api.py": "from impl import helper as h\n",
            "app.py": _app("from api import *", "helper()"),
        },
    ],
    ids=[
        "star-of-a-module-that-never-imports-it",
        "named-import-of-another-name",
        "passed-on-under-another-name",
    ],
)
def test_a_name_no_hop_binds_stays_unbound(
    tmp_path: Path, files: dict[str, str]
) -> None:
    graph = _index_files(tmp_path / "rx", files)
    assert _targets(graph, "app.main") == {}
