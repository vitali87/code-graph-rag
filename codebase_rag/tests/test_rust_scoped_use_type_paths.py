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


def test_qualified_constructor_local_is_not_rebound_by_a_scoped_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A local built by a module-qualified struct literal keeps that path, as
    # a declared type does: `crate::a::Builder { .. }` names one type, not
    # whatever `Builder` the body's `use` binds (#2622 with #3168).
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_ctor",
        _two_builders_lib(
            "pub fn constructed() -> u32 {\n"
            "    use crate::b::Builder;\n"
            "    let y = crate::a::Builder { n: 1 };\n"
            "    let _z = Builder::new();\n"
            "    y.build()\n"
            "}\n"
        ),
    )
    caller = "rs_use_ctor.src.lib.constructed"
    assert edges.get((caller, "rs_use_ctor.src.a.Builder.build")) == {"exact"}, edges
    assert (caller, "rs_use_ctor.src.b.Builder.build") not in edges, edges


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


def test_relative_path_through_a_mod_use_binds_its_target_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # After `self::` or `super::`, the next segment may be a `use` binding of
    # the mod the prefix names, renamed or not, and a glob may supply the
    # type itself. Each names a first-party `Builder`, which the inline
    # lookup marked unresolvable, dropping the edge (PR #2791 review).
    files = _inline_mods_lib(
        "rs_inline_alias",
        "\npub mod outer {\n"
        "    use crate::a as alias;\n\n"
        "    pub mod inner {\n"
        "        use crate::b as alias;\n"
        "        use crate::a;\n\n"
        "        pub fn via_self(x: self::alias::Builder) -> u32 {\n"
        "            x.build()\n"
        "        }\n\n"
        "        pub fn via_super(x: super::alias::Builder) -> u32 {\n"
        "            x.build()\n"
        "        }\n\n"
        "        pub fn via_plain(x: self::a::Builder) -> u32 {\n"
        "            x.build()\n"
        "        }\n"
        "    }\n"
        "}\n\n"
        "pub mod globbed {\n"
        "    use crate::b::*;\n\n"
        "    pub fn via_glob(x: self::Builder) -> u32 {\n        x.build()\n    }\n"
        "}\n",
    )
    files["src/lib.rs"] = "pub mod a;\npub mod b;\n\n" + files["src/lib.rs"]
    files["src/a.rs"] = _BUILDER_RS
    files["src/b.rs"] = _BETA_LIB_RS
    edges = _index(temp_repo, mock_ingestor, "rs_inline_alias", files)
    root = "rs_inline_alias.src"
    inner = f"{root}.lib.outer.inner"
    expected = {
        f"{inner}.via_self": f"{root}.b.Builder.build",
        f"{inner}.via_super": f"{root}.a.Builder.build",
        f"{inner}.via_plain": f"{root}.a.Builder.build",
        f"{root}.lib.globbed.via_glob": f"{root}.b.Builder.build",
    }
    for caller, callee in expected.items():
        assert edges.get((caller, callee)) == {"exact"}, edges
        assert _callees(edges, caller) == {callee}, edges


def test_relative_path_through_an_external_alias_binds_no_first_party_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `self::rx::Builder` with `use regex_syntax as rx;` is the registry
    # crate's type: neither the file's `Builder` nor the sibling mod's.
    files = _inline_mods_lib(
        "rs_inline_ext_alias",
        "\npub mod ext {\n"
        "    use regex_syntax as rx;\n\n"
        "    pub fn external(x: self::rx::Builder) -> u32 {\n        x.build()\n    }\n"
        "}\n",
    )
    files["Cargo.toml"] += '\n[dependencies]\nregex-syntax = "0.8"\n'
    edges = _index(temp_repo, mock_ingestor, "rs_inline_ext_alias", files)
    callees = _callees(edges, "rs_inline_ext_alias.src.lib.ext.external")
    assert not {c for c in callees if c.endswith(".Builder.build")}, edges


# --- a scoped type's methods written away from its definition -----------------


def _crate(project: str, files: dict[str, str], deps: str = "") -> dict[str, str]:
    manifest = f'[package]\nname = "{project}"\nversion = "0.1.0"\n'
    if deps:
        manifest += f"\n[dependencies]\n{deps}"
    return {"Cargo.toml": manifest, **files}


def _tests_mod(uses: str, body: str) -> str:
    return (
        f"\n#[cfg(test)]\nmod tests {{\n{uses}\n"
        f"    #[test]\n    fn t() {{\n{body}    }}\n}}\n"
    )


