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

import pytest

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


# --- a dependency names the package it fetches, whatever the code calls it -----

_ALIAS_TOP_RS = (
    "use base_alias::Meter;\n\n"
    "pub fn level() -> usize {\n    Meter::new().gauge()\n}\n\n"
    "pub fn qualified() -> usize {\n    base_alias::Meter::new().gauge()\n}\n\n"
    "pub fn guessed(items: &str) -> usize {\n"
    "    let parts = items.split(',');\n"
    "    parts.gauge()\n"
    "}\n"
)


# How `top` declares its dependency on the member `my-base` under the name
# `base-alias`: (its own entry, the root's [workspace.dependencies] entry).
_ALIAS_FORMS = {
    "version": ('base-alias = { version = "0.1", package = "my-base" }\n', ""),
    "path": ('base-alias = { path = "../base", package = "my-base" }\n', ""),
    "ws_version": (
        "base-alias = { workspace = true }\n",
        'base-alias = { version = "0.1", package = "my-base" }\n',
    ),
    "ws_path": (
        "base-alias = { workspace = true }\n",
        'base-alias = { path = "crates/base", package = "my-base" }\n',
    ),
}


@pytest.mark.parametrize("form", list(_ALIAS_FORMS))
def test_renamed_workspace_member_dependency_keeps_its_edges(
    temp_repo: Path, mock_ingestor: MagicMock, form: str
) -> None:
    # `package = "my-base"` fetches the member `my-base`, which the code
    # names by the entry's key, `base_alias`. A pathless entry was looked up
    # by that key among the members' lib names, so the member fell out of
    # `top`'s closure and `base_alias::` read as an external crate: every
    # call into it was dropped (PR #2791 review). The path forms always
    # kept them, and all four must agree.
    dep, workspace_deps = _ALIAS_FORMS[form]
    root = '[workspace]\nmembers = ["crates/base", "crates/top"]\n'
    if workspace_deps:
        root += f"\n[workspace.dependencies]\n{workspace_deps}"
    project = f"rs_dep_alias_{form}"
    edges = _index(
        temp_repo,
        mock_ingestor,
        project,
        {
            "Cargo.toml": root,
            "crates/base/Cargo.toml": _manifest("my-base"),
            "crates/base/src/lib.rs": _METER_RS,
            "crates/top/Cargo.toml": _manifest("top", dep),
            "crates/top/src/lib.rs": _ALIAS_TOP_RS,
        },
    )
    meter = f"{project}.crates.base.src.lib.Meter"
    top = f"{project}.crates.top.src.lib"
    assert (f"{top}.level", f"{meter}.new") in edges, edges
    assert (f"{top}.level", f"{meter}.gauge") in edges, edges
    assert (f"{top}.qualified", f"{meter}.gauge") in edges, edges
    assert (f"{top}.guessed", f"{meter}.gauge") in edges, edges


def test_version_only_member_named_apart_from_its_lib_keeps_its_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Unrenamed, the entry's key is the member's package name, while the
    # code says the lib target's name (`[lib] name = "basics"`).
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_dep_libname",
        {
            "Cargo.toml": '[workspace]\nmembers = ["crates/base", "crates/top"]\n',
            "crates/base/Cargo.toml": _manifest("my-base")
            + '\n[lib]\nname = "basics"\n',
            "crates/base/src/lib.rs": _METER_RS,
            "crates/top/Cargo.toml": _manifest("top", 'my-base = "0.1"\n'),
            "crates/top/src/lib.rs": _ALIAS_TOP_RS.replace("base_alias", "basics"),
        },
    )
    meter = "rs_dep_libname.crates.base.src.lib.Meter"
    top = "rs_dep_libname.crates.top.src.lib"
    assert (f"{top}.level", f"{meter}.new") in edges, edges
    assert (f"{top}.level", f"{meter}.gauge") in edges, edges
    assert (f"{top}.guessed", f"{meter}.gauge") in edges, edges


