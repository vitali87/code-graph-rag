"""The per-file lookup tables behind issue #2453 never outlive their source.

The linear-scan fix replaced per-definition and per-call rescans with tables
built once per parent, tree, scope, class or pass. A table that survived an
edit would answer for code that is no longer there, so each test here edits
the one fact a table holds (a doc comment, a factory's return, an overload,
a field's type, a loop's element type), syncs, and requires the graph to
equal a clean index of the edited tree: through a fresh updater, as
`cgr start --update-graph` runs, and through `reingest` on the SAME updater,
as the watcher and the MCP tool run, which keeps every instance-level table.

The negative tests at the bottom pin behaviour the tables must not change:
ambiguous names stay unbound, shadowed receivers stay unconfirmed, and
nested-class members stay out of the outer class's overload set.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import force_mtime_after_cache
from codebase_rag.types_defs import PropertyDict, PropertyValue
from evals.cgr_graph import _StatefulIngestor

_CALLS = cs.RelationshipType.CALLS.value
_EXACT = cs.EdgeResolution.EXACT.value
_HEURISTIC = cs.EdgeResolution.HEURISTIC.value

NodeSnapshot = dict[tuple[str, str], PropertyDict]
EdgeSnapshot = dict[tuple[str, str, str, str, str], PropertyValue]


@dataclass(frozen=True)
class EditCase:
    """An edit, and what the edited tree's graph must say afterwards.

    `docs` and `calls` are checked outright, not only against a clean index:
    a table shared by the whole process that went stale would feed the clean
    index the same stale answer, and the two would agree.
    """

    language: cs.SupportedLanguage
    before: dict[str, str]
    edited: str
    after: str
    docs: dict[str, str | None]
    calls: dict[tuple[str, str], str]


_C_BEFORE = """\
/** One. */
int f(void) { return 0; }
/// Two.
int g(void) { return f(); }
"""
_C_AFTER = """\
/** Inserted above, shifting every sibling. */
int e(void) { return 1; }
/** One, reworded. */
int f(void) { return e(); }
int g(void) { return f(); }
"""

# Same length, same shape: only the comment text differs, so a table keyed
# by byte span rather than by tree would serve the old tree's comments.
_C_SAME_SPAN_AFTER = """\
/** Uno. */
int f(void) { return 0; }
/// Dos.
int g(void) { return f(); }
"""

_JS_BEFORE = """\
class Lid { seal() { return 1; } }
class Jar { twist() { return 2; } }
/** Makes a lid. */
function make() { return new Lid(); }
function use() { const r = make(); r.seal(); r.twist(); }
"""
_JS_AFTER = """\
class Lid { seal() { return 1; } }
class Jar { twist() { return 2; } }
/** Makes a jar. */
function make() { return new Jar(); }
function use() { const r = make(); r.seal(); r.twist(); }
"""

# Same span again: only which factory the receiver is introduced from changes.
_JS_RECEIVER_BEFORE = """\
class Lid { seal() { return 1; } }
class Jar { twist() { return 2; } }
function makeLid() { return new Lid(); }
function makeJar() { return new Jar(); }
function use() { const r = makeLid(); r.seal(); r.twist(); }
"""
_JS_RECEIVER_AFTER = _JS_RECEIVER_BEFORE.replace(
    "const r = makeLid();", "const r = makeJar();"
)

_TS_BEFORE = """\
export class Widget { spin(): void {} }
export class Gadget { stop(): void {} }
/** Widgets. */
export function items(): Widget[] { return []; }
export function use(): void { for (const w of items()) { w.spin(); w.stop(); } }
"""
_TS_AFTER = """\
export class Widget { spin(): void {} }
export class Gadget { stop(): void {} }
/** Gadgets. */
export function items(): Gadget[] { return []; }
export function use(): void { for (const w of items()) { w.spin(); w.stop(); } }
"""

_JAVA_REPOS = {
    "src/p/Repo.java": "package p;\npublic class Repo { public void save() {} }\n",
    "src/p/Bank.java": "package p;\npublic class Bank { public void save() {} }\n",
}
_JAVA_BEFORE = """\
package p;
public class Service {
  private Repo store;
  /** Runs. */
  public void run() { store.save(); }
}
"""
_JAVA_AFTER = """\
package p;
public class Service {
  private Bank store;
  /** Runs. */
  public void run() { store.save(); }
}
"""

_CSHARP_BEFORE = """\
namespace N {
  public class Big {
    /// <summary>No args.</summary>
    public void Handle() { }
    public void Run() { Handle(); Handle(1); }
  }
}
"""
_CSHARP_AFTER = """\
namespace N {
  public class Big {
    /// <summary>No args.</summary>
    public void Handle() { }
    /// <summary>An overload added by the edit.</summary>
    public void Handle(int x) { }
    public void Run() { Handle(); Handle(1); }
  }
}
"""

EDIT_CASES = [
    pytest.param(
        EditCase(
            cs.SupportedLanguage.C,
            {"gen.c": _C_BEFORE},
            "gen.c",
            _C_AFTER,
            docs={
                "proj.gen.e": "Inserted above, shifting every sibling.",
                "proj.gen.f": "One, reworded.",
                "proj.gen.g": None,
            },
            calls={("proj.gen.f", "proj.gen.e"): _EXACT},
        ),
        id="c-doc-comments",
    ),
    pytest.param(
        EditCase(
            cs.SupportedLanguage.C,
            {"gen.c": _C_BEFORE},
            "gen.c",
            _C_SAME_SPAN_AFTER,
            docs={"proj.gen.f": "Uno.", "proj.gen.g": "Dos."},
            calls={("proj.gen.g", "proj.gen.f"): _EXACT},
        ),
        id="c-same-span-doc-comments",
    ),
    pytest.param(
        EditCase(
            cs.SupportedLanguage.JS,
            {"gen.js": _JS_BEFORE},
            "gen.js",
            _JS_AFTER,
            docs={"proj.gen.make": "Makes a jar."},
            calls={
                ("proj.gen.use", "proj.gen.Jar.twist"): _EXACT,
                ("proj.gen.use", "proj.gen.Lid.seal"): _HEURISTIC,
            },
        ),
        id="js-factory-return",
    ),
    pytest.param(
        EditCase(
            cs.SupportedLanguage.JS,
            {"gen.js": _JS_RECEIVER_BEFORE},
            "gen.js",
            _JS_RECEIVER_AFTER,
            docs={},
            calls={
                ("proj.gen.use", "proj.gen.Jar.twist"): _EXACT,
                ("proj.gen.use", "proj.gen.Lid.seal"): _HEURISTIC,
            },
        ),
        id="js-receiver-introduction",
    ),
    pytest.param(
        EditCase(
            cs.SupportedLanguage.TS,
            {"gen.ts": _TS_BEFORE},
            "gen.ts",
            _TS_AFTER,
            docs={"proj.gen.items": "Gadgets."},
            calls={
                ("proj.gen.use", "proj.gen.Gadget.stop"): _EXACT,
                ("proj.gen.use", "proj.gen.Widget.spin"): _HEURISTIC,
            },
        ),
        id="ts-for-of-element",
    ),
    pytest.param(
        EditCase(
            cs.SupportedLanguage.JAVA,
            {**_JAVA_REPOS, "src/p/Service.java": _JAVA_BEFORE},
            "src/p/Service.java",
            _JAVA_AFTER,
            docs={"proj.src.p.Service.Service.run()": "Runs."},
            calls={
                (
                    "proj.src.p.Service.Service.run()",
                    "proj.src.p.Bank.Bank.save()",
                ): _EXACT
            },
        ),
        id="java-field-type",
    ),
    pytest.param(
        EditCase(
            cs.SupportedLanguage.CSHARP,
            {"Big.cs": _CSHARP_BEFORE},
            "Big.cs",
            _CSHARP_AFTER,
            docs={
                "proj.Big.N.Big.Handle(int)": (
                    "<summary>An overload added by the edit.</summary>"
                )
            },
            calls={("proj.Big.N.Big.Run", "proj.Big.N.Big.Handle(int)"): _EXACT},
        ),
        id="csharp-new-overload",
    ),
]


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(
    store: _StatefulIngestor, root: Path, language: cs.SupportedLanguage
) -> GraphUpdater:
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def _nodes(store: _StatefulIngestor) -> NodeSnapshot:
    # The sync stamp is the one property that differs between two runs.
    return {
        (label, str(uid)): {
            key: value for key, value in props.items() if key != cs.KEY_LAST_SYNCED_AT
        }
        for (label, uid), props in store.nodes.items()
    }


def _edges(store: _StatefulIngestor) -> EdgeSnapshot:
    return {
        (str(fl), str(fv), str(rel), str(tl), str(tv)): store.props_for(
            (fl, fv, rel, tl, tv)
        ).get(cs.KEY_RESOLUTION)
        for (fl, fv, rel, tl, tv) in store.edges
    }


def _clean_index(root: Path, language: cs.SupportedLanguage) -> _StatefulIngestor:
    clean = _StatefulIngestor()
    _updater(clean, root, language).run(force=True)
    return clean


def _edit(root: Path, case: EditCase) -> Path:
    target = root / case.edited
    target.write_text(case.after, encoding="utf-8")
    force_mtime_after_cache(root, target)
    return target


def _assert_same_graph(store: _StatefulIngestor, clean: _StatefulIngestor) -> None:
    assert _nodes(store) == _nodes(clean)
    assert _edges(store) == _edges(clean)


def _assert_reflects_the_edit(store: _StatefulIngestor, case: EditCase) -> None:
    for qn, doc in case.docs.items():
        assert _docstring(store, qn) == doc, qn
    for (caller, callee), resolution in case.calls.items():
        assert _calls_from(store, caller).get(callee) == resolution, (caller, callee)


def _assert_edit_changed_the_graph(
    before: _StatefulIngestor, clean: _StatefulIngestor
) -> None:
    # Fixture guard: an edit that changed nothing would let a stale table pass.
    assert (_nodes(before), _edges(before)) != (_nodes(clean), _edges(clean))


@pytest.mark.parametrize("case", EDIT_CASES)
def test_a_sync_after_an_edit_matches_a_clean_index(
    temp_repo: Path, case: EditCase
) -> None:
    root = temp_repo / "proj"
    _write(root, case.before)
    store = _StatefulIngestor()
    _updater(store, root, case.language).run()
    before = _StatefulIngestor()
    _updater(before, root, case.language).run(force=True)

    _edit(root, case)
    _updater(store, root, case.language).run()
    clean = _clean_index(root, case.language)

    _assert_edit_changed_the_graph(before, clean)
    _assert_reflects_the_edit(store, case)
    _assert_same_graph(store, clean)


@pytest.mark.parametrize("case", EDIT_CASES)
def test_a_reingest_on_the_same_updater_matches_a_clean_index(
    temp_repo: Path, case: EditCase
) -> None:
    root = temp_repo / "proj"
    _write(root, case.before)
    store = _StatefulIngestor()
    updater = _updater(store, root, case.language)
    updater.run()
    before = _StatefulIngestor()
    _updater(before, root, case.language).run(force=True)

    updater.reingest([_edit(root, case)])
    clean = _clean_index(root, case.language)

    _assert_edit_changed_the_graph(before, clean)
    _assert_reflects_the_edit(store, case)
    _assert_same_graph(store, clean)


# --- what the tables must not change ----------------------------------------


def _index(
    temp_repo: Path, files: dict[str, str], language: cs.SupportedLanguage
) -> _StatefulIngestor:
    root = temp_repo / "proj"
    _write(root, files)
    store = _StatefulIngestor()
    _updater(store, root, language).run()
    return store


def _calls_from(store: _StatefulIngestor, caller_qn: str) -> dict[str, PropertyValue]:
    return {
        edge[4]: resolution
        for edge, resolution in _edges(store).items()
        if edge[2] == _CALLS and edge[1] == caller_qn
    }


def _docstring(store: _StatefulIngestor, qn: str) -> PropertyValue:
    for (_label, uid), props in store.nodes.items():
        if uid == qn:
            return props.get(cs.KEY_DOCSTRING)
    raise AssertionError(f"no node {qn}")


def test_every_sibling_keeps_its_own_doc_comment(temp_repo: Path) -> None:
    # One cached index serves every definition under the file root; each
    # must still read the comment directly above itself, and none other.
    source = "".join(
        f"/** Doc {i}. */\nint f{i}(void) {{ return {i}; }}\n" for i in range(5)
    )
    source += "int undocumented(void) { return 0; } /** trailing */\n"
    source += "/** detached */\n\nint detached(void) { return 0; }\n"
    store = _index(temp_repo, {"gen.c": source}, cs.SupportedLanguage.C)
    for i in range(5):
        assert _docstring(store, f"proj.gen.f{i}") == f"Doc {i}."
    assert _docstring(store, "proj.gen.undocumented") is None
    assert _docstring(store, "proj.gen.detached") is None


def test_nested_parents_keep_their_own_doc_comments(temp_repo: Path) -> None:
    # Definitions alternate between parents (module, class body, nested
    # function body); every parent's index must serve only its children.
    source = """\
