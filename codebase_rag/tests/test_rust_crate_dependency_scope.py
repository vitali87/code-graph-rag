"""A Rust call only reaches crates its own crate can depend on.

Cargo lets a crate name items of itself and of its (dev-)dependencies,
directly or through them, and nothing else. The resolver's name-based
candidates (the bare-name guess, the simple-name type search, a match arm
typed by its variant's name) ignored the manifests, so `globset`, which
depends on no workspace crate, "called" into `ignore`, and ripgrep's
`ignore` bound `ent.path()` to the root package's integration-test helper
`tests::util::Dir` as an exact edge (issue #2622: 803 such edges).
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _write,
    create_and_run_updater,
)

_METER_RS = (
    "pub struct Meter;\n\n"
    "impl Meter {\n"
    "    pub fn new() -> Meter {\n        Meter\n    }\n"
    "    pub fn gauge(&self) -> usize {\n        1\n    }\n"
    "}\n"
)

_COUNTER_RS = (
    "pub struct Counter;\n\n"
    "impl Counter {\n"
    "    pub fn count(&self) -> usize {\n        0\n    }\n"
    "}\n"
)


def _manifest(name: str, deps: str = "", dev_deps: str = "") -> str:
    text = f'[package]\nname = "{name}"\nversion = "0.1.0"\n'
    if deps:
        text += f"\n[dependencies]\n{deps}"
    if dev_deps:
        text += f"\n[dev-dependencies]\n{dev_deps}"
    return text


def _index(
    temp_repo: Path, mock_ingestor: MagicMock, name: str, files: dict[str, str]
) -> dict[tuple[str, str], set[str]]:
    project = temp_repo / name
    _write(project, files)
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    edges: dict[tuple[str, str], set[str]] = {}
    for c in mock_ingestor.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) != cs.RelationshipType.CALLS:
            continue
        props = c.kwargs.get("properties") or {}
        edges.setdefault((str(c.args[0][2]), str(c.args[2][2])), set()).add(
            str(props.get(cs.KEY_RESOLUTION))
        )
    return edges


def _callees(edges: dict[tuple[str, str], set[str]], caller: str) -> set[str]:
    return {callee for src, callee in edges if src == caller}


# --- the issue: candidates in crates the caller cannot depend on ---------------


def test_name_guess_into_a_non_dependency_crate_yields_no_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `parts` is an untyped iterator, so `parts.count()` falls to the
    # bare-name guess. The only first-party `count` is in `top`, which
    # depends on `base`, never the other way round.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_dep_guess",
        {
            "Cargo.toml": '[workspace]\nmembers = ["crates/base", "crates/top"]\n',
            "crates/base/Cargo.toml": _manifest("base"),
            "crates/base/src/lib.rs": (
                "pub fn total(items: &str) -> usize {\n"
                "    let parts = items.split(',');\n"
                "    parts.count()\n"
                "}\n"
            ),
            "crates/top/Cargo.toml": _manifest("top", 'base = { path = "../base" }\n'),
            "crates/top/src/lib.rs": _COUNTER_RS,
        },
    )
    callees = _callees(edges, "rs_dep_guess.crates.base.src.lib.total")
    assert "rs_dep_guess.crates.top.src.lib.Counter.count" not in callees, edges


def test_type_found_by_name_in_a_non_dependency_crate_yields_no_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Nothing in `base` imports or defines `Meter`; the type search by
    # simple name found `top`'s and recorded the call as exact.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_dep_type",
        {
            "Cargo.toml": '[workspace]\nmembers = ["crates/base", "crates/top"]\n',
            "crates/base/Cargo.toml": _manifest("base"),
            "crates/base/src/lib.rs": (
                "pub fn level() -> usize {\n    Meter::new().gauge()\n}\n"
            ),
            "crates/top/Cargo.toml": _manifest("top", 'base = { path = "../base" }\n'),
            "crates/top/src/lib.rs": _METER_RS,
        },
    )
    leaked = {
        callee
        for callee in _callees(edges, "rs_dep_type.crates.base.src.lib.level")
        if callee.startswith("rs_dep_type.crates.top.")
    }
    assert not leaked, edges


def test_match_arm_typed_by_variant_name_binds_no_non_dependency_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # ripgrep's `ignore` crate: `Ok(WalkEvent::Dir(ent))` types `ent` by the
    # variant's name, `Dir`, and the only first-party `Dir` is the root
    # package's integration-test helper. The root package depends on
    # `walker`, never the other way round, so `ent.path()` is walkdir's.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_dep_arm",
        {
            "Cargo.toml": _manifest("app", 'walker = { path = "crates/walker" }\n')
            + '\n[workspace]\nmembers = ["crates/walker"]\n',
            "src/main.rs": "fn main() {}\n",
            "tests/util.rs": (
                "pub struct Dir {\n    p: String,\n}\n\n"
                "impl Dir {\n"
                "    pub fn path(&self) -> &str {\n        &self.p\n    }\n"
                "}\n"
            ),
            "crates/walker/Cargo.toml": _manifest("walker", 'walkdir = "2"\n'),
            "crates/walker/src/lib.rs": (
                "pub enum WalkEvent {\n    Dir(walkdir::DirEntry),\n    Exit,\n}\n\n"
                "pub fn next(ev: Result<WalkEvent, ()>) -> usize {\n"
                "    match ev {\n"
                "        Ok(WalkEvent::Dir(ent)) => ent.path().as_os_str().len(),\n"
                "        _ => 0,\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    callees = _callees(edges, "rs_dep_arm.crates.walker.src.lib.next")
    assert "rs_dep_arm.tests.util.Dir.path" not in callees, edges


# --- what must keep resolving as before ---------------------------------------


def test_dependency_and_same_crate_targets_keep_their_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `app` depends on `top`, which depends on `base`: a guess into a direct
    # or transitive dependency stays, as do an imported type's exact call
    # and a guess into the caller's own crate.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_dep_keep",
        {
            "Cargo.toml": (
                '[workspace]\nmembers = ["crates/base", "crates/top", "crates/app"]\n'
            ),
            "crates/base/Cargo.toml": _manifest("base"),
            "crates/base/src/lib.rs": _METER_RS,
            "crates/top/Cargo.toml": _manifest("top", 'base = { path = "../base" }\n'),
            "crates/top/src/lib.rs": _COUNTER_RS
            + (
                "\nuse base::Meter;\n\n"
                "pub fn exact() -> usize {\n    Meter::new().gauge()\n}\n\n"
                "pub fn guessed(items: &str) -> usize {\n"
                "    let parts = items.split(',');\n"
                "    parts.gauge() + parts.count()\n"
                "}\n"
            ),
            "crates/app/Cargo.toml": _manifest("app", 'top = { path = "../top" }\n'),
            "crates/app/src/lib.rs": (
                "pub fn transitive(items: &str) -> usize {\n"
                "    let parts = items.split(',');\n"
                "    parts.gauge()\n"
                "}\n"
            ),
        },
    )
    meter = "rs_dep_keep.crates.base.src.lib.Meter"
    top = "rs_dep_keep.crates.top.src.lib"
    assert edges.get((f"{top}.exact", f"{meter}.new")) == {"exact"}, edges
    assert edges.get((f"{top}.exact", f"{meter}.gauge")) == {"exact"}, edges
    assert (f"{top}.guessed", f"{meter}.gauge") in edges, edges
    assert (f"{top}.guessed", f"{top}.Counter.count") in edges, edges
    assert ("rs_dep_keep.crates.app.src.lib.transitive", f"{meter}.gauge") in edges, (
        edges
    )


def test_dev_dependency_and_version_only_workspace_member_keep_their_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A dev-dependency is callable from the crate (its tests above all), and
    # a workspace member declared by version alone is still the member the
    # repo holds (the published-workspace shape).
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_dep_dev",
        {
            "Cargo.toml": (
                '[workspace]\nmembers = ["crates/base", "crates/probe", "crates/top"]\n'
            ),
            "crates/probe/Cargo.toml": _manifest("probe"),
            "crates/probe/src/lib.rs": _COUNTER_RS,
            "crates/base/Cargo.toml": _manifest(
                "base", dev_deps='probe = { path = "../probe" }\n'
            ),
            "crates/base/src/lib.rs": _METER_RS
            + (
                "\n#[cfg(test)]\nmod tests {\n"
                "    #[test]\n    fn counts() {\n"
                "        let parts = \"a,b\".split(',');\n"
                "        let n = parts.count();\n"
                "        assert_eq!(n, 2);\n"
                "    }\n"
                "}\n"
            ),
            "crates/top/Cargo.toml": _manifest("top", 'base = "0.1"\n'),
            "crates/top/src/lib.rs": (
                "use base::Meter;\n\n"
                "pub fn level() -> usize {\n    Meter::new().gauge()\n}\n"
            ),
        },
    )
    probe = "rs_dep_dev.crates.probe.src.lib.Counter.count"
    meter = "rs_dep_dev.crates.base.src.lib.Meter"
    assert ("rs_dep_dev.crates.base.src.lib.tests.counts", probe) in edges, edges
    level = "rs_dep_dev.crates.top.src.lib.level"
    assert (level, f"{meter}.new") in edges, edges
    assert (level, f"{meter}.gauge") in edges, edges
