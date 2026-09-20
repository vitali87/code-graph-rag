"""Go composite literals are struct constructions and must emit INSTANTIATES.

`&Error{...}` and `Error{...}` are how a Go program constructs a struct. There
is no constructor call, so the call pass never saw a construction, and no Go
program produced an `INSTANTIATES` edge in any syntactic position (issue
#1642): on gin-gonic/gin every one of the 82 such edges came from the #1641
method-name collision rather than from a literal. The composite literal is the
construction site, and this pins the five forms the issue enumerates plus the
control that a method call is not one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

TYPES_GO = "package m\n\ntype Error struct {\n\tMsg string\n}\n\ntype Box[T any] struct {\n\tV T\n}\n"
SVC_GO = (
    "package m\n\n"
    'import "errors"\n\n'
    'func retPtr() *Error    { return &Error{Msg: "a"} }\n'
    'func retVal() Error     { return Error{Msg: "b"} }\n'
    'func assignPtr() *Error { e := &Error{Msg: "c"}; return e }\n'
    'func assignVal() Error  { e := Error{Msg: "d"}; return e }\n'
    'func reassign() *Error  { var e *Error; e = &Error{Msg: "e"}; return e }\n'
    "func generic() Box[int] { return Box[int]{V: 1} }\n"
    'func container() []Error { return []Error{{Msg: "f"}} }\n\n'
    'var moduleError = &Error{Msg: "module scope"}\n\n'
    "func callsMethod() string {\n"
    '\terr := errors.New("x")\n'
    "\treturn err.Error()\n"
    "}\n"
)
OTHER_GO = 'package other\n\nimport "proj/m"\n\nfunc build() *m.Error { return &m.Error{Msg: "g"} }\n'


def _index(root: Path) -> _StatefulIngestor:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.GO not in parsers:
        pytest.skip("go parser not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run()
    store.flush_all()
    return store


def _instantiates(store: _StatefulIngestor) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for _sl, src, rel, _tl, dst in store.edges:
        if rel == cs.RelationshipType.INSTANTIATES.value:
            out.setdefault(str(src).rsplit(".", 1)[-1], set()).add(str(dst))
    return out


def test_every_struct_construction_form_emits_instantiates(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "m").mkdir(parents=True)
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    (root / "m" / "types.go").write_text(TYPES_GO, encoding="utf-8")
    (root / "m" / "svc.go").write_text(SVC_GO, encoding="utf-8")
    store = _index(root)

    by_caller = _instantiates(store)
    error_qn = "proj.m.types.Error"
    for caller in ("retPtr", "retVal", "assignPtr", "assignVal", "reassign"):
        assert by_caller.get(caller) == {error_qn}, (
            f"{caller} constructs Error and emitted {by_caller.get(caller)}"
        )
    # A generic instantiation constructs its base type.
    assert by_caller.get("generic") == {"proj.m.types.Box"}, by_caller.get("generic")
    # The control: a method call is not a construction (#1641), and a slice
    # literal's element literals carry no type of their own, so `container`
    # emits nothing rather than guessing.
    assert "callsMethod" not in by_caller, by_caller.get("callsMethod")
    assert "container" not in by_caller, by_caller.get("container")
    # A package-level `var e = &Error{}` constructs too; its edge hangs off
    # the Module node (CodeRabbit, #1747).
    assert by_caller.get("svc") == {error_qn}, by_caller.get("svc")


def test_a_qualified_construction_resolves_through_the_import(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "m").mkdir(parents=True)
    (root / "other").mkdir()
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    (root / "m" / "types.go").write_text(TYPES_GO, encoding="utf-8")
    (root / "other" / "build.go").write_text(OTHER_GO, encoding="utf-8")
    store = _index(root)

    by_caller = _instantiates(store)
    assert by_caller.get("build") == {"proj.m.types.Error"}, by_caller.get("build")


def test_a_bare_name_binds_only_to_its_own_package(tmp_path: Path) -> None:
    # Go types are package-scoped. The same struct name in another package,
    # or a same-named class in another language, must not receive the edge:
    # the repo-wide simple-name fallback did exactly that, so dead-code
    # revived the wrong type and reported the constructed one dead (#1642
    # review). `a` sorts before `m`, which is the order that fallback picked.
    root = tmp_path / "proj"
    for pkg in ("a", "m"):
        (root / pkg).mkdir(parents=True)
        (root / pkg / "types.go").write_text(
            f"package {pkg}\n\ntype Error struct {{\n\tMsg string\n}}\n",
            encoding="utf-8",
        )
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    (root / "m" / "svc.go").write_text(
        'package m\n\nfunc build() *Error { return &Error{Msg: "x"} }\n',
        encoding="utf-8",
    )
    (root / "config.py").write_text("class Config:\n    pass\n", encoding="utf-8")
    (root / "m" / "cfg.go").write_text(
        "package m\n\ntype Config struct{}\n\nfunc cfg() Config { return Config{} }\n",
        encoding="utf-8",
    )
    store = _index(root)

    by_caller = _instantiates(store)
    assert by_caller.get("build") == {"proj.m.types.Error"}, by_caller.get("build")
    assert by_caller.get("cfg") == {"proj.m.cfg.Config"}, by_caller.get("cfg")
    assert not any(
        "proj.a." in dst or dst == "proj.config.Config"
        for targets in by_caller.values()
        for dst in targets
    ), by_caller


def test_a_dot_imported_struct_and_a_shadowing_local_type_resolve(
    tmp_path: Path,
) -> None:
    # Two shapes from review (#1747): a bare `Error{}` after `import . "proj/m"`
    # names m's struct, and a function-local `type Name struct{}` shadows the
    # package-level one for a literal in that file.
    root = tmp_path / "proj"
    (root / "m").mkdir(parents=True)
    (root / "dot").mkdir()
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    (root / "m" / "types.go").write_text(TYPES_GO, encoding="utf-8")
    (root / "dot" / "use.go").write_text(
        'package dot\n\nimport . "proj/m"\n\n'
        'func viaDot() *Error { return &Error{Msg: "d"} }\n',
        encoding="utf-8",
    )
    (root / "m" / "shadow.go").write_text(
        "package m\n\ntype Local struct{}\n\nfunc pkgLevel() Local { return Local{} }\n",
        encoding="utf-8",
    )
    (root / "m" / "inner.go").write_text(
        "package m\n\nfunc withLocal() any {\n\ttype Local struct{ V int }\n"
        "\treturn Local{V: 1}\n}\n",
        encoding="utf-8",
    )
    store = _index(root)

    by_caller = _instantiates(store)
    assert by_caller.get("viaDot") == {"proj.m.types.Error"}, by_caller.get("viaDot")
    # The package-level literal in shadow.go names shadow.go's Local; the
    # literal inside withLocal names inner.go's local Local, not shadow.go's.
    assert by_caller.get("pkgLevel") == {"proj.m.shadow.Local"}, by_caller.get(
        "pkgLevel"
    )
    targets = by_caller.get("withLocal")
    assert targets is not None, by_caller
    assert len(targets) == 1, targets
    assert next(iter(targets)).startswith("proj.m.inner."), targets


def test_a_local_type_shadows_the_package_one_within_its_function(
    tmp_path: Path,
) -> None:
    # Both declarations in ONE file register under one qn, the local one as
    # a `@line` variant, and every literal fanned out to both. Go scoping:
    # the literal inside `withLocal` names the local type, the one in
    # `pkgLevel` the package-level type (CodeRabbit, #1747).
    root = tmp_path / "proj"
    (root / "m").mkdir(parents=True)
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    (root / "m" / "one.go").write_text(
        "package m\n\n"
        "type Local struct{}\n\n"
        "func pkgLevel() Local { return Local{} }\n\n"
        "func withLocal() any {\n"
        "\ttype Local struct{ V int }\n"
        "\treturn Local{V: 1}\n"
        "}\n",
        encoding="utf-8",
    )
    store = _index(root)

    by_caller = _instantiates(store)
    assert by_caller.get("pkgLevel") == {"proj.m.one.Local"}, by_caller
    assert by_caller.get("withLocal") == {f"proj.m.one.Local{cs.DUP_QN_MARKER}8"}, (
        by_caller
    )


def test_a_test_packages_type_is_invisible_to_production_code(tmp_path: Path) -> None:
    # `package m_test` shares the directory with `package m` and is a
    # different package; any `_test.go` is compiled only under `go test`.
    # A production literal in a THIRD file saw both `Error` declarations and
    # the ambiguity rule emitted nothing (CodeRabbit, #1747). The external
    # test's own literal binds to its own `Error`; an internal test file
    # (`package m` in `_test.go`) sees the production type.
    root = tmp_path / "proj"
    (root / "m").mkdir(parents=True)
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    (root / "m" / "types.go").write_text(TYPES_GO, encoding="utf-8")
    (root / "m" / "svc.go").write_text(
        'package m\n\nfunc build() Error { return Error{Msg: "a"} }\n',
        encoding="utf-8",
    )
    (root / "m" / "m_test.go").write_text(
        "package m_test\n\ntype Error struct{ Other int }\n\n"
        "func external() Error { return Error{Other: 1} }\n",
        encoding="utf-8",
    )
    (root / "m" / "internal_test.go").write_text(
        'package m\n\nfunc internal() Error { return Error{Msg: "t"} }\n',
        encoding="utf-8",
    )
    store = _index(root)

    by_caller = _instantiates(store)
    assert by_caller.get("build") == {"proj.m.types.Error"}, by_caller
    assert by_caller.get("external") == {"proj.m.m_test.Error"}, by_caller
    assert by_caller.get("internal") == {"proj.m.types.Error"}, by_caller


def test_a_local_type_is_visible_from_its_declaration_to_its_block_end(
    tmp_path: Path,
) -> None:
    # Go scoping, not function scoping: a literal BEFORE the local
    # declaration names the package type, and so does one outside the nested
    # block that holds the declaration (CodeRabbit and #1747 review). Each
    # function here constructs both, so its edge set must hold the package
    # qn and its own local variant; a function-wide span would give only the
    # local one.
    root = tmp_path / "proj"
    (root / "m").mkdir(parents=True)
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    (root / "m" / "one.go").write_text(
        "package m\n\n"
        "type Local struct{}\n\n"
        "func before() Local {\n"
        "\tx := Local{}\n"
        "\ttype Local struct{ V int }\n"
        "\t_ = Local{V: 1}\n"
        "\treturn x\n"
        "}\n\n"
        "func nested() Local {\n"
        "\t{\n"
        "\t\ttype Local struct{ V int }\n"
        "\t\t_ = Local{V: 1}\n"
        "\t}\n"
        "\treturn Local{}\n"
        "}\n",
        encoding="utf-8",
    )
    store = _index(root)

    by_caller = _instantiates(store)
    package_qn = "proj.m.one.Local"
    assert by_caller.get("before") == {
        package_qn,
        f"{package_qn}{cs.DUP_QN_MARKER}7",
    }, by_caller
    assert by_caller.get("nested") == {
        package_qn,
        f"{package_qn}{cs.DUP_QN_MARKER}14",
    }, by_caller


def test_a_local_type_scope_respects_columns_and_case_clauses(tmp_path: Path) -> None:
    # Two more shapes of the same rule (#1747 review). On one line, a literal
    # BEFORE the declaration names the package type, which needs column
    # order and not just rows. A `case` clause's body is an implicit block,
    # so a type declared under `case 0:` is invisible to `default:`.
    root = tmp_path / "proj"
    (root / "m").mkdir(parents=True)
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    (root / "m" / "one.go").write_text(
        "package m\n\n"
        "type Local struct{}\n\n"
        "func sameLine() any { x := Local{}; type Local struct{ V int }; "
        "y := Local{V: 1}; return []any{x, y} }\n\n"
        "func inSwitch(n int) any {\n"
        "\tswitch n {\n"
        "\tcase 0:\n"
        "\t\ttype Local struct{ V int }\n"
        "\t\treturn Local{V: 1}\n"
        "\tdefault:\n"
        "\t\treturn Local{}\n"
        "\t}\n"
        "}\n",
        encoding="utf-8",
    )
    store = _index(root)

    by_caller = _instantiates(store)
    package_qn = "proj.m.one.Local"
    assert by_caller.get("sameLine") == {
        package_qn,
        f"{package_qn}{cs.DUP_QN_MARKER}5",
    }, by_caller
    assert by_caller.get("inSwitch") == {
        package_qn,
        f"{package_qn}{cs.DUP_QN_MARKER}10",
    }, by_caller
