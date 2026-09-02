# A GraphUpdater can be reused across runs (the watcher keeps one and
# removes deleted files from its state; a caller may hold one across `run()`
# calls). A deleted file's in-memory state (registry entries, simple-name
# lookup, module-qn map) used to be removed only in the late deletion block,
# after Pass 2 had parsed the changed files, so a rename or move done in one
# cycle resolved the re-parsed importer against the deleted definitions and a
# same-stem replacement got a suffixed qn. A fresh updater never saw this
# (issue #1575).
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.frontends import go as go_fe
from codebase_rag.parsers.frontends import java as java_fe
from codebase_rag.parsers.go_frontend import GoCallSite, GoSemanticFacts
from codebase_rag.parsers.java_frontend import JavaCallSite, JavaSemanticFacts
from evals.cgr_graph import _StatefulIngestor

_ABSOLUTE = {cs.NodeLabel.FOLDER.value, cs.NodeLabel.PACKAGE.value}
Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


def _snapshot(store: _StatefulIngestor, root: Path) -> Snapshot:
    prefix = root.resolve().as_posix() + "/"

    def norm(label: str, uid: str) -> str:
        return (
            uid[len(prefix) :] if label in _ABSOLUTE and uid.startswith(prefix) else uid
        )

    nodes = frozenset(
        (label, norm(label, str(uid)))
        for (label, uid) in store.nodes
        if label != cs.NodeLabel.FILE.value
    )
    edges = frozenset(
        (str(fl), norm(str(fl), str(fv)), str(rel), str(tl), norm(str(tl), str(tv)))
        for (fl, fv, rel, tl, tv) in store.edges
        if cs.NodeLabel.FILE.value not in (fl, tl)
    )
    return nodes, edges


