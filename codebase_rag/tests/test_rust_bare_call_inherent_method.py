"""A bare Rust call never binds an inherent method (issue #1011).

Inside an impl method, a bare path resolves through module items and
imports only: inherent methods are reachable solely via `self.helper()`,
`Self::helper()` or `S::helper()` (rustc-verified; the bare spelling calls
the free function and `S::helper` stays unused).
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


def test_bare_call_in_impl_method_binds_free_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_bare_inherent"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_bare_inherent"\nversion = "0.1.0"\n',
            "src/main.rs": "mod foo;\n\nfn main() {\n    let _ = foo::S.run();\n}\n",
            "src/foo.rs": (
                "pub fn helper() -> u32 {\n    1\n}\n\n"
                "pub struct S;\n\n"
                "impl S {\n"
                "    fn helper(&self) -> u32 {\n        2\n    }\n"
                "    pub fn run(&self) -> u32 {\n        helper()\n    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_bare_inherent.src"
    calls = _calls(mock_ingestor)
    assert (f"{base}.foo.S.run", f"{base}.foo.helper") in calls, calls
    assert (f"{base}.foo.S.run", f"{base}.foo.S.helper") not in calls, calls


def test_self_method_call_still_binds_the_inherent_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The receiver spelling keeps its method edge: only the BARE path is
    # barred from inherent methods.
    project = temp_repo / "rs_bare_inherent_self"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_bare_inherent_self"\nversion = "0.1.0"\n'
            ),
            "src/main.rs": "mod foo;\n\nfn main() {\n    let _ = 1;\n}\n",
            "src/foo.rs": (
                "pub struct S;\n\n"
                "impl S {\n"
                "    fn helper(&self) -> u32 {\n        2\n    }\n"
                "    pub fn run(&self) -> u32 {\n        self.helper()\n    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_bare_inherent_self.src"
    calls = _calls(mock_ingestor)
    assert (f"{base}.foo.S.run", f"{base}.foo.S.helper") in calls, calls
