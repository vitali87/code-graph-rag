"""Iterator-adaptor closure parameters type from the collection's element.

A closure bound by an iterator adaptor over a collection of known element
type (`workers.into_iter().map(|worker| ...)`) never typed its parameter,
so method calls on it resolved to nothing: ripgrep's `Worker::run` had
zero incoming CALLS and the whole parallel-walk machinery reported dead
(issue #1045). Calls inside closures attribute to the enclosing function,
so bindings overlay the caller's map span-gated, like match arms.
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)

_CARGO = '[package]\nname = "rs_iterclosure"\nversion = "0.1.0"\n'

_WORKER = (
    "pub struct Worker {\n    pub id: i32,\n}\n\n"
    "impl Worker {\n    pub fn run(&self) -> i32 {\n        self.id\n    }\n}\n\n"
    "pub struct Decoy;\n\n"
    "impl Decoy {\n    pub fn run(&self) -> i32 {\n        0\n    }\n}\n"
)


def test_map_closure_param_types_from_vec_local(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The ripgrep shape, including the nested capture: the adaptor closure
    # spawns an inner closure that calls the captured element.
    project = temp_repo / "rs_ic_local"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod types;\npub mod visit;\n",
            "src/types.rs": _WORKER,
            "src/visit.rs": (
                "use crate::types::Worker;\n\n"
                "fn run_later<F: Fn() -> i32>(f: F) -> i32 {\n    f()\n}\n\n"
                "pub fn visit(workers: Vec<Worker>) -> Vec<i32> {\n"
                "    workers.into_iter().map(|worker| "
                "run_later(|| worker.run())).collect()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_local.src"
    assert (f"{base}.visit.visit", f"{base}.types.Worker.run") in calls, calls
    assert (f"{base}.visit.visit", f"{base}.types.Decoy.run") not in calls, calls


def test_filter_closure_param_types_from_iter(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `iter()` + `filter` hand the closure a reference; method binding is
    # unaffected by the reference level.
    project = temp_repo / "rs_ic_filter"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod types;\npub mod scan;\n",
            "src/types.rs": _WORKER,
            "src/scan.rs": (
                "use crate::types::Worker;\n\n"
                "pub fn scan(workers: Vec<Worker>) -> usize {\n"
                "    workers.iter().filter(|worker| worker.run() > 0).count()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_filter.src"
    assert (f"{base}.scan.scan", f"{base}.types.Worker.run") in calls, calls


def test_same_name_closures_bind_their_own_collections(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two closures reusing one parameter name over different collections:
    # each binds ITS collection's element (a flat map would keep only the
    # last), exactly the match-arm span-overlay contract.
    project = temp_repo / "rs_ic_spans"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod both;\n",
            "src/both.rs": (
                "pub struct Alpha;\n\n"
                "impl Alpha {\n    pub fn go(&self) -> i32 {\n        1\n    }\n}\n\n"
                "pub struct Beta;\n\n"
                "impl Beta {\n    pub fn go(&self) -> i32 {\n        2\n    }\n}\n\n"
                "pub fn both(aa: Vec<Alpha>, bb: Vec<Beta>) {\n"
                "    aa.iter().for_each(|x| {\n        x.go();\n    });\n"
                "    bb.iter().for_each(|x| {\n        x.go();\n    });\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_spans.src.both"
    assert (f"{base}.both", f"{base}.Alpha.go") in calls, calls
    assert (f"{base}.both", f"{base}.Beta.go") in calls, calls


def test_field_collection_types_closure_param(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The collection is a struct FIELD: `self.workers.iter().for_each(...)`
    # reads the field's declared element type.
    project = temp_repo / "rs_ic_field"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod types;\npub mod pool;\n",
            "src/types.rs": _WORKER,
            "src/pool.rs": (
                "use crate::types::Worker;\n\n"
                "pub struct Pool {\n    pub workers: Vec<Worker>,\n}\n\n"
                "impl Pool {\n"
                "    pub fn drive(&self) {\n"
                "        self.workers.iter().for_each(|worker| {\n"
                "            worker.run();\n        });\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_field.src"
    assert (f"{base}.pool.Pool.drive", f"{base}.types.Worker.run") in calls, calls


def test_slice_param_types_closure_param(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The collection is a borrowed slice parameter: `&[Worker]`.
    project = temp_repo / "rs_ic_slice"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod types;\npub mod sum;\n",
            "src/types.rs": _WORKER,
            "src/sum.rs": (
                "use crate::types::Worker;\n\n"
                "pub fn sum(workers: &[Worker]) -> i32 {\n"
                "    workers.iter().map(|worker| worker.run()).sum()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_slice.src"
    assert (f"{base}.sum.sum", f"{base}.types.Worker.run") in calls, calls


def test_collected_struct_literals_type_the_collection(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # ripgrep's actual shape: `let workers: Vec<_> = ...map(|s| Worker
    # { .. }).collect();` — the annotation is inferred, so the element
    # comes from the collect chain's map closure returning a struct
    # literal. The later adaptor over `workers` then types its parameter.
    project = temp_repo / "rs_ic_collect"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod types;\npub mod pool;\n",
            "src/types.rs": _WORKER,
            "src/pool.rs": (
                "use crate::types::Worker;\n\n"
                "fn run_later<F: Fn() -> i32>(f: F) -> i32 {\n    f()\n}\n\n"
                "pub fn drive(ids: Vec<i32>) -> Vec<i32> {\n"
                "    let workers: Vec<_> = ids\n"
                "        .into_iter()\n"
                "        .map(|id| Worker { id })\n"
                "        .collect();\n"
                "    workers\n"
                "        .into_iter()\n"
                "        .map(|worker| run_later(|| worker.run()))\n"
                "        .collect()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_collect.src"
    assert (f"{base}.pool.drive", f"{base}.types.Worker.run") in calls, calls
    assert (f"{base}.pool.drive", f"{base}.types.Decoy.run") not in calls, calls


def test_annotated_closure_param_uses_its_annotation(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An explicit `|worker: Worker|` annotation types the parameter even
    # when the receiver chain offers no element type (a generic iterator).
    project = temp_repo / "rs_ic_annot"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod types;\npub mod gen;\n",
            "src/types.rs": _WORKER,
            "src/gen.rs": (
                "use crate::types::Worker;\n\n"
                "pub fn consume<I: Iterator<Item = Worker>>(it: I) -> i32 {\n"
                "    it.map(|worker: Worker| worker.run()).sum()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_annot.src"
    assert (f"{base}.gen.consume", f"{base}.types.Worker.run") in calls, calls
