"""Inline-function review regressions (Greptile and Copilot, PRs #2058/#2060).

Every finding here is about runtime behaviour, so each test executes the
rewritten program and compares its output with what the original printed.
Where a call cannot be inlined without changing semantics, the operation must
leave it unchanged (and keep the definition), never emit different code.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import InlineRefused, InlineReport, inline
from codebase_rag.tests.extract_inline_helpers import PROJECT, _index, _write
from evals.cgr_graph import _StatefulIngestor


def _project_qn(rel: str) -> str:
    return f"{PROJECT}.{rel}"


def _build(temp_repo: Path, files: dict[str, str]) -> tuple[Path, _StatefulIngestor]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in files.items():
        _write(root, rel, text)
    store, _updater = _index(root)
    return root, store


def _inline(root: Path, store: _StatefulIngestor, rel: str) -> InlineReport | None:
    try:
        return inline(root, store.fetch_all, PROJECT, _project_qn(rel))
    except InlineRefused:
        return None


def _python(root: Path, code: str) -> str:
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    return (done.stdout + done.stderr).strip()


def _node(root: Path, rel: str) -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    done = subprocess.run(
        [node, rel],
        cwd=root,
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    return (done.stdout + done.stderr).strip()


# --- async and generators (kMrpr, kYUeF) -------------------------------------------

_ASYNC_FILES = {
    "pkg/__init__.py": "",
    "pkg/defs.py": (
        "async def fetch():\n    return 1\n\n\n"
        "def gen():\n    return (yield 1)\n\n\n"
        "async def use_fetch():\n    return await fetch()\n\n\n"
        "def use_gen():\n    return list(gen())\n"
    ),
    "web/defs.js": (
        "async function load() { return 1; }\n"
        "function* items() { return 1; }\n"
        "load().then((v) => console.log(v, [...items()].length));\n"
    ),
}


@pytest.mark.parametrize(
    "rel", ["pkg.defs.fetch", "pkg.defs.gen", "web.defs.load", "web.defs.items"]
)
def test_inline_refuses_async_and_generator_definitions(
    temp_repo: Path, rel: str
) -> None:
    root, store = _build(temp_repo, _ASYNC_FILES)
    with pytest.raises(InlineRefused, match="async|generator"):
        inline(root, store.fetch_all, PROJECT, _project_qn(rel))
    assert (root / "pkg/defs.py").read_text() == _ASYNC_FILES["pkg/defs.py"]
    assert (root / "web/defs.js").read_text() == _ASYNC_FILES["web/defs.js"]


# --- argument evaluation (kMrpx, kMr98) ---------------------------------------------

_EVAL_FILES = {
    "pkg/__init__.py": "",
    "pkg/twice.py": (
        "calls = []\n\n\n"
        "def next_value():\n    calls.append(1)\n    return len(calls)\n\n\n"
        "def twice(x):\n    return x + x\n\n\n"
        "def use_twice():\n    return twice(next_value())\n\n\n"
        "def use_atomic(n):\n    return twice(n)\n"
    ),
    "pkg/five.py": (
        "launched = []\n\n\n"
        "def launch():\n    launched.append(1)\n    return 0\n\n\n"
        "def always_five(x):\n    return 5\n\n\n"
        "def use_five():\n    return always_five(launch())\n"
    ),
    "pkg/sub.py": (
        "log = []\n\n\n"
        "def first():\n    log.append('a')\n    return 1\n\n\n"
        "def second():\n    log.append('b')\n    return 10\n\n\n"
        "def sub(a, b):\n    return b - a\n\n\n"
        "def use_sub():\n    return sub(first(), second())\n"
    ),
    "pkg/later.py": (
        "log = []\n\n\n"
        "def first():\n    log.append('first')\n    return 1\n\n\n"
        "def tick():\n    log.append('tick')\n    return 10\n\n\n"
        "def later(x):\n    return tick() + x\n\n\n"
        "def use_later():\n    return later(first())\n"
    ),
    "pkg/pick.py": (
        "launched = []\n\n\n"
        "def launch():\n    launched.append(1)\n    return 7\n\n\n"
        "def pick(c, x):\n    return x if c else 0\n\n\n"
        "def use_pick():\n    return pick(False, launch())\n"
    ),
}


def test_inline_evaluates_every_argument_once_and_left_to_right(
    temp_repo: Path,
) -> None:
    root, store = _build(temp_repo, _EVAL_FILES)
    for rel in (
        "pkg.twice.twice",
        "pkg.five.always_five",
        "pkg.sub.sub",
        "pkg.later.later",
        "pkg.pick.pick",
    ):
        _inline(root, store, rel)
    assert (
        _python(
            root,
            "from pkg.twice import use_twice, use_atomic, calls\n"
            "print(use_twice(), len(calls), use_atomic(4))",
        )
        == "2 1 8"
    )
    # An atomic argument is still substituted, however often it is read.
    assert (
        "def use_atomic(n):\n    return (n + n)\n"
        in (root / "pkg/twice.py").read_text()
    )
    assert (
        _python(
            root, "from pkg.five import use_five, launched\nprint(use_five(), launched)"
        )
        == "5 [1]"
    )
    assert (
        _python(root, "from pkg.sub import use_sub, log\nprint(use_sub(), log)")
        == "9 ['a', 'b']"
    )
    assert (
        _python(root, "from pkg.later import use_later, log\nprint(use_later(), log)")
        == "11 ['first', 'tick']"
    )
    assert (
        _python(
            root, "from pkg.pick import use_pick, launched\nprint(use_pick(), launched)"
        )
        == "0 [1]"
    )


# --- non-literal defaults (kMrpz) ---------------------------------------------------

_DEFAULT_FILES = {
    "pkg/__init__.py": "",
    "pkg/box.py": (
        "made = []\n\n\n"
        "def new_box():\n    made.append(1)\n    return []\n\n\n"
        "def tag(x, box=new_box()):\n    return box\n\n\n"
        "def one():\n    return tag(1)\n\n\n"
        "def two():\n    return tag(2)\n"
    ),
    # A default with no name in it: one list shared by every call.
    "pkg/shared.py": (
        "def keep(x, box=[]):\n    return box\n\n\n"
        "def one():\n    return keep(1)\n\n\n"
        "def two():\n    return keep(2)\n"
    ),
}


def test_inline_does_not_copy_a_non_literal_default(temp_repo: Path) -> None:
    root, store = _build(temp_repo, _DEFAULT_FILES)
    _inline(root, store, "pkg.box.tag")
    _inline(root, store, "pkg.shared.keep")
    assert (
        _python(
            root, "from pkg.box import one, two, made\nprint(one() is two(), len(made))"
        )
        == "True 1"
    )
    assert (
        _python(root, "from pkg.shared import one, two\nprint(one() is two())")
        == "True"
    )


# --- splat and spread arguments (kMrp2) ---------------------------------------------

_SPLAT_FILES = {
    "pkg/__init__.py": "",
    "pkg/ident.py": (
        "values = [3]\nnamed = {'x': 5}\n\n\n"
        "def identity(x):\n    return x\n\n\n"
        "def spread():\n    return identity(*values)\n\n\n"
        "def keywords():\n    return identity(**named)\n\n\n"
        "def plain():\n    return identity(4)\n"
    ),
    "web/ident.js": (
        "function identity(x) { return x; }\n"
        "const values = [3];\n"
        "console.log(identity(...values), identity(4));\n"
    ),
}


def test_inline_leaves_splat_calls_unchanged_and_keeps_the_definition(
    temp_repo: Path,
) -> None:
    root, store = _build(temp_repo, _SPLAT_FILES)
    report = _inline(root, store, "pkg.ident.identity")
    assert report is not None and report.applied, report
    assert not report.definition_removed
    text = (root / "pkg/ident.py").read_text()
    assert "identity(*values)" in text and "identity(**named)" in text
    assert "def plain():\n    return 4\n" in text
    assert (
        _python(
            root,
            "from pkg.ident import spread, keywords, plain\n"
            "print(spread(), keywords(), plain())",
        )
        == "3 5 4"
    )
    report = _inline(root, store, "web.ident.identity")
    assert report is not None and report.applied, report
    assert not report.definition_removed
    js = (root / "web/ident.js").read_text()
    assert "identity(...values)" in js and "function identity(x)" in js
    assert _node(root, "web/ident.js") in (None, "3 4")


# --- JS/TS `this` receivers (kMr99, kYVUv) ------------------------------------------

_THIS_FILES = {
    "web/box.js": (
        "class Box {\n"
        "  constructor(v) { this.value = v; }\n"
        "  getValue() { return this.value; }\n"
        "}\n"
        "const b = new Box(3);\n"
        "console.log(b.getValue());\n"
    ),
}


def test_inline_never_copies_this_out_of_a_js_method(temp_repo: Path) -> None:
    root, store = _build(temp_repo, _THIS_FILES)
    _inline(root, store, "web.box.Box.getValue")
    js = (root / "web/box.js").read_text()
    # Refusal is the chosen behaviour: the call keeps its receiver.
    assert js == _THIS_FILES["web/box.js"]
    assert _node(root, "web/box.js") in (None, "3")


# --- free identifiers in the callee (kMrpv) -----------------------------------------

_SCOPE_FILES = {
    "pkg/__init__.py": "",
    "pkg/scale.py": (
        "SCALE = 10\n\n\n"
        "def scaled(x):\n    return x * SCALE\n\n\n"
        "def local_use():\n    return scaled(2)\n\n\n"
        "def shadowed():\n    SCALE = 3\n    return scaled(2) + SCALE - 3\n"
    ),
    "pkg/other.py": (
        "from pkg.scale import scaled\n\nSCALE = 100\n\n\n"
        "def conflict():\n    return scaled(2)\n"
    ),
    "pkg/unbound.py": (
        "from pkg.scale import scaled\n\n\ndef unbound():\n    return scaled(2)\n"
    ),
}


def test_inline_keeps_callee_free_names_in_the_callee_scope(temp_repo: Path) -> None:
    root, store = _build(temp_repo, _SCOPE_FILES)
    _inline(root, store, "pkg.scale.scaled")
    assert (
        _python(
            root,
            "from pkg.scale import local_use, shadowed\n"
            "from pkg.other import conflict\n"
            "from pkg.unbound import unbound\n"
            "print(local_use(), shadowed(), conflict(), unbound())",
        )
        == "20 20 20 20"
    )
    # A same-module caller that does not rebind the name still inlines.
    assert (
        "def local_use():\n    return (2 * SCALE)\n"
        in (root / "pkg/scale.py").read_text()
    )


# --- live imports (kMrp6) -----------------------------------------------------------

_IMPORT_FILES = {
    "pkg/__init__.py": "from pkg.util import helper\n",
    "pkg/util.py": "def helper(x):\n    return x + 1\n",
    "pkg/app.py": (
        "from pkg.util import helper\n\n"
        '__all__ = ["helper", "use"]\n\n\n'
        "def use():\n    return helper(1)\n"
    ),
}


def test_inline_keeps_an_import_whose_binding_is_still_live(temp_repo: Path) -> None:
    root, store = _build(temp_repo, _IMPORT_FILES)
    report = _inline(root, store, "pkg.util.helper")
    assert report is not None and report.applied, report
    assert not report.definition_removed
    assert "def use():\n    return (1 + 1)\n" in (root / "pkg/app.py").read_text()
    assert (
        _python(
            root,
            "import pkg\nimport pkg.app as app\nfrom pkg.app import *\n"
            "print(app.use(), app.helper(2), pkg.helper(3), helper(4))",
        )
        == "2 3 4 5"
    )


# --- non-call references (kYVVF) ----------------------------------------------------

_REFERENCE_FILES = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(x):\n    return x + 1\n",
    "pkg/app.py": (
        "from pkg.util import helper\n\n\n"
        "def register(fn):\n    return fn\n\n\n"
        "def use():\n    return helper(1)\n\n\n"
        "HANDLER = register(helper)\n"
    ),
}


def test_inline_keeps_a_definition_still_referenced_as_a_value(
    temp_repo: Path,
) -> None:
    root, store = _build(temp_repo, _REFERENCE_FILES)
    report = _inline(root, store, "pkg.util.helper")
    assert report is not None and report.applied, report
    assert not report.definition_removed
    assert "def use():\n    return (1 + 1)\n" in (root / "pkg/app.py").read_text()
    assert (
        _python(root, "from pkg.app import use, HANDLER\nprint(use(), HANDLER(4))")
        == "2 5"
    )


# --- a chained call site (Copilot, PR #2064) ---------------------------------------


def test_inline_rewrites_the_inner_call_of_a_chained_site(temp_repo: Path) -> None:
    """`helper(2).upper()` starts where `helper(2)` does. Located by its start
    alone the outermost call was taken, so `.upper()`'s empty argument list
    was bound to `helper`'s parameter; the site's recorded end picks the
    call the graph actually recorded, as change_signature does."""
    root, store = _build(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a):\n    return str(a)\n",
            "pkg/app.py": (
                "from pkg.util import helper\n\n\n"
                "def run():\n    return helper(2).upper()\n"
            ),
        },
    )
    report = _inline(root, store, "pkg.util.helper")

    assert report is not None and report.applied, report
    assert "str(2).upper()" in (root / "pkg/app.py").read_text()
    assert _python(root, "from pkg.app import run; print(run())") == "2"


# --- behaviour preservation (Greptile, PR #2932) ----------------------------------

_PROPERTY_FILES = {
    "pkg/__init__.py": "",
    "pkg/meter.py": (
        "class Meter:\n"
        "    def __init__(self):\n        self.reads = 0\n\n"
        "    @property\n"
        "    def value(self):\n        self.reads += 1\n        return self.reads\n"
    ),
    "pkg/twice.py": (
        "def twice(x):\n    return x + x\n\n\n"
        "def use_twice(meter):\n    return twice(meter.value)\n"
    ),
    "pkg/ignore.py": (
        "def ignore(x):\n    return 5\n\n\n"
        "def use_ignore(meter):\n    return ignore(meter.value)\n"
    ),
}


@pytest.mark.parametrize(
    ("rel", "probe", "expected"),
    [
        ("pkg.twice.twice", "use_twice", "2 1"),
        ("pkg.ignore.ignore", "use_ignore", "5 1"),
    ],
    ids=["read-twice", "never-read"],
)
def test_inline_reads_an_attribute_argument_exactly_once(
    temp_repo: Path, rel: str, probe: str, expected: str
) -> None:
    root, store = _build(temp_repo, _PROPERTY_FILES)
    module = rel.rsplit(".", 1)[0]
    code = (
        f"from pkg.meter import Meter\nfrom {module} import {probe}\n"
        f"m = Meter()\nprint({probe}(m), m.reads)"
    )
    assert _python(root, code) == expected
    _inline(root, store, rel)
    assert _python(root, code) == expected


_DECORATED_FILES = {
    "pkg/__init__.py": "",
    "pkg/greet.py": (
        "def shout(fn):\n"
        "    def wrapper(x):\n        return fn(x).upper()\n\n"
        "    return wrapper\n\n\n"
        "@shout\n"
        "def greet(x):\n    return 'hi ' + x\n\n\n"
        "def use():\n    return greet('bob')\n"
    ),
}


def test_inline_refuses_a_decorated_definition(temp_repo: Path) -> None:
    root, store = _build(temp_repo, _DECORATED_FILES)
    with pytest.raises(InlineRefused, match="decorat"):
        inline(root, store.fetch_all, PROJECT, _project_qn("pkg.greet.greet"))
    assert (root / "pkg/greet.py").read_text() == _DECORATED_FILES["pkg/greet.py"]
    assert _python(root, "from pkg.greet import use; print(use())") == "HI BOB"


_SHARED_LINE_JS = (
    "function helper(x) { return x + 1; } console.log('ready');\n"
    "console.log(helper(1));\n"
)


def test_inline_keeps_code_sharing_a_line_with_the_definition(
    temp_repo: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    root, store = _build(temp_repo, {"web/ready.js": _SHARED_LINE_JS})
    before = _node(root, "web/ready.js")
    assert before == "ready\n2"
    report = _inline(root, store, "web.ready.helper")
    assert report is not None and report.applied, report
    assert "console.log('ready');" in (root / "web/ready.js").read_text()
    assert _node(root, "web/ready.js") == before


# --- removed imports --------------------------------------------------------------

_HOOK_FILES = {
    "pkg/__init__.py": "",
    "pkg/hooks.py": "loaded = []\n",
    "pkg/util.py": (
        "from pkg import hooks\n\nhooks.loaded.append('util')\n\n\n"
        "def helper(x):\n    return x + 1\n"
    ),
}


_USE = "\n\n\ndef use():\n    return helper(1)\n"


@pytest.mark.parametrize(
    ("files", "qn"),
    [
        (
            {
                **_HOOK_FILES,
                "pkg/app.py": "from pkg.util import helper; print('ready')" + _USE,
            },
            "pkg.util.helper",
        ),
        (
            {
                **_HOOK_FILES,
                "pkg/app.py": "print('ready'); from pkg.util import helper" + _USE,
            },
            "pkg.util.helper",
        ),
        # The importer's own package is loaded anyway, so the import goes.
        (
            {
                "pkg/__init__.py": "def helper(x):\n    return x + 1\n",
                "pkg/app.py": "from . import helper; print('ready')" + _USE,
            },
            "pkg.helper",
        ),
    ],
    ids=["code-after", "code-before", "own-package"],
)
def test_inline_keeps_code_sharing_a_line_with_a_removed_import(
    temp_repo: Path, files: dict[str, str], qn: str
) -> None:
    root, store = _build(temp_repo, files)
    probe = "import pkg.app\nprint(pkg.app.use())"
    before = _python(root, probe)
    assert before == "ready\n2"
    report = _inline(root, store, qn)
    assert report is not None and report.applied, report
    assert report.definition_removed
    assert "helper" not in (root / "pkg/app.py").read_text()
    assert _python(root, probe) == before


_JS_IMPORT_FILES = {
    "web/package.json": '{"type": "module"}\n',
    "web/util.js": (
        "globalThis.loaded = ['util'];\nexport function helper(x) { return x + 1; }\n"
    ),
    "web/app.js": (
        "import { helper } from './util.js'; console.log('ready');\n"
        "console.log(helper(1), globalThis.loaded);\n"
    ),
}


def test_inline_keeps_js_code_sharing_a_line_with_a_removed_import(
    temp_repo: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    root, store = _build(temp_repo, _JS_IMPORT_FILES)
    before = _node(root, "web/app.js")
    assert before == "ready\n2 [ 'util' ]"
    report = _inline(root, store, "web.util.helper")
    assert report is not None and report.applied, report
    assert report.definition_removed
    assert "helper" not in (root / "web/app.js").read_text()
    assert _node(root, "web/app.js") == before


_SIDE_EFFECT_APPS = {
    "absolute": "from pkg.util import helper\n\n\ndef use():\n    return helper(1)\n",
    "relative": "from .util import helper\n\n\ndef use():\n    return helper(1)\n",
    "aliased": "from pkg.util import helper as h\n\n\ndef use():\n    return h(1)\n",
}


@pytest.mark.parametrize("shape", sorted(_SIDE_EFFECT_APPS))
def test_inline_still_loads_the_module_of_a_removed_import(
    temp_repo: Path, shape: str
) -> None:
    root, store = _build(
        temp_repo, {**_HOOK_FILES, "pkg/app.py": _SIDE_EFFECT_APPS[shape]}
    )
    probe = "import pkg.app\nfrom pkg import hooks\nprint(pkg.app.use(), hooks.loaded)"
    before = _python(root, probe)
    assert before == "2 ['util']"
    report = _inline(root, store, "pkg.util.helper")
    assert report is not None and report.applied, report
    assert report.definition_removed
    assert "def helper" not in (root / "pkg/util.py").read_text()
    assert _python(root, probe) == before


def test_inline_keeps_the_import_when_the_module_name_is_taken(
    temp_repo: Path,
) -> None:
    # `import pkg.util` would rebind the file's own `pkg`, so the import and
    # with it the definition stay.
    app = (
        "from pkg.util import helper\n\npkg = 'mine'\n\n\n"
        "def use():\n    return helper(1), pkg\n"
    )
    root, store = _build(temp_repo, {**_HOOK_FILES, "pkg/app.py": app})
    report = _inline(root, store, "pkg.util.helper")
    assert report is not None and report.applied, report
    assert not report.definition_removed
    probe = "from pkg import app, hooks\nprint(app.use(), hooks.loaded)"
    assert _python(root, probe) == "(2, 'mine') ['util']"
