"""A Rust `use` that names `Type` decides what `Type::f()` calls.

`Type::new()` and the methods called on its result bound `Type` to a
first-party type found by simple name, even when the calling scope imported
`Type` from somewhere else: a manifest-declared external crate
(`use regex_syntax::Parser;`), or a dev-dependency imported inside
`mod tests { use my_beta::Builder; }` while the enclosing crate defines its
own `Builder`. The edge was labelled exact and could land in a crate the
caller does not depend on at all (issue #2622: ripgrep's
`use regex_syntax::Parser; Parser::new().parse(p)` became a call to rg's own
command-line `Parser`).
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _write,
    create_and_run_updater,
)

_BUILDER_RS = (
    "pub struct Builder {\n    n: u32,\n}\n\n"
    "impl Builder {\n"
    "    pub fn new() -> Builder {\n        Builder { n: 1 }\n    }\n"
    "    pub fn build(&self) -> u32 {\n        self.n\n    }\n"
    "}\n\n"
)

_ALPHA_LIB_RS = _BUILDER_RS + (
    "pub struct Parser;\n\n"
    "impl Parser {\n"
    "    pub fn new() -> Parser {\n        Parser\n    }\n"
    "    pub fn parse(&self, _s: &str) -> Result<(), ()> {\n        Ok(())\n    }\n"
    "}\n"
)

_BETA_LIB_RS = (
    "pub struct Builder {\n    n: u32,\n}\n\n"
    "impl Builder {\n"
    "    pub fn new() -> Builder {\n        Builder { n: 2 }\n    }\n"
    "    pub fn build(&self) -> u32 {\n        self.n * 2\n    }\n"
    "}\n\n"
    "pub struct Other;\n"
)

_WORKSPACE = {
    "Cargo.toml": (
        '[workspace]\nmembers = ["crates/alpha", "crates/beta", "crates/app"]\n'
    ),
    "crates/alpha/Cargo.toml": (
        '[package]\nname = "my-alpha"\nversion = "0.1.0"\n\n'
        '[dev-dependencies]\nmy-beta = { path = "../beta" }\n'
    ),
    "crates/beta/Cargo.toml": '[package]\nname = "my-beta"\nversion = "0.1.0"\n',
    "crates/beta/src/lib.rs": _BETA_LIB_RS,
    "crates/app/Cargo.toml": (
        '[package]\nname = "app"\nversion = "0.1.0"\n\n'
        '[dependencies]\nmy-beta = { path = "../beta" }\nregex-syntax = "0.8"\n'
    ),
}


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


# --- the issue: a `use` in scope loses to a same-named first-party type ------


def test_file_use_of_external_crate_type_binds_no_first_party_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's first row: `regex_syntax` is a registry dependency of
    # `app`, so neither `Parser::new()` nor `.parse` on its result is a call
    # into `alpha`, a crate `app` does not even depend on.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_ext_file",
        {
            **_WORKSPACE,
            "crates/alpha/src/lib.rs": _ALPHA_LIB_RS,
            "crates/app/src/lib.rs": "pub mod top;\n",
            "crates/app/src/top.rs": (
                "use regex_syntax::Parser;\n\n"
                "pub fn top_level() -> bool {\n"
                '    Parser::new().parse("a+").is_ok()\n'
                "}\n"
            ),
        },
    )
    leaked = {
        callee
        for callee in _callees(edges, "rs_use_ext_file.crates.app.src.top.top_level")
        if callee.startswith("rs_use_ext_file.crates.alpha.")
    }
    assert not leaked, edges


def test_fn_scoped_use_of_external_crate_type_binds_no_first_party_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # ripgrep's crates/regex/src/ban.rs writes the `use` inside the function.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_ext_fn",
        {
            **_WORKSPACE,
            "crates/alpha/src/lib.rs": _ALPHA_LIB_RS,
            "crates/app/src/lib.rs": "pub mod inner;\n",
            "crates/app/src/inner.rs": (
                "pub fn fn_scoped() -> bool {\n"
                "    use regex_syntax::Parser;\n"
                '    Parser::new().parse("a+").is_ok()\n'
                "}\n"
            ),
        },
    )
    leaked = {
        callee
        for callee in _callees(edges, "rs_use_ext_fn.crates.app.src.inner.fn_scoped")
        if callee.startswith("rs_use_ext_fn.crates.alpha.")
    }
    assert not leaked, edges


def test_inline_mod_use_binds_the_dev_dependency_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's second row: `mod tests` imports beta's `Builder`, which
    # shadows alpha's own for every path in the module (`cargo test` sees
    # `b.build() == 4`, beta's value).
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_mod",
        {
            **_WORKSPACE,
            "crates/alpha/src/lib.rs": _ALPHA_LIB_RS
            + (
                "\n#[cfg(test)]\nmod tests {\n"
                "    use my_beta::Builder;\n\n"
                "    #[test]\n    fn uses_beta() {\n"
                "        let b = Builder::new();\n"
                "        assert_eq!(b.build(), 4);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    caller = "rs_use_mod.crates.alpha.src.lib.tests.uses_beta"
    beta = "rs_use_mod.crates.beta.src.lib.Builder"
    alpha = "rs_use_mod.crates.alpha.src.lib.Builder"
    assert edges.get((caller, f"{beta}.new")) == {"exact"}, edges
    assert edges.get((caller, f"{beta}.build")) == {"exact"}, edges
    assert (caller, f"{alpha}.new") not in edges, edges
    assert (caller, f"{alpha}.build") not in edges, edges


def test_inline_mod_use_binds_a_chained_call_on_the_imported_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_mod_chain",
        {
            **_WORKSPACE,
            "crates/alpha/src/lib.rs": _ALPHA_LIB_RS
            + (
                "\n#[cfg(test)]\nmod tests {\n"
                "    use my_beta::Builder;\n\n"
                "    #[test]\n    fn chained() {\n"
                "        let v = Builder::new().build();\n"
                "        assert_eq!(v, 4);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    caller = "rs_use_mod_chain.crates.alpha.src.lib.tests.chained"
    beta = "rs_use_mod_chain.crates.beta.src.lib.Builder"
    alpha = "rs_use_mod_chain.crates.alpha.src.lib.Builder"
    assert edges.get((caller, f"{beta}.build")) == {"exact"}, edges
    assert (caller, f"{alpha}.build") not in edges, edges


def test_inline_mod_use_of_external_crate_type_binds_no_first_party_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A test module importing a dev-dependency from the registry, beside a
    # crate that defines a type of the same name.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_mod_ext",
        {
            **_WORKSPACE,
            "crates/alpha/Cargo.toml": (
                '[package]\nname = "my-alpha"\nversion = "0.1.0"\n\n'
                '[dev-dependencies]\nregex-syntax = "0.8"\n'
            ),
            "crates/alpha/src/lib.rs": _ALPHA_LIB_RS
            + (
                "\n#[cfg(test)]\nmod tests {\n"
                "    use regex_syntax::Parser;\n\n"
                "    #[test]\n    fn external() {\n"
                '        let ok = Parser::new().parse("a+").is_ok();\n'
                "        assert!(ok);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    alpha = "rs_use_mod_ext.crates.alpha.src.lib.Parser"
    callees = _callees(edges, "rs_use_mod_ext.crates.alpha.src.lib.tests.external")
    assert f"{alpha}.new" not in callees, edges
    assert f"{alpha}.parse" not in callees, edges


def test_fn_scoped_use_of_first_party_type_shadows_the_module_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A function-body `use` shadows the module's own item of that name for
    # the rest of the body, the receiver it types included.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_fn_local",
        {
            "Cargo.toml": '[package]\nname = "rs_use_fn_local"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod other;\n\n"
            + _BUILDER_RS
            + (
                "pub fn shadowed() -> u32 {\n"
                "    use crate::other::Builder;\n"
                "    let b = Builder::new();\n"
                "    b.build() + Builder::new().build()\n"
                "}\n"
            ),
            "src/other.rs": _BETA_LIB_RS,
        },
    )
    caller = "rs_use_fn_local.src.lib.shadowed"
    other = "rs_use_fn_local.src.other.Builder"
    own = "rs_use_fn_local.src.lib.Builder"
    assert edges.get((caller, f"{other}.new")) == {"exact"}, edges
    assert edges.get((caller, f"{other}.build")) == {"exact"}, edges
    assert (caller, f"{own}.new") not in edges, edges
    assert (caller, f"{own}.build") not in edges, edges


def test_scoped_type_without_the_item_binds_no_other_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # beta's `Builder` has no `reset` in the graph (a derive or a macro
    # could supply it), while alpha's shadowed `Builder` and an unrelated
    # `Parser` both define one. The path names beta's type, so neither of
    # them is the target, however close it sits.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_mod_miss",
        {
            **_WORKSPACE,
            "crates/alpha/src/lib.rs": _ALPHA_LIB_RS
            + (
                "\nimpl Builder {\n    pub fn reset() -> u32 {\n        0\n    }\n}\n\n"
                "impl Parser {\n    pub fn reset() -> u32 {\n        1\n    }\n}\n\n"
                "#[cfg(test)]\nmod tests {\n"
                "    use my_beta::Builder;\n\n"
                "    #[test]\n    fn missing() {\n"
                "        let v = Builder::reset();\n"
                "        assert_eq!(v, 0);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    callees = _callees(edges, "rs_use_mod_miss.crates.alpha.src.lib.tests.missing")
    assert not {c for c in callees if c.endswith(".reset")}, edges


def _two_builders_lib(body: str) -> dict[str, str]:
    return {
        "Cargo.toml": '[package]\nname = "two_builders"\nversion = "0.1.0"\n',
        "src/lib.rs": "pub mod a;\npub mod b;\n\n" + body,
        "src/a.rs": _BUILDER_RS,
        "src/b.rs": _BETA_LIB_RS,
    }


def test_qualified_declared_type_is_not_rebound_by_a_scoped_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `x` and `y` are declared as `crate::a::Builder`, a path that names one
    # type wherever it is written; the body's `use crate::b::Builder;` binds
    # only the bare name `Builder` (PR #2791 review).
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_qualified",
        _two_builders_lib(
            "pub fn qualified(x: crate::a::Builder) -> u32 {\n"
            "    use crate::b::Builder;\n"
            "    let y: crate::a::Builder = crate::a::Builder::new();\n"
            "    let _z = Builder::new();\n"
            "    x.build() + y.build()\n"
            "}\n"
        ),
    )
    caller = "rs_use_qualified.src.lib.qualified"
    assert edges.get((caller, "rs_use_qualified.src.a.Builder.build")) == {"exact"}, (
        edges
    )
    assert (caller, "rs_use_qualified.src.b.Builder.build") not in edges, edges


# --- what must keep resolving as before ---------------------------------------


def test_local_type_path_still_binds_the_local_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # No `use` names `Builder` here, so the crate's own type is meant: at
    # module level, and in a test module that glob-imports its parent.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_local",
        {
            **_WORKSPACE,
            "crates/alpha/src/lib.rs": _ALPHA_LIB_RS
            + (
                "\npub fn make() -> u32 {\n"
                "    let b = Builder::new();\n"
                "    b.build() + Builder::new().build()\n"
                "}\n\n"
                "#[cfg(test)]\nmod tests {\n"
                "    use super::*;\n\n"
                "    #[test]\n    fn own() {\n"
                "        let v = Builder::new().build();\n"
                "        assert_eq!(v, 1);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    alpha = "rs_use_local.crates.alpha.src.lib.Builder"
    make = "rs_use_local.crates.alpha.src.lib.make"
    own = "rs_use_local.crates.alpha.src.lib.tests.own"
    assert edges.get((make, f"{alpha}.new")) == {"exact"}, edges
    assert edges.get((make, f"{alpha}.build")) == {"exact"}, edges
    assert edges.get((own, f"{alpha}.new")) == {"exact"}, edges
    assert edges.get((own, f"{alpha}.build")) == {"exact"}, edges


def test_imported_dependency_type_is_not_captured_by_a_same_named_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `app` imports beta's `Builder` at file level; alpha's `Builder`, in a
    # crate `app` does not depend on, and a same-named type in app's own
    # sibling module never capture the call.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_dep",
        {
            **_WORKSPACE,
            "crates/alpha/src/lib.rs": _ALPHA_LIB_RS,
            "crates/app/src/lib.rs": "pub mod shapes;\npub mod top;\n",
            "crates/app/src/shapes.rs": _ALPHA_LIB_RS,
            "crates/app/src/top.rs": (
                "use my_beta::Builder;\n\n"
                "pub fn top_level() -> u32 {\n"
                "    let b = Builder::new();\n"
                "    b.build() + Builder::new().build()\n"
                "}\n"
            ),
        },
    )
    caller = "rs_use_dep.crates.app.src.top.top_level"
    beta = "rs_use_dep.crates.beta.src.lib.Builder"
    assert edges.get((caller, f"{beta}.new")) == {"exact"}, edges
    assert edges.get((caller, f"{beta}.build")) == {"exact"}, edges
    assert _callees(edges, caller) == {f"{beta}.new", f"{beta}.build"}, edges


def test_scoped_use_of_a_different_name_leaves_the_call_alone(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The test module and the function each import something, but not
    # `Builder`, so the crate's own `Builder` still answers.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_other",
        {
            **_WORKSPACE,
            "crates/alpha/src/lib.rs": _ALPHA_LIB_RS
            + (
                "\npub fn fn_use() -> u32 {\n"
                "    use std::fmt::Write;\n"
                "    Builder::new().build()\n"
                "}\n\n"
                "#[cfg(test)]\nmod tests {\n"
                "    use super::Builder;\n"
                "    use my_beta::Other;\n\n"
                "    #[test]\n    fn other_name() {\n"
                "        let _o = Other;\n"
                "        let b = Builder::new();\n"
                "        assert_eq!(b.build(), 1);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    alpha = "rs_use_other.crates.alpha.src.lib.Builder"
    beta = "rs_use_other.crates.beta.src.lib.Builder"
    fn_use = "rs_use_other.crates.alpha.src.lib.fn_use"
    other_name = "rs_use_other.crates.alpha.src.lib.tests.other_name"
    assert edges.get((fn_use, f"{alpha}.new")) == {"exact"}, edges
    assert edges.get((fn_use, f"{alpha}.build")) == {"exact"}, edges
    assert edges.get((other_name, f"{alpha}.new")) == {"exact"}, edges
    assert edges.get((other_name, f"{alpha}.build")) == {"exact"}, edges
    assert (other_name, f"{beta}.new") not in edges, edges


def test_bare_declared_type_follows_the_scoped_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Spelled bare, the declared type is whatever the scope binds `Builder`
    # to, and the body's `use` shadows the module's import.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_bare_decl",
        _two_builders_lib(
            "use crate::a::Builder;\n\n"
            "pub fn bare(y: Builder) -> u32 {\n"
            "    use crate::b::Builder;\n"
            "    let x: Builder = Builder::new();\n"
            "    x.build() + y.build()\n"
            "}\n"
        ),
    )
    caller = "rs_use_bare_decl.src.lib.bare"
    assert edges.get((caller, "rs_use_bare_decl.src.b.Builder.build")) == {"exact"}, (
        edges
    )


# --- a module path written in an inline mod counts from that mod ---------------


def _builder_in(module: str, n: int) -> str:
    # A `Builder` with `new`/`build` inside `mod <module>`, or at file level.
    body = (
        "pub struct Builder {\n    n: u32,\n}\n\n"
        "impl Builder {\n"
        f"    pub fn new() -> Builder {{\n        Builder {{ n: {n} }}\n    }}\n"
        "    pub fn build(&self) -> u32 {\n        self.n\n    }\n"
        "}\n"
    )
    if not module:
        return body
    inner = "".join(f"    {line}" if line else line for line in body.splitlines(True))
    return f"pub mod {module} {{\n{inner}}}\n"


def _inline_mods_lib(project: str, body: str) -> dict[str, str]:
    # A file-level `Builder` and a `sibling` mod holding another, written
    # before the mod under test so a search by name meets them first.
    return {
        "Cargo.toml": f'[package]\nname = "{project}"\nversion = "0.1.0"\n',
        "src/lib.rs": _builder_in("", 1) + "\n" + _builder_in("sibling", 3) + body,
    }


def test_self_path_in_inline_mod_binds_the_mod_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `self::` inside `mod inner` is `inner`, never the file: the receiver
    # declared `self::Builder` bound the file's `Builder` (PR #2791 review).
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_inline_self",
        _inline_mods_lib(
            "rs_inline_self",
            "\npub mod inner {\n"
            "    pub struct Builder;\n\n"
            "    impl Builder {\n"
            "        pub fn new() -> Builder {\n            Builder\n        }\n"
            "        pub fn build(&self) -> u32 {\n            2\n        }\n"
            "    }\n\n"
            "    pub fn param(x: self::Builder) -> u32 {\n        x.build()\n    }\n\n"
            "    pub fn local() -> u32 {\n"
            "        let y: self::Builder = self::Builder::new();\n"
            "        y.build()\n"
            "    }\n"
            "}\n",
        ),
    )
    lib = "rs_inline_self.src.lib"
    for caller in (f"{lib}.inner.param", f"{lib}.inner.local"):
        assert edges.get((caller, f"{lib}.inner.Builder.build")) == {"exact"}, edges
        assert (caller, f"{lib}.Builder.build") not in edges, edges
        assert (caller, f"{lib}.sibling.Builder.build") not in edges, edges


def test_super_path_in_nested_inline_mod_binds_the_parent_mod_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `super::` inside `mod inner { mod deeper { .. } }` is `inner`. Read
    # through the file module, the path counted from the file and bound its
    # `Builder`; read as the bare leaf, `deeper`'s own `Builder` answered
    # (PR #2791 review). Two `super::`s reach the file.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_inline_super",
        _inline_mods_lib(
            "rs_inline_super",
            "\npub mod inner {\n"
            "    pub struct Builder;\n\n"
            "    impl Builder {\n"
            "        pub fn build(&self) -> u32 {\n            2\n        }\n"
            "    }\n\n"
            "    pub mod deeper {\n"
            "        pub struct Builder;\n\n"
            "        impl Builder {\n"
            "            pub fn build(&self) -> u32 {\n                4\n            }\n"
            "        }\n\n"
            "        pub fn up_one(x: super::Builder) -> u32 {\n"
            "            x.build()\n"
            "        }\n\n"
            "        pub fn up_two(x: super::super::Builder) -> u32 {\n"
            "            x.build()\n"
            "        }\n"
            "    }\n"
            "}\n",
        ),
    )
    lib = "rs_inline_super.src.lib"
    up_one = f"{lib}.inner.deeper.up_one"
    up_two = f"{lib}.inner.deeper.up_two"
    assert edges.get((up_one, f"{lib}.inner.Builder.build")) == {"exact"}, edges
    assert _callees(edges, up_one) == {f"{lib}.inner.Builder.build"}, edges
    assert edges.get((up_two, f"{lib}.Builder.build")) == {"exact"}, edges
    assert _callees(edges, up_two) == {f"{lib}.Builder.build"}, edges


def test_bare_path_in_inline_mod_binds_the_mod_child_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `kinds::Builder` inside `mod inner` names `inner::kinds`, not the
    # file's own `kinds`.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_inline_bare",
        _inline_mods_lib(
            "rs_inline_bare",
            "\n" + _builder_in("kinds", 4) + "\npub mod inner {\n"
            "    pub mod kinds {\n"
            "        pub struct Builder;\n\n"
            "        impl Builder {\n"
            "            pub fn build(&self) -> u32 {\n                5\n            }\n"
            "        }\n"
            "    }\n\n"
            "    pub fn child(x: kinds::Builder) -> u32 {\n        x.build()\n    }\n"
            "}\n",
        ),
    )
    lib = "rs_inline_bare.src.lib"
    caller = f"{lib}.inner.child"
    assert edges.get((caller, f"{lib}.inner.kinds.Builder.build")) == {"exact"}, edges
    assert _callees(edges, caller) == {f"{lib}.inner.kinds.Builder.build"}, edges


def test_module_path_without_inline_shadowing_binds_the_file_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # No inline mod defines `Builder` here: a file-level `self::`, an inline
    # mod's `super::`, and an inline mod's `self::` reaching the file's type
    # through `use super::*;` all name the file's `Builder`, and the
    # sibling mod's never answers.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_inline_none",
        _inline_mods_lib(
            "rs_inline_none",
            "\npub fn file_self(x: self::Builder) -> u32 {\n    x.build()\n}\n\n"
            "pub mod plain {\n"
            "    pub fn up(x: super::Builder) -> u32 {\n        x.build()\n    }\n"
            "}\n\n"
            "pub mod globbed {\n"
            "    use super::*;\n\n"
            "    pub fn via_glob(x: self::Builder) -> u32 {\n        x.build()\n    }\n"
            "}\n",
        ),
    )
    lib = "rs_inline_none.src.lib"
    for caller in (f"{lib}.file_self", f"{lib}.plain.up", f"{lib}.globbed.via_glob"):
        assert edges.get((caller, f"{lib}.Builder.build")) == {"exact"}, edges
        assert _callees(edges, caller) == {f"{lib}.Builder.build"}, edges


def test_inline_mod_path_to_an_external_type_binds_no_sibling_or_file_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `mod ext` takes its `Builder` from a registry crate's glob, so its
    # `self::Builder` is that crate's: neither the file's `Builder` nor the
    # sibling mod's may answer for it.
    files = _inline_mods_lib(
        "rs_inline_ext",
        "\npub mod ext {\n"
        "    use regex_syntax::*;\n\n"
        "    pub fn external(x: self::Builder) -> u32 {\n        x.build()\n    }\n"
        "}\n",
    )
    files["Cargo.toml"] += '\n[dependencies]\nregex-syntax = "0.8"\n'
    edges = _index(temp_repo, mock_ingestor, "rs_inline_ext", files)
    callees = _callees(edges, "rs_inline_ext.src.lib.ext.external")
    assert not {c for c in callees if c.endswith(".Builder.build")}, edges
