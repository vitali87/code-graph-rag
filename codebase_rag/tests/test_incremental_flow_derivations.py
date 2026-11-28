# Every way the call pass derives a callable-flow edge, or the binding one is
# written from, synced against a fresh index (issue #3781).
#
# A flow edge `run_callback -> handler` starts in the file defining the
# function that invokes its parameter, but is written from records other
# files' walks leave: the call binding the callback, each function
# forwarding it, each factory returning the closure that runs it. The re-parse
# set must hold every file whose record an edge needs whenever the file the
# edge starts in re-parses, and the file the edge starts in whenever a record
# moves. `DERIVATIONS` takes each derivation in the first direction: the edit
# (a utility the edge's source calls changing its signature) re-parses the
# source as a dependent, so only the closure's rules bring the rest in.
# `CALL_SITE_EDITS` takes the second: a call site's edit, which nothing
# depends on, moves the edge.
#
# | derivation | records the edge needs | re-parse rule |
# |---|---|---|
# | parameter invocation, keyword argument, method reference, assignment alias, module-level call, class method, closure invocation, JS bound method and arrow argument (named by its position) | the call site's argument binding | callers of an invoker (`flow_caller_paths`) |
# | forwarding: positional, keyword, through an import alias or a module, in Python, JS/TS, Go and C++ | each forwarder's pass-through | callers of a forwarder |
# | factory return, factory alias, factory chain | each factory's `return make()`, and the alias call | callers of a producer |
# | returned invoker (`return run_callback`) | the getter's return | callers of an invoker: the getter holds a REFERENCES edge to it |
# | closure call (`make()(cb)`, written in the caller's file) | the factories' returns | the factories a re-parsed file's closure calls name (`FlowCallees`) |
# | a call site gaining, changing or losing a callback, directly, through a forwarder or a factory, or by its file's deletion | the edge starts at the receiver | the receivers a new or deleted file, or an edited one whose interface fingerprint moved, hands a function to now (found by name, by the slot it is handed at) or did (by its old edges); an edited file in a language with no fingerprint (Go, C++) has no rule, as nothing says its calls moved (`test_a_go_call_site_edit_keeps_its_receivers_edges`) |
# | a local handed on (`b = h; run(b)`) bound anew | the same | the receivers an edited file hands a local it assigns, even when its fingerprint held: the fingerprint keeps the argument's text only |
# | decorator, `functools.partial`, higher-order builtin | none: the edge starts at the call site, from its own walk | dependents |
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_file
from codebase_rag.tests.test_incremental_interface_skip import (
    PROJECT,
    _clean,
    _diff,
    _materialise,
    _snapshot,
    _updater,
)
from codebase_rag.tests.test_incremental_reparse_scope import (
    _late_flow_sync,
    treesitter_go,  # noqa: F401 -- the fixture, by name
)
from evals.cgr_graph import _StatefulIngestor

_LOG = "def log(msg):\n    print(msg)\n"
_JS_LOG = "export function log(msg) {\n  console.log(msg);\n}\n"
_EDIT = {"util.py": _LOG.replace("(msg)", "(msg, level=0)", 1)}
_JS_EDIT = {"util.js": _JS_LOG.replace("(msg)", "(msg, level)", 1)}
_INVOKER = (
    "from util import log\n\n\n"
    "def run_callback(fn):\n"
    '    log("running")\n'
    "    return fn()\n\n\n"
    "def describe():\n"
    '    return "registry"\n'
)
_READER = "from registry import describe\n\n\ndef show():\n    return describe()\n"


def _passer(call: str, preamble: str = "") -> dict[str, str]:
    return {
        "util.py": _LOG,
        "registry.py": _INVOKER,
        "reader.py": _READER,
        "entry.py": (
            f"from registry import run_callback\n{preamble}\n\n"
            "def handler():\n"
            '    return "handled"\n\n\n'
            f"def main():\n    {call}\n"
        ),
    }


