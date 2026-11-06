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

# The wrapper calls the receiver under an import alias.
_ALIAS_WRAP = (
    "from registry import run_callback as invoke\n\n\n"
    "def alias_wrap(cb):\n"
    "    return invoke(cb)\n"
)
_ALIAS_USER = (
    "from alias_wrap import alias_wrap\n\n\n"
    "def _aliased():\n"
    '    return "aliased"\n\n\n'
    "def run():\n"
    "    return alias_wrap(_aliased)\n"
)

_FILES = {
    "util.py": _UTIL,
    "alias_wrap.py": _ALIAS_WRAP,
    "alias_user.py": _ALIAS_USER,
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
_ALIASED = ("proj.registry.run_callback", "CALLS", "proj.alias_user._aliased")


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
    assert _ALIASED in clean, "fixture must produce the aliased wrapper's edge"
    assert _instantiates_tag(clean), "fixture must produce the register edge"

    _widen_log(root)
    _index(store, root, force=False)
    after = _edges(store)

    assert _CALLBACK in after, sorted(clean - after)
    assert _FORWARDED in after, sorted(clean - after)
    assert _EARLY in after, sorted(clean - after)
    assert _ALIASED in after, sorted(clean - after)
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


def test_reingest_walks_the_passers_of_a_reparsed_receiver(temp_repo: Path) -> None:
    # The watcher's path: a fresh updater holds no callable-flow records of
    # the files it does not walk, so the receiver's edges come back only
    # from re-walking its passers.
    root = temp_repo / "proj"
    _write(root)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    clean = _edges(store)

    _widen_log(root)
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).reingest([root / "util.py"])
    after = _edges(store)

    assert _CALLBACK in after, sorted(clean - after)
    assert _FORWARDED in after, sorted(clean - after)
    assert after == clean, (sorted(clean - after), sorted(after - clean))


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


# A passer the callable flow brings in can hold a class whose member another
# file defines out of line: re-parsing plugins.h deletes its subtree, and only
# plugins.cpp re-creates Plugin::id where a clean index puts it (issue #3271).
_CPP_MEMBER_FILES = {
    **{key: text for key, text in _CPP_FILES.items() if key != "plugins.cpp"},
    "plugins.h": (
        '#pragma once\n#include "registry.h"\n'
        "static int on_start() { return 1; }\n"
        "class Plugin {\n public:\n  int id() const;\n"
        "  int start() const { return run_callback(on_start); }\n};\n"
    ),
    "plugins.cpp": '#include "plugins.h"\nint Plugin::id() const { return 7; }\n',
}
_PLUGIN_ID = (cs.NodeLabel.METHOD.value, "proj.plugins.h.Plugin.id")


@pytest.mark.parametrize("scoped", [False, True], ids=["run", "reingest"])
def test_a_passer_keeps_the_members_another_file_defines(
    temp_repo: Path, scoped: bool
) -> None:
    root = temp_repo / "proj"
    _write(root, _CPP_MEMBER_FILES)
    store = _StatefulIngestor()
    _index(store, root, force=True, language=cs.SupportedLanguage.CPP)
    clean = _edges(store)
    member = store.nodes[_PLUGIN_ID]
    assert (
        "proj.registry.run_callback",
        "CALLS",
        "proj.plugins.h.on_start",
    ) in clean, "fixture must produce the callable-argument edge"
    assert member.get(cs.KEY_PATH) == "plugins.cpp", member

    _rewrite(
        root / "util.h",
        root,
        "#pragma once\nvoid log_msg(const char* msg, int level);\n",
    )
    if scoped:
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        ).reingest([root / "util.h"])
    else:
        _index(store, root, force=False, language=cs.SupportedLanguage.CPP)

    after = store.nodes[_PLUGIN_ID]
    assert after.get(cs.KEY_PATH) == "plugins.cpp", after
    assert after.get(cs.KEY_START_LINE) == member.get(cs.KEY_START_LINE)
    assert _edges(store) == clean


