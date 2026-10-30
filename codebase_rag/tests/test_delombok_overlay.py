# Issue #1140 tier 1: Lombok-generated members exist only after expansion, so
# the updater parses delomboked BYTES keyed by the ORIGINAL path. The seam is
# subprocess-shaped: these tests fake the delombok run (no jar in CI) and
# assert the graph gains the expanded member while the on-disk source lacks
# it, the hash cache keys the checked-in bytes, and every missing piece
# degrades to raw parsing. Everything else is real: the jar is a file the
# configured `LOMBOK_JAR` names, found and digested by the module's own
# lookup, and the graph is the suite's recording ingestor.
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import ALL_ENABLED
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.java import lombok as java_lombok
from codebase_rag.sync.checkout_state import state_file
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor

_RAW = (
    "package com.app;\n\nimport lombok.Getter;\n\n@Getter\n"
    "public class Widget {\n    private String name;\n}\n"
)
_EXPANDED = (
    "package com.app;\n\npublic class Widget {\n    private String name;\n\n"
    "    public String getName() {\n        return this.name;\n    }\n}\n"
)


def _write_repo(repo: Path) -> None:
    (repo / "src/main/java/com/app").mkdir(parents=True)
    (repo / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.projectlombok</groupId><artifactId>lombok</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )
    (repo / "src/main/java/com/app/Widget.java").write_text(_RAW, encoding="utf-8")


def _fake_java(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        java_lombok.shutil,
        "which",
        lambda command: "/fake/java" if command == "java" else None,
    )


def _use_jar(monkeypatch: pytest.MonkeyPatch, jar: Path) -> None:
    # A configured path that is no file reads as no jar, without falling
    # back to a developer's ~/.m2.
    monkeypatch.setattr(java_lombok.settings, "LOMBOK_JAR", str(jar))


def _jar(directory: Path, name: str = "lombok-1.18.30.jar") -> Path:
    jar = directory / name
    jar.write_bytes(b"jar")
    return jar


def _fake_delombok(monkeypatch: pytest.MonkeyPatch, jar_dir: Path) -> None:
    _fake_java(monkeypatch)
    _use_jar(monkeypatch, _jar(jar_dir))

    def fake_run(java, jar, source_root, out_dir):
        for original in Path(source_root).rglob("*.java"):
            target = Path(out_dir) / original.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                _EXPANDED
                if "@Getter" in original.read_text()
                else original.read_text(),
                encoding="utf-8",
            )
        return True

    monkeypatch.setattr(java_lombok, "_run_delombok", fake_run)


def _index_repo(repo: Path) -> RecordingIngestor:
    parsers, queries = load_parsers()
    mock = RecordingIngestor()
    GraphUpdater(
        ingestor=mock,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    ).run()
    return mock


def _method_qns(mock: RecordingIngestor) -> set[str]:
    return {
        c.properties["qualified_name"]
        for c in get_nodes(mock)
        if str(c.label) == "Method"
    }


def test_expanded_member_lands_in_the_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    _fake_delombok(monkeypatch, tmp_path)
    mock = _index_repo(repo)
    assert any("Widget.getName" in qn for qn in _method_qns(mock))
    # The expanded bytes are keyed by the ORIGINAL path.
    module_paths = {n.properties.get("path") for n in get_nodes(mock, "Module")}
    assert "src/main/java/com/app/Widget.java" in module_paths
    # The overlay never touches the checked-in source.
    assert "@Getter" in (repo / "src/main/java/com/app/Widget.java").read_text()


def test_without_a_jar_raw_source_parses_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    _fake_java(monkeypatch)
    _use_jar(monkeypatch, tmp_path / "absent.jar")
    assert java_lombok.find_lombok_jar() is None
    mock = _index_repo(repo)
    assert not any("Widget.getName" in qn for qn in _method_qns(mock))
    # Raw parsing still indexes the class itself.
    assert "proj.src.main.java.com.app.Widget.Widget" in {
        n.properties["qualified_name"] for n in get_nodes(mock, "Class")
    }


