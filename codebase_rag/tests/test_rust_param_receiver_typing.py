"""Rust brace-list use paths keep their base; receivers stay typed.

Issue #1039 presented as missing parameter-receiver typing (ripgrep's
`fn search(args: &HiArgs)` calling `args.matcher()` emitted no edges),
but the root cause was a scoped identifier inside a brace-list use
(`use crate::flags::{hiargs::HiArgs}`) dropping the list's base path,
which severed every consumer of the re-export. The use-list test here
pins the fix; the receiver-shaped tests pin the already-working
parameter typing the issue was filed against, so a regression in either
half now fails loudly.
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


def test_method_call_on_reference_param_receiver(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_param_ref"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_param_ref"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "pub struct S;\n\n"
                "impl S {\n"
                "    pub fn go(&self) -> u32 {\n        1\n    }\n"
                "}\n\n"
                "fn run(s: &S) -> u32 {\n    s.go()\n}\n\n"
                "fn main() {\n    let _ = run(&S);\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_param_ref.src.main"
    calls = _calls(mock_ingestor)
    assert (f"{base}.run", f"{base}.S.go") in calls, calls


def test_method_call_on_owned_and_mut_param_receivers(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_param_owned"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_param_owned"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "pub struct S;\n\n"
                "impl S {\n"
                "    pub fn go(&self) -> u32 {\n        1\n    }\n"
                "    pub fn bump(&mut self) -> u32 {\n        2\n    }\n"
                "}\n\n"
                "fn consume(s: S) -> u32 {\n    s.go()\n}\n\n"
                "fn mutate(s: &mut S) -> u32 {\n    s.bump()\n}\n\n"
                "fn main() {\n"
                "    let _ = consume(S);\n"
                "    let mut s = S;\n"
                "    let _ = mutate(&mut s);\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_param_owned.src.main"
    calls = _calls(mock_ingestor)
    assert (f"{base}.consume", f"{base}.S.go") in calls, calls
    assert (f"{base}.mutate", f"{base}.S.bump") in calls, calls


def test_method_call_on_cross_module_param_receiver(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # ripgrep's exact shape: the receiver type lives in another module's
    # submodule and reaches the caller through a mod.rs re-export
    # (`use crate::flags::HiArgs;` where flags/mod.rs re-exports
    # hiargs::HiArgs); the parameter annotation must still type the
    # receiver.
    project = temp_repo / "rs_param_xmod"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_param_xmod"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "mod flags;\nmod search;\n\n"
                "fn main() {\n"
                "    let args = crate::flags::HiArgs::new();\n"
                "    let _ = crate::search::search(&args);\n"
                "}\n"
            ),
            "src/flags/mod.rs": (
                "mod hiargs;\n\npub(crate) use crate::flags::hiargs::HiArgs;\n"
            ),
            "src/flags/hiargs.rs": (
                "pub(crate) struct HiArgs;\n\n"
                "impl HiArgs {\n"
                "    pub(crate) fn new() -> HiArgs {\n        HiArgs\n    }\n"
                "    pub(crate) fn matcher(&self) -> u32 {\n        1\n    }\n"
                "}\n"
            ),
            "src/search.rs": (
                "use crate::flags::HiArgs;\n\n"
                "pub(crate) fn search(args: &HiArgs) -> u32 {\n"
                "    args.matcher()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_param_xmod.src"
    calls = _calls(mock_ingestor)
    assert (
        f"{base}.search.search",
        f"{base}.flags.hiargs.HiArgs.matcher",
    ) in calls, calls


def test_param_receiver_in_entry_file_with_brace_list_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # ripgrep's exact shape: the caller fn lives in the crate ROOT file
    # itself, importing the type through a brace-list use of the mod.rs
    # re-export, and a same-named method exists on another type.
    project = temp_repo / "rs_param_entry"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_param_entry"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "mod flags;\nmod worker;\n\n"
                "use crate::flags::{HiArgs, SearchMode};\n\n"
                "fn search(args: &HiArgs, _mode: SearchMode) -> u32 {\n"
                "    args.matcher()\n"
                "}\n\n"
                "fn main() {\n"
                "    let args = crate::flags::HiArgs::new();\n"
                "    let _ = search(&args, SearchMode::Standard);\n"
                "    let w = crate::worker::Worker;\n"
                "    let _ = w.matcher();\n"
                "}\n"
            ),
            "src/flags/mod.rs": (
                "mod hiargs;\n\n"
                "pub(crate) use crate::flags::hiargs::HiArgs;\n\n"
                "pub(crate) enum SearchMode {\n    Standard,\n}\n"
            ),
            "src/flags/hiargs.rs": (
                "pub(crate) struct HiArgs;\n\n"
                "impl HiArgs {\n"
                "    pub(crate) fn new() -> HiArgs {\n        HiArgs\n    }\n"
                "    pub(crate) fn matcher(&self) -> u32 {\n        1\n    }\n"
                "}\n"
            ),
            "src/worker.rs": (
                "pub(crate) struct Worker;\n\n"
                "impl Worker {\n"
                "    pub(crate) fn matcher(&self) -> u32 {\n        2\n    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_param_entry.src"
    calls = _calls(mock_ingestor)
    assert (
        f"{base}.main.search",
        f"{base}.flags.hiargs.HiArgs.matcher",
    ) in calls, calls
    assert (f"{base}.main.search", f"{base}.worker.Worker.matcher") not in calls, calls


def test_param_receiver_in_deep_manifest_target(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # ripgrep's real layout: the workspace ROOT manifest declares
    # `[[bin]] path = "crates/core/main.rs"`, so the crate root sits deep
    # in the tree with no local Cargo.toml or src/ directory.
    project = temp_repo / "rs_param_deep"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_param_deep"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "rg"\npath = "crates/core/main.rs"\n'
            ),
            "crates/core/main.rs": (
                "mod flags;\n\n"
                "use crate::flags::HiArgs;\n\n"
                "fn search(args: &HiArgs) -> u32 {\n"
                "    args.matcher()\n"
                "}\n\n"
                "fn main() {\n"
                "    let args = crate::flags::HiArgs::new();\n"
                "    let _ = search(&args);\n"
                "}\n"
            ),
            "crates/core/flags/mod.rs": (
                "mod hiargs;\n\npub(crate) use crate::flags::hiargs::HiArgs;\n"
            ),
            "crates/core/flags/hiargs.rs": (
                "pub(crate) struct HiArgs;\n\n"
                "impl HiArgs {\n"
                "    pub(crate) fn new() -> HiArgs {\n        HiArgs\n    }\n"
                "    pub(crate) fn matcher(&self) -> u32 {\n        1\n    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_param_deep.crates.core"
    calls = _calls(mock_ingestor)
    assert (
        f"{base}.main.search",
        f"{base}.flags.hiargs.HiArgs.matcher",
    ) in calls, calls


def test_method_call_beats_same_named_free_fn_in_type_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # ripgrep's `args.stats()`: HiArgs::stats(&self) AND a free
    # `fn stats(low: &LowArgs)` live in the same file, and the caller
    # binds `let mut stats = args.stats();`. The receiver call must bind
    # the METHOD, never the free fn.
    project = temp_repo / "rs_param_freefn"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_param_freefn"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "mod flags;\n\n"
                "use crate::flags::HiArgs;\n\n"
                "fn search(args: &HiArgs) -> u32 {\n"
                "    let mut stats = args.stats();\n"
                "    stats += 1;\n"
                "    stats\n"
                "}\n\n"
                "fn main() {\n"
                "    let args = crate::flags::HiArgs::new();\n"
                "    let _ = search(&args);\n"
                "}\n"
            ),
            "src/flags/mod.rs": (
                "mod hiargs;\n\npub(crate) use crate::flags::hiargs::HiArgs;\n"
            ),
            "src/flags/hiargs.rs": (
                "pub(crate) struct HiArgs;\n"
                "pub(crate) struct LowArgs;\n\n"
                "impl HiArgs {\n"
                "    pub(crate) fn new() -> HiArgs {\n        HiArgs\n    }\n"
                "    pub(crate) fn stats(&self) -> u32 {\n        stats(&LowArgs)\n    }\n"
                "}\n\n"
                "fn stats(_low: &LowArgs) -> u32 {\n    1\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_param_freefn.src"
    calls = _calls(mock_ingestor)
    assert (
        f"{base}.main.search",
        f"{base}.flags.hiargs.HiArgs.stats",
    ) in calls, calls
    assert (f"{base}.main.search", f"{base}.flags.hiargs.stats") not in calls, calls


def test_scoped_identifier_in_use_list_keeps_its_base_path(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # ripgrep's flags/mod.rs: `pub(crate) use crate::flags::{ ...,
    # hiargs::HiArgs, ... };`. A scoped identifier INSIDE a brace list
    # must keep the list's base path; stored relative it can never be
    # followed, and every receiver typed through the re-export loses its
    # method edges (the real cause behind issue #1039's HiArgs surface).
    project = temp_repo / "rs_param_nested_use"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_param_nested_use"\nversion = "0.1.0"\n'
            ),
            "src/main.rs": (
                "mod flags;\n\n"
                "use crate::flags::{HiArgs, SearchMode};\n\n"
                "fn search(args: &HiArgs, _mode: SearchMode) -> u32 {\n"
                "    args.matcher()\n"
                "}\n\n"
                "fn main() {\n"
                "    let args = crate::flags::HiArgs::new();\n"
                "    let _ = search(&args, SearchMode::Standard);\n"
                "}\n"
            ),
            "src/flags/mod.rs": (
                "mod hiargs;\nmod lowargs;\n\n"
                "pub(crate) use crate::flags::{\n"
                "    hiargs::HiArgs,\n"
                "    lowargs::SearchMode,\n"
                "};\n"
            ),
            "src/flags/hiargs.rs": (
                "pub(crate) struct HiArgs;\n\n"
                "impl HiArgs {\n"
                "    pub(crate) fn new() -> HiArgs {\n        HiArgs\n    }\n"
                "    pub(crate) fn matcher(&self) -> u32 {\n        1\n    }\n"
                "}\n"
            ),
            "src/flags/lowargs.rs": (
                "pub(crate) enum SearchMode {\n    Standard,\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_param_nested_use.src"
    calls = _calls(mock_ingestor)
    assert (
        f"{base}.main.search",
        f"{base}.flags.hiargs.HiArgs.matcher",
    ) in calls, calls


def test_param_type_disambiguates_same_named_methods(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two types define `go`; the parameter annotation picks the right one.
    project = temp_repo / "rs_param_disambig"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_param_disambig"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "pub struct A;\n"
                "pub struct B;\n\n"
                "impl A {\n"
                "    pub fn go(&self) -> u32 {\n        1\n    }\n"
                "}\n\n"
                "impl B {\n"
                "    pub fn go(&self) -> u32 {\n        2\n    }\n"
                "}\n\n"
                "fn run(b: &B) -> u32 {\n    b.go()\n}\n\n"
                "fn main() {\n"
                "    let a = A;\n"
                "    let _ = a.go() + run(&B);\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_param_disambig.src.main"
    calls = _calls(mock_ingestor)
    assert (f"{base}.run", f"{base}.B.go") in calls, calls
    assert (f"{base}.run", f"{base}.A.go") not in calls, calls