def test_scoped_use_reaches_an_impl_block_in_another_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `impl Builder` sits in `builder_impl`, away from the struct, so its
    # `new` registers under `builder_impl.Builder`, not the struct's qn. The
    # test module's `use` names the struct, and the impl block's own path
    # names it too (PR #2791 review).
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_impl_away",
        _crate(
            "rs_use_impl_away",
            {
                "src/lib.rs": "pub mod builder_impl;\npub mod model;\n"
                + _tests_mod(
                    "    use crate::model::Builder;\n",
                    "        let _b = Builder::new();\n        Builder::reset();\n",
                ),
                "src/model.rs": "pub struct Builder {\n    pub n: u32,\n}\n",
                "src/builder_impl.rs": (
                    "use crate::model::Builder;\n\n"
                    "impl Builder {\n"
                    "    pub fn new() -> Builder {\n        Builder { n: 1 }\n    }\n"
                    "}\n\n"
                    "impl crate::model::Builder {\n    pub fn reset() {}\n}\n"
                ),
            },
        ),
    )
    caller = "rs_use_impl_away.src.lib.tests.t"
    impl = "rs_use_impl_away.src.builder_impl.Builder"
    assert edges.get((caller, f"{impl}.new")) == {"exact"}, edges
    assert edges.get((caller, f"{impl}.reset")) == {"exact"}, edges
    assert _callees(edges, caller) == {f"{impl}.new", f"{impl}.reset"}, edges


def test_scoped_use_reaches_a_trait_default_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `Builder` does not override `describe`, so the call runs the trait's
    # default body, registered under the trait (PR #2791 review).
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_trait_default",
        _crate(
            "rs_use_trait_default",
            {
                "src/lib.rs": "pub mod model;\n"
                + _tests_mod(
                    "    use crate::model::Builder;\n    use crate::model::Describe;\n",
                    "        let _n = Builder::describe();\n",
                ),
                "src/model.rs": (
                    "pub struct Builder;\n\n"
                    "pub trait Describe {\n"
                    "    fn describe() -> u32 {\n        7\n    }\n"
                    "}\n\n"
                    "impl Describe for Builder {}\n"
                ),
            },
        ),
    )
    caller = "rs_use_trait_default.src.lib.tests.t"
    target = "rs_use_trait_default.src.model.Describe.describe"
    assert _callees(edges, caller) == {target}, edges


def test_scoped_use_of_external_type_reaches_a_first_party_impl_on_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The registry crate's `Parser` is not in the graph, but the crate's own
    # `impl Make for regex_syntax::Parser` is, and the call runs it
    # (PR #2791 review).
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_ext_impl",
        _crate(
            "rs_use_ext_impl",
            {
                "src/lib.rs": "pub mod ext;\n"
                + _tests_mod(
                    "    use crate::ext::Make;\n    use regex_syntax::Parser;\n",
                    "        let _p = Parser::make();\n",
                ),
                "src/ext.rs": (
                    "pub trait Make {\n    fn make() -> Self;\n}\n\n"
                    "impl Make for regex_syntax::Parser {\n"
                    "    fn make() -> Self {\n"
                    "        regex_syntax::Parser::new()\n"
                    "    }\n"
                    "}\n"
                ),
            },
            deps='regex-syntax = "0.8"\n',
        ),
    )
    caller = "rs_use_ext_impl.src.lib.tests.t"
    assert "rs_use_ext_impl.src.ext.Parser.make" in _callees(edges, caller), edges


def test_scoped_use_ignores_impls_away_on_the_shadowed_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # alpha's own `Builder` gains `reset` and the `Describe` default from
    # blocks in another module, which register under `ext.Builder` by name
    # alone. The test module's `use` names beta's `Builder`, so neither is
    # the target: only a block whose written self type names beta's counts.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_shadow_away",
        {
            **_WORKSPACE,
            "crates/alpha/src/lib.rs": "pub mod ext;\n\n"
            + _ALPHA_LIB_RS
            + _tests_mod(
                "    use crate::ext::Describe;\n    use my_beta::Builder;\n",
                "        let _r = Builder::reset();\n"
                "        let _d = Builder::describe();\n",
            ),
            "crates/alpha/src/ext.rs": (
                "use crate::Builder;\n\n"
                "pub trait Describe {\n"
                "    fn describe() -> u32 {\n        7\n    }\n"
                "}\n\n"
                "impl Builder {\n    pub fn reset() -> u32 {\n        0\n    }\n}\n\n"
                "impl Describe for Builder {}\n"
            ),
        },
    )
    callees = _callees(edges, "rs_use_shadow_away.crates.alpha.src.lib.tests.t")
    assert not {c for c in callees if c.endswith((".reset", ".describe"))}, edges


