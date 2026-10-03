"""An incremental sync must type receivers the way a clean index does (#2559).

An incremental run re-parses only the changed files, so the import maps of the
unchanged modules they import from stayed empty. A receiver typed through a
package re-export (`from pkg import Client`, with `pkg/__init__.py` re-exporting
`._client.Client`) lost its type, and its method call fell back to whatever
same-named function the name-only heuristic reached. Rust lost the same
re-export maps, and also the recorded return types of unchanged functions that
`Get::new(k).into_frame()` and `let b = Builder::new(); b.build()` type from.

Every scenario edits only the consumer or the re-exporting file and compares
the incremental graph with a clean index of the same tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.rs import utils as rs_utils
from codebase_rag.tests.conftest import force_mtime_after_cache
from codebase_rag.types_defs import PropertyValue
from evals.cgr_graph import _StatefulIngestor

_CALLS = cs.RelationshipType.CALLS.value
_IMPORTS = cs.RelationshipType.IMPORTS.value
_INHERITS = cs.RelationshipType.INHERITS.value
_EXACT = cs.EdgeResolution.EXACT.value
_UNRELATED_FN = "\n\ndef test_other():\n    assert True\n"
_UNRELATED_RS_FN = "\npub fn unrelated() -> u32 {\n    0\n}\n"

PY_FIXTURE: dict[str, str] = {
    "pkg/__init__.py": (
        "from ._client import Client, AsyncClient\n"
        "from ._api import send\n"
        "from .factory import make\n"
    ),
    "pkg/_client.py": (
        "class Client:\n"
        "    def send(self, request: str) -> str:\n"
        "        return request\n\n\n"
        "class AsyncClient:\n"
        "    pass\n"
    ),
    "pkg/_api.py": "def send(request: str) -> str:\n    return request\n",
    "pkg/factory.py": (
        "from . import Client\n\n\ndef make() -> Client:\n    return Client()\n"
    ),
    # Imports from the package too, but no re-parsed file reaches it.
    "pkg/unrelated.py": (
        "from ._api import send\n\n\ndef relay():\n    return send('z')\n"
    ),
    "tests/test_client.py": (
        "from pkg import Client, make\n\n\n"
        "class Child(Client):\n    pass\n\n\n"
        "def test_send():\n    client = Client()\n    client.send('x')\n\n\n"
        "def test_factory():\n    client = make()\n    client.send('y')\n"
    ),
}
PY_CONSUMER = "tests/test_client.py"

# The consumer imports the factory straight from its module, so only the
# factory's own map leads on to the package re-export its `-> Client` names.
PY_FACTORY_FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "from ._client import Client\nfrom ._api import send\n",
    "pkg/_client.py": PY_FIXTURE["pkg/_client.py"],
    "pkg/_api.py": PY_FIXTURE["pkg/_api.py"],
    "pkg/factory.py": (
        "from pkg import Client\n\n\ndef make() -> Client:\n    return Client()\n"
    ),
    "tests/test_factory.py": (
        "from pkg.factory import make\n\n\n"
        "def test_factory():\n    client = make()\n    client.send('y')\n"
    ),
}
PY_FACTORY_CONSUMER = "tests/test_factory.py"

# `shim.py` shares its stem with `shim.js`, so its module carries the `.py`
# suffix while `api.py` still writes `from shim import Widget` (issue #2586).
PY_STEM_SIBLING_FIXTURE: dict[str, str] = {
    "widgets.py": "class Widget:\n    def render(self) -> str:\n        return 'w'\n",
    "decoy.py": "def render() -> str:\n    return 'd'\n",
    "shim.py": "from widgets import Widget\n",
    "shim.js": "export function helper() {\n  return 1;\n}\n",
    "api.py": "from shim import Widget\n",
    "app.py": (
        "from api import Widget\n\n\n"
        "def main():\n    widget = Widget()\n    widget.render()\n"
    ),
}
PY_STEM_SIBLING_CONSUMER = "app.py"

# `db.rs` is the decoy: it defines `into_frame` and `is_shutdown` too, so a
# receiver that loses its type lands there by name.
RS_FIXTURE: dict[str, str] = {
    "Cargo.toml": '[package]\nname = "rsreexp"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": ("pub mod client_b;\npub mod cmd;\npub mod db;\npub mod shutdown;\n"),
    "src/cmd/mod.rs": "pub mod get;\npub use self::get::Get;\n",
    "src/cmd/get.rs": (
        "pub struct Get {\n    key: String,\n}\n\n"
        "impl Get {\n"
        "    pub fn new(key: &str) -> Get {\n"
        "        Get { key: key.to_string() }\n"
        "    }\n\n"
        "    pub fn into_frame(self) -> u32 {\n        1\n    }\n"
        "}\n"
    ),
    "src/shutdown.rs": (
        "pub struct Shutdown {\n    flag: bool,\n}\n\n"
        "pub trait Make {\n"
        "    fn make() -> Self;\n"
        "    fn peek(&self) -> Option<Shutdown> {\n        None\n    }\n"
        "}\n\n"
        "impl Make for Shutdown {\n"
        "    fn make() -> Self {\n        Shutdown { flag: false }\n    }\n"
        "}\n\n"
        "impl Shutdown {\n"
        "    pub fn is_shutdown(&self) -> bool {\n        self.flag\n    }\n\n"
        "    pub fn checked(&self) -> Result<Self, ()> {\n        Err(())\n    }\n"
        "}\n\n"
        "pub fn current() -> Shutdown {\n    Shutdown { flag: true }\n}\n"
    ),
    "src/db.rs": (
        "pub struct Shared;\n\n"
        "impl Shared {\n"
        "    pub fn is_shutdown(&self) -> bool {\n        false\n    }\n\n"
        "    pub fn into_frame(self) -> u32 {\n        2\n    }\n"
        "}\n"
    ),
    "src/client_b.rs": (
        "use crate::cmd::Get;\n\n"
        "pub fn chained(k: &str) -> u32 {\n    Get::new(k).into_frame()\n}\n\n"
        "pub fn local(k: &str) -> u32 {\n    let g = Get::new(k);\n    g.into_frame()\n}\n\n"
        "pub fn param(g: Get) -> u32 {\n    g.into_frame()\n}\n"
    ),
}
RS_CONSUMER = "src/client_b.rs"
RS_REEXPORTER = "src/cmd/mod.rs"
RS_INTO_FRAME = "proj.src.cmd.get.Get.into_frame"

# `app` depends on `my-beta` only; `my-alpha` holds a same-named `Builder`
# whose `build` a lost receiver type reaches by name.
RS_WORKSPACE: dict[str, str] = {
    "Cargo.toml": '[workspace]\nmembers = ["crates/app", "crates/alpha", "crates/beta"]\n',
    "crates/app/Cargo.toml": (
        '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n\n'
        '[dependencies]\nmy-beta = { path = "../beta" }\n'
    ),
    "crates/app/src/main.rs": (
        "use my_beta::Builder;\n\n"
        "fn main() {\n"
        "    let b = Builder::new();\n"
        '    println!("{}", b.build());\n'
        "}\n"
    ),
    "crates/alpha/Cargo.toml": (
        '[package]\nname = "my-alpha"\nversion = "0.1.0"\nedition = "2021"\n'
    ),
    "crates/alpha/src/lib.rs": (
        "pub struct Builder;\n\n"
        "impl Builder {\n"
        "    pub fn new() -> Self {\n        Builder\n    }\n\n"
        "    pub fn build(&self) -> u32 {\n        1\n    }\n"
        "}\n"
    ),
    "crates/beta/Cargo.toml": (
        '[package]\nname = "my-beta"\nversion = "0.1.0"\nedition = "2021"\n'
    ),
    "crates/beta/src/lib.rs": (
        "pub struct Builder;\n\n"
        "impl Builder {\n"
        "    pub fn new() -> Self {\n        Builder\n    }\n\n"
        "    pub fn build(&self) -> u32 {\n        2\n    }\n"
        "}\n"
    ),
}
RS_WORKSPACE_CONSUMER = "crates/app/src/main.rs"

Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


class _RelationshipRecorder(_StatefulIngestor):
    """Records the source and type of every relationship written."""

    def __init__(self) -> None:
        super().__init__()
        self.written: list[tuple[str, str]] = []

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: dict[str, PropertyValue] | None = None,
    ) -> None:
        self.written.append((str(rel_type), str(from_spec[2])))
        super().ensure_relationship_batch(from_spec, rel_type, to_spec, properties)


def _materialise(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(store: _StatefulIngestor, root: Path, language: str) -> GraphUpdater:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage(language) not in parsers:
        pytest.skip(f"{language} parser not available")
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


class _Synced:
    """The store an incremental sync mutated, its updater, and a clean index."""

    def __init__(
        self,
        store: _StatefulIngestor,
        updater: GraphUpdater,
        clean: _StatefulIngestor,
        clean_updater: GraphUpdater,
        root: Path,
    ) -> None:
        self.store = store
        self.updater = updater
        self.clean = clean
        self.clean_updater = clean_updater
        self.root = root


def _index_then_edit(
    store: _StatefulIngestor,
    root: Path,
    files: dict[str, str],
    edited: str,
    appended: str,
    language: str,
) -> None:
    _materialise(root, files)
    _updater(store, root, language).run()
    target = root / edited
    target.write_text(files[edited] + appended, encoding="utf-8")
    force_mtime_after_cache(root, target)


def _sync_after_edit(
    tmp_path: Path, files: dict[str, str], edited: str, appended: str, language: str
) -> _Synced:
    root = tmp_path / "proj"
    store = _StatefulIngestor()
    _index_then_edit(store, root, files, edited, appended, language)
    # A fresh updater, as `cgr start --update-graph` builds one per sync: it
    # holds nothing but what this run parses and reads back from the graph.
    updater = _updater(store, root, language)
    updater.run()
    clean = _StatefulIngestor()
    clean_updater = _updater(clean, root, language)
    clean_updater.run(force=True)
    return _Synced(store, updater, clean, clean_updater, root)


def _calls_from(store: _StatefulIngestor, caller_qn: str) -> dict[str, str | None]:
    """Each CALLS target of `caller_qn`, with the edge's resolution."""
    out: dict[str, str | None] = {}
    for edge in store.edges:
        if edge[2] != _CALLS or edge[1] != caller_qn:
            continue
        resolution = store.props_for(edge).get(cs.KEY_RESOLUTION)
        out[str(edge[4])] = str(resolution) if resolution is not None else None
    return out


