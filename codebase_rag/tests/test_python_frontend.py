# Jedi Python semantic frontend, stage 1 of issue #1183. The heuristics
# approximate re-export chains, decorated callables, and MRO dispatch; the
# frontend's compiler-grade facts resolve them exactly and prove externality
# for calls leaving the repo. Off (the default) everything stays byte-for-byte
# heuristic; ambiguity emits no fact, never a guess.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import ALL_ENABLED
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

jedi = pytest.importorskip("jedi")


@pytest.fixture(autouse=True)
def _isolated_jedi_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pytest-xdist workers otherwise share jedi's on-disk pickle cache and
    # race it (EOFError: Ran out of input on CI); each test gets its own.
    monkeypatch.setattr(jedi.settings, "cache_directory", str(tmp_path / "jedi-cache"))


from codebase_rag.parsers.py_frontend import frontend as py_fe  # noqa: E402
from codebase_rag.parsers.py_frontend.frontend import (  # noqa: E402
    _byte_to_char_col,
    _CallSite,
    _char_to_byte_col,
    run_python_frontend,
)

CALLS = cs.RelationshipType.CALLS.value


def _calls_edges_for(
    tmp_path: Path, files: dict[str, str], mode: cs.PythonFrontend
) -> set:
    parsers, queries = load_parsers()
    repo = tmp_path / "proj"
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    previous = settings.PYTHON_FRONTEND
    settings.PYTHON_FRONTEND = mode
    try:
        mock = MagicMock()
        GraphUpdater(
            ingestor=mock,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            capture=ALL_ENABLED,
        ).run()
    finally:
        settings.PYTHON_FRONTEND = previous
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == CALLS
    }


def test_reexport_chain_resolves_to_the_definition(tmp_path: Path) -> None:
    files = {
        "pkg/__init__.py": "from .impl import f\n",
        "pkg/impl.py": "def f():\n    return 1\n",
        "caller.py": "from pkg import f\n\n\ndef use():\n    f()\n",
    }
    calls = _calls_edges_for(tmp_path, files, cs.PythonFrontend.JEDI)
    assert ("proj.caller.use", "proj.pkg.impl.f") in calls


def test_stdlib_call_is_proven_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cold jedi cache makes the first stdlib inference slow; the budget is
    # not what this test measures, so give it room (loaded CI workers).
    monkeypatch.setattr(py_fe, "_FILE_BUDGET_SECONDS", 60.0)
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "caller.py").write_text(
        'import os\n\n\ndef use():\n    os.getenv("X")\n', encoding="utf-8"
    )
    facts = run_python_frontend(repo, [repo / "caller.py"])
    assert ("caller.py", 5, 7, "getenv") in facts.external_sites
    assert not facts.resolved_call_sites


def test_repo_local_shadow_beats_the_stdlib(tmp_path: Path) -> None:
    # A first-party os.py next to the caller IS what `import os` binds at
    # runtime (script-dir precedence); jedi models that, so the edge to the
    # local module is correct, never a fabrication.
    files = {
        "os.py": "def getenv(key):\n    return key\n",
        "caller.py": 'import os\n\n\ndef use():\n    os.getenv("X")\n',
    }
    calls = _calls_edges_for(tmp_path, files, cs.PythonFrontend.JEDI)
    assert ("proj.caller.use", "proj.os.getenv") in calls


def test_decorated_function_still_resolves(tmp_path: Path) -> None:
    files = {
        "deco.py": (
            "import functools\n\n\ndef wrap(fn):\n"
            "    @functools.wraps(fn)\n    def inner(*a, **k):\n"
            "        return fn(*a, **k)\n\n    return inner\n\n\n"
            "@wrap\ndef work():\n    return 1\n"
        ),
        "caller.py": "from deco import work\n\n\ndef use():\n    work()\n",
    }
    calls = _calls_edges_for(tmp_path, files, cs.PythonFrontend.JEDI)
    assert ("proj.caller.use", "proj.deco.work") in calls


_ALIASED_REEXPORT = {
    "pkg/__init__.py": "from .impl import real_fn as f\n",
    "pkg/impl.py": "def real_fn():\n    return 1\n",
    "caller.py": "from pkg import f\n\n\ndef use():\n    f()\n",
}


def test_aliased_reexport_is_the_jedi_discriminator(tmp_path: Path) -> None:
    # The heuristics drop an aliased re-export entirely; jedi follows the
    # rename through __init__ to the real definition.
    calls = _calls_edges_for(
        tmp_path, files=_ALIASED_REEXPORT, mode=cs.PythonFrontend.JEDI
    )
    assert ("proj.caller.use", "proj.pkg.impl.real_fn") in calls


def test_heuristic_mode_stays_heuristic(tmp_path: Path) -> None:
    calls = _calls_edges_for(
        tmp_path, files=_ALIASED_REEXPORT, mode=cs.PythonFrontend.HEURISTIC
    )
    assert ("proj.caller.use", "proj.pkg.impl.real_fn") not in calls


def test_budget_exhaustion_degrades_to_heuristics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(py_fe, "_FILE_BUDGET_SECONDS", -1.0)
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "a.py").write_text("import os\n\n\ndef f():\n    os.getenv('X')\n")
    facts = run_python_frontend(repo, [repo / "a.py"])
    assert not facts.resolved_call_sites
    assert not facts.external_sites


def test_call_site_walker_byte_columns_on_unicode_lines() -> None:
    import ast

    source = 'x = "café"; obj.método(1)\nfrom m import fn\nfn()\n'
    collector = _CallSite()
    collector.visit(ast.parse(source))
    sites = {(name, line, col) for name, line, col in collector.sites}
    method_col = len('x = "café"; obj.'.encode())
    assert ("método", 1, method_col) in sites
    assert ("fn", 3, 0) in sites


def test_column_conversions_round_trip() -> None:
    line = 'x = "café" + f("a")'
    for char_col in range(len(line)):
        byte_col = _char_to_byte_col(line, char_col)
        assert _byte_to_char_col(line, byte_col) == char_col