def _forwarding(forward: str, header: str = "from registry import run_callback"):
    return {
        "util.py": _LOG,
        "registry.py": _INVOKER,
        "reader.py": _READER,
        "wrap.py": f"{header}\n\n\ndef wrap(cb):\n    {forward}\n",
        "entry.py": (
            "from wrap import wrap\n\n\n"
            "def handler():\n"
            '    return "handled"\n\n\n'
            "def main():\n    return wrap(handler)\n"
        ),
    }


_MAKER = (
    "from util import log\n\n\n"
    "def make():\n"
    '    log("making")\n\n'
    "    def invoke(cb):\n"
    "        return cb()\n\n"
    "    return invoke\n\n\n"
    "def describe():\n"
    '    return "registry"\n'
)


def _factory(call: str) -> dict[str, str]:
    return {
        "util.py": _LOG,
        "registry.py": _MAKER,
        "reader.py": _READER,
        "facade.py": "from registry import make\n\n\ndef factory():\n    return make()\n",
        "entry.py": (
            "from facade import factory\n\n\n"
            "def handler():\n"
            '    return "handled"\n\n\n'
            f"def main():\n    {call}\n"
        ),
    }


_HANDLER = f"{PROJECT}.entry.handler"
_RUN = f"{PROJECT}.registry.run_callback"
_INVOKE = f"{PROJECT}.registry.make.invoke"

