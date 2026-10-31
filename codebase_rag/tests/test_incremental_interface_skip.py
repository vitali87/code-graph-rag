# A sync re-parsed every file that depends on an edited one, so a comment
# added to a central file cost a re-parse of all its callers (issue #2531:
# 112 files and ~65 s for one line in gson's Gson.java, 418 files for
# django's query.py). When nothing a caller resolves against changed, the
# callers keep their edges instead. The graph must still equal a clean
# index of the edited tree, so every edit here is checked against one, and
# the edits that DO change what callers see must still re-parse them.
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyValue, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "proj"

PY_CORE = """class Base:
    def run(self) -> int:
        return 1


class Widget(Base):
    def size(self) -> int:
        return 2


class Gadget(Base):
    def size(self) -> int:
        return 3

    def spin(self) -> int:
        return 4


def make() -> Widget:
    return Widget()


def build():
    return Widget()


def helper(x: int) -> int:
    y = x + 1
    return y + 1


def log_event(name):
    message = "event " + name
    print(message)


__all__ = ["Base", "Widget", "Gadget", "make", "build", "helper", "log_event"]
"""

PYTHON: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/core.py": PY_CORE,
    "pkg/a.py": (
        "from pkg.core import helper, log_event\n\n\n"
        "def use_a():\n"
        '    log_event("a")\n'
        "    return helper(1)\n"
    ),
    "pkg/b.py": (
        "from pkg.core import build, make\n\n\n"
        "def use_make():\n"
        "    return make().size()\n\n\n"
        "def use_run():\n"
        "    return make().run()\n\n\n"
        "def use_build():\n"
        "    return build().size()\n"
    ),
    "pkg/c.py": (
        "from pkg.core import Base\n\n\n"
        "class Impl(Base):\n"
        "    def run(self) -> int:\n"
        "        return 3\n"
    ),
    "pkg/star.py": (
        "from pkg.core import *\n\n\ndef use_star():\n    return helper(2)\n"
    ),
    "main.py": ("from pkg.a import use_a\n\n\ndef main():\n    return use_a()\n"),
}
PY_TARGET = "pkg/core.py"
PY_CALLERS = {"pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/star.py"}

JAVA_FACTORY = """package com.acme;

public class Factory {
    public static Shape make() {
        return new Circle();
    }

    public static int twice(int x) {
        int y = x * 2;
        return y;
    }

    public static void log(String message) {
        System.out.println(message);
    }
}
"""

JAVA: dict[str, str] = {
    "src/com/acme/Shape.java": (
        "package com.acme;\n\npublic class Shape {\n"
        "    public int area() { return 0; }\n"
        "    public int perimeter() { return 1; }\n}\n"
    ),
    "src/com/acme/Circle.java": (
        "package com.acme;\n\npublic class Circle extends Shape {\n"
        "    public int area() { return 3; }\n}\n"
    ),
    "src/com/acme/Square.java": (
        "package com.acme;\n\npublic class Square {\n"
        "    public int area() { return 4; }\n"
        "    public int perimeter() { return 5; }\n}\n"
    ),
    "src/com/acme/Factory.java": JAVA_FACTORY,
    "src/com/acme/App.java": (
        "package com.acme;\n\npublic class App {\n"
        "    public int run() {\n"
        '        Factory.log("run");\n'
        "        return Factory.make().area() + Factory.twice(2);\n"
        "    }\n}\n"
    ),
    "src/com/acme/Report.java": (
        "package com.acme;\n\npublic class Report {\n"
        "    public int total() { return Factory.twice(3); }\n}\n"
    ),
    "src/com/acme/Wheel.java": (
        "package com.acme;\n\npublic class Wheel extends Circle {\n"
        "    public int perimeter() { return 6; }\n}\n"
    ),
}
JAVA_TARGET = "src/com/acme/Factory.java"
JAVA_CALLERS = {"src/com/acme/App.java", "src/com/acme/Report.java"}

TS_UTIL = """export function helper(x: number): number {
  const y = x + 1;
  return y;
}

export function logEvent(name: string): void {
  const message = "event " + name;
  console.log(message);
}
"""

TYPESCRIPT: dict[str, str] = {
    "src/util.ts": TS_UTIL,
    "src/app.ts": (
        "import { helper, logEvent } from './util';\n\n"
        "export function run(): number {\n"
        "  logEvent('run');\n"
        "  return helper(1);\n}\n"
    ),
    "src/other.ts": (
        "import { helper } from './util';\n\n"
        "export function twice(): number {\n  return helper(2) * 2;\n}\n"
    ),
}
TS_TARGET = "src/util.ts"
TS_CALLERS = {"src/app.ts", "src/other.ts"}

Snapshot = tuple[frozenset[tuple[str, ...]], frozenset[tuple[str, ...]]]
Edit = Callable[[str], str]


def _freeze(value: PropertyValue) -> str:
    if isinstance(value, list | tuple):
        return repr(sorted(str(item) for item in value))
    return repr(value)


def _snapshot(store: _StatefulIngestor) -> Snapshot:
    """Every node with its properties and every edge with its site and
    properties (the CALLS `resolution` label among them), so a kept edge
    that a clean index would have bound differently cannot hide."""
    nodes = frozenset(
        (str(label), str(uid))
        + tuple(
            sorted(
                f"{key}={_freeze(value)}"
                for key, value in (props or {}).items()
                if key != cs.KEY_LAST_SYNCED_AT
            )
        )
        for (label, uid), props in store.nodes.items()
    )
    edges = frozenset(
        (str(fl), str(fv), str(rel), str(tl), str(tv), repr(site))
        + tuple(sorted(f"{k}={_freeze(v)}" for k, v in store.props_for(e).items()))
        for e in store.keyed_edges
        for (fl, fv, rel, tl, tv, site) in [e]
    )
    return nodes, edges


