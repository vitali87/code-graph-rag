"""An incremental sync keeps the field types of classes it does not re-parse.

A field-hop receiver (`args.mode.update(...)`, `args.mode.Update(1)`) types
through the declaring class's field map, and only parsing filled that map.
Editing the caller re-parsed it while `LowArgs` came back from the graph
without its fields, so Rust re-bound the call by name to the trait method
and to the caller itself, and C# dropped the edge (issue #3004).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_RUST = {
    "Cargo.toml": '[package]\nname = "rsfieldinc"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/main.rs": "mod flags;\nfn main() {}\n",
    "src/flags/mod.rs": (
        "mod defs;\nmod lowargs;\n\n"
        "pub(crate) use crate::flags::lowargs::{LowArgs, Mode};\n\n"
        "pub(crate) trait Flag {\n    fn update(&self, args: &mut LowArgs);\n}\n"
    ),
    "src/flags/lowargs.rs": (
        "pub(crate) struct LowArgs {\n    pub(crate) mode: Mode,\n}\n\n"
        "pub(crate) enum Mode {\n    Search,\n    Count,\n}\n\n"
        "impl Mode {\n    pub(crate) fn update(&mut self, new: Mode) {\n"
        "        *self = new;\n    }\n}\n"
    ),
    "src/flags/defs.rs": (
        "use crate::flags::lowargs::{LowArgs, Mode};\nuse crate::flags::Flag;\n\n"
        "struct Count;\n\nimpl Flag for Count {\n"
        "    fn update(&self, args: &mut LowArgs) {\n"
        "        args.mode.update(Mode::Count);\n    }\n}\n"
    ),
}
_CSHARP = {
    "src/LowArgs.cs": (
        "public class Mode { public void Update(int n) {} }\n"
        "public class LowArgs { public Mode mode = new Mode(); }\n"
    ),
    "src/Defs.cs": (
        "public class Defs { public void Update(LowArgs args) "
        "{ args.mode.Update(1); } }\n"
    ),
}

_Calls = dict[tuple[str, str], str]


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _sync(root: Path, store: _StatefulIngestor, force: bool) -> _Calls:
    parsers, queries = load_parsers()
    GraphUpdater(ingestor=store, repo_path=root, parsers=parsers, queries=queries).run(
        force=force
    )
    return {
        (str(edge[1]).split(".src.", 1)[1], str(edge[4]).split(".src.", 1)[1]): str(
            store.props_for(edge).get(cs.KEY_RESOLUTION)
        )
        for edge in store.edges
        if edge[2] == cs.RelationshipType.CALLS.value and "pdate" in str(edge[1])
    }


def _append(root: Path, rel: str, text: str) -> None:
    (root / rel).write_text((root / rel).read_text() + text, encoding="utf-8")


@pytest.mark.parametrize(
    ("files", "caller_file", "grammar"),
    [(_RUST, "src/flags/defs.rs", "rust"), (_CSHARP, "src/Defs.cs", "c_sharp")],
    ids=["rust", "csharp"],
)
def test_editing_the_caller_keeps_the_field_typed_call(
    tmp_path: Path, files: dict[str, str], caller_file: str, grammar: str
) -> None:
    if grammar not in load_parsers()[0]:
        pytest.skip(f"{grammar} parser not available")
    root = tmp_path / "repo"
    _write(root, files)
    store = _StatefulIngestor()
    fresh = _sync(root, store, force=True)
    assert list(fresh.values()) == ["exact"], fresh

    _append(root, caller_file, "// unrelated edit\n")

    assert _sync(root, store, force=False) == fresh


def test_a_re_parsed_class_takes_its_new_field_type(tmp_path: Path) -> None:
    # Negative: when the class's own file changes, its fresh fields decide,
    # not the ones the graph held.
    if "rust" not in load_parsers()[0]:
        pytest.skip("rust parser not available")
    root = tmp_path / "repo"
    _write(root, _RUST)
    store = _StatefulIngestor()
    _sync(root, store, force=True)

    lowargs = (root / "src/flags/lowargs.rs").read_text()
    (root / "src/flags/lowargs.rs").write_text(
        lowargs.replace("pub(crate) mode: Mode,", "pub(crate) mode: Other,")
        + "\npub(crate) struct Other;\n\nimpl Other {\n"
        "    pub(crate) fn update(&mut self, new: Mode) {}\n}\n",
        encoding="utf-8",
    )
    _append(root, "src/flags/defs.rs", "// unrelated edit\n")

    after = _sync(root, store, force=False)
    assert after == {("flags.defs.Count.update", "flags.lowargs.Other.update"): "exact"}
