"""Each parser module imports on its own, whichever is imported first.

`parsers/__init__.py` used to import every processor, and the frontends
package registered every bundled frontend on import. Any `parsers.<x>` import
therefore loaded the whole pipeline in one fixed order, which hid cycles
between the frontends package and the language packages: entering through a
language frontend first failed on a partially initialised module.
"""

import subprocess
import sys

import pytest

from codebase_rag import constants as cs

# Deadline for the child interpreter, not a performance assertion.
_TIMEOUT_SECONDS = 120


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


@pytest.mark.parametrize(
    "module",
    [
        "codebase_rag.parsers.go_frontend",
        "codebase_rag.parsers.java_frontend",
        "codebase_rag.parsers.py_frontend",
        "codebase_rag.parsers.semantic_call_join",
        "codebase_rag.language.catalog",
    ],
)
def test_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    result = _run(f"import {module}")
    assert result.returncode == 0, result.stderr


def test_a_leaf_parser_module_does_not_load_the_processors() -> None:
    result = _run(
        "import sys, codebase_rag.parsers.notebook; "
        "assert 'codebase_rag.parsers.call_processor' not in sys.modules"
    )
    assert result.returncode == 0, result.stderr


def test_the_graph_builder_sees_every_bundled_frontend() -> None:
    result = _run(
        "import codebase_rag.graph_updater as g; "
        "print(','.join(sorted(map(str, g.FRONTENDS))))"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["c_sharp,go,java,python"]