def test_local_type_shadowing_an_import_ignores_the_import_impl_away(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `mod inner` defines its own `Builder`, which shadows the file's
    # imported one there; `make` exists only in a block on the imported
    # type, written in another module.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_local_shadow_away",
        _crate(
            "rs_local_shadow_away",
            {
                "src/lib.rs": (
                    "pub mod model;\npub mod model_impl;\n\n"
                    "use crate::model::Builder;\n\n"
                    "pub fn outer() {\n    Builder::make();\n}\n\n"
                    "pub mod inner {\n"
                    "    pub struct Builder;\n\n"
                    "    pub fn shadowed() {\n        Builder::make();\n    }\n"
                    "}\n"
                ),
                "src/model.rs": "pub struct Builder;\n",
                "src/model_impl.rs": (
                    "use crate::model::Builder;\n\n"
                    "impl Builder {\n    pub fn make() {}\n}\n"
                ),
            },
        ),
    )
    lib = "rs_local_shadow_away.src.lib"
    target = "rs_local_shadow_away.src.model_impl.Builder.make"
    assert target in _callees(edges, f"{lib}.outer"), edges
    assert not _callees(edges, f"{lib}.inner.shadowed"), edges


def test_scoped_use_of_external_type_ignores_an_impl_on_a_same_named_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The crate's own `Parser` gets `Make` from a block in another module;
    # the test module's `Parser` is the registry crate's, which has none.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_use_ext_shadow",
        _crate(
            "rs_use_ext_shadow",
            {
                "src/lib.rs": "pub mod ext;\n\npub struct Parser;\n"
                + _tests_mod(
                    "    use crate::ext::Make;\n    use regex_syntax::Parser;\n",
                    "        let _p = Parser::make();\n",
                ),
                "src/ext.rs": (
                    "use crate::Parser;\n\n"
                    "pub trait Make {\n    fn make() -> Self;\n}\n\n"
                    "impl Make for Parser {\n"
                    "    fn make() -> Self {\n        Parser\n    }\n"
                    "}\n"
                ),
            },
            deps='regex-syntax = "0.8"\n',
        ),
    )
    callees = _callees(edges, "rs_use_ext_shadow.src.lib.tests.t")
    assert not {c for c in callees if c.endswith(".make")}, edges


def test_block_scoped_use_reaches_only_its_type_impl_away(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A `use` in a const initializer block binds `Builder` for that block,
    # whose `Builder::zero()` the scope decides the same way: the model's
    # block in `builder_impl` answers, the crate's own `Builder`'s block in
    # `ext` never does, for `one`'s model type or `two`'s.
    edges = _index(
        temp_repo,
        mock_ingestor,
        "rs_block_impl_away",
        _crate(
            "rs_block_impl_away",
            {
                "src/lib.rs": (
                    "pub mod builder_impl;\npub mod ext;\npub mod model;\n\n"
                    "pub struct Builder;\n\n"
                    "pub fn one() -> u32 {\n"
                    "    const Y: u32 = {\n"
                    "        use crate::model::Builder;\n"
                    "        Builder::zero()\n"
                    "    };\n"
                    "    Y\n"
                    "}\n\n"
                    "pub fn two() -> u32 {\n"
                    "    const Z: u32 = {\n"
                    "        use crate::model::Builder;\n"
                    "        Builder::one()\n"
                    "    };\n"
                    "    Z\n"
                    "}\n"
                ),
                "src/model.rs": "pub struct Builder;\n",
                "src/builder_impl.rs": (
                    "use crate::model::Builder;\n\n"
                    "impl Builder {\n"
                    "    pub const fn zero() -> u32 {\n        0\n    }\n"
                    "}\n"
                ),
                "src/ext.rs": (
                    "use crate::Builder;\n\n"
                    "impl Builder {\n"
                    "    pub const fn zero() -> u32 {\n        1\n    }\n"
                    "    pub const fn one() -> u32 {\n        1\n    }\n"
                    "}\n"
                ),
            },
        ),
    )
    lib = "rs_block_impl_away.src.lib"
    target = "rs_block_impl_away.src.builder_impl.Builder.zero"
    assert edges.get((f"{lib}.one", target)) == {"exact"}, edges
    assert _callees(edges, f"{lib}.one") == {target}, edges
    assert not _callees(edges, f"{lib}.two"), edges
