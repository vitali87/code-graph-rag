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

import pytest
import tree_sitter
import tree_sitter_rust

from codebase_rag.parsers.rs.type_inference import RustTypeInferenceEngine
from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


@pytest.fixture
def rust_fn_parser():
    lang = tree_sitter.Language(tree_sitter_rust.language())
    parser = tree_sitter.Parser(lang)

    def parse_fn(source: str):
        tree = parser.parse(source.encode("utf8"))
        return tree.root_node.children[0]

    return parse_fn


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


def test_generic_param_collection_keeps_trie_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `Vec<T>` with T a type parameter: "T" resolves to no registered
    # class, and a binding to it would let the external-receiver guard
    # DELETE the edge the trie fallback produces today. Unresolvable
    # element types must leave the parameter untyped.
    project = temp_repo / "rs_ic_generic"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod task;\npub mod runner;\n",
            "src/task.rs": (
                "pub trait Task {\n    fn execute(&self);\n}\n\n"
                "pub struct Job;\n\n"
                "impl Task for Job {\n    fn execute(&self) {}\n}\n"
            ),
            "src/runner.rs": (
                "use crate::task::Task;\n\n"
                "pub fn run_all<T: Task>(items: Vec<T>) {\n"
                "    items.iter().for_each(|t| {\n        t.execute();\n    });\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_generic.src"
    assert (f"{base}.runner.run_all", f"{base}.task.Job.execute") in calls, calls


def test_generic_field_collection_keeps_trie_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The generic-element shape on a struct FIELD: `Pool<T> { items:
    # Vec<T> }` must not bind `T` either.
    project = temp_repo / "rs_ic_genfield"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod task;\npub mod pool;\n",
            "src/task.rs": (
                "pub trait Task {\n    fn execute(&self);\n}\n\n"
                "pub struct Job;\n\n"
                "impl Task for Job {\n    fn execute(&self) {}\n}\n"
            ),
            "src/pool.rs": (
                "use crate::task::Task;\n\n"
                "pub struct Pool<T: Task> {\n    pub items: Vec<T>,\n}\n\n"
                "impl<T: Task> Pool<T> {\n"
                "    pub fn drive(&self) {\n"
                "        self.items.iter().for_each(|t| {\n"
                "            t.execute();\n        });\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_genfield.src"
    assert (f"{base}.pool.Pool.drive", f"{base}.task.Job.execute") in calls, calls


def test_unresolvable_element_keeps_trie_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `Vec<String>` with a first-party `impl Shout for String` in ANOTHER
    # module: "String" is a real type but resolves to nothing from the
    # calling module, so binding it would delete the trie's edges.
    project = temp_repo / "rs_ic_stdtype"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod ext;\npub mod use_it;\n",
            "src/ext.rs": (
                "pub trait Shout {\n    fn shout(&self) -> String;\n}\n\n"
                "impl Shout for String {\n"
                "    fn shout(&self) -> String {\n        self.clone()\n    }\n"
                "}\n"
            ),
            "src/use_it.rs": (
                "use crate::ext::Shout;\n\n"
                "pub fn all(names: Vec<String>) {\n"
                "    names.iter().for_each(|n| {\n        n.shout();\n    });\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_stdtype.src"
    assert (f"{base}.use_it.all", f"{base}.ext.String.shout") in calls, calls


def test_nested_fn_let_does_not_leak(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    # A `let` inside a nested `fn` item shares no scope with the outer
    # body: its element must not rebind the outer closure's parameter.
    project = temp_repo / "rs_ic_nestedfn"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod types;\npub mod f;\n",
            "src/types.rs": _WORKER,
            "src/f.rs": (
                "use crate::types::{Decoy, Worker};\n\n"
                "pub fn go(items: Vec<Decoy>) {\n"
                "    fn helper() {\n"
                "        let items: Vec<Worker> = Vec::new();\n"
                "        let _ = items;\n    }\n"
                "    helper();\n"
                "    items.iter().for_each(|x| {\n        x.run();\n    });\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_nestedfn.src"
    assert (f"{base}.f.go", f"{base}.types.Decoy.run") in calls, calls
    assert (f"{base}.f.go", f"{base}.types.Worker.run") not in calls, calls


def test_later_block_let_does_not_rebind(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A block-scoped `let` written AFTER the adaptor call is not in scope
    # for it (position and block span both exclude it).
    project = temp_repo / "rs_ic_laterlet"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod types;\npub mod f;\n",
            "src/types.rs": _WORKER,
            "src/f.rs": (
                "use crate::types::{Decoy, Worker};\n\n"
                "pub fn go(items: Vec<Decoy>) {\n"
                "    items.iter().for_each(|x| {\n        x.run();\n    });\n"
                "    {\n"
                "        let items: Vec<_> =\n"
                "            (0..3).map(|n| Worker { id: n }).collect();\n"
                "        let _ = items;\n    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_laterlet.src"
    assert (f"{base}.f.go", f"{base}.types.Decoy.run") in calls, calls
    assert (f"{base}.f.go", f"{base}.types.Worker.run") not in calls, calls


def test_filter_hop_preserves_element(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `filter` between the collection and the binding adaptor preserves
    # the element.
    project = temp_repo / "rs_ic_filterhop"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": "pub mod types;\npub mod f;\n",
            "src/types.rs": _WORKER,
            "src/f.rs": (
                "use crate::types::Worker;\n\n"
                "pub fn go(items: Vec<Worker>) -> Vec<i32> {\n"
                "    items\n"
                "        .iter()\n"
                "        .filter(|w| true)\n"
                "        .map(|worker| worker.run())\n"
                "        .collect()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ic_filterhop.src"
    assert (f"{base}.f.go", f"{base}.types.Worker.run") in calls, calls


def test_result_annotation_blocks_collect_inference(rust_fn_parser) -> None:
    # `let v: Result<Vec<Worker>, ()> = ...collect();` names a NON
    # sequence: the value a later `v.map(|list| ...)` closure receives is
    # the whole Vec, so the collect chain must not type v's element.
    fn = rust_fn_parser(
        "pub fn go(xs: Vec<i32>) {\n"
        "    let v: Result<Vec<Worker>, ()> ="
        " xs.iter().map(|x| Worker { id: 1 }).collect();\n"
        "    let _ = v;\n"
        "}\n"
    )
    entries = RustTypeInferenceEngine().collect_element_entries(fn)
    assert not [e for e in entries if e[0] == "v"], entries


def test_trailing_comment_keeps_struct_literal(rust_fn_parser) -> None:
    # A trailing comment in the map closure's block must not hide the
    # struct literal from the collect-chain inference.
    fn = rust_fn_parser(
        "pub fn go(xs: Vec<i32>) {\n"
        "    let v: Vec<_> = xs\n"
        "        .iter()\n"
        "        .map(|x| {\n"
        "            Worker { id: 1 } // build it\n"
        "        })\n"
        "        .collect();\n"
        "    let _ = v;\n"
        "}\n"
    )
    entries = RustTypeInferenceEngine().collect_element_entries(fn)
    assert [e for e in entries if e[0] == "v" and e[1] == "Worker"], entries


def test_turbofish_collect_types_element(rust_fn_parser) -> None:
    # The turbofish spelling `collect::<Vec<_>>()` types the element from
    # the mapped struct literal; a typed `collect::<Vec<Worker>>()` reads
    # it straight from the turbofish.
    fn = rust_fn_parser(
        "pub fn go(xs: Vec<i32>) {\n"
        "    let a = xs.iter().map(|x| Worker { id: 1 }).collect::<Vec<_>>();\n"
        "    let b = xs.iter().map(make).collect::<Vec<Worker>>();\n"
        "    let _ = (a, b);\n"
        "}\n"
    )
    entries = RustTypeInferenceEngine().collect_element_entries(fn)
    assert [e for e in entries if e[0] == "a" and e[1] == "Worker"], entries
    assert [e for e in entries if e[0] == "b" and e[1] == "Worker"], entries


def test_enclosing_closure_param_shadows_collection(rust_fn_parser) -> None:
    # An enclosing closure parameter shadowing a collection name hides
    # the function-level entry: the inner adaptor's receiver is the
    # closure's binding, whose element the engine does not know, so the
    # inner parameter must stay unbound rather than type from the
    # shadowed outer collection.
    fn = rust_fn_parser(
        "pub fn drive(items: Vec<Worker>, groups: Vec<Group>) {\n"
        "    groups.into_iter().for_each(|items| {\n"
        "        items.iter().for_each(|x| {\n"
        "            x.run();\n"
        "        });\n"
        "    });\n"
        "}\n"
    )
    engine = RustTypeInferenceEngine()
    entries = engine.collect_element_entries(fn)
    bindings = engine.collect_closure_param_bindings(fn, entries)
    assert not [b for b in bindings if b[2] == "x"], bindings