# id: (files, edit, the flow edge a fresh index holds, files the sync must
# re-parse beside the edited one)
DERIVATIONS: dict[
    str, tuple[dict[str, str], dict[str, str], tuple[str, str], set[str]]
] = {
    "parameter-invocation": (
        _passer("return run_callback(handler)"),
        _EDIT,
        (_RUN, _HANDLER),
        {"registry.py", "entry.py"},
    ),
    "keyword-argument": (
        _passer("return run_callback(fn=handler)"),
        _EDIT,
        (_RUN, _HANDLER),
        {"registry.py", "entry.py"},
    ),
    "method-reference": (
        _passer(
            "obj = Holder()\n    return run_callback(obj.meth)",
            "\n\nclass Holder:\n    def meth(self):\n        return 1\n",
        ),
        _EDIT,
        (_RUN, f"{PROJECT}.entry.Holder.meth"),
        {"registry.py", "entry.py"},
    ),
    "assignment-alias": (
        _passer("b = handler\n    return run_callback(b)"),
        _EDIT,
        (_RUN, _HANDLER),
        {"registry.py", "entry.py"},
    ),
    "module-level-call": (
        {
            **_passer("return 0"),
            "entry.py": (
                "from registry import run_callback\n\n\n"
                "def handler():\n"
                '    return "handled"\n\n\n'
                "run_callback(handler)\n"
            ),
        },
        _EDIT,
        (_RUN, _HANDLER),
        {"registry.py", "entry.py"},
    ),
    "closure-invocation": (
        {
            **_passer("return run_callback(handler)"),
            "registry.py": _INVOKER.replace(
                "    return fn()\n",
                "    def later():\n        return fn()\n\n    return later()\n",
            ),
        },
        _EDIT,
        (_RUN, _HANDLER),
        {"registry.py", "entry.py"},
    ),
    "class-method": (
        {
            **_passer("return Registry().run(handler)"),
            "registry.py": (
                "from util import log\n\n\n"
                "class Registry:\n"
                "    def run(self, fn):\n"
                '        log("running")\n'
                "        return fn()\n\n\n"
                "def describe():\n"
                '    return "registry"\n'
            ),
            "entry.py": (
                "from registry import Registry\n\n\n"
                "def handler():\n"
                '    return "handled"\n\n\n'
                "def main():\n    return Registry().run(handler)\n"
            ),
        },
        _EDIT,
        (f"{PROJECT}.registry.Registry.run", _HANDLER),
        {"registry.py", "entry.py"},
    ),
    "forwarding": (
        _forwarding("return run_callback(cb)"),
        _EDIT,
        (_RUN, _HANDLER),
        {"registry.py", "wrap.py", "entry.py"},
    ),
    "keyword-forwarding": (
        _forwarding("return run_callback(fn=cb)"),
        _EDIT,
        (_RUN, _HANDLER),
        {"registry.py", "wrap.py", "entry.py"},
    ),
    "aliased-forwarding": (
        _forwarding("return rc(cb)", "from registry import run_callback as rc"),
        _EDIT,
        (_RUN, _HANDLER),
        {"registry.py", "wrap.py", "entry.py"},
    ),
    "module-forwarding": (
        _forwarding("return registry.run_callback(cb)", "import registry"),
        _EDIT,
        (_RUN, _HANDLER),
        {"registry.py", "wrap.py", "entry.py"},
    ),
    "factory-return": (
        _factory("return factory()(handler)"),
        _EDIT,
        (_INVOKE, _HANDLER),
        {"registry.py", "facade.py", "entry.py"},
    ),
    "factory-alias": (
        _factory("f = factory()\n    return f(handler)"),
        _EDIT,
        (_INVOKE, _HANDLER),
        {"registry.py", "facade.py", "entry.py"},
    ),
    "factory-chain": (
        {
            **_factory("return outer()(handler)"),
            "outer.py": "import facade\n\n\ndef outer():\n    return facade.factory()\n",
            "entry.py": (
                "from outer import outer\n\n\n"
                "def handler():\n"
                '    return "handled"\n\n\n'
                "def main():\n    return outer()(handler)\n"
            ),
        },
        _EDIT,
        (_INVOKE, _HANDLER),
        {"registry.py", "facade.py", "outer.py", "entry.py"},
    ),
    "returned-invoker": (
        {
            **_passer("return 0"),
            "getter.py": (
                "from registry import run_callback\n\n\n"
                "def get():\n    return run_callback\n"
            ),
            "entry.py": (
                "from getter import get\n\n\n"
                "def handler():\n"
                '    return "handled"\n\n\n'
                "def main():\n    return get()(handler)\n"
            ),
        },
        _EDIT,
        (_RUN, _HANDLER),
        {"registry.py", "getter.py", "entry.py"},
    ),
    "decorator": (
        {
            **_passer("return 0"),
            "registry.py": _INVOKER
            + "\n\ndef deco(fn):\n    def wrapper():\n        return fn()\n\n"
            "    return wrapper\n",
            "entry.py": (
                "from registry import deco\n\n\n"
                "@deco\ndef handler():\n"
                '    return "handled"\n'
            ),
        },
        _EDIT,
        (f"{PROJECT}.entry", f"{PROJECT}.registry.deco"),
        {"registry.py"},
    ),
    "partial": (
        _passer(
            "return functools.partial(run_callback, handler)()",
            "import functools",
        ),
        _EDIT,
        (f"{PROJECT}.entry.main", _HANDLER),
        {"registry.py", "entry.py"},
    ),
    "js-invoker": (
        {
            "util.js": _JS_LOG,
            "registry.js": (
                "import { log } from './util';\n\n"
                "export function runCallback(fn) {\n  log('running');\n  return fn();\n}\n"
            ),
            "entry.js": (
                "import { runCallback } from './registry';\n\n"
                "function handler() {\n  return 1;\n}\n\n"
                "export function main() {\n  return runCallback(handler.bind(null));\n}\n"
            ),
        },
        _JS_EDIT,
        (f"{PROJECT}.registry.runCallback", f"{PROJECT}.entry.handler"),
        {"registry.js", "entry.js"},
    ),
    "js-arrow-argument": (
        {
            "util.js": _JS_LOG,
            "registry.js": (
                "import { log } from './util';\n\n"
                "export function runCallback(fn) {\n  log('running');\n  return fn();\n}\n"
            ),
            "entry.js": (
                "import { runCallback } from './registry';\n\n"
                "export function main() {\n  return runCallback(() => 1);\n}\n"
            ),
        },
        _JS_EDIT,
        (f"{PROJECT}.registry.runCallback", f"{PROJECT}.entry.main.anonymous_3_21"),
        {"registry.js", "entry.js"},
    ),
    "js-forwarding": (
        {
            "util.js": _JS_LOG,
            "registry.js": (
                "import { log } from './util';\n\n"
                "export function runCallback(fn) {\n  log('running');\n  return fn();\n}\n"
            ),
            "wrap.js": (
                "import { runCallback } from './registry';\n\n"
                "export function wrap(cb) {\n  return runCallback(cb);\n}\n"
            ),
            "entry.js": (
                "import { wrap } from './wrap';\n\n"
                "function handler() {\n  return 1;\n}\n\n"
                "export function main() {\n  return wrap(handler);\n}\n"
            ),
        },
        _JS_EDIT,
        (f"{PROJECT}.registry.runCallback", f"{PROJECT}.entry.handler"),
        {"registry.js", "wrap.js", "entry.js"},
    ),
    "js-factory-alias": (
        {
            "util.js": _JS_LOG,
            "registry.js": (
                "import { log } from './util';\n\n"
                "export function make() {\n  log('making');\n"
                "  function invoke(cb) {\n    return cb();\n  }\n  return invoke;\n}\n"
            ),
            "entry.js": (
                "import { make } from './registry';\n\n"
                "function handler() {\n  return 1;\n}\n\n"
                "export function main() {\n  const f = make();\n  return f(handler);\n}\n"
            ),
        },
        _JS_EDIT,
        (f"{PROJECT}.registry.make.invoke", f"{PROJECT}.entry.handler"),
        {"registry.js", "entry.js"},
    ),
}