def _snapshot(store: _StatefulIngestor, root: Path) -> Snapshot:
    # File and Folder nodes are keyed by absolute path, identical here since
    # both indexes read the same tree; normalised anyway so a failure diff
    # reads as relative paths.
    prefix = root.resolve().as_posix() + "/"

    def norm(uid: object) -> str:
        text = str(uid)
        return text[len(prefix) :] if text.startswith(prefix) else text

    nodes = frozenset((label, norm(uid)) for (label, uid) in store.nodes)
    # The resolution is part of the answer: the issue's edge kept its
    # target in some variants and only dropped from exact to heuristic.
    edges = frozenset(
        (
            str(fl),
            norm(fv),
            str(rel),
            str(tl),
            norm(tv),
            str(store.props_for((fl, fv, rel, tl, tv)).get(cs.KEY_RESOLUTION)),
        )
        for (fl, fv, rel, tl, tv) in store.edges
    )
    return nodes, edges


class TestPythonPackageReexport:
    def test_receiver_typed_through_reexport_stays_exact(self, tmp_path: Path) -> None:
        synced = _sync_after_edit(
            tmp_path, PY_FIXTURE, PY_CONSUMER, _UNRELATED_FN, "python"
        )
        calls = _calls_from(synced.store, "proj.tests.test_client.test_send")
        assert calls.get("proj.pkg._client.Client.send") == _EXACT
        assert "proj.pkg._api.send" not in calls

    def test_factory_return_typed_through_reexport_stays_exact(
        self, tmp_path: Path
    ) -> None:
        # `make` lives in an unchanged module and annotates `-> Client`, a
        # name that module itself imports through the package re-export.
        synced = _sync_after_edit(
            tmp_path, PY_FIXTURE, PY_CONSUMER, _UNRELATED_FN, "python"
        )
        calls = _calls_from(synced.store, "proj.tests.test_client.test_factory")
        assert calls.get("proj.pkg._client.Client.send") == _EXACT
        assert "proj.pkg._api.send" not in calls

    def test_base_class_reached_through_reexport_keeps_inherits(
        self, tmp_path: Path
    ) -> None:
        synced = _sync_after_edit(
            tmp_path, PY_FIXTURE, PY_CONSUMER, _UNRELATED_FN, "python"
        )
        inherits = (
            cs.NodeLabel.CLASS.value,
            "proj.tests.test_client.Child",
            _INHERITS,
            cs.NodeLabel.CLASS.value,
            "proj.pkg._client.Client",
        )
        assert inherits in synced.store.edges

    def test_incremental_graph_equals_a_clean_index(self, tmp_path: Path) -> None:
        synced = _sync_after_edit(
            tmp_path, PY_FIXTURE, PY_CONSUMER, _UNRELATED_FN, "python"
        )
        assert _snapshot(synced.store, synced.root) == _snapshot(
            synced.clean, synced.root
        )

    def test_unreached_unchanged_module_is_not_parsed(self, tmp_path: Path) -> None:
        # The import maps are restored only along what the re-parsed file
        # imports: a module none of its imports leads to is not read at all.
        synced = _sync_after_edit(
            tmp_path, PY_FIXTURE, PY_CONSUMER, _UNRELATED_FN, "python"
        )
        mapping = synced.updater.factory.import_processor.import_mapping
        assert "proj.pkg" in mapping
        assert "proj.pkg.unrelated" not in mapping

    def test_restored_modules_write_no_import_edges(self, tmp_path: Path) -> None:
        # An unchanged module's IMPORTS edges never left the graph, so reading
        # its imports back must not queue them again; the re-parsed file's own
        # edges are still written.
        root = tmp_path / "proj"
        store = _RelationshipRecorder()
        _index_then_edit(store, root, PY_FIXTURE, PY_CONSUMER, _UNRELATED_FN, "python")
        store.written.clear()
        _updater(store, root, "python").run()
        importers = {source for rel, source in store.written if rel == _IMPORTS}
        assert "proj.tests.test_client" in importers
        assert importers.isdisjoint(
            {"proj.pkg", "proj.pkg._client", "proj.pkg._api", "proj.pkg.factory"}
        )

    def test_scoped_reingest_on_a_fresh_updater_stays_exact(
        self, tmp_path: Path
    ) -> None:
        # The MCP path: a new updater per call re-ingests just the named file.
        root = tmp_path / "proj"
        store = _StatefulIngestor()
        _index_then_edit(store, root, PY_FIXTURE, PY_CONSUMER, _UNRELATED_FN, "python")
        _updater(store, root, "python").reingest([PY_CONSUMER])
        calls = _calls_from(store, "proj.tests.test_client.test_send")
        assert calls.get("proj.pkg._client.Client.send") == _EXACT
        assert "proj.pkg._api.send" not in calls

    def test_full_build_restores_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A forced run parses every file, so every import map is already
        # there and no module is read a second time.
        restored: list[str] = []
        original = GraphUpdater._restore_module_imports

        def record(updater: GraphUpdater, module_qn: str, path: Path) -> bool:
            restored.append(module_qn)
            return original(updater, module_qn, path)

        monkeypatch.setattr(GraphUpdater, "_restore_module_imports", record)
        root = tmp_path / "proj"
        _materialise(root, PY_FIXTURE)
        _updater(_StatefulIngestor(), root, "python").run(force=True)
        assert restored == []


