"""Issue #3822: a Go function passed by name is resolved with Go's rules.

A function handed on as a value (`sort.Slice(xs, handler)`, `less :=
handler`, `register(handler)`) was resolved with no language, so the three
rules Go gives a bare name were skipped and the edge said something no call of
the same name would:

1. A local hides the package function (#2571): in `func authors(authors
   []string)`, `len(authors)` passes the parameter, so the edge the name trie
   finds to the function is a guess, not `exact`.
2. A function of a sibling file in the package is the one the name means
   (#2703's Go counterpart): `sort.Slice(xs, handler)` binds it `exact`, as
   the call `handler(1, 2)` does, not as a `heuristic` guess.
3. A bare name never names a method (#3783): `fmt.Println(format)` inside
   method `T.Printf` passes the parameter, never the method `T.format`.

The resolver's cache did not key on the language either, so a call made
after the pass reused the pass's answer.

Each case runs on tree-sitter alone and with the go/types helper when Go is
installed: the helper gives no fact for a function value, so both modes take
the same path.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

pytestmark = pytest.mark.usefixtures(
    "one_go_helper_per_session", "fingerprint_inputs_once_per_session"
)

GO_MOD = "module example.com/proj\n\ngo 1.22\n"

LOCAL = (
    "package local\n"
    "\n"
    "func authors(authors []string) int {\n"
    "\treturn len(authors)\n"
    "}\n"
    "\n"
    "func names(names []string) []string {\n"
    "\tkept := names\n"
    "\treturn kept\n"
    "}\n"
    "\n"
    "func Use() int {\n"
    "\treturn authors(nil) + len(names(nil))\n"
    "}\n"
)
HANDLER = "package sorted\n\nfunc handler(i, j int) bool { return i < j }\n"
DECOY = "package other\n\nfunc handler(i, j int) bool { return false }\n"
SORTED = (
    "package sorted\n"
    "\n"
    'import "sort"\n'
    "\n"
    "func Use(xs []int) {\n"
    "\tsort.Slice(xs, handler)\n"
    "}\n"
    "\n"
    "func Keep() func(int, int) bool {\n"
    "\tless := handler\n"
    "\treturn less\n"
    "}\n"
    "\n"
    "func Direct() bool {\n"
    "\treturn handler(1, 2)\n"
    "}\n"
)
PRINTER = (
    "package printer\n"
    "\n"
    'import "fmt"\n'
    "\n"
    "type T struct{}\n"
    "\n"
    'func (t T) format() string { return "" }\n'
    "\n"
    "func (t T) Printf(format string) {\n"
    "\tfmt.Println(format)\n"
    "\tshown := format\n"
    "\t_ = shown\n"
    "}\n"
    "\n"
    "func (t T) Bound() {\n"
    "\tfmt.Println(t.format)\n"
    "\tshown := t.format\n"
    "\t_ = shown\n"
    "}\n"
)
FILES = {
    "go.mod": GO_MOD,
    "local/local.go": LOCAL,
    "sorted/handler.go": HANDLER,
    "sorted/sorted.go": SORTED,
    "other/other.go": DECOY,
    "printer/printer.go": PRINTER,
}

AUTHORS = "proj.local.local.authors"
NAMES = "proj.local.local.names"
HANDLER_QN = "proj.sorted.handler.handler"
DECOY_QN = "proj.other.other.handler"
FORMAT = "proj.printer.printer.T.format"

Edges = dict[tuple[str, str, str], set[str]]


def _write(root: Path) -> None:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _set_frontend(
    root: Path, monkeypatch: pytest.MonkeyPatch, frontend: cs.GoFrontend
) -> None:
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", frontend)
    if frontend != cs.GoFrontend.GOTYPES:
        return
    from codebase_rag.parsers.go_frontend import (
        go_frontend_available,
        run_go_frontend,
    )

    if shutil.which("go") is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    if not run_go_frontend(root).call_sites:
        pytest.skip("gotypes frontend could not build in this environment")


@pytest.fixture(params=[cs.GoFrontend.TREESITTER, cs.GoFrontend.GOTYPES])
def indexed(
    request: pytest.FixtureRequest,
    temp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[gu.GraphUpdater, Edges]:
    """The run, and {(relationship, caller qn, callee qn): resolutions} for
    every CALLS and REFERENCES edge it wrote."""
    root = temp_repo / "proj"
    _write(root)
    _set_frontend(root, monkeypatch, request.param)
    ingestor = MagicMock()
    updater = create_and_run_updater(root, ingestor, skip_if_missing="go")
    edges: Edges = {}
    for rel in (cs.RelationshipType.CALLS, cs.RelationshipType.REFERENCES):
        for c in get_relationships(ingestor, rel):
            props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
            key = (str(rel), str(c.args[0][2]), str(c.args[2][2]))
            edges.setdefault(key, set()).add(str(props.get(cs.KEY_RESOLUTION)))
    return updater, edges


@pytest.fixture
def edges(indexed: tuple[gu.GraphUpdater, Edges]) -> Edges:
    return indexed[1]


CALLS = str(cs.RelationshipType.CALLS)
REFERENCES = str(cs.RelationshipType.REFERENCES)


def test_a_passed_local_named_like_its_function_is_a_guess(edges: Edges) -> None:
    # `len(authors)` and `kept := names` pass the parameter. The name trie
    # still finds the function, but only as a guess, as the call path labels
    # a call through a local (#2571).
    assert edges[(CALLS, AUTHORS, AUTHORS)] == {cs.EdgeResolution.HEURISTIC}, edges
    assert edges[(REFERENCES, NAMES, NAMES)] == {cs.EdgeResolution.HEURISTIC}, edges


def test_the_function_itself_is_still_called_exactly(edges: Edges) -> None:
    # The negative of the rule: from `Use`, where no local hides them.
    use = "proj.local.local.Use"
    assert edges[(CALLS, use, AUTHORS)] == {cs.EdgeResolution.EXACT}, edges
    assert edges[(CALLS, use, NAMES)] == {cs.EdgeResolution.EXACT}, edges


def test_a_passed_function_of_a_sibling_file_is_exact(edges: Edges) -> None:
    # `sort.Slice(xs, handler)` and `less := handler` name the package's
    # `handler` in handler.go, not other's, as the call `handler(1, 2)` does.
    use, keep = "proj.sorted.sorted.Use", "proj.sorted.sorted.Keep"
    assert edges[(CALLS, use, HANDLER_QN)] == {cs.EdgeResolution.EXACT}, edges
    assert edges[(REFERENCES, keep, HANDLER_QN)] == {cs.EdgeResolution.EXACT}, edges
    assert not any(callee == DECOY_QN for _, _, callee in edges), edges


def test_a_call_after_the_pass_keeps_its_own_verdict(edges: Edges) -> None:
    # `handler(1, 2)` in the file that passed `handler` above it: the
    # resolver's cache must not hand the call the pass's answer.
    direct = "proj.sorted.sorted.Direct"
    assert edges[(CALLS, direct, HANDLER_QN)] == {cs.EdgeResolution.EXACT}, edges


def test_a_passed_bare_name_in_a_method_is_not_the_method(edges: Edges) -> None:
    # `fmt.Println(format)` and `shown := format` inside `T.Printf` pass its
    # parameter: no edge to the method `T.format` (#3783).
    printf = "proj.printer.printer.T.Printf"
    assert not any(caller == printf for _, caller, _ in edges), edges


def test_a_passed_method_value_is_reached_through_its_selector(
    edges: Edges,
) -> None:
    # The negative of the rule: `t.format` names the method.
    bound = "proj.printer.printer.T.Bound"
    assert edges[(CALLS, bound, FORMAT)] == {cs.EdgeResolution.EXACT}, edges
    assert edges[(REFERENCES, bound, FORMAT)] == {cs.EdgeResolution.EXACT}, edges


def test_the_cache_keeps_answers_with_and_without_a_language_apart(
    indexed: tuple[gu.GraphUpdater, Edges],
) -> None:
    # A resolve that names no language skips Go's rules and guesses; the
    # same name resolved for a Go caller afterwards must not get that guess
    # back from the cache.
    resolver = indexed[0].factory.call_processor._resolver
    resolver.reset_resolution_caches()
    module = "proj.sorted.sorted"
    bare = resolver.resolve_function_call("handler", module)
    assert bare is not None and bare[1] == HANDLER_QN
    assert resolver.last_resolution == cs.EdgeResolution.HEURISTIC
    go = resolver.resolve_function_call(
        "handler", module, language=cs.SupportedLanguage.GO
    )
    assert go is not None and go[1] == HANDLER_QN
    assert resolver.last_resolution == cs.EdgeResolution.EXACT
