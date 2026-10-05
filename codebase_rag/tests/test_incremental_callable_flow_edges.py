# A function passed as an argument (`run_callback(_on_start)`) gets an edge
# from the function that invokes the parameter (`run_callback -> _on_start`).
# The edge leaves the RECEIVING file but is derived from bindings recorded
# while walking the PASSING file. An incremental run that re-parses the
# receiving file only as a dependent of an edited import deleted its outgoing
# edges, and the passing file, a dependent of a dependent, was never walked
# to re-emit this one: it stayed gone until the receiving file itself was
# edited (issue #2911).
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_UTIL = "def log(msg):\n    print(msg)\n"
_REGISTRY = (
    "from util import log\n\n\n"
    "def run_callback(fn):\n"
    '    log("running")\n'
    "    return fn()\n"
)
_PLUGINS = (
    "from registry import run_callback\n\n\n"
    "def _on_start():\n"
    '    return "started"\n\n\n'
    "def main():\n"
    "    return run_callback(_on_start)\n"
)
# The flask json/tag.py shape: a registry method instantiates the class it
# is handed.
_TAG = (
    "from util import log\n\n\n"
    "class Serializer:\n"
    "    def register(self, tag_class):\n"
    '        log("registering")\n'
    "        return tag_class()\n"
)
_TAG_USER = (
    "from tag import Serializer\n\n\n"
    "class TagFoo:\n"
    "    pass\n\n\n"
    "def setup():\n"
    "    return Serializer().register(TagFoo)\n"
)
# A dependent of util.py whose functions invoke no parameter, and a caller
# of it: nothing of other.py derives from a binding recorded elsewhere.
_PLAIN = "from util import log\n\n\ndef plain(x):\n    log(x)\n    return x\n"
_OTHER = "from plain import plain\n\n\ndef use():\n    return plain(1)\n"

# A pass-through: wrap.py only forwards its parameter into run_callback, so
# the binding of `_hook` is recorded while walking user.py, two levels past
# registry.py.
_WRAP = (
    "from registry import run_callback\n\n\n"
    "def wrap(cb):\n"
    "    return run_callback(cb)\n"
)
_USER = (
    "from wrap import wrap\n\n\n"
    "def _hook():\n"
    '    return "hooked"\n\n\n'
    "def go():\n"
    "    return wrap(_hook)\n"
)

_FILES = {
    "util.py": _UTIL,
    "wrap.py": _WRAP,
    "user.py": _USER,
    "registry.py": _REGISTRY,
    "plugins.py": _PLUGINS,
    "tag.py": _TAG,
    "tag_user.py": _TAG_USER,
    "plain.py": _PLAIN,
    "other.py": _OTHER,
}

_EdgeSet = frozenset[tuple[str, str, str]]


def _write(root: Path) -> None:
    root.mkdir()
    for rel, text in _FILES.items():
        (root / rel).write_text(text, encoding="utf-8")


def _index(store: _StatefulIngestor, root: Path, force: bool) -> GraphUpdater:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=force)
    return updater


def _edges(store: _StatefulIngestor) -> _EdgeSet:
    return frozenset(
        (str(fv), str(rel), str(tv)) for (_fl, fv, rel, _tl, tv) in store.edges
    )


def _append_comment(path: Path, root: Path) -> None:
    # Past the hash cache's mtime, so the incremental pass hashes the file.
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(path.read_text(encoding="utf-8") + "# comment\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


_CALLBACK = ("proj.registry.run_callback", "CALLS", "proj.plugins._on_start")
_FORWARDED = ("proj.registry.run_callback", "CALLS", "proj.user._hook")


def _instantiates_tag(edges: _EdgeSet) -> set[tuple[str, str, str]]:
    return {
        e
        for e in edges
        if e[0] == "proj.tag.Serializer.register" and e[2] == "proj.tag_user.TagFoo"
    }


def test_editing_an_import_of_the_receiver_keeps_the_callable_edges(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _write(root)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    clean = _edges(store)
    assert _CALLBACK in clean, "fixture must produce the callable-argument edge"
    assert _FORWARDED in clean, "fixture must produce the pass-through edge"
    assert _instantiates_tag(clean), "fixture must produce the register edge"

    _append_comment(root / "util.py", root)
    _index(store, root, force=False)
    after = _edges(store)

    assert _CALLBACK in after, sorted(clean - after)
    assert _FORWARDED in after, sorted(clean - after)
    assert _instantiates_tag(after) == _instantiates_tag(clean)
    assert after == clean, (sorted(clean - after), sorted(after - clean))


def test_editing_the_receiver_itself_keeps_the_callable_edge(
    temp_repo: Path,
) -> None:
    # The receiver's direct callers are re-parsed as its dependents on main
    # too, so `_on_start` survives there; `_hook` is passed through wrap.py,
    # and its passer, user.py, is a level further out.
    root = temp_repo / "proj"
    _write(root)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    clean = _edges(store)

    _append_comment(root / "registry.py", root)
    _index(store, root, force=False)
    assert _edges(store) == clean


def test_a_dependent_without_callable_parameters_does_not_widen_the_reparse(
    temp_repo: Path,
) -> None:
    # Negative: plain.py is re-parsed as util.py's dependent, but none of its
    # functions invokes a parameter, so its own caller stays untouched.
    root = temp_repo / "proj"
    _write(root)
    store = _StatefulIngestor()
    _index(store, root, force=True)

    _append_comment(root / "util.py", root)
    updater = _index(store, root, force=False)
    reparsed = updater._reparsed_file_keys
    assert "plain.py" in reparsed, sorted(reparsed)
    assert "other.py" not in reparsed, sorted(reparsed)