class TestPythonFactoryModule:
    def test_factory_receiver_stays_exact(self, tmp_path: Path) -> None:
        # The walk reaches `make`, a definition, and must still follow its
        # module's own map on to `pkg`, where `Client` is re-exported.
        synced = _sync_after_edit(
            tmp_path, PY_FACTORY_FIXTURE, PY_FACTORY_CONSUMER, _UNRELATED_FN, "python"
        )
        calls = _calls_from(synced.store, "proj.tests.test_factory.test_factory")
        assert calls.get("proj.pkg._client.Client.send") == _EXACT
        assert "proj.pkg._api.send" not in calls

    def test_incremental_graph_equals_a_clean_index(self, tmp_path: Path) -> None:
        synced = _sync_after_edit(
            tmp_path, PY_FACTORY_FIXTURE, PY_FACTORY_CONSUMER, _UNRELATED_FN, "python"
        )
        assert _snapshot(synced.store, synced.root) == _snapshot(
            synced.clean, synced.root
        )


class TestStemSiblingReexport:
    def test_receiver_through_suffixed_module_stays_exact(self, tmp_path: Path) -> None:
        # `api.py`'s restored map names `proj.shim.Widget`; only once it reads
        # `proj.shim.py.Widget` does the walk find `shim.py` to restore.
        synced = _sync_after_edit(
            tmp_path,
            PY_STEM_SIBLING_FIXTURE,
            PY_STEM_SIBLING_CONSUMER,
            _UNRELATED_FN,
            "javascript",
        )
        calls = _calls_from(synced.store, "proj.app.main")
        assert calls.get("proj.widgets.Widget.render") == _EXACT
        assert "proj.decoy.render" not in calls

    def test_incremental_graph_equals_a_clean_index(self, tmp_path: Path) -> None:
        synced = _sync_after_edit(
            tmp_path,
            PY_STEM_SIBLING_FIXTURE,
            PY_STEM_SIBLING_CONSUMER,
            _UNRELATED_FN,
            "javascript",
        )
        assert _snapshot(synced.store, synced.root) == _snapshot(
            synced.clean, synced.root
        )


