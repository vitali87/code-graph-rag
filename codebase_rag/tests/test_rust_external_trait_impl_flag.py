# A Rust method in an `impl <ExternalTrait> for Type` block is invoked through
# the external trait's dispatch by code outside the graph (the stdlib calls
# `io::Read::read`, serde calls its `Visitor` methods, the `log` facade calls
# `Log::log`), so no first-party call site can ever prove it live and it
# reported dead: 26 of ripgrep's 221 remaining candidates were these. Flag the
# whole impl block's methods so the dead-code surfaces root them (issue #1048).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater


def _method_props(mock_ingestor: MagicMock) -> dict[str, dict]:
    # Mirror the ingestor's MERGE ... SET n += props semantics: the deferred
    # overrides_external row merges into the full row ingested during Pass 2.
    props: dict[str, dict] = {}
    for c in mock_ingestor.ensure_node_batch.call_args_list:
        if c.args[0] == cs.NodeLabel.METHOD:
            props.setdefault(c.args[1][cs.KEY_QUALIFIED_NAME], {}).update(c.args[1])
    return props


def _build(root: Path, mock_ingestor: MagicMock, files: dict[str, str]) -> dict:
    root.mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "fixture"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    src = root / "src"
    src.mkdir()
    for name, text in files.items():
        (src / name).write_text(text, encoding="utf-8")
    create_and_run_updater(root, mock_ingestor, skip_if_missing="rust")
    return _method_props(mock_ingestor)


def _prop(props: dict[str, dict], suffix: str) -> dict:
    # Exactly one match, or the assertion the caller makes is about a node it
    # did not mean: the decoy fixtures turn on selecting the right one.
    matches = [(k, v) for k, v in props.items() if k.endswith(suffix)]
    assert len(matches) == 1, f"{suffix} matched {[k for k, _ in matches]}"
    return matches[0][1]


def _write_workspace(project: Path, files: dict[str, str]) -> None:
    project.mkdir(parents=True)
    for name, text in files.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _workspace_files(printer_dep: str, use_head: str) -> dict[str, str]:
    return {
        "Cargo.toml": '[workspace]\nmembers = ["searcher", "printer"]\n',
        "searcher/Cargo.toml": '[package]\nname = "grep-searcher"\nversion = "0.1.0"\n',
        # Declared in a submodule and re-exported from the crate root, so the
        # importing crate's spelling reaches it through the re-export:
        # exactly ripgrep's grep_searcher::Sink.
        "searcher/src/lib.rs": "pub mod sink;\npub use crate::sink::Sink;\n",
        "searcher/src/sink.rs": "pub trait Sink {\n    fn matched(&mut self) -> u8;\n}\n",
        "printer/Cargo.toml": (
            '[package]\nname = "printer"\nversion = "0.1.0"\n\n'
            f"[dependencies]\n{printer_dep}\n"
        ),
        "printer/src/lib.rs": (
            f"use {use_head}::Sink;\n"
            "\n"
            "pub struct JSON;\n"
            "\n"
            "impl Sink for JSON {\n"
            "    fn matched(&mut self) -> u8 {\n"
            "        1\n"
            "    }\n"
            "}\n"
        ),
    }


def test_scoped_stdlib_trait_impl_is_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `impl std::io::Read for Reader`: the trait is spelled as a scoped path
    # whose head is the stdlib, and an inherent method on the same type stays
    # an ordinary candidate.
    props = _build(
        temp_repo / "rsstd",
        mock_ingestor,
        {
            "lib.rs": (
                "pub struct Reader;\n"
                "\n"
                "impl std::io::Read for Reader {\n"
                "    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {\n"
                "        Ok(buf.len())\n"
                "    }\n"
                "}\n"
                "\n"
                "impl Reader {\n"
                "    fn helper(&self) -> usize {\n"
                "        0\n"
                "    }\n"
                "}\n"
            )
        },
    )
    read = _prop(props, "Reader.read")
    helper = _prop(props, "Reader.helper")
    assert read.get(cs.KEY_OVERRIDES_EXTERNAL) is True, read
    assert not helper.get(cs.KEY_OVERRIDES_EXTERNAL), helper


