"""Rust types reached through a `use` re-export keep their method edges.

A crate usually exports its types from `lib.rs` or a `mod.rs` rather than
from the defining file (`pub use get::Get;`), and consumers import them from
there (`use crate::cmd::Get;`). Since the 2018 edition that bare `get::Get`
path starts at the `get` module declared beside it, but it was recorded as a
path into an external crate named `get`. Every method called on a value of
the re-exported type then had no edge, and `Type::new()` or a field receiver
fell back to a same-named type elsewhere (issue #2542).

Every fixture keeps a same-named decoy type in `aaa.rs`, which sorts first,
so a name-only fallback would land on it: only a resolved re-export gives
the edges asserted here. Every fixture but the malformed named cycle passes
`cargo check`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_CARGO = '[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'

_GET_RS = """\
pub struct Get {
    key: String,
}

impl Get {
    pub fn new(key: &str) -> Get {
        Get {
            key: key.to_string(),
        }
    }

    pub fn into_frame(self) -> String {
        self.key
    }
}
"""

_CONSUMER_RS = """\
{use_line}

pub fn chained(k: &str) -> String {{
    {ty}::new(k).into_frame()
}}

pub fn local(k: &str) -> String {{
    let g = {ty}::new(k);
    g.into_frame()
}}

pub fn param(g: {ty}) -> String {{
    g.into_frame()
}}

pub struct Handler {{
    g: {ty},
}}

