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
from codebase_rag.checkout_state import state_file
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

# A wrapper that, like registry.py, depends on util.py, and sorts before
# it: it is read before `run_callback` is known to take part in the flow.
_EARLY_WRAP = (
    "from registry import run_callback\n"
    "from util import log\n\n\n"
    "def early_wrap(cb):\n"
    '    log("wrapping")\n'
    "    return run_callback(cb)\n"
)
_EARLY_USER = (
    "from aaa_wrap import early_wrap\n\n\n"
    "def _early():\n"
    '    return "early"\n\n\n'
    "def kick():\n"
    "    return early_wrap(_early)\n"
)

_FILES = {
    "util.py": _UTIL,
    "aaa_wrap.py": _EARLY_WRAP,
    "aaa_user.py": _EARLY_USER,
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


# Receivers whose name is not in a `name` field: a JS arrow function is
# named by the variable it is bound to, a C++ definition by its declarator.
_JS_FILES = {
    "util.js": "export function log(msg) {\n  console.log(msg);\n}\n",
    "registry.js": (
        "import { log } from './util';\n\n"
        "export const runCallback = (fn) => {\n"
        "  log('running');\n"
        "  return fn();\n"
        "};\n"
    ),
    "plugins.js": (
        "import { runCallback } from './registry';\n\n"
        "function onStart() {\n  return 1;\n}\n\n"
        "export function main() {\n  return runCallback(onStart);\n}\n"
    ),
}
_CPP_FILES = {
    "util.h": "#pragma once\nvoid log_msg(const char* msg);\n",
    "util.cpp": '#include "util.h"\nvoid log_msg(const char* msg) {}\n',
    "registry.h": "#pragma once\nint run_callback(int (*fn)());\n",
    "registry.cpp": (
        '#include "registry.h"\n#include "util.h"\n'
        "int run_callback(int (*fn)()) {\n"
        '  log_msg("running");\n'
        "  return fn();\n"
        "}\n"
    ),
    "plugins.cpp": (
        '#include "registry.h"\n'
        "static int on_start() { return 1; }\n"
        "int main_entry() { return run_callback(on_start); }\n"
    ),
}


def _write(root: Path, files: dict[str, str] = _FILES) -> None:
    root.mkdir()
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")


def _index(
    store: _StatefulIngestor,
    root: Path,
    force: bool,
    language: cs.SupportedLanguage = cs.SupportedLanguage.PYTHON,
) -> GraphUpdater:
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
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


def _rewrite(path: Path, root: Path, text: str) -> None:
    # Past the hash cache's mtime, so the incremental pass hashes the file.
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(text, encoding="utf-8")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


def _append_comment(path: Path, root: Path) -> None:
    _rewrite(path, root, path.read_text(encoding="utf-8") + "# comment\n")


def _widen_log(root: Path) -> None:
    # A new parameter changes util.py's interface, so its dependents
    # re-parse; a comment alone keeps it and they keep their edges as they
    # are (issue #2531). No edge moves: every caller still binds `log`.
    _rewrite(root / "util.py", root, _UTIL.replace("(msg)", "(msg, level=0)", 1))


_CALLBACK = ("proj.registry.run_callback", "CALLS", "proj.plugins._on_start")
_FORWARDED = ("proj.registry.run_callback", "CALLS", "proj.user._hook")
_EARLY = ("proj.registry.run_callback", "CALLS", "proj.aaa_user._early")


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
    assert _EARLY in clean, "fixture must produce the early wrapper's edge"
    assert _instantiates_tag(clean), "fixture must produce the register edge"

    _widen_log(root)
    _index(store, root, force=False)
    after = _edges(store)

    assert _CALLBACK in after, sorted(clean - after)
    assert _FORWARDED in after, sorted(clean - after)
    assert _EARLY in after, sorted(clean - after)
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


def test_an_edited_wrapper_brings_in_its_callers(temp_repo: Path) -> None:
    # A comment keeps wrap.py's interface, so its callers are not its
    # dependents on this run; the receiver it forwards into re-parses as
    # util.py's dependent all the same, and needs user.py walked.
    root = temp_repo / "proj"
    _write(root)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    clean = _edges(store)

    _widen_log(root)
    _append_comment(root / "wrap.py", root)
    _index(store, root, force=False)
    after = _edges(store)

    assert _FORWARDED in after, sorted(clean - after)
    assert after == clean, (sorted(clean - after), sorted(after - clean))


def test_a_dependent_without_callable_parameters_does_not_widen_the_reparse(
    temp_repo: Path,
) -> None:
    # Negative: plain.py is re-parsed as util.py's dependent, but none of its
    # functions invokes a parameter, so its own caller stays untouched.
    root = temp_repo / "proj"
    _write(root)
    store = _StatefulIngestor()
    _index(store, root, force=True)

    _widen_log(root)
    updater = _index(store, root, force=False)
    reparsed = updater._reparsed_file_keys
    assert "plain.py" in reparsed, sorted(reparsed)
    assert "other.py" not in reparsed, sorted(reparsed)


@pytest.mark.parametrize(
    ("language", "files", "edited", "text", "edge"),
    [
        pytest.param(
            cs.SupportedLanguage.JS,
            _JS_FILES,
            "util.js",
            "export function log(msg, level) {\n  console.log(msg);\n}\n",
            ("proj.registry.runCallback", "CALLS", "proj.plugins.onStart"),
            id="js-arrow",
        ),
        pytest.param(
            cs.SupportedLanguage.CPP,
            _CPP_FILES,
            "util.h",
            "#pragma once\nvoid log_msg(const char* msg, int level);\n",
            ("proj.registry.run_callback", "CALLS", "proj.plugins.on_start"),
            id="cpp-definition",
        ),
    ],
)
def test_a_receiver_named_outside_a_name_field_brings_in_its_callers(
    temp_repo: Path,
    language: cs.SupportedLanguage,
    files: dict[str, str],
    edited: str,
    text: str,
    edge: tuple[str, str, str],
) -> None:
    root = temp_repo / "proj"
    _write(root, files)
    store = _StatefulIngestor()
    _index(store, root, force=True, language=language)
    clean = _edges(store)
    assert edge in clean, "fixture must produce the callable-argument edge"

    _rewrite(root / edited, root, text)
    _index(store, root, force=False, language=language)
    after = _edges(store)

    assert edge in after, sorted(clean - after)
