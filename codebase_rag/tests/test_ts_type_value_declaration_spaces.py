# TypeScript keeps types and values in separate declaration spaces, so a module
# may export `type input<T>` AND `function input(...)` (Zod v4 does exactly
# this). The registry treated the pair as duplicates of one name: the type
# became the `input@1` variant of the function, every call to the function
# fanned out as `overload` over "both definitions" (so `rename` refused), and
# the paths that fan out without checking kinds aimed CALLS rows at the Type
# node, which the database drops (issue #2520). A type-only declaration and a
# value now each keep the natural qualified name under their own label.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import RenameRefused, rename
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.tests.conftest import get_relationships, run_updater
from codebase_rag.types_defs import NodeType
from codec import schema_pb2 as pb
from evals.cgr_graph import _StatefulIngestor

TYPE_AND_FUNCTION_TS = """export type input<T> = { value: T };

export function input<T>(value: T): input<T> {
  return { value };
}
"""

DECLARED_TYPE_AND_FUNCTION_TS = """export declare type input<T> = { value: T };

export function input<T>(value: T): input<T> {
  return { value };
}
"""

INTERFACE_AND_ARROW_TS = """export interface Opts { a: number }

export const Opts = (a: number): Opts => ({ a });
"""

INTERFACE_AND_FUNCTION_TS = """export interface Opts { a: number }

export function Opts(a: number): Opts {
  return { a };
}
"""

USE_INPUT_TS = """import { input } from "./shape";

export function run() {
  return input(1).value;
}
"""

USE_INPUT_AS_CALLBACK_TS = """import { input } from "./shape";

export function viaCallback() {
  return [1, 2].map(input);
}
"""

USE_OPTS_TS = """import { Opts } from "./shape";

export function run() {
  return Opts(1);
}
"""

INTERFACE_THEN_CLASS_TS = """export interface Box { extra: number }

export class Box {
  get(): number {
    return 1;
  }
}
"""

USE_BOX_TS = """import { Box } from "./shape";

export function run() {
  const box = new Box();
  return box.get();
}
"""

IMPLEMENTS_TWIN_TS = """export interface Base { b: number }

export interface Opts extends Base { a: number }

export const Opts = (a: number): Opts => ({ a, b: 0 });

export class Impl implements Opts {
  a = 1;
  b = 2;
}
"""

REDECLARED_FUNCTION_TS = """export function f() {
  return 1;
}

export function f() {
  return 2;
}
"""

USE_F_TS = """import { f } from "./shape";

export function run() {
  return f();
}
"""

OVERLOADED_FUNCTION_TS = """export function f(a: number): number;
export function f(a: string): string;
export function f(a: number | string) {
  return a;
}
"""

TYPE_ONLY_TS = """export type input<T> = { value: T };
"""

FUNCTION_ONLY_TS = """export function input<T>(value: T) {
  return { value };
}
"""

USE_OTHER_INPUT_TS = """import { input } from "./other";

export function run() {
  return input(1);
}
"""


# One import binds both twins: the call names the function, the return
# annotation the type.
USE_INPUT_AND_ITS_TYPE_TS = """import { input } from "./shape";

export function run(): input<number> {
  return input(1);
}
"""

USE_OPTS_AND_IMPLEMENT_IT_TS = """import { Opts } from "./shape";

export class Impl implements Opts {
  a = 1;
}

export function run() {
  return Opts(1);
}
"""

BASE_AND_BOX_TWINS_TS = """export class Base {
  base(): number {
    return 0;
  }
}

export interface Box extends Base { extra: number }

export class Box extends Base {
  get(): number {
    return 1;
  }
}
"""


def _write(repo: Path, files: dict[str, str]) -> None:
    src = repo / "src"
    src.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (src / name).write_text(text)


def _nodes(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0]), c.args[1][cs.KEY_QUALIFIED_NAME])
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if cs.KEY_QUALIFIED_NAME in c.args[1]
    }


def _edges(
    mock_ingestor: MagicMock, rel_type: cs.RelationshipType
) -> list[tuple[str, str, str, str, str | None]]:
    edges = []
    for c in get_relationships(mock_ingestor, rel_type.value):
        props = c.args[3] if len(c.args) > 3 else c.kwargs.get("properties")
        resolution = props.get(cs.KEY_RESOLUTION) if props else None
        edges.append(
            (
                str(c.args[0][0]),
                c.args[0][2],
                str(c.args[2][0]),
                c.args[2][2],
                resolution,
            )
        )
    return edges