def _flow_edges(root: Path) -> set[tuple[str, str]]:
    return {
        (edge[1], edge[4])
        for edge in _clean(root)[1]
        if edge[2] == cs.RelationshipType.CALLS.value
    }


@pytest.mark.parametrize("mode", ["new", "reused", "reingest"])
@pytest.mark.parametrize(
    ("files", "edits", "edge", "reparsed"),
    DERIVATIONS.values(),
    ids=DERIVATIONS.keys(),
)
def test_each_flow_derivation_resyncs_as_a_fresh_index_has_it(
    temp_repo: Path,
    files: dict[str, str],
    edits: dict[str, str],
    edge: tuple[str, str],
    reparsed: set[str],
    mode: str,
) -> None:
    root = temp_repo / PROJECT
    _materialise(root, files)
    assert edge in _flow_edges(root), "the case must hold the flow edge it names"

    got = _late_flow_sync(root, edits, mode)

    assert reparsed <= got, sorted(got)
    # A caller of a function taking no part in callable flow keeps its edges.
    assert not {"reader.py", "reader.js"} & got, sorted(got)


# The other direction: a flow edge starts in the receiver's file, so a call
# site that gains, changes or loses a callback must bring that file in, or
# the receiver keeps the edge it had (or never gets the new one); and a
# closure call `make()(cb)` starts in the caller's file but is followed
# through the factory's walk, so a re-parsed caller brings in the factory.
# None of the receiver's files is a dependent of the call site: it holds no
# edge into it.
_RUN = "def run_callback(fn):\n    return fn()\n"
_CALLBACKS = "def h1():\n    return 1\n\n\ndef h2():\n    return 2\n"
_PASS_H1 = (
    "import cbs\nfrom registry import run_callback\n\n\n"
    "def main():\n    return run_callback(cbs.h1)\n"
)
_NO_PASS = "from registry import run_callback\n\n\ndef handler():\n    return 1\n"
_MAKE = "def make():\n    def invoke(cb):\n        return cb()\n\n    return invoke\n"
_ALIAS_CALLER = (
    "from util import log\nfrom registry import make\n\n\n"
    "def handler():\n    return 1\n\n\n"
    "def main():\n    log(1)\n    return make()(handler)\n"
)
_WRAP = "from registry import run_callback\n\n\ndef wrap(cb):\n    return run_callback(cb)\n"
_JS_RUN = "export function runCallback(fn) {\n  return fn();\n}\n"
_JS_NO_PASS = (
    "import { runCallback } from './registry';\n\n"
    "export function handler() {\n  return 1;\n}\n"
)

