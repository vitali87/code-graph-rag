"""A Rust `Type::f()` whose Type is not a candidate's owner never binds it.

`String::new()`, `Vec::new()`, `Default::default()`, a derived
`Config::default()` or clap's derived `Cli::parse()` name an associated
function of a std/prelude type, or one a derive generates; the graph holds
neither. The qualifier was dropped and the bare name went to the resolver's
guesses, which bound the call to whichever first-party type or module
defined a same-named function (issue #2543: tokio-rs/mini-redis recorded
both binaries' `Cli::parse()` as calls to the RESP parser `Frame::parse`).
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.rs.utils import literal_receiver_types
from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _write,
    create_and_run_updater,
)

_FRAME_RS = (
    "pub struct Frame { n: usize }\n\n"
    "impl Frame {\n"
    "    pub fn new() -> Frame { Frame { n: 0 } }\n"
    "    pub fn parse(s: &str) -> Frame { Frame { n: s.len() } }\n"
    "    pub fn from(n: usize) -> Frame { Frame { n } }\n"
    "    pub fn default() -> Frame { Frame { n: 7 } }\n"
    "    pub fn clone(&self) -> Frame { Frame { n: self.n } }\n"
    "}\n"
)


def _index(
    temp_repo: Path, mock_ingestor: MagicMock, name: str, files: dict[str, str]
) -> dict[tuple[str, str], set[str]]:
    project = temp_repo / name
    _write(
        project,
        {"Cargo.toml": f'[package]\nname = "{name}"\nversion = "0.1.0"\n', **files},
    )
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


# --- the issue: qualified calls that must not bind a first-party method ------


def test_prelude_and_derived_assoc_calls_do_not_bind_project_methods(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's repro verbatim: app.rs never mentions Frame, yet every
    # line bound one of Frame's same-named methods.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_std_assoc",
        {
            "src/lib.rs": "pub mod frame;\npub mod app;\n",
            "src/frame.rs": _FRAME_RS,
            "src/app.rs": (
                "use std::collections::HashMap;\n\n"
                "#[derive(Default, Debug)]\n"
                "pub struct Config { name: String }\n\n"
                "pub fn build() -> usize {\n"
                "    let s = String::new();\n"
                "    let v: Vec<u8> = Vec::new();\n"
                "    let m: HashMap<u8, u8> = HashMap::new();\n"
                '    let t = String::from("x");\n'
                "    let c = Config::default();\n"
                '    let n: u32 = "42".parse().unwrap();\n'
                "    s.len() + v.len() + m.len() + t.len() + c.name.len() + n as usize\n"
                "}\n"
            ),
        },
    )
    frame = "rs_std_assoc.src.frame.Frame"
    leaked = {
        callee
        for callee in _callees(edges, "rs_std_assoc.src.app.build")
        if callee.startswith(f"{frame}.")
    }
    assert not leaked, edges


def test_derived_parser_parse_does_not_bind_the_frame_parser(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # mini-redis: clap's `#[derive(Parser)]` generates `Cli::parse()`; the
    # crate's only `parse` is the RESP frame parser.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_derive_parse",
        {
            "src/lib.rs": "pub mod frame;\n",
            "src/frame.rs": _FRAME_RS,
            "src/bin/cli.rs": (
                "use clap::Parser;\n\n"
                "#[derive(Parser, Debug)]\n"
                "struct Cli { port: u16 }\n\n"
                "fn main() {\n"
                "    let cli = Cli::parse();\n"
                "    let _ = cli.port;\n"
                "}\n"
            ),
        },
    )
    callees = _callees(edges, "rs_derive_parse.src.bin.cli.main")
    assert "rs_derive_parse.src.frame.Frame.parse" not in callees, edges


def test_prelude_trait_qualified_calls_do_not_bind_project_methods(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `Default::default()` and `Clone::clone(&c)` dispatch through the std
    # trait on a type that only derives it.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_prelude_trait",
        {
            "src/lib.rs": "pub mod frame;\npub mod app;\n",
            "src/frame.rs": _FRAME_RS,
            "src/app.rs": (
                "#[derive(Default, Clone)]\n"
                "pub struct Config { n: u8 }\n\n"
                "pub fn build() -> u8 {\n"
                "    let c: Config = Default::default();\n"
                "    let d = Clone::clone(&c);\n"
                "    let e = Config::clone(&d);\n"
                "    e.n\n"
                "}\n"
            ),
        },
    )
    frame = "rs_prelude_trait.src.frame.Frame"
    callees = _callees(edges, "rs_prelude_trait.src.app.build")
    assert f"{frame}.default" not in callees, edges
    assert f"{frame}.clone" not in callees, edges


def test_self_qualified_derived_call_does_not_bind_another_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `Self::default()` inside Config's impl is Config's derived Default.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_self_derived",
        {
            "src/lib.rs": "pub mod frame;\npub mod app;\n",
            "src/frame.rs": _FRAME_RS,
            "src/app.rs": (
                "#[derive(Default)]\n"
                "pub struct Config { n: u8 }\n\n"
                "impl Config {\n"
                "    pub fn fresh() -> Self {\n"
                "        Self::default()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    callees = _callees(edges, "rs_self_derived.src.app.Config.fresh")
    assert "rs_self_derived.src.frame.Frame.default" not in callees, edges


def test_std_type_calls_do_not_bind_a_same_module_free_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `String::new()` never names the caller module's own `fn new`, not
    # even for `HashMap`, which is explicitly imported from std; nor does
    # a method call on a string literal name its `fn parse`.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_std_same_mod",
        {
            "src/lib.rs": "pub mod app;\n",
            "src/app.rs": (
                "use std::collections::HashMap;\n\n"
                "pub fn new() -> usize { 0 }\n"
                "pub fn parse() -> usize { 0 }\n\n"
                "pub fn build() -> usize {\n"
                "    let s = String::new();\n"
                "    let m: HashMap<u8, u8> = HashMap::new();\n"
                '    let n: u32 = "42".parse().unwrap();\n'
                "    s.len() + m.len() + n as usize\n"
                "}\n"
            ),
        },
    )
    callees = _callees(edges, "rs_std_same_mod.src.app.build")
    assert "rs_std_same_mod.src.app.new" not in callees, edges
    assert "rs_std_same_mod.src.app.parse" not in callees, edges


def test_std_path_qualified_calls_do_not_bind_project_methods(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A path through std (written out, or through a `use std::fmt;` head)
    # names std's item; `fmt::Debug::fmt` inside Wrap's own `fmt` must not
    # become a recursion edge onto itself.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_std_path",
        {
            "src/lib.rs": "pub mod frame;\npub mod app;\n",
            "src/frame.rs": _FRAME_RS,
            "src/app.rs": (
                "use std::fmt;\n\n"
                "#[derive(Debug)]\n"
                "pub struct Inner(u8);\n\n"
                "pub struct Wrap(Inner);\n\n"
                "impl fmt::Debug for Wrap {\n"
                "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n"
                "        fmt::Debug::fmt(&self.0, f)\n"
                "    }\n"
                "}\n\n"
                "pub fn build() -> usize {\n"
                "    std::string::String::new().len()\n"
                "}\n"
            ),
        },
    )
    base = "rs_std_path.src"
    assert f"{base}.frame.Frame.new" not in _callees(edges, f"{base}.app.build"), edges
    assert f"{base}.app.Wrap.fmt" not in _callees(edges, f"{base}.app.Wrap.fmt"), edges


def test_external_crate_path_does_not_bind_project_methods(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # mini-redis spells tokio's types by path; the manifest proves `tokio`
    # external, so its `Mutex::new` is no first-party `new`.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_ext_path",
        {
            "Cargo.toml": (
                '[package]\nname = "rs_ext_path"\nversion = "0.1.0"\n\n'
                '[dependencies]\ntokio = "1"\n'
            ),
            "src/lib.rs": "pub mod frame;\npub mod app;\n",
            "src/frame.rs": _FRAME_RS,
            "src/app.rs": (
                "pub fn build() -> u8 {\n"
                "    let m = tokio::sync::Mutex::new(1u8);\n"
                "    m.into_inner()\n"
                "}\n"
            ),
        },
    )
    callees = _callees(edges, "rs_ext_path.src.app.build")
    assert "rs_ext_path.src.frame.Frame.new" not in callees, edges


def test_prelude_type_does_not_bind_an_out_of_scope_same_named_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # app.rs neither defines nor imports a `String`, so `String::new()` is
    # the prelude's; the crate's own `String` lives in another module.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_prelude_scope",
        {
            "src/lib.rs": "pub mod shadow;\npub mod app;\n",
            "src/shadow.rs": (
                "pub struct String { n: usize }\n\n"
                "impl String {\n"
                "    pub fn new() -> String { String { n: 1 } }\n"
                "}\n"
            ),
            "src/app.rs": "pub fn build() -> usize {\n    String::new().len()\n}\n",
        },
    )
    callees = _callees(edges, "rs_prelude_scope.src.app.build")
    assert "rs_prelude_scope.src.shadow.String.new" not in callees, edges


# --- what must keep resolving -----------------------------------------------


def test_first_party_assoc_calls_still_resolve_exact(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The type's own associated fn, by its name, through a `use ... as`
    # alias, and via `Self::` inside its impl.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_own_assoc",
        {
            "src/lib.rs": "pub mod frame;\npub mod app;\n",
            "src/frame.rs": _FRAME_RS
            + (
                "\nimpl Frame {\n"
                "    pub fn empty() -> Frame {\n"
                "        Self::new()\n"
                "    }\n"
                "}\n"
            ),
            "src/app.rs": (
                "use crate::frame::Frame;\n"
                "use crate::frame::Frame as F;\n\n"
                "pub fn build() -> usize {\n"
                "    let a = Frame::new();\n"
                '    let b = F::parse("x");\n'
                "    let _ = (a, b);\n"
                "    1\n"
                "}\n"
            ),
        },
    )
    frame = "rs_own_assoc.src.frame.Frame"
    assert edges.get(("rs_own_assoc.src.app.build", f"{frame}.new")) == {"exact"}
    assert edges.get(("rs_own_assoc.src.app.build", f"{frame}.parse")) == {"exact"}
    assert (f"{frame}.empty", f"{frame}.new") in edges, edges


def test_first_party_type_shadowing_a_prelude_name_resolves_locally(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A crate's own `String`, defined in the module or imported into it,
    # shadows the prelude's and keeps its exact edge.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_shadow_prelude",
        {
            "src/lib.rs": "pub mod shadow;\npub mod user;\n",
            "src/shadow.rs": (
                "pub struct String { n: usize }\n\n"
                "impl String {\n"
                "    pub fn new() -> String { String { n: 1 } }\n"
                "}\n\n"
                "pub fn make() -> String {\n"
                "    String::new()\n"
                "}\n"
            ),
            "src/user.rs": (
                "use crate::shadow::String;\n\n"
                "pub fn make() -> String {\n"
                "    String::new()\n"
                "}\n"
            ),
        },
    )
    target = "rs_shadow_prelude.src.shadow.String.new"
    assert edges.get(("rs_shadow_prelude.src.shadow.make", target)) == {"exact"}
    assert edges.get(("rs_shadow_prelude.src.user.make", target)) == {"exact"}


def test_impl_elsewhere_trait_default_and_type_alias_keep_their_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A method from an impl block in another module, a trait's default
    # method reached through an implementing type, and a type alias (whose
    # target the resolver cannot read) keep binding as before.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_keep_assoc",
        {
            "src/lib.rs": "pub mod frame;\npub mod other;\npub mod greet;\npub mod app;\n",
            "src/frame.rs": _FRAME_RS + "\npub type Alias = Frame;\n",
            "src/other.rs": (
                "use crate::frame::Frame;\n\n"
                "impl Frame {\n"
                "    pub fn split_impl() -> usize { 3 }\n"
                "}\n"
            ),
            "src/greet.rs": (
                "pub trait Greet {\n"
                "    fn hello() -> usize { 1 }\n"
                "}\n\n"
                "pub struct P;\n\n"
                "impl Greet for P {}\n"
            ),
            "src/app.rs": (
                "use crate::frame::{Alias, Frame};\n"
                "use crate::greet::P;\n\n"
                "pub fn build() -> usize {\n"
                "    let _a = Alias::new();\n"
                "    Frame::split_impl() + P::hello()\n"
                "}\n"
            ),
        },
    )
    base = "rs_keep_assoc.src"
    callees = _callees(edges, f"{base}.app.build")
    assert f"{base}.other.Frame.split_impl" in callees, edges
    assert f"{base}.greet.Greet.hello" in callees, edges
    assert f"{base}.frame.Frame.new" in callees, edges


def test_std_type_method_from_a_first_party_impl_still_binds(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `impl From<Foo> for u8` puts a first-party `from` on the std type, so
    # `u8::from(..)` may still reach it; a different type's `from` may not.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_std_impl",
        {
            "src/lib.rs": "pub mod frame;\npub mod conv;\n",
            "src/frame.rs": _FRAME_RS,
            "src/conv.rs": (
                "pub enum Foo { A, B }\n\n"
                "impl From<Foo> for u8 {\n"
                "    fn from(value: Foo) -> Self {\n"
                "        match value {\n"
                "            Foo::A => 0,\n"
                "            Foo::B => 1,\n"
                "        }\n"
                "    }\n"
                "}\n\n"
                "pub fn code() -> u8 {\n"
                "    u8::from(Foo::A)\n"
                "}\n"
            ),
        },
    )
    callees = _callees(edges, "rs_std_impl.src.conv.code")
    assert "rs_std_impl.src.conv.u8.from" in callees, edges
    assert "rs_std_impl.src.frame.Frame.from" not in callees, edges


# --- literal receivers ---------------------------------------------------------


@pytest.mark.parametrize(
    ("receiver", "expected"),
    [
        ('"42"', frozenset({cs.RS_STR_TYPE})),
        ('r#"a.b"#', frozenset({cs.RS_STR_TYPE})),
        ('b"ab"', frozenset()),
        ('br"ab"', frozenset()),
        ("b'a'", frozenset({cs.RS_BYTE_TYPE})),
        ("'c'", frozenset({cs.RS_CHAR_TYPE})),
        ("true", frozenset({cs.RS_BOOL_TYPE})),
        ("1u32", frozenset({"u32"})),
        ("2.5f64", frozenset({"f64"})),
        ("7", cs.RS_NUMERIC_TYPES),
    ],
)
def test_literal_receiver_types(receiver: str, expected: frozenset[str]) -> None:
    assert literal_receiver_types(receiver) == expected


@pytest.mark.parametrize("receiver", ["x", "self", "r#type", "self.items", "Frame"])
def test_non_literal_receiver_has_no_literal_type(receiver: str) -> None:
    # A raw identifier (`r#type`) is a variable, not a raw string.
    assert literal_receiver_types(receiver) is None


# --- #2595 review: an impl block elsewhere of the crate's own `String` -------

_SPLIT_STRING = {
    "src/lib.rs": "pub mod shadow;\npub mod impls;\npub mod paths;\npub mod app;\npub mod user;\n",
    "src/shadow.rs": "pub struct String { n: usize }\n",
    "src/impls.rs": (
        "use crate::shadow::String;\n\n"
        "impl String {\n"
        "    pub fn new() -> String { String { n: 1 } }\n"
        "}\n"
    ),
    "src/paths.rs": (
        "impl crate::shadow::String {\n"
        "    pub fn with_capacity(n: usize) -> crate::shadow::String {\n"
        "        crate::shadow::String { n }\n"
        "    }\n"
        "}\n"
    ),
    "src/app.rs": (
        "pub fn build() -> usize {\n"
        "    String::new().len() + String::with_capacity(4).len()\n"
        "}\n"
    ),
    "src/user.rs": (
        "use crate::shadow::String;\n\n"
        "pub fn make() -> String {\n"
        "    String::new()\n"
        "}\n"
    ),
}


def test_prelude_call_does_not_bind_an_impl_elsewhere_of_the_crates_own_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # app.rs imports no `String`: its `String::new()` is the prelude's, not
    # the crate's `String` whose inherent impl sits in impls.rs (a `use`) or
    # paths.rs (a path).
    edges = _index(temp_repo, mock_ingestor, "rs_split_string", _SPLIT_STRING)
    callees = _callees(edges, "rs_split_string.src.app.build")
    assert "rs_split_string.src.impls.String.new" not in callees, edges
    assert "rs_split_string.src.paths.String.with_capacity" not in callees, edges


def test_imported_own_type_still_reaches_its_impl_elsewhere(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: where the crate's `String` IS in scope, the impl in another
    # module is still its method.
    edges = _index(temp_repo, mock_ingestor, "rs_split_string", _SPLIT_STRING)
    callees = _callees(edges, "rs_split_string.src.user.make")
    assert "rs_split_string.src.impls.String.new" in callees, edges


def test_trait_impl_on_the_prelude_type_still_binds(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: with no crate `String` at all, a trait impl for the
    # prelude's `String` in another module is what `String::describe()`
    # reaches.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_trait_on_std",
        {
            "src/lib.rs": "pub mod describe;\npub mod app;\n",
            "src/describe.rs": (
                "pub trait Describe {\n"
                "    fn describe() -> usize;\n"
                "}\n\n"
                "impl Describe for String {\n"
                "    fn describe() -> usize { 6 }\n"
                "}\n"
            ),
            "src/app.rs": (
                "use crate::describe::Describe;\n\n"
                "pub fn build() -> usize {\n"
                "    String::describe()\n"
                "}\n"
            ),
        },
    )
    callees = _callees(edges, "rs_trait_on_std.src.app.build")
    assert "rs_trait_on_std.src.describe.String.describe" in callees, edges


def test_trait_impl_on_a_std_path_reaches_it_beside_the_crates_own_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # #2595 re-review: the crate's own `String` shares the name, but the
    # impl block is written on `std::string::String` (or a bare `String`
    # nothing in its module rebinds, which is the prelude's), so an explicit
    # std path call reaches it.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_std_path_impl",
        {
            "src/lib.rs": "pub mod shadow;\npub mod describe;\npub mod bare;\npub mod app;\n",
            "src/shadow.rs": "pub struct String { n: usize }\n",
            "src/describe.rs": (
                "pub trait Describe {\n"
                "    fn describe() -> usize;\n"
                "}\n\n"
                "impl Describe for std::string::String {\n"
                "    fn describe() -> usize { 6 }\n"
                "}\n"
            ),
            "src/bare.rs": (
                "pub trait Label {\n"
                "    fn label() -> usize;\n"
                "}\n\n"
                "impl Label for String {\n"
                "    fn label() -> usize { 7 }\n"
                "}\n"
            ),
            "src/app.rs": (
                "use crate::bare::Label;\n"
                "use crate::describe::Describe;\n\n"
                "pub fn build() -> usize {\n"
                "    std::string::String::describe() + String::label()\n"
                "}\n"
            ),
        },
    )
    callees = _callees(edges, "rs_std_path_impl.src.app.build")
    assert "rs_std_path_impl.src.describe.String.describe" in callees, edges
    assert "rs_std_path_impl.src.bare.String.label" in callees, edges