/** Outer class. */
class Outer {
  /** Outer method. */
  run() {
    /** Inner function. */
    function inner() { return 1; }
    return inner();
  }
}
/** After the class. */
function after() { return 2; }
"""
    store = _index(temp_repo, {"gen.js": source}, cs.SupportedLanguage.JS)
    assert _docstring(store, "proj.gen.Outer") == "Outer class."
    assert _docstring(store, "proj.gen.Outer.run") == "Outer method."
    assert _docstring(store, "proj.gen.Outer.run.inner") == "Inner function."
    assert _docstring(store, "proj.gen.after") == "After the class."


def test_a_name_declared_twice_still_types_nothing(temp_repo: Path) -> None:
    # The per-tree declaration index keeps every same-named function, so a
    # local shadowing a top-level `items` still leaves the loop untyped.
    source = """\
export class Widget { spin(): void {} }
export class Gadget { spin(): void {} }
export function items(): Widget[] { return []; }
export function use(): void {
  function items(): Gadget[] { return []; }
  for (const w of items()) { w.spin(); }
}
"""
    store = _index(temp_repo, {"gen.ts": source}, cs.SupportedLanguage.TS)
    targets = _calls_from(store, "proj.gen.use")
    assert "proj.gen.Widget.spin" not in targets
    assert "proj.gen.Gadget.spin" not in targets


def test_a_shadowed_receiver_is_not_confirmed(temp_repo: Path) -> None:
    # The scope index answers for the INNERMOST introduction: a local `box`
    # holding a Map hides the module-level Lid, so the pick is dropped there
    # and confirmed exact only where the module binding is the one read.
    source = """\