def test_bare_imported_external_trait_impl_is_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The common spelling: `use std::io::Write;` then a bare `impl Write`.
    # Every method of the block is trait dispatch, so both flag.
    props = _build(
        temp_repo / "rsuse",
        mock_ingestor,
        {
            "lib.rs": (
                "use std::io::Write;\n"
                "\n"
                "pub struct Sink;\n"
                "\n"
                "impl Write for Sink {\n"
                "    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {\n"
                "        Ok(buf.len())\n"
                "    }\n"
                "\n"
                "    fn flush(&mut self) -> std::io::Result<()> {\n"
                "        Ok(())\n"
                "    }\n"
                "}\n"
            )
        },
    )
    assert _prop(props, "Sink.write").get(cs.KEY_OVERRIDES_EXTERNAL) is True
    assert _prop(props, "Sink.flush").get(cs.KEY_OVERRIDES_EXTERNAL) is True


def test_imported_module_head_trait_impl_is_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Rust's usual spelling for a stdlib trait: `use std::io;` then a scoped
    # `impl io::Read`. The written head is the imported MODULE, so taking it
    # at face value misses every io::Read and io::Write impl there is.
    props = _build(
        temp_repo / "rsmodhead",
        mock_ingestor,
        {
            "lib.rs": (
                "use std::io;\n"
                "\n"
                "pub struct Reader;\n"
                "\n"
                "impl io::Read for Reader {\n"
                "    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {\n"
                "        Ok(buf.len())\n"
                "    }\n"
                "}\n"
            )
        },
    )
    read = _prop(props, "Reader.read")
    assert read.get(cs.KEY_OVERRIDES_EXTERNAL) is True, read


def test_generic_external_trait_impl_is_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A generic trait head (`impl FromIterator<u8> for Seq`) names the same
    # external contract; the type argument must not defeat the match.
    props = _build(
        temp_repo / "rsgeneric",
        mock_ingestor,
        {
            "lib.rs": (
                "pub struct Seq(Vec<u8>);\n"
                "\n"
                "impl FromIterator<u8> for Seq {\n"
                "    fn from_iter<I: IntoIterator<Item = u8>>(it: I) -> Seq {\n"
                "        Seq(it.into_iter().collect())\n"
                "    }\n"
                "}\n"
            )
        },
    )
    assert _prop(props, "Seq.from_iter").get(cs.KEY_OVERRIDES_EXTERNAL) is True


def test_generic_scoped_external_trait_impl_is_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A generic wrapped around a SCOPED path is the spelling the trait-name
    # extractor read nothing off, so the block reached none of this: no
    # RustTraitImpl was recorded and its methods stayed ordinary dead-code
    # candidates however plainly `std` speaks for the trait (issue #1080).
    # `other::Add` is a same-named FIRST-PARTY trait, which the simple-name
    # resolution behind the trait binding sweeps up project-wide. Letting it
    # answer for `std::ops::Add` says `S` implements an unrelated trait,
    # carries liveness in from that trait's method, and silences the external
    # root this test is about. A head the toolchain speaks for settles it.
    props = _build(
        temp_repo / "rsgenericscoped",
        mock_ingestor,
        {
            "lib.rs": (
                "pub mod other;\n"
                "\n"
                "pub struct S(u32);\n"
                "\n"
                "impl std::ops::Add<u32> for S {\n"
                "    type Output = S;\n"
                "    fn add(self, other: u32) -> S {\n"
                "        S(self.0 + other)\n"
                "    }\n"
                "}\n"
            ),
            "other.rs": (
                "pub trait Add<T> {\n    fn add(self, other: T) -> Self;\n}\n"
            ),
        },
    )
    assert _prop(props, "S.add").get(cs.KEY_OVERRIDES_EXTERNAL) is True
    # Nothing here overrides anything first-party: the only trait implemented
    # belongs to std, and `other::Add` is a stranger that shares its name.
    overrides = {
        (call.args[0][2], call.args[2][2])
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if call.args[1] == cs.RelationshipType.OVERRIDES.value
    }
    assert not overrides, overrides