def _diff(actual: Snapshot, expected: Snapshot) -> str:
    lines = []
    for kind, got, want in (
        ("node", actual[0], expected[0]),
        ("edge", actual[1], expected[1]),
    ):
        lines.extend(f"extra {kind}: {item}" for item in sorted(got - want))
        lines.extend(f"missing {kind}: {item}" for item in sorted(want - got))
    return "\n".join(lines)


def _updater(
    store: _StatefulIngestor, root: Path, capture: CaptureSelection | None = None
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
        capture=capture,
    )


def _materialise(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _edit_after_cache(root: Path, edits: dict[str, str]) -> None:
    # The incremental pass trusts an mtime at or below the hash cache's, so
    # the edited files are dated past it, as a later save would be.
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    for rel, text in edits.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        for touched in (path, path.parent):
            os.utime(touched, (cache_mtime + 1, cache_mtime + 1))


def _clean(root: Path, capture: CaptureSelection | None = None) -> Snapshot:
    store = _StatefulIngestor()
    _updater(store, root, capture).run(force=True)
    return _snapshot(store)


def _sync(
    root: Path,
    files: dict[str, str],
    edits: dict[str, str],
    capture: CaptureSelection | None = None,
) -> tuple[set[str], Snapshot]:
    """Index `files`, apply `edits`, and sync the way the CLI does (a new
    updater over the populated graph): the files re-parsed, and the graph."""
    _materialise(root, files)
    store = _StatefulIngestor()
    _updater(store, root, capture).run(force=True)
    _edit_after_cache(root, edits)
    updater = _updater(store, root, capture)
    updater.run()
    return set(updater._reparsed_file_keys), _snapshot(store)


def _append(text: str) -> Edit:
    return lambda source: source + text


def _replace(old: str, new: str) -> Edit:
    def apply(source: str) -> str:
        assert old in source, f"fixture lost {old!r}"
        return source.replace(old, new, 1)

    return apply


def _calls(snapshot: Snapshot, caller: str) -> set[str]:
    return {
        edge[4]
        for edge in snapshot[1]
        if edge[1] == caller and edge[2] == cs.RelationshipType.CALLS.value
    }


# Edits that leave everything a caller resolves against as it was: only the
# edited file is re-parsed.
UNSEEN_EDITS: list[tuple[str, dict[str, str], str, Edit, set[str]]] = [
    ("py-trailing-comment", PYTHON, PY_TARGET, _append("# trailing note\n"), set()),
    (
        "py-comment-above-every-definition",
        PYTHON,
        PY_TARGET,
        lambda source: "# header\n# second line\n" + source,
        set(),
    ),
    (
        "py-procedure-body",
        PYTHON,
        PY_TARGET,
        _replace(
            '    message = "event " + name\n    print(message)\n',
            '    message = f"event: {name}"\n    if message:\n'
            "        print(message, flush=True)\n",
        ),
        set(),
    ),
    (
        "py-body-of-a-function-returning-arithmetic",
        PYTHON,
        PY_TARGET,
        _replace(
            "    y = x + 1\n    return y + 1\n",
            '    """Add two."""\n    y = x * 2\n    total = y - x\n    return total + 2 - x\n',
        ),
        set(),
    ),
    ("java-trailing-comment", JAVA, JAVA_TARGET, _append("// trailing note\n"), set()),
    (
        "java-comment-above-every-definition",
        JAVA,
        JAVA_TARGET,
        lambda source: "/* header\n   two lines */\n" + source,
        set(),
    ),
    (
        "java-method-body",
        JAVA,
        JAVA_TARGET,
        _replace(
            "        int y = x * 2;\n        return y;\n",
            "        int y = x << 1;\n        // doubled\n        y += 0;\n"
            "        return y;\n",
        ),
        set(),
    ),
    (
        "ts-trailing-comment",
        TYPESCRIPT,
        TS_TARGET,
        _append("// trailing note\n"),
        set(),
    ),
    (
        "ts-procedure-body",
        TYPESCRIPT,
        TS_TARGET,
        _replace(
            '  const message = "event " + name;\n  console.log(message);\n',
            "  const message = `event: ${name}`;\n  if (message) {\n"
            "    console.log(message);\n  }\n",
        ),
        set(),
    ),
]


@pytest.mark.parametrize(
    ("files", "target", "edit", "callers"),
    [case[1:] for case in UNSEEN_EDITS],
    ids=[case[0] for case in UNSEEN_EDITS],
)
def test_an_edit_callers_cannot_see_reparses_only_the_edited_file(
    temp_repo: Path,
    files: dict[str, str],
    target: str,
    edit: Edit,
    callers: set[str],
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(root, files, {target: edit(files[target])})

    assert reparsed == {target, *callers}
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)


@pytest.mark.parametrize("fresh_updater", [False, True], ids=["warm", "fresh"])
def test_a_scoped_reingest_of_a_comment_edit_reports_no_dependents(
    temp_repo: Path, fresh_updater: bool
) -> None:
    """The watcher's and the MCP tool's path: `reingest` names what it
    re-parsed beside the edited file, and a comment touches nothing."""
    root = temp_repo / PROJECT
    _materialise(root, PYTHON)
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    (root / PY_TARGET).write_text(PY_CORE + "# trailing note\n", encoding="utf-8")
    if fresh_updater:
        updater = _updater(store, root)

    report = updater.reingest([PY_TARGET])

    assert report.reparsed == (PY_TARGET,)
    assert report.affected == ()
    clean = _clean(root)
    assert _snapshot(store) == clean, _diff(_snapshot(store), clean)


PY_MAKE_GADGET = _replace(
    "def make() -> Widget:\n    return Widget()",
    "def make() -> Gadget:\n    return Gadget()",
)


# A warm updater keeps Python return-type inference cached across reingest
# calls, so a changed return type re-binds only through a fresh one until
# issue #2927 is fixed; that case runs fresh alone.
@pytest.mark.parametrize(
    ("fresh_updater", "edit"),
    [
        (False, _replace("def helper(", "def helper2(")),
        (True, _replace("def helper(", "def helper2(")),
        (True, PY_MAKE_GADGET),
    ],
    ids=[
        "renamed-function-warm",
        "renamed-function-fresh",
        "changed-return-type-fresh",
    ],
)
def test_a_scoped_reingest_of_an_interface_change_reparses_the_callers(
    temp_repo: Path, fresh_updater: bool, edit: Edit
) -> None:
    root = temp_repo / PROJECT
    _materialise(root, PYTHON)
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    (root / PY_TARGET).write_text(edit(PY_CORE), encoding="utf-8")
    if fresh_updater:
        updater = _updater(store, root)

    report = updater.reingest([PY_TARGET])

    assert set(report.affected) == PY_CALLERS
    clean = _clean(root)
    assert _snapshot(store) == clean, _diff(_snapshot(store), clean)


def test_a_save_during_a_reingest_is_judged_before_its_callers_keep_edges(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scoped re-ingest judges the interface on the bytes it then parses.
    A save landing between the two (here: `make` starts returning a Gadget)
    is parsed by the next event instead, and that event sees the change: had
    the first call parsed it under a verdict reached on the older bytes, the
    stored fingerprint would already match it and `b.py` would keep binding
    `Widget.size` for good."""
    root = temp_repo / PROJECT
    _materialise(root, PYTHON)
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    target = root / PY_TARGET
    target.write_text(PY_CORE + "# trailing note\n", encoding="utf-8")
    capture = updater._capture_inbound_edges

    def save_then_capture(keys: list[str]) -> list[ResultRow]:
        target.write_text(PY_MAKE_GADGET(PY_CORE), encoding="utf-8")
        return capture(keys)

    monkeypatch.setattr(updater, "_capture_inbound_edges", save_then_capture)
    assert updater.reingest([PY_TARGET]).affected == ()

    # The next event, through a fresh updater (see issue #2927 above).
    report = _updater(store, root).reingest([PY_TARGET])

    assert "pkg/b.py" in report.affected
    clean = _clean(root)
    assert "proj.pkg.core.Gadget.size" in _calls(clean, "proj.pkg.b.use_make")
    assert _snapshot(store) == clean, _diff(_snapshot(store), clean)


PY_REEXPORT_HELPER = "def helper(x: int) -> int:\n    y = x + 1\n    return y + 1\n"

# Edits that change what callers resolve against: every caller re-parses, as
# before, and the graph still equals a clean index.
SEEN_EDITS: list[tuple[str, dict[str, str], str, dict[str, Edit], str | None]] = [
    (
        "py-renamed-function",
        PYTHON,
        PY_TARGET,
        {PY_TARGET: _replace("def helper(", "def helper2(")},
        None,
    ),
    (
        "py-changed-parameter-list",
        PYTHON,
        PY_TARGET,
        {PY_TARGET: _replace("def helper(x: int)", "def helper(x: int, z: int = 0)")},
        None,
    ),
    (
        "py-changed-annotation",
        PYTHON,
        PY_TARGET,
        {PY_TARGET: _replace("def helper(x: int)", "def helper(x: str)")},
        None,
    ),
    (
        "py-changed-return-annotation-of-a-chained-call",
        PYTHON,
        PY_TARGET,
        {PY_TARGET: PY_MAKE_GADGET},
        "proj.pkg.core.Gadget.size",
    ),
    (
        "py-changed-inferred-return-of-a-chained-call",
        PYTHON,
        PY_TARGET,
        {
            PY_TARGET: _replace(
                "def build():\n    return Widget()", "def build():\n    return Gadget()"
            )
        },
        "proj.pkg.core.Gadget.size",
    ),
    (
        "py-added-method",
        PYTHON,
        PY_TARGET,
        {
            PY_TARGET: _replace(
                "    def size(self) -> int:\n        return 2\n",
                "    def size(self) -> int:\n        return 2\n\n"
                "    def run(self) -> int:\n        return 5\n",
            )
        },
        "proj.pkg.core.Widget.run",
    ),
    (
        "py-removed-method",
        PYTHON,
        PY_TARGET,
        {
            PY_TARGET: _replace(
                "class Widget(Base):\n    def size(self) -> int:\n        return 2\n",
                "class Widget(Base):\n    pass\n",
            )
        },
        None,
    ),
    (
        "py-changed-base-class",
        PYTHON,
        PY_TARGET,
        {PY_TARGET: _replace("class Widget(Base):", "class Widget(Gadget):")},
        "proj.pkg.core.Gadget.size",
    ),
    (
        "py-changed-all",
        PYTHON,
        PY_TARGET,
        {PY_TARGET: _replace('"make", "build", "helper",', '"make", "build",')},
        None,
    ),
    (
        "py-moved-definition",
        PYTHON,
        PY_TARGET,
        {
            PY_TARGET: lambda source: (
                "from pkg.helpers import helper\n\n\n"
                + source.replace(PY_REEXPORT_HELPER, "")
            ),
            "pkg/helpers.py": lambda _source: PY_REEXPORT_HELPER,
        },
        "proj.pkg.helpers.helper",
    ),
    (
        "py-new-same-named-definition",
        PYTHON,
        PY_TARGET,
        {
            PY_TARGET: _append(
                "\n\nif __debug__:\n\n    def helper(x: int) -> int:\n"
                "        return x\n"
            )
        },
        None,
    ),
    (
        "py-new-overload",
        PYTHON,
        PY_TARGET,
        {
            PY_TARGET: lambda source: (
                "from typing import overload\n\n\n"
                + source.replace(
                    "def helper(x: int) -> int:\n",
                    "@overload\ndef helper(x: int) -> int: ...\n\n\n"
                    "@overload\ndef helper(x: str) -> str: ...\n\n\n"
                    "def helper(x: int) -> int:\n",
                )
            )
        },
        None,
    ),
    (
        "java-renamed-method",
        JAVA,
        JAVA_TARGET,
        {JAVA_TARGET: lambda source: source.replace("twice(", "doubled(")},
        None,
    ),
    (
        "java-changed-parameter-list",
        JAVA,
        JAVA_TARGET,
        {JAVA_TARGET: _replace("twice(int x)", "twice(int x, int z)")},
        None,
    ),
    (
        "java-changed-return-type-of-a-chained-call",
        JAVA,
        JAVA_TARGET,
        {
            JAVA_TARGET: _replace(
                "public static Shape make()", "public static Square make()"
            )
        },
        "proj.src.com.acme.Square.Square.area()",
    ),
    (
        "java-added-method",
        JAVA,
        JAVA_TARGET,
        {
            JAVA_TARGET: _replace(
                "    public static void log(",
                "    public static int area() { return 9; }\n\n    public static void log(",
            )
        },
        None,
    ),
    (
        "java-removed-method",
        JAVA,
        JAVA_TARGET,
        {
            JAVA_TARGET: _replace(
                "    public static void log(String message) {\n"
                "        System.out.println(message);\n    }\n",
                "",
            )
        },
        None,
    ),
    (
        "java-new-same-named-overload",
        JAVA,
        JAVA_TARGET,
        {
            JAVA_TARGET: _replace(
                "    public static void log(",
                "    public static long twice(long x) { return x; }\n\n"
                "    public static void log(",
            )
        },
        None,
    ),
    (
        "java-changed-base-class",
        JAVA,
        "src/com/acme/Circle.java",
        {"src/com/acme/Circle.java": _replace("extends Shape", "extends Square")},
        "proj.src.com.acme.Square.Square.perimeter()",
    ),
    (
        "ts-renamed-export",
        TYPESCRIPT,
        TS_TARGET,
        {
            TS_TARGET: lambda source: source.replace(
                "function helper(", "function helper2("
            )
        },
        None,
    ),
]


@pytest.mark.parametrize(
    ("files", "target", "edits", "new_callee"),
    [case[1:] for case in SEEN_EDITS],
    ids=[case[0] for case in SEEN_EDITS],
)
def test_an_edit_callers_can_see_still_reparses_them(
    temp_repo: Path,
    files: dict[str, str],
    target: str,
    edits: dict[str, Edit],
    new_callee: str | None,
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root, files, {rel: edit(files.get(rel, "")) for rel, edit in edits.items()}
    )

    dependents = reparsed - set(edits)
    assert dependents, "the callers of an interface change must re-parse"
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)
    if new_callee is not None:
        # The caller binds what the edit made it bind, not what it held.
        assert any(edge[4] == new_callee for edge in clean[1])


def test_a_python_twin_named_by_its_line_reparses_its_callers(
    temp_repo: Path,
) -> None:
    """`f@12` is minted from a position: a comment above it renames it, so a
    caller bound to it cannot keep its edge by name."""
    files = {
        "twins.py": (
            "import sys\n\nif sys.platform == 'win32':\n\n"
            "    def pick():\n        return 1\n\nelse:\n\n"
            "    def pick():\n        return 2\n"
        ),
        "user.py": "from twins import pick\n\n\ndef use():\n    return pick()\n",
    }
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root, files, {"twins.py": "# header\n" + files["twins.py"]}
    )

    assert "user.py" in reparsed
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)


def test_a_caller_that_passes_a_callable_in_is_reparsed(temp_repo: Path) -> None:
    """`run(on_done)` gives `run` a CALLS edge to `on_done`, written from the
    caller's parse. A sync that re-parsed only `runner.py` would lose it, so
    the caller is re-parsed even though nothing it resolves against moved."""
    files = {
        "runner.py": "def run(callback):\n    callback()\n",
        "user.py": (
            "from runner import run\n\n\ndef on_done():\n    return 1\n\n\n"
            "def go():\n    run(on_done)\n"
        ),
    }
    root = temp_repo / PROJECT
    reparsed, synced = _sync(root, files, {"runner.py": files["runner.py"] + "# c\n"})

    assert "user.py" in reparsed
    clean = _clean(root)
    assert "proj.user.on_done" in _calls(clean, "proj.runner.run")
    assert synced == clean, _diff(synced, clean)


def test_an_opt_in_capture_group_keeps_reparsing_every_caller(
    temp_repo: Path,
) -> None:
    """Parameters, fields, data flow and the other opt-in groups bring
    cross-file readers the fingerprint does not model."""
    capture = resolve_capture([cs.CaptureGroup.PARAMETERS.value])
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root,
        PYTHON,
        {PY_TARGET: PY_CORE + "# trailing note\n"},
        capture=capture,
    )

    assert reparsed == {PY_TARGET, *PY_CALLERS}
    clean = _clean(root, capture)
    assert synced == clean, _diff(synced, clean)


# A file whose own source keeps its fingerprint can still change what its
# callers see when something it depends on changes in the same sync: `make()`
# returns whatever `build()` returns, so `factory.py` returning a `B` makes
# `make().method()` in `user.py` bind `B.method` -- through `wrapper.py`,
# which only gained a comment. Its callers are not callers of `factory.py`,
# so nothing but the stable file's own dependency can send them to a re-parse.
CHAIN: dict[str, str] = {
    "models.py": (
        "class A:\n    def method(self) -> int:\n        return 1\n\n\n"
        "class B:\n    def method(self) -> int:\n        return 2\n"
    ),
    "factory.py": "from models import A, B\n\n\ndef build():\n    return A()\n",
    "wrapper.py": "from factory import build\n\n\ndef make():\n    return build()\n",
    "user.py": "from wrapper import make\n\n\ndef use():\n    return make().method()\n",
    "other.py": "def alone():\n    return 3\n",
}
CHAIN_TWO_LEVELS: dict[str, str] = {
    **CHAIN,
    "wrapper2.py": "from wrapper import make\n\n\ndef make2():\n    return make()\n",
    "user2.py": (
        "from wrapper2 import make2\n\n\ndef use2():\n    return make2().method()\n"
    ),
}
FACTORY_RETURNS_B = _replace("return A()", "return B()")


def _comment(source: str) -> str:
    return source + "# note\n"


def test_a_stable_file_whose_dependency_changed_in_the_same_sync_reparses_its_callers(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root,
        CHAIN,
        {
            "factory.py": FACTORY_RETURNS_B(CHAIN["factory.py"]),
            "wrapper.py": _comment(CHAIN["wrapper.py"]),
        },
    )

    assert "user.py" in reparsed
    clean = _clean(root)
    assert "proj.models.B.method" in _calls(clean, "proj.user.use")
    assert synced == clean, _diff(synced, clean)


def test_the_change_reaches_callers_through_a_chain_of_stable_files(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root,
        CHAIN_TWO_LEVELS,
        {
            "factory.py": FACTORY_RETURNS_B(CHAIN_TWO_LEVELS["factory.py"]),
            "wrapper.py": _comment(CHAIN_TWO_LEVELS["wrapper.py"]),
            "wrapper2.py": _comment(CHAIN_TWO_LEVELS["wrapper2.py"]),
        },
    )

    assert {"user.py", "user2.py"} <= reparsed
    clean = _clean(root)
    assert "proj.models.B.method" in _calls(clean, "proj.user2.use2")
    assert synced == clean, _diff(synced, clean)


def test_a_scoped_reingest_of_a_stable_file_and_its_changed_dependency(
    temp_repo: Path,
) -> None:
    """The watcher's path, handed both files of the edit at once. Through a
    fresh updater: a warm one keeps return types cached (issue #2927)."""
    root = temp_repo / PROJECT
    _materialise(root, CHAIN)
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    (root / "factory.py").write_text(
        FACTORY_RETURNS_B(CHAIN["factory.py"]), encoding="utf-8"
    )
    (root / "wrapper.py").write_text(_comment(CHAIN["wrapper.py"]), encoding="utf-8")

    report = _updater(store, root).reingest(["factory.py", "wrapper.py"])

    assert "user.py" in report.affected
    clean = _clean(root)
    assert _snapshot(store) == clean, _diff(_snapshot(store), clean)


def test_a_stable_file_waiting_on_a_file_the_same_sync_adds_reparses_its_callers(
    temp_repo: Path,
) -> None:
    """`wrapper.py` imported a `factory` that did not exist yet, so `make()`
    returned nothing known; the run that adds it gives `make()` a type, and
    the caller's `make().method()` an edge."""
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root,
        {rel: text for rel, text in CHAIN.items() if rel != "factory.py"},
        {
            "factory.py": CHAIN["factory.py"],
            "wrapper.py": _comment(CHAIN["wrapper.py"]),
        },
    )

    assert "user.py" in reparsed
    clean = _clean(root)
    assert "proj.models.A.method" in _calls(clean, "proj.user.use")
    assert synced == clean, _diff(synced, clean)


@pytest.mark.parametrize(
    ("edits", "reparsed_only"),
    [
        (
            {"factory.py": _comment, "wrapper.py": _comment},
            {"factory.py", "wrapper.py"},
        ),
        (
            {
                "wrapper.py": _comment,
                "other.py": _replace("def alone(", "def alone2("),
            },
            {"wrapper.py", "other.py"},
        ),
    ],
    ids=["both-files-stable", "unrelated-interface-change"],
)
def test_stable_files_keep_their_callers_when_nothing_they_depend_on_moved(
    temp_repo: Path, edits: dict[str, Edit], reparsed_only: set[str]
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root, CHAIN, {rel: edit(CHAIN[rel]) for rel, edit in edits.items()}
    )

    assert reparsed == reparsed_only
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)


# A body hands a function on to OTHER files through the arguments of a call
# it makes: a constructor stores it in a field that `spec.handler()` invokes
# elsewhere (the callable-field pre-pass), or a function forwards it to one
# that calls it (the callable-flow pass). The caller of the field and the
# function that calls the callback call nothing that moved in the edited
# file, so the edited file's fingerprint is all that can send them to a
# re-parse; a clean index binds them to the new argument.
FIELD_SPEC = (
    "class Spec:\n"
    "    def __init__(self, handler, retries=0):\n"
    "        self.handler = handler\n"
    "        self.retries = retries\n\n\n"
    "def a():\n    return 1\n\n\n"
    "def b():\n    return 2\n\n\n"
    "def setup():\n"
    "    spec = Spec(handler=a, retries=3)\n"
    "    total = 1 + 2\n"
    '    print("ready", total, spec)\n'
)
FIELD_CALLS: dict[str, str] = {
    "spec.py": FIELD_SPEC,
    "user.py": (
        "from spec import Spec\n\n\ndef run(spec: Spec):\n    return spec.handler()\n"
    ),
}
FIELD_CALLS_POSITIONAL: dict[str, str] = {
    **FIELD_CALLS,
    "spec.py": FIELD_SPEC.replace("Spec(handler=a,", "Spec(a,"),
}
FIELD_TARGET = "spec.py"
FIELD_CALLER = "user.py"
FIELD_HANDLER_B = _replace("handler=a", "handler=b")


@pytest.mark.parametrize(
    ("files", "edit"),
    [
        (FIELD_CALLS, FIELD_HANDLER_B),
        (FIELD_CALLS_POSITIONAL, _replace("Spec(a,", "Spec(b,")),
    ],
    ids=["keyword", "positional"],
)
def test_a_changed_constructor_argument_rebinds_the_fields_callers(
    temp_repo: Path, files: dict[str, str], edit: Edit
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(root, files, {FIELD_TARGET: edit(files[FIELD_TARGET])})

    assert FIELD_CALLER in reparsed
    clean = _clean(root)
    assert "proj.spec.b" in _calls(clean, "proj.user.run")
    assert synced == clean, _diff(synced, clean)


def test_a_scoped_reingest_of_a_changed_constructor_argument_reparses_the_fields_callers(
    temp_repo: Path,
) -> None:
    """The watcher's path. Through a fresh updater, as a changed binding
    needs (issue #2927 above)."""
    root = temp_repo / PROJECT
    _materialise(root, FIELD_CALLS)
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    (root / FIELD_TARGET).write_text(FIELD_HANDLER_B(FIELD_SPEC), encoding="utf-8")

    report = _updater(store, root).reingest([FIELD_TARGET])

    assert FIELD_CALLER in report.affected
    clean = _clean(root)
    assert _snapshot(store) == clean, _diff(_snapshot(store), clean)


# The same body edited where no argument naming a callable moves: a log
# string, local arithmetic, a literal argument (a literal names no function,
# so no binding reads it).
FIELD_UNSEEN_EDITS: list[tuple[str, Edit]] = [
    ("log-string", _replace('"ready"', '"set up"')),
    ("local-arithmetic", _replace("total = 1 + 2", "total = 2 * 3 - 1")),
    ("literal-argument", _replace("retries=3", "retries=4")),
]


@pytest.mark.parametrize(
    "edit",
    [case[1] for case in FIELD_UNSEEN_EDITS],
    ids=[case[0] for case in FIELD_UNSEEN_EDITS],
)
def test_an_edit_that_moves_no_named_argument_keeps_the_fields_callers(
    temp_repo: Path, edit: Edit
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(root, FIELD_CALLS, {FIELD_TARGET: edit(FIELD_SPEC)})

    assert reparsed == {FIELD_TARGET}
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)


def test_a_scoped_reingest_that_moves_no_named_argument_reports_no_dependents(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    _materialise(root, FIELD_CALLS)
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    (root / FIELD_TARGET).write_text(
        _replace('"ready"', '"set up"')(FIELD_SPEC), encoding="utf-8"
    )

    report = _updater(store, root).reingest([FIELD_TARGET])

    assert report.affected == ()
    clean = _clean(root)
    assert _snapshot(store) == clean, _diff(_snapshot(store), clean)


# `go` hands `a` to `relay`, which hands it on to `run`, which calls it: the
# flow edge `run -> a` is written from `app`'s parse, and `runner`, which
# holds it, calls nothing in `app`. A sync re-parsing `runner` still cannot
# give it the edge to what `go` passes now (that takes `relay`'s parse as
# well, and `relay` depends on nothing that changed), so these edits are
# held to the graph the same sync writes with every dependent re-parsed:
# keeping `runner`'s edges would keep `run -> a` on top of that.
PY_RUNNER = "def run(callback):\n    callback()\n"


def _none_stable(_updater: GraphUpdater, _sources: object) -> dict[str, str]:
    """`_interface_stable_modules` judging no interface stable: every
    dependent re-parses, as before issue #2531."""
    return {}


PY_RELAY = (
    "from runner import run\n\n\n"
    "def relay(cb):\n    run(cb)\n\n\n"
    "class Relay:\n    def relay(self, cb):\n        run(cb)\n\n\n"
    "class Quiet:\n    def relay(self, cb):\n        return 0\n"
)
PY_APP = (
    "from relay import Quiet, Relay, relay\n\n\n"
    "def a():\n    return 1\n\n\n"
    "def b():\n    return 2\n\n\n"
    "def go():\n    relay(a)\n\n\n"
    "def go_typed():\n    r = Relay()\n    r.relay(b)\n"
)
PY_FLOW: dict[str, str] = {
    "runner.py": PY_RUNNER,
    "relay.py": PY_RELAY,
    "app.py": PY_APP,
}
TS_RUNNER = "export function run(callback: () => number): void {\n  callback();\n}\n"
TS_RELAY = (
    "import { run } from './runner';\n\n"
    "export function relay(cb: () => number): void {\n  run(cb);\n}\n\n"
    "export class Relay {\n  relay(cb: () => number): void {\n    run(cb);\n  }\n}\n\n"
    "export class Quiet {\n  relay(cb: () => number): void {\n"
    "    console.log(cb);\n  }\n}\n"
)
TS_APP = (
    "import { relay, Relay, Quiet } from './relay';\n\n"
    "export function a(): number {\n  return 1;\n}\n\n"
    "export function b(): number {\n  return 2;\n}\n\n"
    "export function go(): void {\n  relay(a);\n}\n\n"
    "export function goTyped(): void {\n  const r = new Relay();\n  r.relay(b);\n}\n"
)
TS_FLOW: dict[str, str] = {
    "src/runner.ts": TS_RUNNER,
    "src/relay.ts": TS_RELAY,
    "src/app.ts": TS_APP,
}

FLOW_EDITS: list[tuple[str, dict[str, str], str, Edit, str]] = [
    ("py-argument", PY_FLOW, "app.py", _replace("relay(a)", "relay(b)"), "runner.py"),
    (
        "py-argument-shadowed-by-a-local",
        PY_FLOW,
        "app.py",
        _replace("    relay(a)\n", "    a = 1\n    relay(a)\n"),
        "runner.py",
    ),
    (
        "py-receiver-type",
        PY_FLOW,
        "app.py",
        _replace("r = Relay()", "r = Quiet()"),
        "runner.py",
    ),
    (
        "ts-argument",
        TS_FLOW,
        "src/app.ts",
        _replace("relay(a)", "relay(b)"),
        "src/runner.ts",
    ),
    (
        "ts-bound-argument",
        {**TS_FLOW, "src/app.ts": TS_APP.replace("relay(a)", "relay(a.bind(null))")},
        "src/app.ts",
        _replace("relay(a.bind(null))", "relay(b.bind(null))"),
        "src/runner.ts",
    ),
    (
        "ts-receiver-type",
        TS_FLOW,
        "src/app.ts",
        _replace("new Relay()", "new Quiet()"),
        "src/runner.ts",
    ),
]


@pytest.mark.parametrize(
    ("files", "target", "edit", "holder"),
    [case[1:] for case in FLOW_EDITS],
    ids=[case[0] for case in FLOW_EDITS],
)
def test_a_changed_forwarded_callable_reparses_the_function_that_calls_it(
    temp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
    target: str,
    edit: Edit,
    holder: str,
) -> None:
    root = temp_repo / PROJECT
    edits = {target: edit(files[target])}
    with monkeypatch.context() as unskipped:
        unskipped.setattr(GraphUpdater, "_interface_stable_modules", _none_stable)
        _all_reparsed, every_dependent = _sync(root, files, edits)

    reparsed, synced = _sync(root, files, edits)

    assert holder in reparsed
    assert synced == every_dependent, _diff(synced, every_dependent)


def test_an_edit_that_forwards_the_same_callables_keeps_their_callers(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root,
        TS_FLOW,
        {
            "src/app.ts": _replace(
                "  relay(a);\n", "  console.log('go');\n  relay(a);\n"
            )(TS_APP)
        },
    )

    assert reparsed == {"src/app.ts"}
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)


# A symbol-keyed installation inside a body types `recv[kImpl]` for every
# file (the JS symbol pre-pass), and its value is read through the body's
# own declarations.
TS_SYMBOL_APP = (
    "export const kImpl = Symbol('impl');\n\n"
    "export class Impl {\n  work(): number {\n    return 1;\n  }\n}\n\n"
    "export class Other {\n  work(): number {\n    return 2;\n  }\n}\n\n"
    "export function install(): void {\n"
    "  const impl = new Impl();\n"
    "  const host = { [kImpl]: impl };\n"
    "  console.log(host);\n}\n"
)
TS_SYMBOLS: dict[str, str] = {
    "src/app.ts": TS_SYMBOL_APP,
    "src/user.ts": (
        "import { kImpl } from './app';\n\n"
        "export function use(recv: any): void {\n  recv[kImpl].work();\n}\n"
    ),
}


@pytest.mark.parametrize(
    "files",
    [
        TS_SYMBOLS,
        {
            **TS_SYMBOLS,
            "src/app.ts": TS_SYMBOL_APP.replace(
                "  const impl = new Impl();\n  const host = { [kImpl]: impl };\n",
                "  const host = { [kImpl]: new Impl() };\n",
            ),
        },
    ],
    ids=["through-a-local", "constructed-in-place"],
)
def test_a_changed_symbol_installation_reparses_the_symbol_callers(
    temp_repo: Path, files: dict[str, str]
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root,
        files,
        {"src/app.ts": _replace("new Impl()", "new Other()")(files["src/app.ts"])},
    )

    assert "src/user.ts" in reparsed
    clean = _clean(root)
    assert "proj.src.app.Other.work" in _calls(clean, "proj.src.user.use")
    assert synced == clean, _diff(synced, clean)


# A file this run re-parses only because an added file (or a gained
# definition) resolves what it was waiting on is unchanged on disk, but
# what it resolves against is not: `make()` in `wrapper.py` returns what
# the new `factory.py` builds, so `make().method()` in `user.py` gains an
# edge. Its callers are its dependents like any re-parsed file's.
WAITING: dict[str, str] = {
    **{rel: text for rel, text in CHAIN.items() if rel != "factory.py"},
    "other_user.py": (
        "from other import alone\n\n\ndef use_alone():\n    return alone()\n"
    ),
}


def test_a_file_woken_by_an_added_file_reparses_its_callers(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(root, WAITING, {"factory.py": CHAIN["factory.py"]})

    assert "user.py" in reparsed
    clean = _clean(root)
    assert "proj.models.A.method" in _calls(clean, "proj.user.use")
    assert synced == clean, _diff(synced, clean)


def test_a_scoped_reingest_creating_a_waited_on_file_reparses_the_waiters_callers(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    _materialise(root, WAITING)
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    (root / "factory.py").write_text(CHAIN["factory.py"], encoding="utf-8")

    report = _updater(store, root).reingest(["factory.py"])

    assert "user.py" in report.affected
    clean = _clean(root)
    assert _snapshot(store) == clean, _diff(_snapshot(store), clean)


def test_a_woken_file_leaves_the_callers_of_an_unrelated_stable_file_alone(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root,
        WAITING,
        {"factory.py": CHAIN["factory.py"], "other.py": _comment(CHAIN["other.py"])},
    )

    assert reparsed == {"factory.py", "other.py", "wrapper.py", "user.py"}
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)


# A Go method keys under the module declaring its receiver type, so a sync
# re-indexing that module re-parses the file defining the method too (issue
# #1660). That file's definitions are rebuilt like any re-parsed file's, so
# the callers of them are its dependents as well.
GO_RECEIVER: dict[str, str] = {
    "go.mod": "module example.com/proj\n\ngo 1.21\n",
    "pkg/types.go": "package pkg\n\ntype T struct{}\n",
    "pkg/methods.go": "package pkg\n\nfunc (t T) M() int {\n\treturn 1\n}\n",
    "pkg/use.go": "package pkg\n\nfunc Use() int {\n\tvar t T\n\treturn t.M()\n}\n",
}


@pytest.mark.parametrize(
    ("edits", "removed"),
    [
        ({"pkg/types.go": GO_RECEIVER["pkg/types.go"] + "// note\n"}, None),
        ({"pkg/kinds.go": GO_RECEIVER["pkg/types.go"]}, "pkg/types.go"),
    ],
    ids=["receiver-module-edited", "receiver-module-renamed"],
)
def test_the_callers_of_a_method_defined_into_a_reindexed_module_are_reparsed(
    temp_repo: Path, edits: dict[str, str], removed: str | None
) -> None:
    """Through a reused updater: only it remembers which file defined into
    the module (a fresh one finds no such file, as before this change)."""
    root = temp_repo / PROJECT
    _materialise(root, GO_RECEIVER)
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    _edit_after_cache(root, edits)
    if removed is not None:
        (root / removed).unlink()

    updater.run()

    assert "pkg/use.go" in updater._reparsed_file_keys
    clean = _clean(root)
    assert _snapshot(store) == clean, _diff(_snapshot(store), clean)


# `make()` hands back a bound method, and which class's method is read off
# the local it was taken from (the returned-callable reader resolves
# `r.run` through the body's own `r = A()`). A caller binding and invoking
# it, `x = make(); x(on_done)`, calls `A.run` and hands it `on_done`; it
# calls nothing else in `app.py`, so only the fingerprint can send it to a
# re-parse when `r` starts holding a `B`. When `run` only stores the
# callback, no flow edge starts in `app.py`, and the fingerprint alone
# decides.
RETURNED_METHOD_APP = (
    "class A:\n    def run(self, cb):\n        cb()\n\n\n"
    "class B:\n    def run(self, cb):\n        cb()\n\n\n"
    "def on_done():\n    return 1\n\n\n"
    "def make():\n    r = A()\n    return r.run\n"
)
RETURNED_METHOD: dict[str, str] = {
    "app.py": RETURNED_METHOD_APP,
    "user.py": (
        "from app import make, on_done\n\n\ndef go():\n    x = make()\n    x(on_done)\n"
    ),
}
RETURNED_METHOD_NOT_INVOKING: dict[str, str] = {
    **RETURNED_METHOD,
    "app.py": RETURNED_METHOD_APP.replace("        cb()\n", "        return cb\n"),
}
RETURNED_FROM_B = _replace("    r = A()\n", "    r = B()\n")
RETURNED_METHOD_CASES = [RETURNED_METHOD, RETURNED_METHOD_NOT_INVOKING]
RETURNED_METHOD_IDS = ["callback-invoked", "callback-not-invoked"]


@pytest.mark.parametrize("files", RETURNED_METHOD_CASES, ids=RETURNED_METHOD_IDS)
def test_a_returned_method_read_through_a_local_rebinds_its_callers(
    temp_repo: Path, files: dict[str, str]
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(root, files, {"app.py": RETURNED_FROM_B(files["app.py"])})

    assert "user.py" in reparsed
    clean = _clean(root)
    assert "proj.app.B.run" in _calls(clean, "proj.user.go")
    assert synced == clean, _diff(synced, clean)


@pytest.mark.parametrize("files", RETURNED_METHOD_CASES, ids=RETURNED_METHOD_IDS)
def test_a_scoped_reingest_of_a_returned_method_read_through_a_local(
    temp_repo: Path, files: dict[str, str]
) -> None:
    root = temp_repo / PROJECT
    _materialise(root, files)
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    (root / "app.py").write_text(RETURNED_FROM_B(files["app.py"]), encoding="utf-8")

    report = _updater(store, root).reingest(["app.py"])

    assert "user.py" in report.affected
    clean = _clean(root)
    assert _snapshot(store) == clean, _diff(_snapshot(store), clean)


# `driver.py` hands `on_done` to `run`, and the flow edge `run -> on_done`
# that its parse seeds lies wholly inside `engine.py`: re-parsing
# `engine.py` drops it, and only `driver.py`'s parse writes it back, however
# little the edit to `engine.py` changed.
FLOW_SEEDED: dict[str, str] = {
    "engine.py": "def run(cb):\n    cb()\n\n\ndef on_done():\n    return 1\n",
    "driver.py": "from engine import on_done, run\n\n\ndef go():\n    run(on_done)\n",
}


def test_a_comment_in_a_file_whose_callbacks_others_seed_reparses_its_dependents(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    reparsed, synced = _sync(
        root, FLOW_SEEDED, {"engine.py": _comment(FLOW_SEEDED["engine.py"])}
    )

    assert "driver.py" in reparsed
    clean = _clean(root)
    assert "proj.engine.on_done" in _calls(clean, "proj.engine.run")
    assert synced == clean, _diff(synced, clean)


def test_a_scoped_reingest_of_a_file_whose_callbacks_others_seed(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    _materialise(root, FLOW_SEEDED)
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    (root / "engine.py").write_text(
        _comment(FLOW_SEEDED["engine.py"]), encoding="utf-8"
    )

    report = _updater(store, root).reingest(["engine.py"])

    assert "driver.py" in report.affected
    clean = _clean(root)
    assert _snapshot(store) == clean, _diff(_snapshot(store), clean)