# id: (files before, files after; None deletes)
CALL_SITE_EDITS: dict[str, tuple[dict[str, str], dict[str, str | None]]] = {
    "gains-a-callback": (
        {"registry.py": _RUN, "entry.py": _NO_PASS},
        {"entry.py": _NO_PASS + "\n\ndef main():\n    return run_callback(handler)\n"},
    ),
    "changes-its-callback": (
        {"registry.py": _RUN, "cbs.py": _CALLBACKS, "entry.py": _PASS_H1},
        {"entry.py": _PASS_H1.replace("h1", "h2")},
    ),
    "drops-its-callback": (
        {"registry.py": _RUN, "cbs.py": _CALLBACKS, "entry.py": _PASS_H1},
        {"entry.py": _PASS_H1.replace("run_callback(cbs.h1)", "None")},
    ),
    # The call's text holds: only the local it hands on is bound anew, which
    # the interface fingerprint does not keep.
    "changes-the-callback-a-local-holds": (
        {
            "registry.py": _RUN,
            "cbs.py": _CALLBACKS,
            "entry.py": _PASS_H1.replace(
                "    return run_callback(cbs.h1)",
                "    b = cbs.h1\n    return run_callback(b)",
            ),
        },
        {
            "entry.py": _PASS_H1.replace(
                "    return run_callback(cbs.h1)",
                "    b = cbs.h2\n    return run_callback(b)",
            )
        },
    ),
    "is-deleted": (
        {"registry.py": _RUN, "cbs.py": _CALLBACKS, "entry.py": _PASS_H1},
        {"entry.py": None},
    ),
    "gains-a-forwarded-callback": (
        {"registry.py": _RUN, "wrap.py": _WRAP, "entry.py": _NO_PASS},
        {
            "entry.py": "from wrap import wrap\n"
            + _NO_PASS
            + "\n\ndef main():\n    return wrap(handler)\n"
        },
    ),
    "gains-a-factory-callback": (
        {"registry.py": _MAKE, "entry.py": _NO_PASS},
        {
            "entry.py": "from registry import make\n"
            + _NO_PASS
            + "\n\ndef main():\n    return make()(handler)\n"
        },
    ),
    "re-parses-its-closure-call": (
        {"util.py": _LOG, "registry.py": _MAKE, "entry.py": _ALIAS_CALLER},
        _EDIT,
    ),
    "re-parses-its-closure-call-through-a-factory": (
        {
            "util.py": _LOG,
            "registry.py": _MAKE,
            "facade.py": "from registry import make\n\n\ndef factory():\n    return make()\n",
            "entry.py": _ALIAS_CALLER.replace(
                "from registry import make", "from facade import factory"
            ).replace("make()(handler)", "factory()(handler)"),
        },
        _EDIT,
    ),
    "js-gains-a-callback": (
        {"registry.js": _JS_RUN, "entry.js": _JS_NO_PASS},
        {
            "entry.js": _JS_NO_PASS
            + "\nexport function main() {\n  return runCallback(handler);\n}\n"
        },
    ),
}


def _apply_files(
    root: Path, before: dict[str, str], after: dict[str, str]
) -> list[str]:
    """Write `after` over the tree holding `before`, dated past the hash
    cache as a later save would be; the files it touched."""
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    touched: list[str] = []
    for rel in sorted({*before, *after}):
        path = root / rel
        if rel not in after:
            path.unlink()
        elif before.get(rel) != after[rel]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(after[rel], encoding="utf-8")
            os.utime(path, (cache_mtime + 1, cache_mtime + 1))
        else:
            continue
        touched.append(rel)
    os.utime(root, (cache_mtime + 1, cache_mtime + 1))
    return touched


def _resync(
    base: Path, before: dict[str, str], after: dict[str, str | None], mode: str
) -> None:
    root = base / PROJECT
    _materialise(root, before)
    store = _StatefulIngestor()
    indexer = _updater(store, root)
    indexer.run(force=True)
    edited = {**before, **after}
    touched = _apply_files(
        root, before, {rel: text for rel, text in edited.items() if text is not None}
    )
    match mode:
        case "reingest":
            _updater(store, root).reingest([root / rel for rel in touched])
        case "reused":
            indexer.run()
        case _:
            _updater(store, root).run()
    synced, clean = _snapshot(store), _clean(root)
    assert synced == clean, _diff(synced, clean)


