"""Two same-named definitions on one line are two nodes, not one.

The dedup variant was built from the start LINE alone, so same-line twins
received the identical qualified name, became a single node, and one of them
left the graph: its callers pointed at the survivor and everything it called
read as unreferenced (issue #1071). `function_span_key` already carries the
column for exactly this reason.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)
from codebase_rag.types_defs import NodeType

# Both block items need a variant, because the module-level `make` owns the
# natural qn, and both are written on one line.
_SAME_LINE = (
    "pub struct Tee;\n"
    "pub struct Vee;\n"
    "pub struct Uuu;\n"
    "impl Tee { pub fn run(&self) -> u32 { 1 } }\n"
    "impl Vee { pub fn run(&self) -> u32 { 2 } }\n"
    "impl Uuu { pub fn run(&self) -> u32 { 3 } }\n"
    "pub fn make() -> Uuu { Uuu }\n"
    "pub fn outer() -> u32 {\n"
    "    { fn make() -> Tee { Tee } let a = make(); a.run(); }"
    " { fn make() -> Vee { Vee } let b = make(); b.run(); }\n"
    "    0\n"
    "}\n"
)


def _function_qns(mock_ingestor: MagicMock) -> list[str]:
    return [
        str(c.args[1]["qualified_name"])
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == "Function"
    ]


def test_same_line_twins_register_as_two_nodes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_same_line"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod m;\n",
            "src/m.rs": _SAME_LINE,
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    qns = _function_qns(mock_ingestor)
    # Both the count and the distinctness: a twin that vanished instead of
    # collapsing would leave a list that is unique but one node short.
    assert len([qn for qn in qns if ".make" in qn]) == 3, qns
    assert len(qns) == len(set(qns)), qns


def test_each_same_line_twin_keeps_its_own_callee(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The collapse was not only a lost node: both locals typed from whichever
    # twin registered last, so one impl's method lost every incoming edge and
    # read as dead.
    project = temp_repo / "rs_same_line_calls"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod m;\n",
            "src/m.rs": _SAME_LINE,
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    base = "rs_same_line_calls.src.m"
    assert (f"{base}.outer", f"{base}.Tee.run") in calls, calls
    assert (f"{base}.outer", f"{base}.Vee.run") in calls, calls


def test_registry_variant_distinguishes_same_line_registrations() -> None:
    # The registry rule on its own, with no parse in the way: a second
    # registration on a line that already holds one gets a distinct name.
    registry = FunctionRegistryTrie()
    natural = "p.m.make"
    registry.insert(natural, NodeType.FUNCTION)
    first = registry.register_unique_qn(natural, 9, 4)
    registry.insert(first, NodeType.FUNCTION)
    second = registry.register_unique_qn(natural, 9, 44)
    assert first != second, (first, second)
    assert first.startswith(f"{natural}{cs.DUP_QN_MARKER}9"), first
    assert second.startswith(f"{natural}{cs.DUP_QN_MARKER}9"), second
    # A different line still needs no column, so every existing variant keeps
    # the name it already had.
    assert registry.register_unique_qn(natural, 12, 4) == (
        f"{natural}{cs.DUP_QN_MARKER}12"
    )


def test_registering_one_definition_twice_keeps_one_name() -> None:
    # Two passes reaching the same definition must agree on its name. Minting
    # a fresh variant for the repeat would add a node no source declares, and
    # fan every caller of the natural qn out onto it.
    registry = FunctionRegistryTrie()
    natural = "p.m.f"
    registry.insert(natural, NodeType.FUNCTION)
    first = registry.register_unique_qn(natural, 9, 4)
    registry.insert(first, NodeType.FUNCTION)
    assert registry.register_unique_qn(natural, 9, 4) == first
    twin = registry.register_unique_qn(natural, 9, 44)
    registry.insert(twin, NodeType.FUNCTION)
    assert twin != first, (first, twin)
    assert registry.register_unique_qn(natural, 9, 44) == twin
    assert registry.variants(natural) == [natural, first, twin]


def test_same_line_class_and_method_twins_stay_distinct(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The class and method registration paths mint variants of their own, and
    # one line holding several definitions is ordinary in minified JS/TS.
    project = temp_repo / "ts_same_line"
    _write(
        project,
        {
            "package.json": '{"name": "app", "version": "1.0.0"}\n',
            # THREE of each on one line: two would take the natural qn and the
            # plain `@line`, never reaching the column at all.
            "m.ts": (
                "class A { m(){ return 1 } m(){ return 2 } m(){ return 3 } }"
                " class A {} class A {}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="typescript")
    classes = [
        str(c.args[1]["qualified_name"])
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == "Class" and ".A" in str(c.args[1]["qualified_name"])
    ]
    methods = [
        str(c.args[1]["qualified_name"])
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == "Method"
    ]
    assert len(classes) == 3, classes
    assert len(classes) == len(set(classes)), classes
    assert len(methods) == 3, methods
    assert len(methods) == len(set(methods)), methods


def test_deleting_a_variant_frees_the_line_it_claimed() -> None:
    # An incremental re-index deletes a file's qns and registers them again.
    # A claim that outlives its definition makes the name a definition gets
    # depend on what was indexed before it, so the same source would come out
    # with different qualified names on a re-run.
    registry = FunctionRegistryTrie()
    natural = "p.m.f"
    registry.insert(natural, NodeType.FUNCTION)
    first = registry.register_unique_qn(natural, 9, 4)
    registry.insert(first, NodeType.FUNCTION)
    del registry[first]
    assert registry.register_unique_qn(natural, 9, 44) == first, first