def test_first_party_trait_impl_is_not_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A trait declared in this project dispatches through OVERRIDES liveness,
    # so flagging its impls would permanently root genuinely dead code.
    props = _build(
        temp_repo / "rsown",
        mock_ingestor,
        {
            "lib.rs": (
                "pub trait Greeter {\n"
                "    fn hi(&self) -> u8;\n"
                "}\n"
                "\n"
                "pub struct En;\n"
                "\n"
                "impl Greeter for En {\n"
                "    fn hi(&self) -> u8 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            )
        },
    )
    hi = _prop(props, "En.hi")
    assert not hi.get(cs.KEY_OVERRIDES_EXTERNAL), hi


def test_workspace_sibling_crate_trait_impl_is_not_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The trait belongs to another WORKSPACE member, reached by its crate
    # name (`use grep_searcher::Sink`). The name resolves to no registered
    # trait here, but the manifest proves the head first-party, and rooting
    # on unresolvedness alone would permanently hide the impls of every
    # cross-crate trait in a workspace: ripgrep's whole Sink surface.
    project = temp_repo / "rs_ws_trait"
    _write_workspace(
        project,
        _workspace_files('grep-searcher = { path = "../searcher" }', "grep_searcher"),
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    matched = _prop(_method_props(mock_ingestor), "JSON.matched")
    assert not matched.get(cs.KEY_OVERRIDES_EXTERNAL), matched


def test_versioned_workspace_sibling_trait_impl_is_not_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A published workspace declares its siblings by VERSION, with no path.
    # The manifest then proves nothing about where the crate lives, but the
    # repo holds it: a manifest says how a crate is fetched, not whether it
    # is indexed here.
    project = temp_repo / "rs_ws_versioned"
    _write_workspace(
        project, _workspace_files('grep-searcher = "0.1"', "grep_searcher")
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    matched = _prop(_method_props(mock_ingestor), "JSON.matched")
    assert not matched.get(cs.KEY_OVERRIDES_EXTERNAL), matched


def test_renamed_path_dependency_trait_impl_is_not_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `searcher = { path = "../searcher", package = "grep-searcher" }` speaks
    # the sibling under another name. The rename changes what the crate is
    # CALLED, not where it lives, so the path still binds a repo directory.
    project = temp_repo / "rs_ws_renamed"
    _write_workspace(
        project,
        _workspace_files(
            'searcher = { path = "../searcher", package = "grep-searcher" }',
            "searcher",
        ),
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    matched = _prop(_method_props(mock_ingestor), "JSON.matched")
    assert not matched.get(cs.KEY_OVERRIDES_EXTERNAL), matched


def test_same_named_first_party_struct_does_not_silence_prelude_trait(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `impl Default for T` names the prelude trait; a struct the project
    # happens to call `Default` in an unrelated, unimported module is not it.
    # Parent resolution ends in a project-wide simple-name sweep, so without
    # a node-kind check that struct answers for the trait and silences the
    # flag (and the same sweep has T "implement" a struct).
    props = _build(
        temp_repo / "rsdecoytype",
        mock_ingestor,
        {
            "lib.rs": "pub mod other;\npub mod thing;\n",
            "other.rs": "pub struct Default {\n    pub x: u8,\n}\n",
            "thing.rs": (
                "pub struct T;\n"
                "\n"
                "impl Default for T {\n"
                "    fn default() -> T {\n"
                "        T\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    default = _prop(props, "T.default")
    assert default.get(cs.KEY_OVERRIDES_EXTERNAL) is True, default


def test_manifest_dependency_trait_impl_is_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A registry dependency (no path, so no repo directory) speaks for its
    # head: `termcolor::WriteColor` dispatch is outside the graph.
    root = temp_repo / "rsdep"
    root.mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "fixture"\nversion = "0.1.0"\n\n'
        '[dependencies]\ntermcolor = "1.1"\n',
        encoding="utf-8",
    )
    src = root / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        "use termcolor::WriteColor;\n"
        "\n"
        "pub struct Out;\n"
        "\n"
        "impl WriteColor for Out {\n"
        "    fn reset(&mut self) -> u8 {\n"
        "        0\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="rust")
    reset = _prop(_method_props(mock_ingestor), "Out.reset")
    assert reset.get(cs.KEY_OVERRIDES_EXTERNAL) is True, reset


def test_cross_file_first_party_trait_impl_is_not_flagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The trait lives in a file parsed AFTER the impl, so it is unregistered
    # at class-ingest time; the decision must wait for deferred inheritance
    # resolution or a first-party impl is wrongly rooted.
    props = _build(
        temp_repo / "rsxfile",
        mock_ingestor,
        {
            "lib.rs": "pub mod a_impl;\npub mod z_trait;\n",
            "a_impl.rs": (
                "use crate::z_trait::Greeter;\n"
                "\n"
                "pub struct En;\n"
                "\n"
                "impl Greeter for En {\n"
                "    fn hi(&self) -> u8 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
            "z_trait.rs": ("pub trait Greeter {\n    fn hi(&self) -> u8;\n}\n"),
        },
    )
    hi = _prop(props, "En.hi")
    assert not hi.get(cs.KEY_OVERRIDES_EXTERNAL), hi