class TestRustReexport:
    @pytest.mark.parametrize("edited", [RS_CONSUMER, RS_REEXPORTER])
    def test_every_receiver_form_stays_exact(self, tmp_path: Path, edited: str) -> None:
        # The issue's table: the consumer edited (all three forms lost their
        # type) and the re-exporting mod.rs edited (the two forms that type
        # from `Get::new`'s return lost theirs).
        synced = _sync_after_edit(
            tmp_path, RS_FIXTURE, edited, _UNRELATED_RS_FN, "rust"
        )
        for fn in ("chained", "local", "param"):
            calls = _calls_from(synced.store, f"proj.src.client_b.{fn}")
            assert calls.get(RS_INTO_FRAME) == _EXACT, (fn, calls)
            assert "proj.src.db.Shared.into_frame" not in calls, (fn, calls)

    @pytest.mark.parametrize("edited", [RS_CONSUMER, RS_REEXPORTER])
    def test_incremental_graph_equals_a_clean_index(
        self, tmp_path: Path, edited: str
    ) -> None:
        synced = _sync_after_edit(
            tmp_path, RS_FIXTURE, edited, _UNRELATED_RS_FN, "rust"
        )
        assert _snapshot(synced.store, synced.root) == _snapshot(
            synced.clean, synced.root
        )

    def test_return_types_of_unchanged_files_match_a_clean_index(
        self, tmp_path: Path
    ) -> None:
        # `Self` reads as the impl target and `Result<Self, _>` as its inner
        # type, exactly as when the file is parsed.
        synced = _sync_after_edit(
            tmp_path, RS_FIXTURE, RS_CONSUMER, _UNRELATED_RS_FN, "rust"
        )
        incremental = synced.updater.factory.definition_processor.method_return_types
        clean = synced.clean_updater.factory.definition_processor.method_return_types
        assert incremental.get("proj.src.cmd.get.Get.new") == "Get"
        assert incremental.get("proj.src.shutdown.Shutdown.make") == "Shutdown"
        assert incremental.get("proj.src.shutdown.Shutdown.checked") == "Shutdown"
        assert incremental.get("proj.src.shutdown.current") == "Shutdown"
        assert incremental == clean

    def test_trait_methods_get_no_return_type(self, tmp_path: Path) -> None:
        # A clean index records return types for impl-block methods only, so
        # a trait's own `fn make() -> Self` must not gain one on a sync:
        # incremental typing that a clean index lacks is the same divergence.
        synced = _sync_after_edit(
            tmp_path, RS_FIXTURE, RS_CONSUMER, _UNRELATED_RS_FN, "rust"
        )
        incremental = synced.updater.factory.definition_processor.method_return_types
        assert "proj.src.shutdown.Make.make" not in incremental
        assert "proj.src.shutdown.Make.peek" not in incremental


