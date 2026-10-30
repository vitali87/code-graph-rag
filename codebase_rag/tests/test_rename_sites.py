"""`rename_sites.SiteCollector`: the sites of one symbol a rename rewrites
(issue #1532), read from graphs indexed from real snippets and checked
against the tree. Pinned directly: the definition's name token and each
call, reference and import site, the sites recorded unlocatable instead of
guessed at (a missing file, a position the file no longer has, a stale call,
a base-class edge with no position, a class run through a parameter), the
links of a fluent chain and a JSX closing tag collected beside their row,
and the JS/TS import bindings that keep, guess at or refuse a site."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.imports import ImportSite
from codebase_rag.editing.rename_sites import SiteCollector
from codebase_rag.editing.rename_spec import (
    CHAIN,
    SITELESS,
    STRUCTURAL,
    RenameRefused,
    RenameSite,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, ResultRow
from evals.cgr_graph import StatefulIngestor

pytestmark = pytest.mark.usefixtures("fingerprint_inputs_once_per_session")

PROJECT = "sites"
EXACT = cs.EdgeResolution.EXACT
CALL = cs.RenameSiteKind.CALL
REFERENCE = cs.RenameSiteKind.REFERENCE
UNLOCATABLE = cs.RenameSiteKind.UNLOCATABLE


def _collector(root: Path, files: dict[str, str]) -> SiteCollector:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)

    def fetch_all(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        return store.fetch_all(query, None if params is None else dict(params))

    return SiteCollector(fetch_all, PROJECT, root)


APP = (
    "from pkg.util import helper as h\n"
    "from pkg import helper\n"
    "import pkg.util\n"
    "\n"
    "\n"
    "def run():\n"
    "    return h(1, 2) + pkg.util.helper(3, 4) + helper(5, 6)\n"
)
PY_FILES = {
    "pkg/__init__.py": 'from pkg.util import helper\n\n__all__ = ["helper"]\n',
    "pkg/util.py": (
        "def helper(a, b):\n    return a + b\n\n\n"
        "class Base:\n    def area(self):\n        return 0\n\n\n"
        "class Circle(Base):\n    def area(self):\n        return 3\n"
    ),
    "pkg/app.py": APP,
}
HELPER = f"{PROJECT}.pkg.util.helper"
RUN = f"{PROJECT}.pkg.app.run"


def test_a_function_has_its_def_token_and_each_call_naming_it(
    temp_repo: Path,
) -> None:
    collector = _collector(temp_repo, PY_FILES)

    sites, unlocatable, old_name, label = collector.collect(HELPER)

    # `h(1, 2)` spells the alias, which keeps binding: nothing to rewrite.
    # `helper(5, 6)` binds through the package's re-export, a guess.
    assert sites == [
        RenameSite(cs.RenameSiteKind.DEFINITION, "pkg/util.py", 1, 4, HELPER, EXACT),
        RenameSite(CALL, "pkg/app.py", 7, 30, RUN, EXACT.value),
        RenameSite(CALL, "pkg/app.py", 7, 45, RUN, cs.EdgeResolution.HEURISTIC.value),
    ]
    assert unlocatable == []
    assert (old_name, label) == ("helper", cs.NodeLabel.FUNCTION)


def test_a_definition_only_a_guess_reaches_is_a_guessed_site(temp_repo: Path) -> None:
    collector = _collector(temp_repo, PY_FILES)

    sites, *_ = collector.collect(HELPER, guessed=frozenset({HELPER}))

    assert sites[0].kind == cs.RenameSiteKind.DEFINITION
    assert sites[0].resolution == cs.EdgeResolution.HEURISTIC


def test_a_name_the_graph_does_not_hold_refuses(temp_repo: Path) -> None:
    collector = _collector(temp_repo, PY_FILES)
    missing = f"{PROJECT}.pkg.util.nothing"

    with pytest.raises(RenameRefused) as refused:
        collector.collect(missing)

    assert str(refused.value) == cs.RENAME_UNKNOWN.format(qn=missing)
    assert (refused.value.ambiguous, refused.value.unlocatable) == ([], [])


def test_a_definition_whose_file_is_gone_refuses_rather_than_raising(
    temp_repo: Path,
) -> None:
    # A stale index naming a deleted file is the documented refusal, which
    # the MCP handler turns into a payload; a PatcherError would escape it.
    collector = _collector(temp_repo, PY_FILES)
    (temp_repo / "pkg" / "util.py").unlink()

    with pytest.raises(RenameRefused) as refused:
        collector.collect(HELPER)

    assert str(refused.value).startswith(
        cs.RENAME_DEFINITION_UNREADABLE.split("{error}")[0].format(
            qn=HELPER, path="pkg/util.py"
        )
    )


def test_a_definition_no_longer_spelled_in_its_span_refuses(temp_repo: Path) -> None:
    collector = _collector(temp_repo, PY_FILES)
    (temp_repo / "pkg" / "util.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(RenameRefused) as refused:
        collector.collect(HELPER)

    assert str(refused.value) == cs.RENAME_NO_DEFINITION_TOKEN.format(
        qn=HELPER, path="pkg/util.py"
    )


@pytest.mark.parametrize(
    ("edit", "resolution"),
    [
        # The file is gone.
        (None, "missing file"),
        # The file shrank: the recorded line is past its end.
        ("def run():\n    pass\n", cs.RENAME_RESOLUTION_BAD_POSITION),
        # A call still starts at the site but no longer ends where the index
        # recorded it: rewriting by text could pick an inner callee.
        (APP.replace("helper(3, 4)", "helper(3, 4, 99)"), "stale site"),
    ],
    ids=["missing-file", "past-the-end", "stale-call"],
)
def test_a_caller_the_tree_no_longer_matches_is_unlocatable(
    temp_repo: Path, edit: str | None, resolution: str
) -> None:
    collector = _collector(temp_repo, PY_FILES)
    app = temp_repo / "pkg" / "app.py"
    if edit is None:
        app.unlink()
    else:
        app.write_text(edit, encoding="utf-8")

    sites, unlocatable, *_ = collector.collect(HELPER)

    assert cs.RENAME_UNLOCATABLE_SITE.format(owner=RUN, resolution=resolution) in (
        unlocatable
    )
    stuck = [site for site in sites if site.kind == UNLOCATABLE]
    assert stuck
    assert {(site.path, site.owner, site.resolution) for site in stuck} == {
        ("pkg/app.py", RUN, SITELESS)
    }


def test_a_base_class_edge_without_a_position_blocks_as_structural(
    temp_repo: Path,
) -> None:
    # `class Circle(Base)` is an INHERITS edge with no site: applying anyway
    # would leave the subclass naming a class that no longer exists.
    collector = _collector(temp_repo, PY_FILES)

    sites, unlocatable, _old, label = collector.collect(f"{PROJECT}.pkg.util.Base")

    assert label == cs.NodeLabel.CLASS
    assert sites[1:] == [
        RenameSite(
            UNLOCATABLE, "pkg/util.py", 0, 0, f"{PROJECT}.pkg.util.Circle", STRUCTURAL
        )
    ]
    assert unlocatable == []


CALLBACK_UTIL = "def apply(f, x):\n    y = x + 1\n    return f(y)\n"
CALLBACK_MAIN = (
    "from pkg.util import apply\n\n\n"
    "def double(x):\n    return x * 2\n\n\n"
    "class Model:\n    pass\n\n\n"
    "def build(Model):\n    return Model()\n\n\n"
    "def main():\n    return apply(double, 3), build(Model)\n"
)
CALLBACK_FILES = {
    "pkg/__init__.py": "",
    "pkg/util.py": CALLBACK_UTIL,
    "pkg/main.py": CALLBACK_MAIN,
}


def test_a_function_run_through_a_parameter_is_renamed_where_it_is_passed(
    temp_repo: Path,
) -> None:
    # `f(y)` in `apply` spells the parameter, not the function (#2459).
    collector = _collector(temp_repo, CALLBACK_FILES)

    sites, unlocatable, *_ = collector.collect(f"{PROJECT}.pkg.main.double")

    assert [(s.kind, s.path, s.line) for s in sites] == [
        (cs.RenameSiteKind.DEFINITION, "pkg/main.py", 4),
        (REFERENCE, "pkg/main.py", 17),
    ]
    assert unlocatable == []


def test_a_class_run_through_a_parameter_cannot_be_located(temp_repo: Path) -> None:
    # A class passed as a value has no edge of its own at the passing site,
    # so its construction through the parameter refuses instead.
    collector = _collector(temp_repo, CALLBACK_FILES)

    _sites, unlocatable, *_ = collector.collect(f"{PROJECT}.pkg.main.Model")

    assert unlocatable == [
        cs.RENAME_UNLOCATABLE_SITE.format(
            owner=f"{PROJECT}.pkg.main.build",
            resolution=cs.RENAME_RESOLUTION_CLASS_VIA_PARAM.format(param="Model"),
        )
    ]


def test_each_link_of_a_fluent_chain_is_collected_beside_its_row(
    temp_repo: Path,
) -> None:
    # A row covers its own link; the others come along as chain guesses,
    # which the plan drops wherever a row of their own covers them.
    collector = _collector(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "class Builder:\n    def add(self, x):\n        return self\n",
            "pkg/app.py": (
                "from pkg.b import Builder\n\n\n"
                "def run():\n    return Builder().add(1).add(2).add(3)\n"
            ),
        },
    )

    sites, *_ = collector.collect(f"{PROJECT}.pkg.b.Builder.add")

    calls = [site for site in sites if site.kind == CALL]
    exact = sorted((s.line, s.col) for s in calls if s.resolution == EXACT.value)
    chained = sorted((s.line, s.col) for s in calls if s.resolution == CHAIN)
    assert exact == [(5, 21), (5, 28), (5, 35)]
    assert chained == sorted(exact * 2)


def test_a_jsx_element_takes_its_closing_tag(temp_repo: Path) -> None:
    collector = _collector(
        temp_repo,
        {
            "src/Widget.jsx": "export function Widget() {\n  return <div />;\n}\n",
            "src/App.jsx": (
                'import { Widget } from "./Widget";\n\n'
                "export function App() {\n  return <Widget>hi</Widget>;\n}\n"
            ),
        },
    )

    sites, *_ = collector.collect(f"{PROJECT}.src.Widget.Widget")

    assert [(s.kind, s.path, s.line, s.col) for s in sites[1:]] == [
        (REFERENCE, "src/App.jsx", 4, 10),
        (REFERENCE, "src/App.jsx", 4, 21),
    ]


def test_a_module_member_is_bound_by_each_import_through_a_reexport(
    temp_repo: Path,
) -> None:
    collector = _collector(temp_repo, PY_FILES)

    found = collector.import_sites(HELPER, "helper")

    # `pkg/__init__.py` re-exports the name, so `from pkg import helper`
    # binds it too.
    assert found == [
        (
            ImportSite("pkg/__init__.py", 1, 0, 1, 27, "helper", "helper"),
            f"{PROJECT}.pkg",
        ),
        (ImportSite("pkg/app.py", 1, 0, 1, 32, "h", "helper"), f"{PROJECT}.pkg.app"),
        (
            ImportSite("pkg/app.py", 2, 0, 2, 22, "helper", "helper"),
            f"{PROJECT}.pkg.app",
        ),
    ]


def test_a_method_is_never_an_import_site(temp_repo: Path) -> None:
    collector = _collector(temp_repo, PY_FILES)

    assert collector.import_sites(f"{PROJECT}.pkg.util.Circle.area", "area") == []


def test_a_methods_module_is_its_files_not_its_classs(temp_repo: Path) -> None:
    collector = _collector(temp_repo, PY_FILES)

    assert collector.module_of(f"{PROJECT}.pkg.util.Circle.area") == (
        f"{PROJECT}.pkg.util",
        "pkg/util.py",
    )
    # A name the graph does not hold falls back to its parent, with no file.
    assert collector.module_of(f"{PROJECT}.gone.helper") == (f"{PROJECT}.gone", None)


TS_FILES = {
    "src/lib/math/add.ts": (
        "export function add(a: number, b: number): number {\n  return a + b;\n}\n"
    ),
    "src/lib/math/mul.ts": (
        "export default function mul(a: number, b: number): number {\n"
        "  return a * b;\n}\n"
    ),
    "src/lib/math/index.ts": (
        'export * from "./add";\nexport { default as times } from "./mul";\n'
    ),
    "src/app/main.ts": (
        'import { add, times } from "../lib/math";\n'
        'import mul from "../lib/math/mul";\n'
        "\n"
        "export function run(): number {\n"
        "  return add(1, 2) + times(2, 3) + mul(8, 9);\n"
        "}\n"
    ),
}


def test_a_ts_call_imported_through_export_star_refuses(temp_repo: Path) -> None:
    # The rename cannot rewrite an import that goes through `export *`, so
    # rewriting the call would leave it naming what the barrel no longer
    # exports, whatever leave the caller gives.
    collector = _collector(temp_repo, TS_FILES)

    with pytest.raises(RenameRefused) as refused:
        collector.collect(f"{PROJECT}.src.lib.math.add.add")

    assert "src/lib/math/index.ts" in str(refused.value)
    assert refused.value.ambiguous == [
        RenameSite(
            CALL,
            "src/app/main.ts",
            5,
            9,
            f"{PROJECT}.src.app.main.run",
            cs.RENAME_SITE_STAR_REEXPORT,
        )
    ]


def test_a_ts_default_import_keeps_the_importers_own_name(temp_repo: Path) -> None:
    # `import mul from` names the binding as the importer chose: renaming
    # the function leaves that binding, and so its call, alone.
    collector = _collector(temp_repo, TS_FILES)
    mul = f"{PROJECT}.src.lib.math.mul.mul"

    sites, *_ = collector.collect(mul)

    assert [site.kind for site in sites] == [cs.RenameSiteKind.DEFINITION]
    assert collector.js_name_bindings(mul, "mul") == (set(), {"src/app/main.ts"}, {})


def _chain(c_barrel: str) -> dict[str, str]:
    # `foo` passes from `a` through `b`'s named re-export, then through `c`.
    return {
        "src/a.ts": "export function foo(): number {\n  return 1;\n}\n",
        "src/b/index.ts": 'export { foo } from "../a";\n',
        "src/c/index.ts": c_barrel,
        "src/app.ts": (
            'import { foo } from "./c";\n\n'
            "export function run(): number {\n  return foo();\n}\n"
        ),
    }


def test_a_star_past_a_named_reexport_binds_the_consumer_through_it(
    temp_repo: Path,
) -> None:
    collector = _collector(temp_repo, _chain('export * from "../b";\n'))

    assert collector.js_star_bound(f"{PROJECT}.src.a", "foo") == {
        "src/app.ts": "src/c/index.ts"
    }


def test_a_chain_of_named_reexports_binds_by_name(temp_repo: Path) -> None:
    collector = _collector(temp_repo, _chain('export { foo } from "../b";\n'))
    foo = f"{PROJECT}.src.a.foo"

    assert collector.js_star_bound(f"{PROJECT}.src.a", "foo") == {}
    by_name, aliased, starred = collector.js_name_bindings(foo, "foo")
    assert by_name == {"src/b/index.ts", "src/c/index.ts", "src/app.ts"}
    assert (aliased, starred) == (set(), {})
    sites, *_ = collector.collect(foo)
    assert [(s.kind, s.path, s.resolution) for s in sites[1:]] == [
        (CALL, "src/app.ts", EXACT.value)
    ]
