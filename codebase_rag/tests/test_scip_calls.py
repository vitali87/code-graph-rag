"""How the SCIP oracle reads call edges off resolved occurrences.

The index is assembled by hand over real source text, so each rule is
pinned against a case that would break it: a bare reference is not a call,
the innermost body owns a call, a decorator runs in the outer scope, a
property read and a constructor call count, an import does not, and the
dispatch view widens a virtual call to the overrides the index records.
"""

from __future__ import annotations

from pathlib import Path

from evals.scip import constants as sc
from evals.scip.calls import module_definition, oracle_edges
from evals.scip.indexers import INDEXERS
from evals.scip.types_defs import (
    Definition,
    ScipDocument,
    ScipIndex,
    ScipOccurrence,
    ScipRange,
    ScipRelationship,
    ScipSymbol,
)

PY_SOURCE = """def helper():
    return 1

class Base:
    def run(self):
        return helper()

class Child(Base):
    def __init__(self):
        self.x = 1
    def run(self):
        return 2
    @property
    def size(self):
        return 3

def outer():
    def inner():
        return helper()
    ref = helper
    return inner()

def deco(fn):
    return fn

@deco
def decorated(b: Base):
    c = Child()
    return b.run() + c.size

helper()
helper()
"""

_PREFIX = "scip-python python pkg 0 `pkg.mod`/"
HELPER = _PREFIX + "helper()."
BASE = _PREFIX + "Base#"
BASE_RUN = _PREFIX + "Base#run()."
CHILD = _PREFIX + "Child#"
CHILD_INIT = _PREFIX + "Child#__init__()."
CHILD_RUN = _PREFIX + "Child#run()."
SIZE = _PREFIX + "Child#size()."
OUTER = _PREFIX + "outer()."
INNER = _PREFIX + "outer().inner()."
DECO = _PREFIX + "deco()."
DECORATED = _PREFIX + "decorated()."
PATH = "pkg/mod.py"


def _span(lines: list[str], line: int, token: str, nth: int = 0) -> ScipRange:
    col = -1
    for _ in range(nth + 1):
        col = lines[line].index(token, col + 1)
    return ScipRange(line, col, line, col + len(token))


def _define(
    lines: list[str],
    line: int,
    token: str,
    symbol: str,
    body: tuple[int, int, int, int],
) -> ScipOccurrence:
    return ScipOccurrence(
        _span(lines, line, token), symbol, sc.ROLE_DEFINITION, ScipRange(*body)
    )


def _use(
    lines: list[str], line: int, token: str, symbol: str, roles: int = 0
) -> ScipOccurrence:
    return ScipOccurrence(_span(lines, line, token), symbol, roles, None)


def _python_index() -> ScipIndex:
    lines = PY_SOURCE.splitlines()
    occurrences = (
        _define(lines, 0, "helper", HELPER, (0, 0, 1, 12)),
        _define(lines, 3, "Base", BASE, (3, 0, 5, 23)),
        _define(lines, 4, "run", BASE_RUN, (4, 4, 5, 23)),
        _use(lines, 5, "helper", HELPER),
        _define(lines, 7, "Child", CHILD, (7, 0, 14, 16)),
        _define(lines, 8, "__init__", CHILD_INIT, (8, 4, 9, 18)),
        _define(lines, 10, "run", CHILD_RUN, (10, 4, 11, 16)),
        _define(lines, 13, "size", SIZE, (12, 4, 14, 16)),
        _define(lines, 16, "outer", OUTER, (16, 0, 20, 18)),
        _define(lines, 17, "inner", INNER, (17, 4, 18, 23)),
        _use(lines, 18, "helper", HELPER),
        _use(lines, 19, "helper", HELPER),
        _use(lines, 20, "inner", INNER),
        _define(lines, 22, "deco", DECO, (22, 0, 23, 13)),
        _use(lines, 25, "deco", DECO),
        _define(lines, 26, "decorated", DECORATED, (25, 0, 28, 25)),
        _use(lines, 26, "Base", BASE),
        _use(lines, 27, "Child", CHILD),
        _use(lines, 28, "run", BASE_RUN),
        _use(lines, 28, "size", SIZE),
        _use(lines, 30, "helper", HELPER),
        # Shaped like a call, but an import: never a call edge.
        _use(lines, 31, "helper", HELPER, roles=sc.ROLE_IMPORT),
    )
    overrides = ScipSymbol(
        CHILD_RUN,
        sc.KIND_METHOD,
        "run",
        (ScipRelationship(BASE_RUN, False, True, False, False),),
        "",
    )
    document = ScipDocument(PATH, "python", occurrences, (overrides,))
    return ScipIndex("scip-python", "0.6.6", "", (document,), ())