def _calls_from(mock_ingestor: MagicMock, caller_qn: str) -> set[tuple[str, str]]:
    return {
        (to_label, to_qn)
        for _fl, from_qn, to_label, to_qn, _res in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        )
        if from_qn == caller_qn
    }


def _resolutions_from(mock_ingestor: MagicMock, caller_qn: str) -> set[str | None]:
    return {
        res
        for _fl, from_qn, _tl, _tq, res in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        )
        if from_qn == caller_qn
    }


@pytest.mark.parametrize(
    "shape",
    [TYPE_AND_FUNCTION_TS, DECLARED_TYPE_AND_FUNCTION_TS],
    ids=["type", "declare-type"],
)
def test_type_alias_and_function_both_keep_the_natural_qn(
    temp_repo: Path, mock_ingestor: MagicMock, shape: str
) -> None:
    _write(temp_repo, {"shape.ts": shape, "use.ts": USE_INPUT_TS})
    run_updater(temp_repo, mock_ingestor)

    qn = f"{temp_repo.name}.src.shape.input"
    nodes = _nodes(mock_ingestor)
    assert (cs.NodeLabel.FUNCTION.value, qn) in nodes, nodes
    assert (cs.NodeLabel.TYPE.value, qn) in nodes, nodes
    assert not any(q.startswith(f"{qn}{cs.DUP_QN_MARKER}") for _l, q in nodes), nodes