def test_lombok_free_build_never_invokes_delombok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "proj"
    (repo / "src/main/java/com/app").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    (repo / "src/main/java/com/app/Plain.java").write_text(
        "package com.app;\npublic class Plain {}\n", encoding="utf-8"
    )
    _fake_java(monkeypatch)
    _use_jar(monkeypatch, _jar(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(
        java_lombok,
        "_run_delombok",
        lambda *a: calls.append("run") or True,
    )
    mock = _index_repo(repo)
    assert calls == []
    assert "proj.src.main.java.com.app.Plain.Plain" in {
        n.properties["qualified_name"] for n in get_nodes(mock, "Class")
    }


def test_hash_cache_keys_the_checked_in_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Editing the ORIGINAL file must invalidate the cache even though the
    # parse consumed overlay bytes; a reused updater re-parses it.
    repo = tmp_path / "proj"
    _write_repo(repo)
    _fake_delombok(monkeypatch, tmp_path)
    parsers, queries = load_parsers()
    mock = RecordingIngestor()
    updater = GraphUpdater(
        ingestor=mock,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    )
    updater.run()
    widget = repo / "src/main/java/com/app/Widget.java"
    widget.write_text(_RAW.replace("name;", "name;\n    private int age;"), "utf-8")
    mock.reset_mock()
    updater.run()
    reparsed = {
        c.properties.get("path") for c in get_nodes(mock) if str(c.label) == "Module"
    }
    assert "src/main/java/com/app/Widget.java" in reparsed


@pytest.mark.skipif(
    shutil.which("java") is None or java_lombok.find_lombok_jar() is None,
    reason="real delombok e2e needs java and a lombok jar",
)
def test_real_delombok_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    mock = _index_repo(repo)
    assert any("Widget.getName" in qn for qn in _method_qns(mock))


def test_no_java_degrades_to_raw_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    monkeypatch.setattr(java_lombok.shutil, "which", lambda _c: None)
    _use_jar(monkeypatch, _jar(tmp_path))
    mock = _index_repo(repo)
    assert not any("Widget.getName" in qn for qn in _method_qns(mock))


def test_jar_appearing_after_a_raw_index_forces_the_reparse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The checked-in bytes never changed, so the hash cache alone would skip
    # every Java file; the persisted overlay identity forces the affected
    # files through a reparse when the jar appears (and symmetrically when
    # it vanishes).
    repo = tmp_path / "proj"
    _write_repo(repo)
    _fake_java(monkeypatch)
    _use_jar(monkeypatch, tmp_path / "absent.jar")
    parsers, queries = load_parsers()
    mock = RecordingIngestor()
    updater = GraphUpdater(
        ingestor=mock,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    )
    updater.run()
    assert not any(".getName(" in qn for qn in _method_qns(mock))
    _fake_delombok(monkeypatch, tmp_path)
    mock.reset_mock()
    updater.run()
    # Under the bare class qn: the forced re-parse drops the raw parse's
    # registry entries first (issue #1657), so the class is not re-registered
    # as a `Widget@N` duplicate. The graph-side delete is covered by
    # `test_a_forced_reparse_deletes_the_files_old_subtree_first`: a recording
    # ingestor holds no graph to observe it in.
    assert "proj.src.main.java.com.app.Widget.Widget.getName()" in _method_qns(mock)
    _use_jar(monkeypatch, tmp_path / "absent.jar")
    mock.reset_mock()
    updater.run()
    assert not any(".getName(" in qn for qn in _method_qns(mock))


def test_a_forced_reparse_deletes_the_files_old_subtree_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file forced through re-parse by an overlay change is a RE-INDEX, not
    # an addition: its previous subtree must be deleted before the parse, or
    # the old parse's entities linger beside the fresh ones. Popping the
    # forced key from `old_hashes` made `is_new` read it as new whenever the
    # graph-backed path set was empty, so the delete AND the in-memory state
    # drop were both skipped: the store kept the raw parse's Widget beside a
    # second, `@N`-suffixed Widget from the expanded parse (issue #1657).
    #
    # The stateful evals store rather than a MagicMock: `_delete_module_entities`
    # runs only for a `QueryProtocol` ingestor, which a MagicMock is not, so a
    # mock cannot observe the delete in either state.
    from evals.cgr_graph import StatefulIngestor

    repo = tmp_path / "proj"
    _write_repo(repo)
    _fake_java(monkeypatch)
    _use_jar(monkeypatch, tmp_path / "absent.jar")
    parsers, queries = load_parsers()
    # Default capture: the store models the graph, not the optional network
    # resource pass ALL_ENABLED would switch on, and the Widget nodes this
    # asserts on are in the default set.
    store = StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store, repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run()
    widget_class = "proj.src.main.java.com.app.Widget.Widget"
    assert (cs.NodeLabel.CLASS.value, widget_class) in store.nodes, (
        "fixture guard: the raw parse did not register Widget"
    )

    # The jar appears: the checked-in bytes are unchanged, so only the overlay
    # identity forces Widget.java back through the parser.
    _fake_delombok(monkeypatch, tmp_path)
    updater.run()

    classes = sorted(
        str(uid) for (label, uid) in store.nodes if label == cs.NodeLabel.CLASS.value
    )
    assert classes == [widget_class], (
        "the forced re-parse did not delete the previous subtree first, so the "
        f"old Widget survives beside the new parse's: {classes}"
    )
    methods = sorted(
        str(uid) for (label, uid) in store.nodes if label == cs.NodeLabel.METHOD.value
    )
    assert f"{widget_class}.getName()" in methods, (
        f"the expanded member did not land under the bare class qn: {methods}"
    )


def test_maven_cache_prefers_the_numerically_newest_jar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m2 = tmp_path / ".m2"
    for version in ("1.18.9", "1.18.30"):
        jar_dir = m2 / "repository/org/projectlombok/lombok" / version
        jar_dir.mkdir(parents=True)
        (jar_dir / f"lombok-{version}.jar").write_bytes(b"jar")
    monkeypatch.setattr(java_lombok.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(java_lombok.settings, "LOMBOK_JAR", None)
    jar = java_lombok.find_lombok_jar()
    assert jar is not None
    assert jar.name == "lombok-1.18.30.jar"


def test_jar_version_change_alone_marks_the_state_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Identical expansion under a different Lombok version still flips the
    # persisted state: versions that agree today can diverge on the next
    # annotation edit, so the files go through one honest reparse.
    repo = tmp_path / "proj"
    _write_repo(repo)
    _fake_delombok(monkeypatch, tmp_path)
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=RecordingIngestor(),
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    )
    updater.run()
    assert java_lombok.current_lombok_version() == "1.18.30"
    _use_jar(monkeypatch, _jar(tmp_path, "lombok-1.18.42.jar"))
    updater.run()
    assert updater.generated_sources.state_changed


def test_corrupt_state_file_degrades_to_the_empty_state(tmp_path: Path) -> None:
    from codebase_rag.sync.state import load_delombok_state

    state = tmp_path / "state.json"
    for payload in ('{"keys": null}', "[1, 2]", "not json", '{"identity": 5}'):
        state.write_text(payload, encoding="utf-8")
        loaded = load_delombok_state(state)
        assert loaded == {"identity": "", "keys": [], "lombok": ""}


def test_replaced_same_named_jar_flips_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A configured /tools/lombok.jar replaced in place keeps its name, so
    # the identity must digest the CONTENT.
    jar = tmp_path / "lombok.jar"
    jar.write_bytes(b"first")
    _use_jar(monkeypatch, jar)
    first = java_lombok.current_lombok_identity()
    jar.write_bytes(b"second")
    assert java_lombok.current_lombok_identity() != first


def test_failed_run_never_commits_the_overlay_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag import constants as cs

    repo = tmp_path / "proj"
    _write_repo(repo)
    _fake_delombok(monkeypatch, tmp_path)
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=RecordingIngestor(),
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    )
    monkeypatch.setattr(
        updater.embedding_sync,
        "generate_semantic_embeddings",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        updater.run()
    assert not state_file(repo, cs.DELOMBOK_STATE_FILENAME).exists()


def test_lombok_in_a_gradle_kotlin_build_counts(tmp_path: Path) -> None:
    build = tmp_path / "build.gradle.kts"
    build.write_text(
        'dependencies { compileOnly("org.projectlombok:lombok:1.18.30") }\n',
        encoding="utf-8",
    )
    assert java_lombok._lombok_used(tmp_path)
    build.write_text("plugins { java }\n", encoding="utf-8")
    assert not java_lombok._lombok_used(tmp_path)


def test_the_overlay_keeps_only_changed_files_keyed_by_repo_path(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "repo/src/main/java"
    out_dir = tmp_path / "out"
    for root, text in ((source_root, _RAW), (out_dir, _EXPANDED)):
        (root / "com/app").mkdir(parents=True)
        (root / "com/app/Widget.java").write_text(text, encoding="utf-8")
        (root / "com/app/Plain.java").write_text("class Plain {}\n", "utf-8")
    # An expansion with no original is not the repo's.
    (out_dir / "com/app/Stray.java").write_text("class Stray {}\n", "utf-8")

    overlay = java_lombok._overlay_from_root(tmp_path / "repo", source_root, out_dir)

    assert overlay == {"src/main/java/com/app/Widget.java": _EXPANDED.encode()}


def test_the_overlay_identity_tracks_paths_and_bytes() -> None:
    overlay = {"a/W.java": b"one", "b/X.java": b"two"}
    identity = java_lombok.overlay_identity(overlay)
    assert java_lombok.overlay_identity({}) == ""
    assert java_lombok.overlay_identity(dict(reversed(overlay.items()))) == identity
    assert java_lombok.overlay_identity({**overlay, "b/X.java": b"three"}) != identity
    assert java_lombok.overlay_identity({"a/W.java": b"one"}) != identity


def test_a_jar_name_without_a_version_is_its_own_version() -> None:
    assert java_lombok.lombok_jar_version(Path("lombok-1.18.30.jar")) == "1.18.30"
    assert java_lombok.lombok_jar_version(Path("lombok.jar")) == "lombok.jar"


def test_a_delombok_run_that_exits_non_zero_degrades(tmp_path: Path) -> None:
    # A real child that refuses `-jar ... delombok`: the interpreter itself.
    source_root = tmp_path / "src"
    source_root.mkdir()
    ran = java_lombok._run_delombok(
        sys.executable, _jar(tmp_path), source_root, tmp_path / "out"
    )
    assert ran is False


def test_a_delombok_run_whose_java_cannot_start_degrades(tmp_path: Path) -> None:
    ran = java_lombok._run_delombok(
        str(tmp_path / "no-such-java"), _jar(tmp_path), tmp_path, tmp_path / "out"
    )
    assert ran is False