def _definition(line: int, name: str, path: str = PATH) -> Definition:
    return Definition(path, line, name)


def _write(tmp_path: Path, relative: str, text: str) -> Path:
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return tmp_path


def test_python_call_sites(tmp_path: Path) -> None:
    root = _write(tmp_path, PATH, PY_SOURCE)
    oracle = oracle_edges(_python_index(), root, INDEXERS[sc.ScipLanguage.PYTHON])

    helper, base_run = _definition(0, "helper"), _definition(4, "run")
    decorated = _definition(26, "decorated")
    module = module_definition(PATH)
    assert oracle.edges == {
        (base_run, helper),
        # The innermost body owns the call, not `outer` around it; and the
        # bare `ref = helper` on the next line is no call at all.
        (_definition(17, "inner"), helper),
        (_definition(16, "outer"), _definition(17, "inner")),
        # `@deco` sits inside `decorated`'s range but runs at module level.
        (module, _definition(22, "deco")),
        (decorated, _definition(8, "__init__")),
        (decorated, base_run),
        (decorated, _definition(13, "size")),
        (module, helper),
    }
    assert oracle.dispatch_edges == oracle.edges | {(decorated, _definition(10, "run"))}


JS_SOURCE = """const f = (x) => g(x);
function g(y) {
  return y;
}
f(1);
html`<b>${g(2)}</b>`;
function html(parts) { return parts; }
wrap(function named() { return g(3); });
export default ok && function unindexed() { return g(4); };
"""


def test_js_function_bindings_and_tagged_templates(tmp_path: Path) -> None:
    root = _write(tmp_path, "main.js", JS_SOURCE)
    lines = JS_SOURCE.splitlines()
    prefix = "scip-typescript npm . . `main.js`/"
    f, g, html = prefix + "f.", prefix + "g().", prefix + "html()."
    # A named function expression: a local symbol, again with no range.
    named = "local 9"
    occurrences = (
        # The binding is a variable to SCIP, with no body range of its own.
        ScipOccurrence(_span(lines, 0, "f"), f, sc.ROLE_DEFINITION, None),
        _use(lines, 0, "g", g),
        _define(lines, 1, "g", g, (1, 0, 3, 1)),
        _use(lines, 4, "f", f),
        _use(lines, 5, "html", html),
        _use(lines, 5, "g", g),
        _define(lines, 6, "html", html, (6, 0, 6, 38)),
        ScipOccurrence(_span(lines, 7, "named"), named, sc.ROLE_DEFINITION, None),
        _use(lines, 7, "g", g),
        _use(lines, 8, "g", g),
    )
    document = ScipDocument("main.js", "JavaScript", occurrences, ())
    index = ScipIndex("scip-typescript", "0.4.0", "", (document,), ())

    oracle = oracle_edges(index, root, INDEXERS[sc.ScipLanguage.JAVASCRIPT])

    module = module_definition("main.js")
    f_def = Definition("main.js", 0, "f")
    g_def = Definition("main.js", 1, "g")
    assert oracle.edges == {
        (f_def, g_def),
        (module, f_def),
        (module, Definition("main.js", 6, "html")),
        (module, g_def),
        (Definition("main.js", 7, "named"), g_def),
        # No definition occurrence at all: found in the source instead.
        (Definition("main.js", 8, "unindexed"), g_def),
    }