class Lid { seal() { return 1; } }
const box = new Lid();
function outerUse() { box.seal(); }
function shadowed() { const box = new Map(); box.seal(); }
function param(box) { box.seal(); }
"""
    store = _index(temp_repo, {"gen.js": source}, cs.SupportedLanguage.JS)
    assert _calls_from(store, "proj.gen.outerUse") == {"proj.gen.Lid.seal": _EXACT}
    assert _calls_from(store, "proj.gen.shadowed") == {}
    assert _calls_from(store, "proj.gen.param") == {"proj.gen.Lid.seal": _HEURISTIC}


def test_a_nested_class_method_is_not_an_outer_overload(temp_repo: Path) -> None:
    # The per-class name index keeps the class's OWN methods only: a nested
    # class's same-named method must not join the outer overload set.
    source = """\
namespace N {
  public class Outer {
    public void M() { }
    public class Inner { public void M(int x) { } }
    public void Run() { M(); M(1); }
  }
}
"""
    store = _index(temp_repo, {"Big.cs": source}, cs.SupportedLanguage.CSHARP)
    targets = _calls_from(store, "proj.Big.N.Outer.Run")
    assert "proj.Big.N.Outer.M" in targets
    assert "proj.Big.N.Outer.Inner.M(int)" not in targets