def test_dependency_renamed_to_a_registry_package_stays_external(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Both entries fetch registry packages the repo does not hold, one of
    # them under the name of a workspace member: neither the member `meters`
    # nor `my-base`, which `top` does not depend on, is called.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_dep_alias_ext",
        {
            "Cargo.toml": (
                '[workspace]\nmembers = ["crates/base", "crates/meters", '
                '"crates/top"]\n'
            ),
            "crates/base/Cargo.toml": _manifest("my-base"),
            "crates/base/src/lib.rs": _METER_RS,
            "crates/meters/Cargo.toml": _manifest("meters"),
            "crates/meters/src/lib.rs": _METER_RS,
            "crates/top/Cargo.toml": _manifest(
                "top",
                'base-alias = { version = "1", package = "serde" }\n'
                'meters = { version = "1", package = "serde_json" }\n',
            ),
            "crates/top/src/lib.rs": _ALIAS_TOP_RS
            + (
                "\npub fn member_named() -> usize {\n"
                "    meters::Meter::new().gauge()\n"
                "}\n"
            ),
        },
    )
    leaked = {
        (src, callee)
        for src, callee in edges
        if src.startswith("rs_dep_alias_ext.crates.top.")
        and not callee.startswith("rs_dep_alias_ext.crates.top.")
    }
    assert not leaked, edges


_GIT_URL = "https://example.com/upstream/base.git"

# A dependency spelled with a member's package name but fetched from
# somewhere else: (`top`'s entry, the root's [workspace.dependencies] entry,
# the crate name `top`'s code writes).
_ELSEWHERE_FORMS = {
    "git_renamed": (
        f'base-alias = {{ git = "{_GIT_URL}", package = "my-base" }}\n',
        "",
        "base_alias",
    ),
    "git_plain": (f'my-base = {{ git = "{_GIT_URL}" }}\n', "", "my_base"),
    "ws_git": (
        "base-alias = { workspace = true }\n",
        f'base-alias = {{ git = "{_GIT_URL}", package = "my-base" }}\n',
        "base_alias",
    ),
    "alt_registry": (
        'base-alias = { version = "1", registry = "corp", package = "my-base" }\n',
        "",
        "base_alias",
    ),
    "path_outside": (
        'base-alias = { path = "../../../vendor/base", package = "my-base" }\n',
        "",
        "base_alias",
    ),
}


@pytest.mark.parametrize("form", list(_ELSEWHERE_FORMS))
def test_member_named_dependency_from_another_source_stays_external(
    temp_repo: Path, mock_ingestor: MagicMock, form: str
) -> None:
    # Only a crates.io entry stands for the workspace member of its package
    # name (the published-workspace shape). A git, alternate-registry or
    # out-of-repo path entry fetches some other copy, so the member joined
    # `top`'s closure by name and its methods took the calls (PR #2791
    # review).
    dep, workspace_deps, crate = _ELSEWHERE_FORMS[form]
    root = '[workspace]\nmembers = ["crates/base", "crates/top"]\n'
    if workspace_deps:
        root += f"\n[workspace.dependencies]\n{workspace_deps}"
    project = f"rs_dep_src_{form}"
    edges = _index(
        temp_repo,
        mock_ingestor,
        project,
        {
            "Cargo.toml": root,
            "crates/base/Cargo.toml": _manifest("my-base"),
            "crates/base/src/lib.rs": _METER_RS,
            "crates/top/Cargo.toml": _manifest("top", dep),
            "crates/top/src/lib.rs": _ALIAS_TOP_RS.replace("base_alias", crate),
        },
    )
    leaked = {
        (src, callee)
        for src, callee in edges
        if src.startswith(f"{project}.crates.top.")
        and callee.startswith(f"{project}.crates.base.")
    }
    assert not leaked, edges


def test_git_dependency_patched_to_the_member_keeps_its_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `[patch."<url>"]` replaces the git source with the member's path, so
    # the member is the crate `top` builds against after all.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_dep_patched",
        {
            "Cargo.toml": (
                '[workspace]\nmembers = ["crates/base", "crates/top"]\n\n'
                f'[patch."{_GIT_URL}"]\nmy-base = {{ path = "crates/base" }}\n'
            ),
            "crates/base/Cargo.toml": _manifest("my-base"),
            "crates/base/src/lib.rs": _METER_RS,
            "crates/top/Cargo.toml": _manifest(
                "top", f'base-alias = {{ git = "{_GIT_URL}", package = "my-base" }}\n'
            ),
            "crates/top/src/lib.rs": _ALIAS_TOP_RS,
        },
    )
    meter = "rs_dep_patched.crates.base.src.lib.Meter"
    top = "rs_dep_patched.crates.top.src.lib"
    assert (f"{top}.level", f"{meter}.new") in edges, edges
    assert (f"{top}.level", f"{meter}.gauge") in edges, edges
    assert (f"{top}.guessed", f"{meter}.gauge") in edges, edges