def test_call_to_value_with_a_same_named_type_resolves_exact(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(temp_repo, {"shape.ts": TYPE_AND_FUNCTION_TS, "use.ts": USE_INPUT_TS})
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    run_qn = f"{project}.src.use.run"
    target = (cs.NodeLabel.FUNCTION.value, f"{project}.src.shape.input")
    assert _calls_from(mock_ingestor, run_qn) == {target}
    assert _resolutions_from(mock_ingestor, run_qn) == {cs.EdgeResolution.EXACT.value}


def test_callback_reference_never_targets_the_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `map(input)` fans out through the callback path, which emits one CALLS
    # row per variant without checking its kind; on main one of them was
    # aimed at the Type node's `@line` name and dropped by the database.
    _write(
        temp_repo,
        {"shape.ts": TYPE_AND_FUNCTION_TS, "use.ts": USE_INPUT_AS_CALLBACK_TS},
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    cb_qn = f"{project}.src.use.viaCallback"
    target = (cs.NodeLabel.FUNCTION.value, f"{project}.src.shape.input")
    assert _calls_from(mock_ingestor, cb_qn) == {target}
    assert _resolutions_from(mock_ingestor, cb_qn) == {cs.EdgeResolution.EXACT.value}


def test_type_position_binds_to_the_type_declaration(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(temp_repo, {"shape.ts": TYPE_AND_FUNCTION_TS, "use.ts": USE_INPUT_TS})
    run_updater(temp_repo, mock_ingestor)

    qn = f"{temp_repo.name}.src.shape.input"
    returns = {
        (fl, fq, tl, tq)
        for fl, fq, tl, tq, _r in _edges(mock_ingestor, cs.RelationshipType.RETURNS)
    }
    assert (
        cs.NodeLabel.FUNCTION.value,
        qn,
        cs.NodeLabel.TYPE.value,
        qn,
    ) in returns, returns


@pytest.mark.parametrize(
    "shape",
    [INTERFACE_AND_ARROW_TS, INTERFACE_AND_FUNCTION_TS],
    ids=["const-arrow", "function"],
)
def test_interface_and_value_share_the_name_and_calls_stay_exact(
    temp_repo: Path, mock_ingestor: MagicMock, shape: str
) -> None:
    _write(temp_repo, {"shape.ts": shape, "use.ts": USE_OPTS_TS})
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    qn = f"{project}.src.shape.Opts"
    nodes = _nodes(mock_ingestor)
    assert (cs.NodeLabel.FUNCTION.value, qn) in nodes, nodes
    assert (cs.NodeLabel.INTERFACE.value, qn) in nodes, nodes
    assert not any(q.startswith(f"{qn}{cs.DUP_QN_MARKER}") for _l, q in nodes), nodes

    run_qn = f"{project}.src.use.run"
    assert _calls_from(mock_ingestor, run_qn) == {(cs.NodeLabel.FUNCTION.value, qn)}
    assert _resolutions_from(mock_ingestor, run_qn) == {cs.EdgeResolution.EXACT.value}


def test_interface_declared_before_class_leaves_the_class_constructible(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Both go through the class pass in source order, so the interface used to
    # take the plain name and push the class to `Box@3`: `new Box()` then
    # resolved to the interface and produced no INSTANTIATES edge at all.
    _write(temp_repo, {"shape.ts": INTERFACE_THEN_CLASS_TS, "use.ts": USE_BOX_TS})
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    box = f"{project}.src.shape.Box"
    nodes = _nodes(mock_ingestor)
    assert (cs.NodeLabel.CLASS.value, box) in nodes, nodes
    assert (cs.NodeLabel.INTERFACE.value, box) in nodes, nodes
    assert (cs.NodeLabel.METHOD.value, f"{box}.get") in nodes, nodes

    run_qn = f"{project}.src.use.run"
    instantiates = {
        (tl, tq)
        for _fl, fq, tl, tq, _r in _edges(
            mock_ingestor, cs.RelationshipType.INSTANTIATES
        )
        if fq == run_qn
    }
    assert instantiates == {(cs.NodeLabel.CLASS.value, box)}
    assert (cs.NodeLabel.METHOD.value, f"{box}.get") in _calls_from(
        mock_ingestor, run_qn
    )


def test_heritage_clauses_on_a_twin_bind_to_the_type_side(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `implements Opts` and `interface Opts extends Base` are type positions:
    # the IMPLEMENTS target and the INHERITS source are the interface, never
    # the same-named function that holds the value side.
    _write(temp_repo, {"shape.ts": IMPLEMENTS_TWIN_TS})
    run_updater(temp_repo, mock_ingestor)

    shape = f"{temp_repo.name}.src.shape"
    implements = {
        (fl, fq, tl, tq)
        for fl, fq, tl, tq, _r in _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    }
    assert implements == {
        (
            cs.NodeLabel.CLASS.value,
            f"{shape}.Impl",
            cs.NodeLabel.INTERFACE.value,
            f"{shape}.Opts",
        )
    }
    inherits = {
        (fl, fq, tl, tq)
        for fl, fq, tl, tq, _r in _edges(mock_ingestor, cs.RelationshipType.INHERITS)
    }
    assert inherits == {
        (
            cs.NodeLabel.INTERFACE.value,
            f"{shape}.Opts",
            cs.NodeLabel.INTERFACE.value,
            f"{shape}.Base",
        )
    }


def test_renaming_the_function_leaves_its_type_twin(temp_repo: Path) -> None:
    # The function's own return annotation names the type. Rename reads edges
    # by qualified name, so without scoping them to the renamed node's label
    # that RETURNS edge became a siteless structural edge and refused.
    _write(temp_repo, {"shape.ts": TYPE_AND_FUNCTION_TS, "use.ts": USE_INPUT_TS})
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store, repo_path=temp_repo, parsers=parsers, queries=queries
    )
    updater.run()

    report = rename(
        temp_repo,
        store.fetch_all,
        updater.project_name,
        f"{updater.project_name}.src.shape.input",
        "makeInput",
        dry_run=True,
    )

    assert "+export function makeInput<T>(value: T): input<T> {" in report.diff
    assert "-export type" not in report.diff
    assert '+import { makeInput } from "./shape";' in report.diff


def test_renaming_a_function_whose_import_also_binds_its_type_twin_refuses(
    temp_repo: Path,
) -> None:
    # `import { input }` binds the function AND the type. Rewriting it to
    # `import { makeInput }` leaves the `input<number>` annotation unbound
    # (TS2304), so the rename must not go ahead.
    _write(
        temp_repo,
        {"shape.ts": TYPE_AND_FUNCTION_TS, "use.ts": USE_INPUT_AND_ITS_TYPE_TS},
    )
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store, repo_path=temp_repo, parsers=parsers, queries=queries
    )
    updater.run()

    with pytest.raises(RenameRefused) as refused:
        rename(
            temp_repo,
            store.fetch_all,
            updater.project_name,
            f"{updater.project_name}.src.shape.input",
            "makeInput",
            dry_run=True,
        )
    assert "src/use.ts" in str(refused.value)
    assert [site.path for site in refused.value.ambiguous] == ["src/use.ts"]


def test_renaming_a_function_whose_import_is_also_implemented_refuses(
    temp_repo: Path,
) -> None:
    # `implements Opts` names the interface the same import binds.
    _write(
        temp_repo,
        {"shape.ts": INTERFACE_AND_FUNCTION_TS, "use.ts": USE_OPTS_AND_IMPLEMENT_IT_TS},
    )
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store, repo_path=temp_repo, parsers=parsers, queries=queries
    )
    updater.run()

    with pytest.raises(RenameRefused) as refused:
        rename(
            temp_repo,
            store.fetch_all,
            updater.project_name,
            f"{updater.project_name}.src.shape.Opts",
            "makeOpts",
            dry_run=True,
        )
    assert [site.path for site in refused.value.ambiguous] == ["src/use.ts"]


def test_protobuf_export_keeps_both_twins_inheritance_edges(temp_repo: Path) -> None:
    # `interface Box extends Base` and `class Box extends Base` are two
    # INHERITS edges between the same two qualified names, told apart only by
    # the source's label; the export must not merge them into one.
    _write(temp_repo, {"shape.ts": BASE_AND_BOX_TWINS_TS})
    out = temp_repo.parent / f"{temp_repo.name}-pb"
    out.mkdir()
    exporter = ProtobufFileIngestor(str(out), repo_path=str(temp_repo))
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=exporter, repo_path=temp_repo, parsers=parsers, queries=queries
    ).run()
    exporter.flush_all()

    index = pb.GraphCodeIndex()
    index.ParseFromString((out / "index.bin").read_bytes())
    shape = f"{temp_repo.name}.src.shape"
    inherits = sorted(
        (rel.source_label, rel.source_id, rel.target_label, rel.target_id)
        for rel in index.relationships
        if rel.type == pb.Relationship.INHERITS
    )
    assert inherits == [
        (
            cs.NodeLabel.CLASS.value,
            f"{shape}.Box",
            cs.NodeLabel.CLASS.value,
            f"{shape}.Base",
        ),
        (
            cs.NodeLabel.INTERFACE.value,
            f"{shape}.Box",
            cs.NodeLabel.CLASS.value,
            f"{shape}.Base",
        ),
    ]


def test_an_incremental_run_reads_both_twins_back(temp_repo: Path) -> None:
    # Only use.ts changes, so shape.ts's two `Box` nodes come back from the
    # store. Registering the first row and skipping the second left the
    # interface as the name's only kind, and `new Box()` bound nothing.
    _write(temp_repo, {"shape.ts": INTERFACE_THEN_CLASS_TS, "use.ts": USE_BOX_TS})
    store = _StatefulIngestor()
    parsers, queries = load_parsers()

    def updater() -> GraphUpdater:
        return GraphUpdater(
            ingestor=store, repo_path=temp_repo, parsers=parsers, queries=queries
        )

    updater().run(force=True)
    (temp_repo / "src" / "use.ts").write_text(f"{USE_BOX_TS}// touched\n")
    updater().run()

    box = f"{updater().project_name}.src.shape.Box"
    instantiated = {
        (str(target_label), str(target))
        for _sl, source, rel, target_label, target in store.edges
        if str(source).endswith(".src.use.run")
        and rel == cs.RelationshipType.INSTANTIATES.value
    }
    assert instantiated == {(cs.NodeLabel.CLASS.value, box)}


# Negative tests: what must NOT change.


def test_redeclared_function_still_takes_a_line_variant(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(temp_repo, {"shape.ts": REDECLARED_FUNCTION_TS, "use.ts": USE_F_TS})
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    f_qn = f"{project}.src.shape.f"
    variant = f"{f_qn}{cs.DUP_QN_MARKER}5"
    nodes = _nodes(mock_ingestor)
    assert (cs.NodeLabel.FUNCTION.value, f_qn) in nodes, nodes
    assert (cs.NodeLabel.FUNCTION.value, variant) in nodes, nodes

    run_qn = f"{project}.src.use.run"
    assert _calls_from(mock_ingestor, run_qn) == {
        (cs.NodeLabel.FUNCTION.value, f_qn),
        (cs.NodeLabel.FUNCTION.value, variant),
    }
    assert _resolutions_from(mock_ingestor, run_qn) == {
        cs.EdgeResolution.OVERLOAD.value
    }


def test_overload_signatures_still_fan_out_as_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(temp_repo, {"shape.ts": OVERLOADED_FUNCTION_TS, "use.ts": USE_F_TS})
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    run_qn = f"{project}.src.use.run"
    assert len(_calls_from(mock_ingestor, run_qn)) == 3
    assert _resolutions_from(mock_ingestor, run_qn) == {
        cs.EdgeResolution.OVERLOAD.value
    }


def test_same_names_in_different_modules_are_unchanged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(
        temp_repo,
        {
            "shape.ts": TYPE_ONLY_TS,
            "other.ts": FUNCTION_ONLY_TS,
            "use.ts": USE_OTHER_INPUT_TS,
        },
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    nodes = _nodes(mock_ingestor)
    assert (cs.NodeLabel.TYPE.value, f"{project}.src.shape.input") in nodes, nodes
    assert (cs.NodeLabel.FUNCTION.value, f"{project}.src.other.input") in nodes, nodes

    run_qn = f"{project}.src.use.run"
    assert _calls_from(mock_ingestor, run_qn) == {
        (cs.NodeLabel.FUNCTION.value, f"{project}.src.other.input")
    }
    assert _resolutions_from(mock_ingestor, run_qn) == {cs.EdgeResolution.EXACT.value}


class TestRegistryDeclarationSpaces:
    def test_a_type_joins_a_value_without_a_variant(self) -> None:
        registry = FunctionRegistryTrie()
        registry["m.input"] = NodeType.FUNCTION
        qn = registry.register_unique_qn("m.input", 1, kind=NodeType.TYPE)
        registry[qn] = NodeType.TYPE

        assert qn == "m.input"
        assert registry.get("m.input") == NodeType.FUNCTION
        assert registry.type_kind("m.input") == NodeType.TYPE
        assert registry.variants("m.input") == ["m.input"]

    def test_a_value_takes_over_the_entry_from_an_earlier_type(self) -> None:
        registry = FunctionRegistryTrie()
        registry["m.Box"] = NodeType.INTERFACE
        qn = registry.register_unique_qn("m.Box", 3, kind=NodeType.CLASS)
        registry[qn] = NodeType.CLASS

        assert qn == "m.Box"
        assert registry.get("m.Box") == NodeType.CLASS
        assert registry.type_kind("m.Box") == NodeType.INTERFACE
        assert registry.variants("m.Box") == ["m.Box"]

    def test_the_same_space_still_takes_a_variant(self) -> None:
        registry = FunctionRegistryTrie()
        registry["m.f"] = NodeType.FUNCTION
        qn = registry.register_unique_qn("m.f", 5, kind=NodeType.FUNCTION)

        assert qn == f"m.f{cs.DUP_QN_MARKER}5"
        assert registry.variants("m.f") == ["m.f", qn]

    def test_a_third_declaration_after_the_pair_takes_a_variant(self) -> None:
        registry = FunctionRegistryTrie()
        registry["m.input"] = NodeType.FUNCTION
        registry[registry.register_unique_qn("m.input", 1, kind=NodeType.TYPE)] = (
            NodeType.TYPE
        )
        qn = registry.register_unique_qn("m.input", 9, kind=NodeType.INTERFACE)

        assert qn == f"m.input{cs.DUP_QN_MARKER}9"

    def test_a_caller_that_names_no_kind_keeps_the_variant(self) -> None:
        # Only the TS passes opt in; every other language's collision between a
        # type and a value is decided exactly as before.
        registry = FunctionRegistryTrie()
        registry["m.input"] = NodeType.FUNCTION
        qn = registry.register_unique_qn("m.input", 1)

        assert qn == f"m.input{cs.DUP_QN_MARKER}1"

    def test_deleting_the_name_forgets_both_sides(self) -> None:
        registry = FunctionRegistryTrie()
        registry["m.input"] = NodeType.FUNCTION
        registry[registry.register_unique_qn("m.input", 1, kind=NodeType.TYPE)] = (
            NodeType.TYPE
        )
        del registry["m.input"]

        assert registry.get("m.input") is None
        assert registry.type_kind("m.input") is None