@pytest.mark.parametrize("mode", ["new", "reused", "reingest"])
@pytest.mark.parametrize(
    ("before", "after"), CALL_SITE_EDITS.values(), ids=CALL_SITE_EDITS.keys()
)
def test_a_call_site_brings_in_the_receiver_its_flow_edge_starts_in(
    temp_repo: Path,
    before: dict[str, str],
    after: dict[str, str | None],
    mode: str,
) -> None:
    _resync(temp_repo, before, after, mode)


# Go forwards a func value as Python does: wrap.Wrap hands its parameter to
# registry.RunCallback, which calls it, and the call pass binds the
# callback entry.go passes through the pass-through.
_GO_WRAP = {
    "go.mod": "module example.com/m\n\ngo 1.21\n",
    "util/util.go": "package util\n\nfunc Log(msg string) {}\n",
    "registry/registry.go": (
        'package registry\n\nimport "example.com/m/util"\n\n'
        "func RunCallback(fn func() int) int {\n"
        '\tutil.Log("running")\n'
        "\treturn fn()\n}\n"
    ),
    "wrap/wrap.go": (
        'package wrap\n\nimport "example.com/m/registry"\n\n'
        "func Wrap(cb func() int) int {\n\treturn registry.RunCallback(cb)\n}\n"
    ),
    "entry/entry.go": (
        'package entry\n\nimport "example.com/m/wrap"\n\n'
        "func handler() int {\n\treturn 1\n}\n\n"
        "func Main() int {\n\treturn wrap.Wrap(handler)\n}\n"
    ),
}


@pytest.mark.usefixtures("treesitter_go")
@pytest.mark.parametrize("mode", ["new", "reused", "reingest"])
def test_a_go_forwarder_brings_in_its_callers(temp_repo: Path, mode: str) -> None:
    root = temp_repo / PROJECT
    _materialise(root, _GO_WRAP)
    assert any(target.endswith(".handler") for _source, target in _flow_edges(root))

    got = _late_flow_sync(
        root,
        {"util/util.go": "package util\n\nfunc Log(msg string, level int) {}\n"},
        mode,
    )

    assert {"wrap/wrap.go", "entry/entry.go"} <= got, sorted(got)


# Go has no interface fingerprint, so nothing tells a comment from a call
# that hands a callback anew, and taking every Go edit as the second
# re-parsed half of golang/tools for one comment. An edited Go call site
# leaves its receivers' edges as they were; a new or deleted Go file is
# followed as any other.
_GO_RUN = {
    "go.mod": _GO_WRAP["go.mod"],
    "registry/registry.go": (
        "package registry\n\nfunc RunCallback(fn func() int) int {\n\treturn fn()\n}\n"
    ),
    "entry/entry.go": (
        'package entry\n\nimport "example.com/m/registry"\n\n'
        "func handler() int {\n\treturn 1\n}\n\n"
        "func Main() int {\n\treturn registry.RunCallback(nil)\n}\n"
    ),
}


@pytest.mark.usefixtures("treesitter_go")
@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(mode, marks=pytest.mark.xfail(strict=True, reason="no Go rule"))
        for mode in ("new", "reingest")
    ],
)
def test_a_go_call_site_edit_keeps_its_receivers_edges(
    temp_repo: Path, mode: str
) -> None:
    after = _GO_RUN["entry/entry.go"].replace(
        "RunCallback(nil)", "RunCallback(handler)"
    )
    _resync(temp_repo, _GO_RUN, {"entry/entry.go": after}, mode)


@pytest.mark.usefixtures("treesitter_go")
@pytest.mark.parametrize("mode", ["new", "reused", "reingest"])
def test_a_new_go_call_site_brings_in_its_receiver(temp_repo: Path, mode: str) -> None:
    caller = _GO_RUN["entry/entry.go"].replace(
        "RunCallback(nil)", "RunCallback(handler)"
    )
    _resync(
        temp_repo,
        {key: text for key, text in _GO_RUN.items() if key != "entry/entry.go"},
        {"entry/entry.go": caller},
        mode,
    )