impl Handler {{
    pub fn run(self) -> String {{
        self.g.into_frame()
    }}
}}
"""

_NEW_CALLERS = ("chained", "local")
_METHOD_CALLERS = ("chained", "local", "param", "Handler.run")


def _consumer(use_line: str, ty: str = "Get") -> str:
    return _CONSUMER_RS.format(use_line=use_line, ty=ty)


def _index(
    temp_repo: Path, mock_ingestor: MagicMock, name: str, files: dict[str, str]
) -> str:
    project = temp_repo / name
    for rel_path, source in {"Cargo.toml": _CARGO.format(name=name), **files}.items():
        target = project / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    return f"{name}.src"


def _calls(mock_ingestor: MagicMock) -> dict[tuple[str, str], str]:
    edges: dict[tuple[str, str], str] = {}
    for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value):
        props = call.kwargs.get("properties") or (
            call.args[3] if len(call.args) > 3 else {}
        )
        edges[(str(call.args[0][2]), str(call.args[2][2]))] = str(
            (props or {}).get(cs.KEY_RESOLUTION)
        )
    return edges


def _imports(mock_ingestor: MagicMock) -> set[tuple[str, str, str]]:
    return {
        (str(call.args[0][2]), str(call.args[2][0]), str(call.args[2][2]))
        for call in get_relationships(mock_ingestor, cs.RelationshipType.IMPORTS.value)
    }


def _assert_bound(
    calls: dict[tuple[str, str], str], caller_module: str, type_qn: str
) -> None:
    for caller in _NEW_CALLERS:
        edge = (f"{caller_module}.{caller}", f"{type_qn}.new")
        assert calls.get(edge) == cs.EdgeResolution.EXACT, (edge, calls)
    for caller in _METHOD_CALLERS:
        edge = (f"{caller_module}.{caller}", f"{type_qn}.into_frame")
        assert calls.get(edge) == cs.EdgeResolution.EXACT, (edge, calls)
    elsewhere = [
        (src, dst)
        for src, dst in calls
        if src.startswith(f"{caller_module}.") and not dst.startswith(f"{type_qn}.")
    ]
    assert not elsewhere, elsewhere


def test_mod_rs_bare_reexport_binds_methods_on_the_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's shape: cmd/mod.rs re-exports `get::Get` with a bare
    # 2018-edition path and the consumer imports `crate::cmd::Get`.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_modrs",
        {
            "src/lib.rs": "pub mod aaa;\npub mod client;\npub mod cmd;\n",
            "src/cmd/mod.rs": "pub mod get;\npub use get::Get;\n",
            "src/cmd/get.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::cmd::Get;"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.cmd.get.Get")


def test_lib_rs_bare_reexport_binds_methods_on_the_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_lib",
        {
            "src/lib.rs": (
                "pub mod aaa;\npub mod client;\npub mod inner;\npub use inner::Get;\n"
            ),
            "src/inner.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::Get;"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.inner.Get")


def test_crate_root_private_use_binds_field_receiver(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # mini-redis: lib.rs brings `Shutdown` in with a PRIVATE `use`, which the
    # whole crate may still name as `crate::Shutdown` (a crate root's private
    # items are visible to every module in it). `self.shutdown.is_shutdown()`
    # used to fall back to db::Shared's same-named method.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_root_private",
        {
            "src/lib.rs": (
                "pub mod db;\npub mod server;\npub mod shutdown;\n"
                "use shutdown::Shutdown;\n"
            ),
            "src/shutdown.rs": (
                "pub struct Shutdown {\n    done: bool,\n}\n\n"
                "impl Shutdown {\n"
                "    pub fn is_shutdown(&self) -> bool {\n        self.done\n    }\n"
                "}\n"
            ),
            "src/db.rs": (
                "pub struct Shared {\n    done: bool,\n}\n\n"
                "impl Shared {\n"
                "    pub fn is_shutdown(&self) -> bool {\n        self.done\n    }\n"
                "}\n"
            ),
            "src/server.rs": (
                "use crate::Shutdown;\n\n"
                "pub struct Handler {\n    shutdown: Shutdown,\n}\n\n"
                "impl Handler {\n"
                "    pub fn run(&self) -> bool {\n"
                "        self.shutdown.is_shutdown()\n    }\n"
                "}\n"
            ),
        },
    )
    calls = _calls(mock_ingestor)
    run = f"{base}.server.Handler.run"
    assert calls.get((run, f"{base}.shutdown.Shutdown.is_shutdown")) == (
        cs.EdgeResolution.EXACT
    ), calls
    assert (run, f"{base}.db.Shared.is_shutdown") not in calls, calls


def test_bare_reexport_imports_the_local_module_not_an_external_crate(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_imports",
        {
            "src/lib.rs": "pub mod cmd;\npub mod shutdown;\nuse shutdown::Shutdown;\n",
            "src/cmd/mod.rs": "pub mod get;\npub use get::Get;\n",
            "src/cmd/get.rs": _GET_RS,
            "src/shutdown.rs": "pub struct Shutdown;\n",
        },
    )
    imports = _imports(mock_ingestor)
    module = cs.NodeLabel.MODULE.value
    external = cs.NodeLabel.EXTERNAL_MODULE.value
    assert (f"{base}.cmd", module, f"{base}.cmd.get") in imports, imports
    assert (f"{base}.lib", module, f"{base}.shutdown") in imports, imports
    assert not {edge for edge in imports if edge[1] == external}, imports


def test_own_crate_name_import_from_binary_follows_reexport(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A binary target imports the library by its crate name
    # (`use rs_reexp_shop::Get;` in src/main.rs).
    main = _consumer("use rs_reexp_shop::Get;") + "\nfn main() {}\n"
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_shop",
        {
            "src/lib.rs": "pub mod aaa;\npub mod inner;\npub use inner::Get;\n",
            "src/inner.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/main.rs": main,
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.main", f"{base}.inner.Get")


def test_two_hop_reexport_chain(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    # lib.rs re-exports what cmd/mod.rs re-exports from cmd/get.rs.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_chain",
        {
            "src/lib.rs": (
                "pub mod aaa;\npub mod client;\npub mod cmd;\npub use cmd::Get;\n"
            ),
            "src/cmd/mod.rs": "pub mod get;\npub use get::Get;\n",
            "src/cmd/get.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::Get;"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.cmd.get.Get")


def test_aliased_reexport(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    # `pub use inner::Get as Fetch;`: the consumer never spells `Get`, so
    # `Fetch::new()` and every `Fetch`-typed receiver must reach inner::Get.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_alias",
        {
            "src/lib.rs": (
                "pub mod aaa;\npub mod client;\npub mod inner;\n"
                "pub use inner::Get as Fetch;\n"
            ),
            "src/inner.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::Fetch;", ty="Fetch"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.inner.Get")


def test_glob_reexport(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    # `pub use inner::*;` binds no name in the import map: the lookup has to
    # expand the glob to find `Get`.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_glob",
        {
            "src/lib.rs": (
                "pub mod aaa;\npub mod client;\npub mod inner;\npub use inner::*;\n"
            ),
            "src/inner.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::Get;"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.inner.Get")


def test_inline_mod_bare_reexport(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    # The bare head may name an inline `mod` declared in the same scope.
    inline = "".join(f"        {line}\n" for line in _GET_RS.splitlines())
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_inline",
        {
            "src/lib.rs": (
                "pub mod aaa;\npub mod client;\n\n"
                f"pub mod net {{\n    pub mod conn {{\n{inline}    }}\n\n"
                "    pub use conn::Get;\n}\n"
            ),
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::net::Get;"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.lib.net.conn.Get")


# --- negative cases ------------------------------------------------------------


def test_direct_import_of_the_defining_module_still_binds(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_direct",
        {
            "src/lib.rs": "pub mod aaa;\npub mod client;\npub mod cmd;\n",
            "src/cmd/mod.rs": "pub mod get;\npub use get::Get;\n",
            "src/cmd/get.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::cmd::get::Get;"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.cmd.get.Get")


def test_bare_head_naming_a_module_outside_the_scope_stays_external(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `core` is declared as a module inside `util` only. In client.rs the
    # bare `core::` head is the extern-prelude crate, so the import must
    # not be pulled onto util::core just because the name matches.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_extern",
        {
            "src/lib.rs": "pub mod client;\npub mod util;\n",
            "src/util/mod.rs": "pub mod core;\n",
            "src/util/core.rs": "pub fn max(a: u8, b: u8) -> u8 {\n    a\n}\n",
            "src/client.rs": (
                "use core::cmp::max;\n\n"
                "pub fn pick(a: u8, b: u8) -> u8 {\n    max(a, b)\n}\n"
            ),
        },
    )
    imports = _imports(mock_ingestor)
    client_imports = {edge for edge in imports if edge[0] == f"{base}.client"}
    assert client_imports == {
        (f"{base}.client", cs.NodeLabel.EXTERNAL_MODULE.value, "core::cmp")
    }, imports


def test_same_named_module_and_type_elsewhere_is_not_the_target(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # other/get.rs defines another `Get` in another module named `get`, so
    # even the trailing `get.Get` of the qn matches; only cmd's re-export
    # decides which one the consumer means.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_twin",
        {
            "src/lib.rs": "pub mod client;\npub mod cmd;\npub mod other;\n",
            "src/cmd/mod.rs": "pub mod get;\npub use get::Get;\n",
            "src/cmd/get.rs": _GET_RS,
            "src/other/mod.rs": "pub mod get;\n",
            "src/other/get.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::cmd::Get;"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.cmd.get.Get")


@pytest.mark.parametrize(
    "inner_use",
    [
        "use crate::aaa::Get;",
        "use crate::aaa::*;",
        # `pub(self)` and `pub(in self)` are the private visibility spelled
        # out (#2599 review).
        "pub(self) use crate::aaa::Get;",
        "pub(in self) use crate::aaa::Get;",
    ],
    ids=["named", "glob", "pub-self", "pub-in-self"],
)
def test_private_use_is_not_carried_by_a_glob_reexport(
    temp_repo: Path, mock_ingestor: MagicMock, inner_use: str
) -> None:
    # `pub use inner::*;` carries only inner's PUBLIC names. inner's private
    # `use` of aaa::Get stays inside inner, so `crate::Get` is real::Get,
    # the one the second glob supplies. Following the private use first
    # would hand every caller the decoy.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_private",
        {
            "src/lib.rs": (
                "pub mod aaa;\npub mod client;\npub mod inner;\npub mod real;\n"
                "pub use inner::*;\npub use real::*;\n"
            ),
            "src/inner.rs": (
                f'{inner_use}\n\npub fn make() -> Get {{\n    Get::new("k")\n}}\n'
            ),
            "src/real.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::Get;"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.real.Get")


def test_glob_reexport_cycle_terminates_and_binds(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # a.rs and b.rs glob-import each other, which Rust allows; the chase
    # must stop on the cycle and still find the type b.rs defines.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_cycle",
        {
            "src/lib.rs": "pub mod a;\npub mod aaa;\npub mod b;\npub mod client;\n",
            "src/a.rs": "pub use crate::b::*;\n",
            "src/b.rs": "pub use crate::a::*;\n\n" + _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::a::Get;"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.b.Get")


def test_named_reexport_cycle_terminates_without_a_target(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Malformed input (rustc rejects it): two modules re-export each other's
    # `Get` and nothing defines it. Indexing must finish, and the chase must
    # not invent a target for any of the calls.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_loop",
        {
            "src/lib.rs": "pub mod a;\npub mod b;\npub mod client;\n",
            "src/a.rs": "pub use crate::b::Get;\n",
            "src/b.rs": "pub use crate::a::Get;\n",
            "src/client.rs": _consumer("use crate::a::Get;"),
        },
    )
    calls = _calls(mock_ingestor)
    assert not [edge for edge in calls if edge[0].startswith(f"{base}.client.")], calls


def test_glob_below_the_base_still_sees_its_private_uses(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The flip side of the rule above: lib.rs's private `use inner::Get;` is
    # visible to every module of the crate, so `use crate::*;` in client.rs
    # and `use super::*;` in lib.rs's own test module both carry it.
    tests_mod = (
        "\n#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
        "    pub fn in_tests(g: Get) -> String {\n        g.into_frame()\n    }\n}\n"
    )
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_below",
        {
            "src/lib.rs": (
                "pub mod aaa;\npub mod client;\npub mod inner;\nuse inner::Get;\n"
                + tests_mod
            ),
            "src/inner.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::*;"),
        },
    )
    calls = _calls(mock_ingestor)
    _assert_bound(calls, f"{base}.client", f"{base}.inner.Get")
    edge = (f"{base}.lib.tests.in_tests", f"{base}.inner.Get.into_frame")
    assert calls.get(edge) == cs.EdgeResolution.EXACT, (edge, calls)


# --- #2599 review: restricted visibility and the crate boundary ------------

_ZZZ_LIB = (
    "pub mod aaa;\npub mod client;\npub mod outer;\npub mod real;\npub mod zzz;\n"
)


def test_pub_super_use_is_not_carried_by_a_glob_outside_its_parent(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # outer::inner's `pub(super) use` is visible inside `outer` only, so a
    # glob of inner written in client.rs gets nothing for `Get`; the
    # `real::*` glob beside it supplies it.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_super",
        {
            "src/lib.rs": _ZZZ_LIB,
            "src/outer.rs": "pub mod inner;\n",
            "src/outer/inner.rs": "pub(super) use crate::zzz::Get;\n",
            "src/real.rs": _GET_RS,
            "src/zzz.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer(
                "use crate::outer::inner::*;\nuse crate::real::*;"
            ),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.real.Get")


def test_pub_super_use_is_carried_by_a_glob_in_its_parent(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: inside `outer`, the parent `pub(super)` names, the glob of
    # inner does carry it.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_super_in",
        {
            "src/lib.rs": _ZZZ_LIB,
            "src/outer.rs": _consumer("pub mod inner;\nuse self::inner::*;"),
            "src/outer/inner.rs": "pub(super) use crate::zzz::Get;\n",
            "src/real.rs": _GET_RS,
            "src/zzz.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": "",
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.outer", f"{base}.zzz.Get")


def test_pub_crate_use_is_carried_by_a_glob_in_the_crate(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: `pub(crate)` reaches every module of the crate.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_crate_vis",
        {
            "src/lib.rs": _ZZZ_LIB,
            "src/outer.rs": "pub(crate) use crate::zzz::Get;\n",
            "src/real.rs": _GET_RS,
            "src/zzz.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/client.rs": _consumer("use crate::outer::*;"),
        },
    )
    _assert_bound(_calls(mock_ingestor), f"{base}.client", f"{base}.zzz.Get")


def test_binary_glob_of_the_library_does_not_see_its_private_uses(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/main.rs and src/bin/tool.rs are crates of their own beside the
    # library. `use rs_reexp_bin::*;` there carries lib.rs's PUBLIC names
    # only: its private `use aaa::Get;` stays in the library, and the
    # `real::*` glob beside it is what supplies `Get`.
    binary = (
        _consumer("use rs_reexp_bin::*;\nuse rs_reexp_bin::real::*;")
        + "\nfn main() {}\n"
    )
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_reexp_bin",
        {
            "src/lib.rs": (
                "pub mod aaa;\npub mod real;\nuse aaa::Get;\n\n"
                'pub fn make() -> Get {\n    Get::new("k")\n}\n'
            ),
            "src/real.rs": _GET_RS,
            "src/aaa.rs": _GET_RS,
            "src/main.rs": binary,
            "src/bin/tool.rs": binary,
        },
    )
    calls = _calls(mock_ingestor)
    _assert_bound(calls, f"{base}.main", f"{base}.real.Get")
    _assert_bound(calls, f"{base}.bin.tool", f"{base}.real.Get")
