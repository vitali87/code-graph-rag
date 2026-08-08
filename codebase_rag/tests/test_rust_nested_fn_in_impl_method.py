"""A Rust definition in a function body takes no name that is reachable.

An item written inside a function body, a closure or a const initializer is
in scope for that body alone, yet it registers in the qn space of the module
or impl target enclosing it. The generic function pass ran before the method
pass, so such an item claimed the natural qn first and the method or module
item that owns the name was pushed onto a `@<line>` variant no caller
resolves to (issue #1037).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)

_IMPL = (
    "pub struct S;\n"
    "\n"
    "impl S {\n"
    "    pub fn helper(&self) -> u32 {\n"
    "        1\n"
    "    }\n"
    "    pub fn run(&self) -> u32 {\n"
    "        fn helper() -> u32 {\n"
    "            2\n"
    "        }\n"
    "        helper()\n"
    "    }\n"
    "}\n"
)


def _qns(mock_ingestor: MagicMock, label: str) -> set[str]:
    return {
        str(c.args[1]["qualified_name"])
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == label
    }


def test_nested_fn_leaves_the_method_its_own_qualified_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_nested_fn"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": _IMPL,
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    methods = _qns(mock_ingestor, "Method")
    functions = _qns(mock_ingestor, "Function")
    assert "rs_nested_fn.src.foo.S.helper" in methods, methods
    assert "rs_nested_fn.src.foo.S.helper@4" not in methods, methods
    # The body-local fn is left with the variant, and it is a Function node,
    # so the CALLS edge from `run` has an endpoint that exists.
    assert "rs_nested_fn.src.foo.S.helper@8" in functions, functions
    assert "rs_nested_fn.src.foo.S.helper" not in functions, functions


def test_method_call_from_another_module_binds_the_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_nested_fn_outside"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod foo;\npub mod bar;\n",
            "src/foo.rs": _IMPL,
            "src/bar.rs": (
                "use crate::foo::S;\n"
                "pub fn outside(s: &S) -> u32 {\n"
                "    s.helper()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_nested_fn_outside.src.bar.outside"
    method = "rs_nested_fn_outside.src.foo.S.helper"
    # The edge alone proves nothing while the body-local fn could hold that
    # same qn, so the label the qn belongs to is part of the assertion.
    assert method in _qns(mock_ingestor, "Method"), _qns(mock_ingestor, "Method")
    assert (caller, method) in calls, calls
    assert (caller, f"{method}@8") not in calls, calls


def test_the_body_local_fn_still_binds_its_own_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Standing the body-local item down from the natural qn must not cost it
    # the call written inside its own body, which is the one call in the file
    # that really does mean it rather than the method.
    project = temp_repo / "rs_nested_fn_inner"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": _IMPL,
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    run = "rs_nested_fn_inner.src.foo.S.run"
    assert (run, "rs_nested_fn_inner.src.foo.S.helper@8") in calls, calls
    assert (run, "rs_nested_fn_inner.src.foo.S.helper") not in calls, calls


def test_free_function_body_item_does_not_take_a_public_fn_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The same theft one scope out, where both items really do share a qn:
    # a fn nested in a free function is written first, so source order alone
    # handed it the module-level name and every importer of the public
    # `helper` resolved to an item private to `run`'s body.
    project = temp_repo / "rs_body_local_free"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod foo;\npub mod bar;\n",
            "src/foo.rs": (
                "pub fn run() -> u32 {\n"
                "    fn helper() -> u32 {\n"
                "        2\n"
                "    }\n"
                "    helper()\n"
                "}\n"
                "\n"
                "pub fn helper() -> u32 {\n"
                "    9\n"
                "}\n"
            ),
            "src/bar.rs": (
                "use crate::foo::helper;\npub fn outside() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    base = "rs_body_local_free.src"
    assert (f"{base}.bar.outside", f"{base}.foo.helper") in calls, calls
    assert (f"{base}.bar.outside", f"{base}.foo.helper@2") not in calls, calls
    assert (f"{base}.foo.run", f"{base}.foo.helper@2") in calls, calls
    assert (f"{base}.foo.run", f"{base}.foo.helper") not in calls, calls


def test_body_local_fn_return_type_leaves_the_module_item_its_own(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Holding the registration back must not cost the fn its return type, and
    # the qn to record it under is the one the registry handed out, not the one
    # resolution proposed: a body-local fn contesting a module item lands on
    # the variant, and recording under the proposal overwrites the module
    # item's own return type, mistyping every caller of it.
    #
    # The other direction, a chained call inside the body typing from the
    # body-local fn, needs the call site the name-keyed lookup does not take;
    # filed as #1069.
    project = temp_repo / "rs_body_local_return"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": (
                "pub struct T;\n"
                "pub struct U;\n"
                "\n"
                "impl T {\n"
                "    pub fn run(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
                "\n"
                "impl U {\n"
                "    pub fn run(&self) -> u32 {\n"
                "        2\n"
                "    }\n"
                "}\n"
                "\n"
                "pub fn make() -> U {\n"
                "    U\n"
                "}\n"
                "\n"
                "pub fn outer() -> u32 {\n"
                "    fn make() -> T {\n"
                "        T\n"
                "    }\n"
                "    let t = make();\n"
                "    t.run()\n"
                "}\n"
                "\n"
                "pub fn sibling() -> u32 {\n"
                "    let u = make();\n"
                "    u.run()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    base = "rs_body_local_return.src"
    assert (f"{base}.foo.sibling", f"{base}.foo.U.run") in calls, calls
    assert (f"{base}.foo.sibling", f"{base}.foo.T.run") not in calls, calls
    # The call to the body-local fn itself still binds it, so the return type
    # is recorded against a callee the graph really reaches.
    assert (f"{base}.foo.outer", f"{base}.foo.make@21") in calls, calls
    assert (f"{base}.foo.outer", f"{base}.foo.make") not in calls, calls


def test_const_initializer_item_does_not_take_a_method_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An associated-const initializer is no function body, but an item inside
    # one is just as unreachable, and it competes for the same impl-qualified
    # name as the method beside it.
    project = temp_repo / "rs_const_body_local"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod foo;\npub mod bar;\n",
            "src/foo.rs": (
                "pub struct S;\n"
                "\n"
                "impl S {\n"
                "    pub const C: u32 = {\n"
                "        const fn helper() -> u32 {\n"
                "            2\n"
                "        }\n"
                "        helper()\n"
                "    };\n"
                "    pub fn helper(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
            "src/bar.rs": (
                "use crate::foo::S;\n"
                "pub fn outside(s: &S) -> u32 {\n"
                "    s.helper()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    base = "rs_const_body_local.src"
    methods = _qns(mock_ingestor, "Method")
    functions = _qns(mock_ingestor, "Function")
    # `S.helper` alone is no assertion: the initializer's item registered under
    # it too, as a Method. The demotion of the real method to a variant is what
    # tells the two apart.
    assert f"{base}.foo.S.helper" in methods, methods
    assert f"{base}.foo.S.helper@10" not in methods, methods
    assert f"{base}.foo.S.helper@5" in functions, functions
    assert (f"{base}.bar.outside", f"{base}.foo.S.helper") in calls, calls
    assert (f"{base}.bar.outside", f"{base}.foo.S.helper@5") not in calls, calls