# The receiver lives in a member file: registry.h re-parses as util.h's
# dependent but holds only declarations, and the member lookup brings in
# registry.cpp for its out-of-line Registry::size. registry.cpp also defines
# the receiver and does not include util.h, so its passer, plugins.cpp, is
# no dependent of this run.
_CPP_MEMBER_RECEIVER_FILES = {
    "util.h": _CPP_FILES["util.h"],
    "util.cpp": _CPP_FILES["util.cpp"],
    "registry.h": (
        '#pragma once\n#include "util.h"\n'
        "int run_callback(int (*fn)());\n"
        "class Registry {\n public:\n  int size() const;\n};\n"
    ),
    "registry.cpp": (
        '#include "registry.h"\n'
        "int Registry::size() const { return 0; }\n"
        "int run_callback(int (*fn)()) { return fn(); }\n"
    ),
    "plugins.cpp": _CPP_FILES["plugins.cpp"],
}
_RUN_ON_START = ("proj.registry.run_callback", "CALLS", "proj.plugins.on_start")


@pytest.mark.parametrize("scoped", [False, True], ids=["run", "reingest"])
def test_a_receiver_in_a_member_file_brings_in_its_callers(
    temp_repo: Path, scoped: bool
) -> None:
    root = temp_repo / "proj"
    _write(root, _CPP_MEMBER_RECEIVER_FILES)
    store = _StatefulIngestor()
    _index(store, root, force=True, language=cs.SupportedLanguage.CPP)
    clean = _edges(store)
    assert _RUN_ON_START in clean, "fixture must produce the callable-argument edge"

    _rewrite(
        root / "util.h",
        root,
        "#pragma once\nvoid log_msg(const char* msg, int level);\n",
    )
    if scoped:
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        ).reingest([root / "util.h"])
    else:
        _index(store, root, force=False, language=cs.SupportedLanguage.CPP)
    after = _edges(store)

    assert _RUN_ON_START in after, sorted(clean - after)
    assert after == clean, (sorted(clean - after), sorted(after - clean))


# The Go shape: app/app.go joins as the passer of reg.Run, and app/each.go
# joins only as its package file, after the callable flow was walked. each.go
# holds a receiver of its own, whose passer, cli/cli.go, depends on nothing
# this run re-parses for any other reason.
_GO_FILES = {
    "go.mod": "module example.com/flow\n\ngo 1.21\n",
    "util/util.go": "package util\n\nfunc Log(msg string) {}\n",
    "reg/reg.go": (
        'package reg\n\nimport "example.com/flow/util"\n\n'
        'func Run(fn func() int) int {\n\tutil.Log("running")\n\treturn fn()\n}\n'
    ),
    "app/app.go": (
        'package app\n\nimport "example.com/flow/reg"\n\n'
        "func onStart() int { return 1 }\n\n"
        "func Main() int { return reg.Run(onStart) }\n"
    ),
    "app/each.go": "package app\n\nfunc Each(fn func() int) int { return fn() }\n",
    "cli/cli.go": (
        'package cli\n\nimport "example.com/flow/app"\n\n'
        "func hook() int { return 2 }\n\n"
        "func Go() int { return app.Each(hook) }\n"
    ),
}
_EACH_HOOK = ("proj.app.each.Each", "CALLS", "proj.cli.cli.hook")


@pytest.mark.parametrize("scoped", [False, True], ids=["run", "reingest"])
def test_a_receiver_in_a_passers_package_brings_in_its_callers(
    temp_repo: Path, scoped: bool
) -> None:
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in _GO_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True, language=cs.SupportedLanguage.GO)
    clean = _edges(store)
    assert _EACH_HOOK in clean, "fixture must produce the callable-argument edge"

    _rewrite(
        root / "util" / "util.go",
        root,
        "package util\n\nfunc Log(msg string, level int) {}\n",
    )
    if scoped:
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        ).reingest([root / "util" / "util.go"])
    else:
        _index(store, root, force=False, language=cs.SupportedLanguage.GO)
    after = _edges(store)

    assert _EACH_HOOK in after, sorted(clean - after)
    assert after == clean, (sorted(clean - after), sorted(after - clean))