def _materialise(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(
    store: _StatefulIngestor, repo: Path, language: cs.SupportedLanguage
) -> GraphUpdater:
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
    return GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def _bump(root: Path, rel: str) -> None:
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    os.utime(root / rel, (cache_mtime + 1, cache_mtime + 1))


def _clean(
    temp_repo: Path, files: dict[str, str], language: cs.SupportedLanguage
) -> Snapshot:
    root = temp_repo / "clean" / "proj"
    _materialise(root, files)
    store = _StatefulIngestor()
    _updater(store, root, language).run(force=True)
    return _snapshot(store, root)


PY_BEFORE = {
    "base.py": "class Base:\n    pass\n",
    "main.py": "from base import Base\n\ndef go():\n    return Base()\n",
}
PY_AFTER = {
    "core.py": "class Base:\n    pass\n",
    "main.py": "from core import Base\n\ndef go():\n    return Base()\n",
}
JS_BEFORE = {
    "util.js": "export function helper() { return 1; }\n",
    "main.js": "import { helper } from './util';\nexport function go() { return helper(); }\n",
}
JS_AFTER = {
    "util.ts": "export function helper(): number { return 1; }\n",
    "main.js": JS_BEFORE["main.js"],
}


def test_a_reused_updater_resolves_a_rename_against_the_new_module(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, PY_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    updater.run(force=True)
    (root / "base.py").rename(root / "core.py")
    (root / "main.py").write_text(PY_AFTER["main.py"], encoding="utf-8")
    _bump(root, "core.py")
    _bump(root, "main.py")
    updater.run(force=False)
    after = _snapshot(store, root)

    instantiates = {
        e[4] for e in after[1] if e[2] == cs.RelationshipType.INSTANTIATES.value
    }
    assert instantiates == {"proj.core.Base"}
    assert after == _clean(temp_repo, PY_AFTER, cs.SupportedLanguage.PYTHON)


def test_a_reused_updater_gives_a_same_stem_replacement_the_bare_qn(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, JS_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.JS)
    updater.run(force=True)
    (root / "util.js").unlink()
    (root / "util.ts").write_text(JS_AFTER["util.ts"], encoding="utf-8")
    _bump(root, "util.ts")
    updater.run(force=False)
    after = _snapshot(store, root)

    modules = {qn for (label, qn) in after[0] if label == cs.NodeLabel.MODULE.value}
    assert modules == {"proj.util", "proj.main"}
    assert after == _clean(temp_repo, JS_AFTER, cs.SupportedLanguage.JS)


# `main.py` names `helper` without an import so the resolver's simple-name
# fallback, which is where the module-language check lives, decides the edge.
RS_PY_BEFORE = {
    "util.rs": "pub fn helper() -> i32 { 1 }\n",
    "main.py": "def go():\n    return helper()\n",
}
RS_PY_AFTER = {
    "util.py": "def helper():\n    return 1\n",
    "main.py": RS_PY_BEFORE["main.py"] + "\n",
}


def test_a_reused_updater_forgets_a_deleted_module_language(
    temp_repo: Path,
) -> None:
    # `reset_resolution_caches` must also drop the qn -> language memo:
    # after `util.rs` is replaced by `util.py`, the bare qn `proj.util` names
    # a Python module, and a stale RUST answer makes the resolver refuse the
    # Python -> Rust call a clean index resolves.
    root = temp_repo / "proj"
    _materialise(root, RS_PY_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    if cs.SupportedLanguage.RUST not in updater.parsers:
        pytest.skip("rust parser not available")
    updater.run(force=True)
    (root / "util.rs").unlink()
    (root / "util.py").write_text(RS_PY_AFTER["util.py"], encoding="utf-8")
    (root / "main.py").write_text(RS_PY_AFTER["main.py"], encoding="utf-8")
    _bump(root, "util.py")
    _bump(root, "main.py")
    updater.run(force=False)
    after = _snapshot(store, root)

    calls = {(e[1], e[4]) for e in after[1] if e[2] == cs.RelationshipType.CALLS.value}
    assert ("proj.main.go", "proj.util.helper") in calls
    assert after == _clean(temp_repo, RS_PY_AFTER, cs.SupportedLanguage.PYTHON)


def test_a_rehydrated_updater_forgets_a_deleted_definitions_language(
    temp_repo: Path,
) -> None:
    # The watcher's shape: a fresh updater whose FIRST run is incremental,
    # so the untouched util.rs definitions are read back from the graph and
    # `rehydrated_definition_paths` records them. `_module_language` falls
    # through to those paths for a definition qn, so removing the file's
    # state must drop them too, or the replacement util.py still answers
    # RUST for `proj.util.helper` and the call it now provides is refused.
    root = temp_repo / "proj"
    before = {**RS_PY_BEFORE, "other.py": "def x():\n    return 1\n"}
    after = {**RS_PY_AFTER, "other.py": "def x():\n    return 2\n"}
    _materialise(root, before)
    store = _StatefulIngestor()
    first = _updater(store, root, cs.SupportedLanguage.PYTHON)
    if cs.SupportedLanguage.RUST not in first.parsers:
        pytest.skip("rust parser not available")
    first.run(force=True)

    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    (root / "other.py").write_text(after["other.py"], encoding="utf-8")
    _bump(root, "other.py")
    updater.run(force=False)
    rehydrated = updater.factory.definition_processor.rehydrated_definition_paths
    assert "proj.util.helper" in rehydrated, "the shape did not rehydrate"

    (root / "util.rs").unlink()
    (root / "util.py").write_text(after["util.py"], encoding="utf-8")
    (root / "main.py").write_text(after["main.py"], encoding="utf-8")
    _bump(root, "util.py")
    _bump(root, "main.py")
    updater.run(force=False)
    after_snapshot = _snapshot(store, root)

    calls = {
        (e[1], e[4])
        for e in after_snapshot[1]
        if e[2] == cs.RelationshipType.CALLS.value
    }
    assert ("proj.main.go", "proj.util.helper") in calls
    assert after_snapshot == _clean(temp_repo, after, cs.SupportedLanguage.PYTHON)


JEDI_CORE = (
    "class Base:\n    def run(self):\n        return 1\n\n\ndef build():\n"
    "    return Base()\n"
)
JEDI_MAIN = (
    "from {module} import build\n\nhandlers = {{'a': build}}\n\n\ndef go():\n"
    "    return handlers['a']().run()\n"
)


def test_a_reused_updater_relearns_a_renamed_module_under_jedi(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rel-path -> module qn memo the JEDI join reads rebuilds only when
    # its size differs from `module_qn_to_file_path`. A rename removes one
    # entry and adds one, so without an explicit reset the memo keeps
    # `base.py`, never learns `core.py`, and every fact targeting the
    # renamed file loses its declared location.
    pytest.importorskip("jedi")
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "PYTHON_FRONTEND", cs.PythonFrontend.JEDI)
    root = temp_repo / "proj"
    _materialise(
        root, {"base.py": JEDI_CORE, "main.py": JEDI_MAIN.format(module="base")}
    )
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    updater.run(force=True)
    (root / "base.py").rename(root / "core.py")
    (root / "main.py").write_text(JEDI_MAIN.format(module="core"), encoding="utf-8")
    _bump(root, "core.py")
    _bump(root, "main.py")
    updater.run(force=False)
    after = _snapshot(store, root)

    calls = {(e[1], e[4]) for e in after[1] if e[2] == cs.RelationshipType.CALLS.value}
    assert ("proj.main.go", "proj.core.Base.run") in calls
    assert after == _clean(
        temp_repo,
        {"core.py": JEDI_CORE, "main.py": JEDI_MAIN.format(module="core")},
        cs.SupportedLanguage.PYTHON,
    )


# Two resolver indexes are derived from module_qn_to_file_path and rebuilt
# lazily: the Go package index only when the map's size changes, the
# path-to-module index never. Removing a deleted module from the map (rather
# than leaving it, as before) means a rename keeps the size constant, so both
# must be reset with the other memos at the start of Pass 3.
GO_BEFORE = {
    "pkg/types.go": "package pkg\n\ntype T struct{}\n",
    "pkg/methods.go": "package pkg\n\nfunc (t T) M() int { return helper() }\n",
    "pkg/util.go": "package pkg\n\nfunc helper() int { return 1 }\n",
}
GO_AFTER = {
    "pkg/types.go": GO_BEFORE["pkg/types.go"],
    "pkg/methods.go": GO_BEFORE["pkg/methods.go"],
    "pkg/helpers.go": GO_BEFORE["pkg/util.go"],
}


def _calls(snapshot: Snapshot) -> set[tuple[str, str]]:
    return {
        (e[1], e[4]) for e in snapshot[1] if e[2] == cs.RelationshipType.CALLS.value
    }


def test_a_reused_updater_resolves_a_go_call_after_a_same_package_rename(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, GO_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.GO)
    updater.run(force=True)
    (root / "pkg" / "util.go").rename(root / "pkg" / "helpers.go")
    _bump(root, "pkg/helpers.go")
    updater.run(force=False)
    after = _snapshot(store, root)

    # A stale package index lists the deleted `proj.pkg.util`, the receiver
    # lookup raises on it, and methods.go then contributes no CALLS at all.
    calls = _calls(after)
    assert ("proj.pkg.types.T.M", "proj.pkg.helpers.helper") in calls
    assert calls == _calls(_clean(temp_repo, GO_AFTER, cs.SupportedLanguage.GO))


JS_ADD_AFTER = {
    **JS_BEFORE,
    "util.ts": (
        "export function helper2(): number { return 1; }\n"
        "export function helper2b(): number { return helper2(); }\n"
    ),
}


def test_a_reused_updater_keys_a_new_same_stem_module_by_its_own_qn(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, JS_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.JS)
    updater.run(force=True)
    (root / "util.ts").write_text(JS_ADD_AFTER["util.ts"], encoding="utf-8")
    _bump(root, "util.ts")
    updater.run(force=False)
    after = _snapshot(store, root)

    # A path index built on the first run has no entry for util.ts, so its
    # calls were attributed to the JS module's qn (`proj.util.helper`).
    calls = _calls(after)
    assert ("proj.util.ts.helper2b", "proj.util.ts.helper2") in calls
    assert after == _clean(temp_repo, JS_ADD_AFTER, cs.SupportedLanguage.JS)


# The Go, Java and C# semantic-join engines memoise rel-path -> module qn
# and rebuild only when module_qn_to_file_path changes size, so they go
# stale on a rename exactly as the resolver's own memos did. Modelled on Go
# with a synthetic fact standing in for the go/types frontend.
GO_IFACE = (
    "package sample\n\ntype Handler interface{ Handle() string }\n\n"
    "func Run(h Handler) {\n\t_ = h.Handle()\n}\n"
)
GO_A = (
    'package sample\n\ntype A struct{}\n\nfunc (a A) Handle() string { return "a" }\n'
)
GO_B = (
    'package sample\n\ntype B struct{}\n\nfunc (b B) Handle() string { return "b" }\n'
)
GO_MOD = "module example.com/p\n\ngo 1.23\n"
GO_FACT_BEFORE = {"go.mod": GO_MOD, "iface.go": GO_IFACE, "a.go": GO_A, "b.go": GO_B}
GO_FACT_AFTER = {"go.mod": GO_MOD, "iface.go": GO_IFACE, "a.go": GO_A, "c.go": GO_B}
GO_CALL_KEY = ("iface.go", 6, len("\t_ = h."), "Handle")
GO_TARGET = (5, len("func (b B) "))


def _go_facts(target_file: str) -> GoSemanticFacts:
    return GoSemanticFacts(
        call_sites={GO_CALL_KEY: GoCallSite("Handle", target_file, *GO_TARGET)},
        external_sites=set(),
        implements=[],
    )


def test_a_reused_updater_joins_a_go_fact_against_the_renamed_module(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = {"facts": _go_facts("b.go")}
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    monkeypatch.setattr(go_fe, "go_frontend_available", lambda: True)
    monkeypatch.setattr(go_fe, "run_go_frontend", lambda repo_path: holder["facts"])

    root = temp_repo / "proj"
    _materialise(root, GO_FACT_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.GO)
    updater.run(force=True)
    first = _calls(_snapshot(store, root))
    # Positive control: the fact, not the heuristic, decides the first run.
    assert ("proj.iface.Run", "proj.b.B.Handle") in first, first
    assert ("proj.iface.Run", "proj.a.A.Handle") not in first, first

    (root / "b.go").rename(root / "c.go")
    _bump(root, "c.go")
    holder["facts"] = _go_facts("c.go")
    updater.run(force=False)
    after = _calls(_snapshot(store, root))

    # With the memo stale the fact's target resolves to nothing and the
    # heuristic binds Run to A.Handle instead.
    assert ("proj.iface.Run", "proj.c.B.Handle") in after, after
    assert after == _calls(_clean(temp_repo, GO_FACT_AFTER, cs.SupportedLanguage.GO))


# Java twin of the Go test above: same declared_location join, same
# rel-path memo on the Java engine. The fact is built after Pass 2 from the
# registered span of B.handle in whichever file currently holds it.
JAVA_HANDLER = "public interface Handler {\n    String handle();\n}\n"
JAVA_A = (
    "public class A implements Handler {\n"
    '    public String handle() { return "a"; }\n}\n'
)
JAVA_B = (
    "public class B implements Handler {\n"
    '    public String handle() { return "b"; }\n}\n'
)
JAVA_RUN = (
    "public class Run {\n    public static void run(Handler h) {\n"
    "        h.handle();\n    }\n}\n"
)
JAVA_FACT_BEFORE = {
    "Handler.java": JAVA_HANDLER,
    "A.java": JAVA_A,
    "B.java": JAVA_B,
    "Run.java": JAVA_RUN,
}
JAVA_FACT_AFTER = {
    "Handler.java": JAVA_HANDLER,
    "A.java": JAVA_A,
    "C.java": JAVA_B,
    "Run.java": JAVA_RUN,
}
JAVA_CALL_KEY = ("Run.java", 3, len("        h."), "handle")


def test_a_reused_updater_joins_a_java_fact_against_the_renamed_module(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "proj"
    holder: dict[str, object] = {"target_file": "B.java"}

    def facts(repo_path: Path) -> JavaSemanticFacts:
        updater = holder["updater"]
        assert isinstance(updater, GraphUpdater)
        target_file = str(holder["target_file"])
        dp = updater.factory.definition_processor
        target_path = root / target_file
        modules = {
            qn for qn, path in dp.module_qn_to_file_path.items() if path == target_path
        }
        spans = sorted(
            (line, col)
            for (mod, line, col), loc in dp.function_locations.items()
            if mod in modules and ".B.handle" in loc.qualified_name
        )
        assert spans, (modules, sorted(dp.function_locations))
        line, col = spans[0]
        return JavaSemanticFacts(
            call_sites={JAVA_CALL_KEY: JavaCallSite("handle", target_file, line, col)},
            external_sites=set(),
        )

    monkeypatch.setattr(gu.settings, "JAVA_FRONTEND", cs.JavaFrontend.JAVAC)
    monkeypatch.setattr(java_fe, "java_frontend_available", lambda: True)
    monkeypatch.setattr(java_fe, "run_java_frontend", facts)

    _materialise(root, JAVA_FACT_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.JAVA)
    holder["updater"] = updater
    updater.run(force=True)
    first = _calls(_snapshot(store, root))
    # Positive control: the fact binds the call to B.handle on the first run.
    assert any(t.startswith("proj.B.B.handle") for _, t in first), first

    (root / "B.java").rename(root / "C.java")
    _bump(root, "C.java")
    holder["target_file"] = "C.java"
    updater.run(force=False)
    after = _calls(_snapshot(store, root))

    assert any(
        s.startswith("proj.Run.Run.run") and t.startswith("proj.C.B.handle")
        for s, t in after
    ), after
    assert after == _calls(
        _clean(temp_repo, JAVA_FACT_AFTER, cs.SupportedLanguage.JAVA)
    )


# A same-stem replacement takes over the deleted file's module qn, and the
# CommonJS export registry keys on that qn: dropping the deleted file's state
# a second time after the parse would strip the replacement's entry and the
# whole-module alias call below would lose its edge.
CJS_BEFORE = {
    "util.js": "module.exports = function helper() { return 1; };\n",
    "main.js": "const f = require('./util');\nfunction go() { return f(); }\n",
}
CJS_AFTER = {
    "util.ts": CJS_BEFORE["util.js"],
    "main.js": CJS_BEFORE["main.js"],
}


def test_a_reused_updater_keeps_a_same_stem_replacements_commonjs_export(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, CJS_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.JS)
    updater.run(force=True)
    first = _calls(_snapshot(store, root))
    assert ("proj.main.go", "proj.util.helper") in first, first

    (root / "util.js").unlink()
    (root / "util.ts").write_text(CJS_AFTER["util.ts"], encoding="utf-8")
    _bump(root, "util.ts")
    updater.run(force=False)
    after = _snapshot(store, root)

    assert ("proj.main.go", "proj.util.helper") in _calls(after), _calls(after)
    assert after == _clean(temp_repo, CJS_AFTER, cs.SupportedLanguage.JS)