class TestRustWorkspace:
    def test_cross_crate_receiver_stays_in_the_dependency(self, tmp_path: Path) -> None:
        synced = _sync_after_edit(
            tmp_path, RS_WORKSPACE, RS_WORKSPACE_CONSUMER, _UNRELATED_RS_FN, "rust"
        )
        calls = _calls_from(synced.store, "proj.crates.app.src.main.main")
        assert calls.get("proj.crates.beta.src.lib.Builder.build") == _EXACT
        assert "proj.crates.alpha.src.lib.Builder.build" not in calls

    def test_incremental_graph_equals_a_clean_index(self, tmp_path: Path) -> None:
        synced = _sync_after_edit(
            tmp_path, RS_WORKSPACE, RS_WORKSPACE_CONSUMER, _UNRELATED_RS_FN, "rust"
        )
        assert _snapshot(synced.store, synced.root) == _snapshot(
            synced.clean, synced.root
        )


_ANNOTATED_RUST = (
    "pub struct Frame;\n"
    "pub mod net { pub struct Conn; }\n"
    "pub struct Get;\n"
    "impl Get {\n"
    "    fn own(&self) -> Self { Get }\n"
    "    fn wrapped(&self) -> Result<Self, String> { Ok(Get) }\n"
    "    fn borrowed(&self) -> &Frame { &Frame }\n"
    "    fn scoped(&self) -> crate::net::Conn { crate::net::Conn }\n"
    "    fn boxed(&self) -> Option<Box<Frame>> { None }\n"
    "    fn generic(&self) -> Vec<Frame> { Vec::new() }\n"
    "    fn traited(&self) -> impl Iterator<Item = Frame> { std::iter::empty() }\n"
    "    fn pair(&self) -> (Frame, Get) { (Frame, Get) }\n"
    "    fn unit(&self) {}\n"
    "}\n"
)


