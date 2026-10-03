"""Issue #2571: Go same-package resolution on the tree-sitter path.

Every file in a directory that shares one `package` clause shares one
namespace, so an unqualified `NewRouter()` in `app.go` naming `func
NewRouter()` in `router.go` is as certain as a same-file call. cgr keys each
file as its own module (`pkg.file.Name`), so the call fell to the bare-name
trie and was labelled `heuristic`; and a bare type name (`var m Mux`) could
bind a same-named type of ANOTHER package, labelled `exact`.

The go/types frontend is pinned off: these are the tree-sitter answers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater

GO_MOD = "module example.com/proj\n\ngo 1.24\n"

ROUTER = (
    "package gotest\n"
    "\n"
    "type Mux struct{ routes []string }\n"
    "\n"
    "func NewRouter() *Mux { return &Mux{} }\n"
    "\n"
    "func (m *Mux) Get(p string) { m.routes = append(m.routes, p) }\n"
    "\n"
    "func (m *Mux) Reset() { m.routes = nil }\n"
)
APP = (
    "package gotest\n"
    "\n"
    "func Build() *Mux {\n"
    "\tr := NewRouter()\n"
    '\tr.Get("/a")\n'
    "\treturn r\n"
    "}\n"
)
ROUTER_TEST = (
    "package gotest\n"
    "\n"
    'import "testing"\n'
    "\n"
    "func TestRouter(t *testing.T) {\n"
    "\tr := NewRouter()\n"
    '\tr.Get("/b")\n'
    "\t_ = t\n"
    "}\n"
)
EXTERNAL_TEST = (
    "package gotest_test\n"
    "\n"
    "import (\n"
    '\t"testing"\n'
    "\n"
    '\t"example.com/proj/gotest"\n'
    ")\n"
    "\n"
    "func TestExternal(t *testing.T) {\n"
    "\tr := gotest.NewRouter()\n"
    '\tr.Get("/c")\n'
    "\t_ = t\n"
    "}\n"
)
# The same names in ANOTHER package (another directory): never in scope for
# an unqualified name in `gotest`, and sorted ahead of it (`aaa` < `gotest`),
# so a name-only pick lands here first.
OTHER_PACKAGE = (
    "package aaa\n"
    "\n"
    "type Mux struct{}\n"
    "\n"
    "func NewRouter() *Mux { return &Mux{} }\n"
    "\n"
    "func (m *Mux) Get(p string) {}\n"
    "\n"
    "func Only() {}\n"
)


@pytest.fixture(autouse=True)
def _tree_sitter_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _calls(root: Path, files: dict[str, str]) -> dict[tuple[str, str], set[str]]:
    """{(caller qn, callee qn): resolutions} for every CALLS edge."""
    _write(root, files)
    ingestor = MagicMock()
    create_and_run_updater(root, ingestor)
    edges: dict[tuple[str, str], set[str]] = {}
    for c in ingestor.ensure_relationship_batch.call_args_list:
        if c.args[1] != cs.RelationshipType.CALLS:
            continue
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        key = (str(c.args[0][2]), str(c.args[2][2]))
        edges.setdefault(key, set()).add(str(props.get(cs.KEY_RESOLUTION)))
    return edges


def _issue_repo(temp_repo: Path) -> dict[tuple[str, str], set[str]]:
    return _calls(
        temp_repo / "proj",
        {
            "go.mod": GO_MOD,
            "gotest/router.go": ROUTER,
            "gotest/app.go": APP,
            "gotest/router_test.go": ROUTER_TEST,
            "gotest/external_test.go": EXTERNAL_TEST,
        },
    )


# --- red: the rows of the issue's table -----------------------------------------


def test_a_bare_call_into_a_sibling_file_of_the_package_is_exact(
    temp_repo: Path,
) -> None:
    calls = _issue_repo(temp_repo)

    assert calls[("proj.gotest.app.Build", "proj.gotest.router.NewRouter")] == {
        cs.EdgeResolution.EXACT
    }


def test_a_test_file_of_the_package_calls_its_functions_exactly(
    temp_repo: Path,
) -> None:
    calls = _issue_repo(temp_repo)

    assert calls[
        ("proj.gotest.router_test.TestRouter", "proj.gotest.router.NewRouter")
    ] == {cs.EdgeResolution.EXACT}


def test_a_package_qualified_constructor_types_its_local(temp_repo: Path) -> None:
    # `r := gotest.NewRouter(); r.Get(...)` in `package gotest_test`: the
    # constructor's `*Mux` is `gotest`'s Mux, so `r.Get` is its method.
    calls = _issue_repo(temp_repo)

    assert calls[
        ("proj.gotest.external_test.TestExternal", "proj.gotest.router.Mux.Get")
    ] == {cs.EdgeResolution.EXACT}


def test_the_rows_that_were_already_exact_stay_exact(temp_repo: Path) -> None:
    # Negative: the issue's exact rows do not move.
    calls = _issue_repo(temp_repo)

    exact = {cs.EdgeResolution.EXACT}
    assert calls[("proj.gotest.app.Build", "proj.gotest.router.Mux.Get")] == exact
    assert (
        calls[("proj.gotest.router_test.TestRouter", "proj.gotest.router.Mux.Get")]
        == exact
    )
    assert (
        calls[
            ("proj.gotest.external_test.TestExternal", "proj.gotest.router.NewRouter")
        ]
        == exact
    )


def test_a_method_on_a_package_type_declared_in_another_file(
    temp_repo: Path,
) -> None:
    # `var m Mux` names gotest's Mux; `aaa.Mux` has the same method and sorts
    # first, and the name-only type lookup used to pick it, labelled exact.
    root = temp_repo / "proj"
    calls = _calls(
        root,
        {
            "go.mod": "module example.com/proj\n\ngo 1.24\n",
            "aaa/mux.go": OTHER_PACKAGE,
            "gotest/router.go": ROUTER,
            "gotest/app.go": (
                "package gotest\n"
                "\n"
                "func Typed(q *Mux) {\n"
                "\tvar m Mux\n"
                '\tm.Get("/m")\n'
                '\tq.Get("/q")\n'
                "\tr := NewRouter()\n"
                '\tr.Get("/r")\n'
                "}\n"
            ),
        },
    )

    assert calls[("proj.gotest.app.Typed", "proj.gotest.router.Mux.Get")] == {
        cs.EdgeResolution.EXACT
    }
    assert ("proj.gotest.app.Typed", "proj.aaa.mux.Mux.Get") not in calls
    assert calls[("proj.gotest.app.Typed", "proj.gotest.router.NewRouter")] == {
        cs.EdgeResolution.EXACT
    }
    assert ("proj.gotest.app.Typed", "proj.aaa.mux.NewRouter") not in calls


# --- negative: what stays out of the package's scope ----------------------------


def test_a_same_named_function_in_another_package_never_becomes_exact(
    temp_repo: Path,
) -> None:
    # `Only` exists only in package aaa. Unqualified, it cannot name that
    # function, so the same-package lookup must not answer for it.
    root = temp_repo / "proj"
    calls = _calls(
        root,
        {
            "aaa/mux.go": OTHER_PACKAGE,
            "gotest/app.go": "package gotest\n\nfunc Build() { Only() }\n",
        },
    )

    assert cs.EdgeResolution.EXACT not in calls.get(
        ("proj.gotest.app.Build", "proj.aaa.mux.Only"), set()
    )


def test_an_external_test_package_does_not_see_the_package_unqualified(
    temp_repo: Path,
) -> None:
    # `package gotest_test` is a different package in the same directory: its
    # bare `helper()` is its own helper, never gotest's (unexported or not),
    # and gotest's own test file sees gotest's helper, not the external one.
    root = temp_repo / "gotest"
    calls = _calls(
        root,
        {
            "go.mod": GO_MOD,
            "util.go": "package gotest\n\nfunc helper() int { return 1 }\n",
            "helpers_test.go": "package gotest_test\n\nfunc helper() int { return 2 }\n",
            "external_test.go": (
                "package gotest_test\n\nfunc TestExternal() { helper() }\n"
            ),
            "internal_test.go": "package gotest\n\nfunc TestInternal() { helper() }\n",
        },
    )

    assert calls[
        ("gotest.external_test.TestExternal", "gotest.helpers_test.helper")
    ] == {cs.EdgeResolution.EXACT}
    assert ("gotest.external_test.TestExternal", "gotest.util.helper") not in calls
    assert calls[("gotest.internal_test.TestInternal", "gotest.util.helper")] == {
        cs.EdgeResolution.EXACT
    }
    assert (
        "gotest.internal_test.TestInternal",
        "gotest.helpers_test.helper",
    ) not in calls


def test_production_code_does_not_see_a_test_file(temp_repo: Path) -> None:
    # A `_test.go` file is compiled only under `go test`: a production bare
    # call never binds a function declared there as exact.
    root = temp_repo / "gotest"
    calls = _calls(
        root,
        {
            "go.mod": GO_MOD,
            "fixture_test.go": "package gotest\n\nfunc fixture() int { return 1 }\n",
            "app.go": "package gotest\n\nfunc Build() { fixture() }\n",
        },
    )

    assert cs.EdgeResolution.EXACT not in calls.get(
        ("gotest.app.Build", "gotest.fixture_test.fixture"), set()
    )


def test_a_file_with_another_package_clause_is_not_a_candidate(
    temp_repo: Path,
) -> None:
    # `//go:build ignore` generators declare `package main` beside the
    # library: another package, so its `helper` neither competes with the
    # library's (the call stays exact) nor receives a variant edge.
    root = temp_repo / "gotest"
    calls = _calls(
        root,
        {
            "go.mod": GO_MOD,
            "gen.go": (
                "//go:build ignore\n\npackage main\n\nfunc helper() int { return 0 }\n"
            ),
            "util.go": "package gotest\n\nfunc helper() int { return 1 }\n",
            "app.go": "package gotest\n\nfunc Build() { helper() }\n",
        },
    )

    assert calls[("gotest.app.Build", "gotest.util.helper")] == {
        cs.EdgeResolution.EXACT
    }
    assert ("gotest.app.Build", "gotest.gen.helper") not in calls


def test_two_build_tag_variants_stay_heuristic(temp_repo: Path) -> None:
    # gin's `validate` shape: two files of one package declare the same name
    # under mutually exclusive build tags. The call genuinely has two
    # candidates, so it is not exact, and both variants keep their edge.
    root = temp_repo / "gotest"
    calls = _calls(
        root,
        {
            "go.mod": GO_MOD,
            "v_msgpack.go": (
                "//go:build !nomsgpack\n\npackage gotest\n\n"
                "func validate() int { return 1 }\n"
            ),
            "v_nomsgpack.go": (
                "//go:build nomsgpack\n\npackage gotest\n\n"
                "func validate() int { return 2 }\n"
            ),
            "app.go": "package gotest\n\nfunc Build() { validate() }\n",
        },
    )

    msgpack = calls[("gotest.app.Build", "gotest.v_msgpack.validate")]
    nomsgpack = calls[("gotest.app.Build", "gotest.v_nomsgpack.validate")]
    assert cs.EdgeResolution.EXACT not in msgpack | nomsgpack


def test_a_bare_call_never_binds_a_method_of_the_package(temp_repo: Path) -> None:
    # `Reset()` unqualified names a package-level function, never the method
    # `(*Mux).Reset`, which needs a receiver.
    root = temp_repo / "gotest"
    calls = _calls(
        root,
        {
            "go.mod": GO_MOD,
            "router.go": ROUTER,
            "app.go": "package gotest\n\nfunc Build() { Reset() }\n",
        },
    )

    assert cs.EdgeResolution.EXACT not in calls.get(
        ("gotest.app.Build", "gotest.router.Mux.Reset"), set()
    )


def test_a_same_file_function_still_wins(temp_repo: Path) -> None:
    # Negative: the file's own function is the same-module answer, as before.
    root = temp_repo / "gotest"
    calls = _calls(
        root,
        {
            "go.mod": GO_MOD,
            "app.go": (
                "package gotest\n\nfunc helper() int { return 1 }\n\n"
                "func Build() { helper() }\n"
            ),
            "b.go": "package gotest\n\nfunc other() {}\n",
        },
    )

    assert calls[("gotest.app.Build", "gotest.app.helper")] == {cs.EdgeResolution.EXACT}


def test_a_parameter_or_local_named_like_the_function_is_not_it(
    temp_repo: Path,
) -> None:
    # `helper()` on a `helper func() int` parameter or a `helper := func...`
    # local calls that value. The package's `helper`, in a sibling file or in
    # the caller's own file, is at best a guess.
    root = temp_repo / "gotest"
    calls = _calls(
        root,
        {
            "go.mod": GO_MOD,
            "util.go": "package gotest\n\nfunc helper() int { return 1 }\n",
            "app.go": (
                "package gotest\n"
                "\n"
                "func local() int { return 2 }\n"
                "\n"
                "func Param(helper func() int) int { return helper() }\n"
                "\n"
                "func Closure() int {\n"
                "\tlocal := func() int { return 3 }\n"
                "\treturn local()\n"
                "}\n"
                "\n"
                "func Plain() int { return helper() + local() }\n"
            ),
        },
    )

    assert cs.EdgeResolution.EXACT not in calls.get(
        ("gotest.app.Param", "gotest.util.helper"), set()
    )
    assert cs.EdgeResolution.EXACT not in calls.get(
        ("gotest.app.Closure", "gotest.app.local"), set()
    )
    # A caller that binds neither name still gets both exactly.
    exact = {cs.EdgeResolution.EXACT}
    assert calls[("gotest.app.Plain", "gotest.util.helper")] == exact
    assert calls[("gotest.app.Plain", "gotest.app.local")] == exact


def test_a_type_an_imported_package_also_declares_keeps_its_old_lookup(
    temp_repo: Path,
) -> None:
    # The Go type walker reduces `q *aaa.Mux` to `Mux`, so in a file that
    # imports `aaa` the bare name may be aaa's: the package-first rule must
    # not claim it for gotest's own Mux.
    root = temp_repo / "proj"
    calls = _calls(
        root,
        {
            "go.mod": "module example.com/proj\n\ngo 1.24\n",
            "aaa/mux.go": OTHER_PACKAGE,
            "gotest/router.go": ROUTER,
            "gotest/wrap.go": (
                "package gotest\n"
                "\n"
                'import "example.com/proj/aaa"\n'
                "\n"
                'func Wrap(q *aaa.Mux) { q.Get("/q") }\n'
            ),
        },
    )

    assert ("proj.gotest.wrap.Wrap", "proj.gotest.router.Mux.Get") not in calls
    assert ("proj.gotest.wrap.Wrap", "proj.aaa.mux.Mux.Get") in calls


def test_an_incremental_run_sees_unchanged_siblings_by_their_path(
    temp_repo: Path,
) -> None:
    # Only the edited file is re-parsed; its siblings come back from the
    # graph with their paths but no `package` clause. The unchanged
    # production sibling still answers exactly, and the unchanged external
    # test file (a `_test.go`, by its path) still stays out.
    from evals.cgr_graph import _StatefulIngestor

    root = temp_repo / "proj"
    _write(
        root,
        {
            "p/router.go": ROUTER.replace("package gotest", "package p"),
            "p/helpers_test.go": "package p_test\n\nfunc NewRouter() int { return 1 }\n",
            "p/app.go": "package p\n\nfunc Build() { NewRouter() }\n",
        },
    )
    store = _StatefulIngestor()
    parsers, queries = load_parsers()

    def run(force: bool) -> None:
        gu.GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        ).run(force=force)

    run(True)
    (root / "p/app.go").write_text(
        "package p\n\nfunc Build() {\n\tNewRouter()\n}\n", encoding="utf-8"
    )
    run(False)

    calls = {
        (key[1], key[4]): props.get(cs.KEY_RESOLUTION)
        for key, props in store.edge_props.items()
        if key[2] == cs.RelationshipType.CALLS
    }
    assert calls == {
        ("proj.p.app.Build", "proj.p.router.NewRouter"): cs.EdgeResolution.EXACT
    }


# --- #2616 review: a local shadows the package's name only where it is in
# scope, and an incremental run keeps the `package` clauses apart. ---

_SCOPED_LOCAL = (
    "package gotest\n"
    "\n"
    "func Before() int {\n"
    "\tn := helper()\n"
    "\t{\n"
    "\t\thelper := 2\n"
    "\t\tn += helper\n"
    "\t}\n"
    "\treturn n\n"
    "}\n"
    "\n"
    "func Inside() int {\n"
    "\tif ok := true; ok {\n"
    "\t\thelper := func() int { return 3 }\n"
    "\t\treturn helper()\n"
    "\t}\n"
    "\treturn 0\n"
    "}\n"
    "\n"
    "func After() int {\n"
    "\ttotal := 0\n"
    "\tfor _, helper := range []int{1} {\n"
    "\t\ttotal += helper * 2\n"
    "\t}\n"
    "\treturn total + helper()\n"
    "}\n"
    "\n"
    "func Self() int {\n"
    "\thelper := helper()\n"
    "\treturn helper\n"
    "}\n"
)


def _scoped_calls(temp_repo: Path) -> dict[tuple[str, str], set[str]]:
    return _calls(
        temp_repo / "gotest",
        {
            "go.mod": GO_MOD,
            "util.go": "package gotest\n\nfunc helper() int { return 1 }\n",
            "app.go": _SCOPED_LOCAL,
        },
    )


def test_a_call_before_a_later_inner_local_stays_exact(temp_repo: Path) -> None:
    calls = _scoped_calls(temp_repo)

    assert calls[("gotest.app.Before", "gotest.util.helper")] == {
        cs.EdgeResolution.EXACT
    }


def test_a_call_after_the_local_block_closes_stays_exact(temp_repo: Path) -> None:
    # `for _, helper := range` scopes `helper` to the loop; the call after it
    # names the package function again. So does `helper := helper()`, whose
    # right-hand side is read before the local exists.
    calls = _scoped_calls(temp_repo)

    exact = {cs.EdgeResolution.EXACT}
    assert calls[("gotest.app.After", "gotest.util.helper")] == exact
    assert calls[("gotest.app.Self", "gotest.util.helper")] == exact


def test_a_call_inside_the_locals_scope_is_still_not_the_package_function(
    temp_repo: Path,
) -> None:
    # Negative: within its block the local is what `helper()` calls.
    calls = _scoped_calls(temp_repo)

    assert cs.EdgeResolution.EXACT not in calls.get(
        ("gotest.app.Inside", "gotest.util.helper"), set()
    )


def _incremental_calls(root: Path, files: dict[str, str], edit: tuple[str, str]):
    from evals.cgr_graph import _StatefulIngestor

    _write(root, files)
    store = _StatefulIngestor()
    parsers, queries = load_parsers()

    def run(force: bool) -> None:
        gu.GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        ).run(force=force)

    run(True)
    (root / edit[0]).write_text(edit[1], encoding="utf-8")
    run(False)
    return {
        (key[1], key[4]): props.get(cs.KEY_RESOLUTION)
        for key, props in store.edge_props.items()
        if key[2] == cs.RelationshipType.CALLS
    }


def test_an_incremental_run_keeps_another_package_clause_out(
    temp_repo: Path,
) -> None:
    # The unchanged `//go:build ignore` generator comes back from the graph
    # without its `package main` clause; it must still not compete with the
    # library's `helper` once only the caller is re-parsed.
    calls = _incremental_calls(
        temp_repo / "proj",
        {
            "p/gen.go": (
                "//go:build ignore\n\npackage main\n\nfunc helper() int { return 0 }\n"
            ),
            "p/util.go": "package p\n\nfunc helper() int { return 1 }\n",
            "p/app.go": "package p\n\nfunc Build() { helper() }\n",
        },
        ("p/app.go", "package p\n\nfunc Build() {\n\thelper()\n}\n"),
    )

    assert calls == {
        ("proj.p.app.Build", "proj.p.util.helper"): cs.EdgeResolution.EXACT
    }


def test_an_incremental_run_keeps_build_tag_variants_heuristic(
    temp_repo: Path,
) -> None:
    # Negative: two unchanged files of the SAME clause are build-tag
    # variants, and stay two heuristic candidates after an incremental run.
    calls = _incremental_calls(
        temp_repo / "proj",
        {
            "p/v_a.go": "//go:build a\n\npackage p\n\nfunc helper() int { return 1 }\n",
            "p/v_b.go": "//go:build !a\n\npackage p\n\nfunc helper() int { return 2 }\n",
            "p/app.go": "package p\n\nfunc Build() { helper() }\n",
        },
        ("p/app.go", "package p\n\nfunc Build() {\n\thelper()\n}\n"),
    )

    assert set(calls) == {
        ("proj.p.app.Build", "proj.p.v_a.helper"),
        ("proj.p.app.Build", "proj.p.v_b.helper"),
    }
    assert cs.EdgeResolution.EXACT not in set(calls.values())


# --- #2616 review: a file's stem can hold dots (`helper.gen.go`, `y.pb.go`),
# so the qn segments below the package are not one per file. The file's
# directory decides its package, not the depth of its qn. ---

_DOTTED_APP = (
    "package pkg\n"
    "\n"
    "func Run() {\n"
    "\tHelper()\n"
    "\tFromPb()\n"
    "\tPlain()\n"
    "\tvar m Mux\n"
    "\tm.Get()\n"
    "}\n"
)
_DOTTED_TEST = (
    "package pkg\n"
    "\n"
    'import "testing"\n'
    "\n"
    "func TestRun(t *testing.T) {\n"
    "\tHelper()\n"
    "\tFromPb()\n"
    "\t_ = t\n"
    "}\n"
)
# A sub-package declaring every name again. Its qns sort ahead of the
# package's own (`aaa` < `helper`, `types`, `y`) at the same import
# distance, so a name-only pick lands here first.
_SUB_PACKAGE = (
    "package aaa\n"
    "\n"
    "type Mux struct{}\n"
    "\n"
    "func (m *Mux) Get() {}\n"
    "\n"
    "func Helper() {}\n"
    "\n"
    "func FromPb() {}\n"
    "\n"
    "func Plain() {}\n"
)
_DOTTED_FILES = {
    "go.mod": GO_MOD,
    "pkg/a.go": _DOTTED_APP,
    "pkg/a_test.go": _DOTTED_TEST,
    "pkg/helper.gen.go": "package pkg\n\nfunc Helper() {}\n",
    "pkg/y.pb.go": "package pkg\n\nfunc FromPb() {}\n\nfunc PbCaller() {\n\tPlain()\n}\n",
    "pkg/types.gen.go": "package pkg\n\ntype Mux struct{}\n\nfunc (m *Mux) Get() {}\n",
    "pkg/plain.go": "package pkg\n\nfunc Plain() {}\n",
    "pkg/aaa/x.go": _SUB_PACKAGE,
}


def test_a_call_into_a_file_with_a_dotted_stem_is_exact(temp_repo: Path) -> None:
    calls = _calls(temp_repo / "proj", _DOTTED_FILES)

    exact = {cs.EdgeResolution.EXACT}
    assert calls[("proj.pkg.a.Run", "proj.pkg.helper.gen.Helper")] == exact
    assert calls[("proj.pkg.a.Run", "proj.pkg.y.pb.FromPb")] == exact
    assert ("proj.pkg.a.Run", "proj.pkg.aaa.x.Helper") not in calls
    assert ("proj.pkg.a.Run", "proj.pkg.aaa.x.FromPb") not in calls


def test_a_test_file_calls_into_a_file_with_a_dotted_stem_exactly(
    temp_repo: Path,
) -> None:
    calls = _calls(temp_repo / "proj", _DOTTED_FILES)

    exact = {cs.EdgeResolution.EXACT}
    assert calls[("proj.pkg.a_test.TestRun", "proj.pkg.helper.gen.Helper")] == exact
    assert calls[("proj.pkg.a_test.TestRun", "proj.pkg.y.pb.FromPb")] == exact
    assert ("proj.pkg.a_test.TestRun", "proj.pkg.aaa.x.Helper") not in calls


def test_a_file_with_a_dotted_stem_calls_its_package_exactly(
    temp_repo: Path,
) -> None:
    calls = _calls(temp_repo / "proj", _DOTTED_FILES)

    assert calls[("proj.pkg.y.pb.PbCaller", "proj.pkg.plain.Plain")] == {
        cs.EdgeResolution.EXACT
    }


def test_a_type_declared_in_a_file_with_a_dotted_stem_is_the_packages(
    temp_repo: Path,
) -> None:
    calls = _calls(temp_repo / "proj", _DOTTED_FILES)

    assert calls[("proj.pkg.a.Run", "proj.pkg.types.gen.Mux.Get")] == {
        cs.EdgeResolution.EXACT
    }
    assert ("proj.pkg.a.Run", "proj.pkg.aaa.x.Mux.Get") not in calls


def test_a_plain_file_still_calls_its_package_exactly(temp_repo: Path) -> None:
    # Negative: the ordinary `plain.go` answers exactly as before, beside the
    # sub-package's `Plain`.
    calls = _calls(temp_repo / "proj", _DOTTED_FILES)

    assert calls[("proj.pkg.a.Run", "proj.pkg.plain.Plain")] == {
        cs.EdgeResolution.EXACT
    }
    assert ("proj.pkg.a.Run", "proj.pkg.aaa.x.Plain") not in calls


def test_a_sub_package_is_not_the_package_whatever_its_qn(temp_repo: Path) -> None:
    # Negative: `pkg/helper/gen.go` (package `helper`) spells the same qn
    # prefix as a `pkg/helper.gen.go` would, `proj.pkg.helper.gen`. It is a
    # sub-package, so `Only()` in package `pkg` cannot name it unqualified.
    calls = _calls(
        temp_repo / "proj",
        {
            "go.mod": GO_MOD,
            "pkg/a.go": "package pkg\n\nfunc Run() {\n\tOnly()\n\tDeep()\n}\n",
            "pkg/helper/gen.go": "package helper\n\nfunc Only() {}\n",
            "pkg/aaa/x.go": "package aaa\n\nfunc Deep() {}\n",
        },
    )

    assert cs.EdgeResolution.EXACT not in calls.get(
        ("proj.pkg.a.Run", "proj.pkg.helper.gen.Only"), set()
    )
    assert cs.EdgeResolution.EXACT not in calls.get(
        ("proj.pkg.a.Run", "proj.pkg.aaa.x.Deep"), set()
    )


def test_an_incremental_run_finds_an_unchanged_file_with_a_dotted_stem(
    temp_repo: Path,
) -> None:
    # The unchanged `helper.gen.go` is known only by the path its definition
    # was rehydrated with; that path's directory still makes it the package's.
    calls = _incremental_calls(
        temp_repo / "proj",
        {
            "pkg/helper.gen.go": "package pkg\n\nfunc Helper() {}\n",
            "pkg/aaa/x.go": _SUB_PACKAGE,
            "pkg/a.go": "package pkg\n\nfunc Run() { Helper() }\n",
        },
        ("pkg/a.go", "package pkg\n\nfunc Run() {\n\tHelper()\n}\n"),
    )

    assert calls == {
        ("proj.pkg.a.Run", "proj.pkg.helper.gen.Helper"): cs.EdgeResolution.EXACT
    }