class TestReturnTypeFromAnnotation:
    def test_annotation_reduces_like_the_parsed_fn(self) -> None:
        # The persisted annotation is the text of the parsed return-type node,
        # so reading it back must give what reading the node gave.
        parsers, _queries = load_parsers()
        parser = parsers.get(cs.SupportedLanguage.RUST)
        if parser is None:
            pytest.skip("rust parser not available")
        root = parser.parse(_ANNOTATED_RUST.encode()).root_node
        impl = next(c for c in root.named_children if c.type == cs.TS_RS_IMPL_ITEM)
        body = impl.child_by_field_name("body")
        assert body is not None
        methods = [c for c in body.named_children if c.type == cs.TS_RS_FUNCTION_ITEM]
        assert len(methods) == 9
        for method in methods:
            node = method.child_by_field_name(cs.FIELD_RETURN_TYPE)
            expected = rs_utils.extract_return_type_name(method, "Get")
            if node is None:
                assert expected is None
                continue
            annotation = node.text.decode() if node.text else ""
            assert (
                rs_utils.return_type_name_from_annotation(parser, annotation, "Get")
                == expected
            ), annotation

    def test_self_names_nothing_without_an_impl(self) -> None:
        # A free fn's record is read with no impl target, exactly as Pass 2
        # records it, so a bare `Self` yields no type rather than a guess.
        parsers, _queries = load_parsers()
        parser = parsers.get(cs.SupportedLanguage.RUST)
        if parser is None:
            pytest.skip("rust parser not available")
        assert rs_utils.return_type_name_from_annotation(parser, "Self", None) is None
        assert (
            rs_utils.return_type_name_from_annotation(parser, "Result<Self, ()>", "W")
            == "W"
        )
