"""Go implicit interface satisfaction on the tree-sitter path (issue #2483).

A Go type implements an interface by having its methods, with no clause
naming it. The graph used to record none of that: the interface's method set
had no nodes, no type IMPLEMENTS any interface, and `s.Area()` through a
`Shape` bound to one implementation picked by name, leaving the others with no
caller. Every case indexes a small module through the real parsers with the
go/types frontend off, which is what most installs run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from filelock import FileLock

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.checkout_state import state_file
from codebase_rag.editing.rename import RenameRefused, rename
from codebase_rag.graph_query import QueryFn
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.go import interfaces as go_ifaces
from codebase_rag.parsers.go.builds import Expr
from codebase_rag.parsers.go_frontend import GoCallSite, GoImplements, GoSemanticFacts
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_relationships,
    shared_session_dir,
)
from codebase_rag.tests.test_rename_op import RecordedGraph, _write
from codebase_rag.tests.test_rename_op import _index as _rename_index
from codebase_rag.utils import qn_markers
from evals.cgr_graph import _StatefulIngestor

# Every go/types test here shares one helper build per session, one helper
# run per tree and GOOS (`_one_real_go_run_per_tree`) and one `go vet` per
# tree and GOOS; every index, the fingerprint's tool and grammar versions.
pytestmark = pytest.mark.usefixtures(
    "one_go_helper_per_session",
    "fingerprint_inputs_once_per_session",
    "_one_real_go_run_per_tree",
    "_shared_vet_cache",
)


def _query(graph: RecordedGraph) -> QueryFn:
    # The recording graph spelled as the query function a rename takes.
    return lambda query, params: graph.fetch_all(
        query, dict(params) if params is not None else None
    )


SHAPES = """package shapes

type Shape interface {
\tArea() float64
}

type Square struct{ S float64 }

func (s Square) Area() float64 { return s.S * s.S }

type Circle struct{ R float64 }

func (c *Circle) Area() float64 { return 3.14 * c.R * c.R }

type NotAShape struct{}

func (n NotAShape) Perimeter() float64 { return 0 }

func Total(shapes []Shape) float64 {
\tt := 0.0
\tfor _, s := range shapes {
\t\tt += s.Area()
\t}
\treturn t
}
"""


def _index(
    root: Path,
    files: dict[str, str],
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    module: str | None = None,
) -> str:
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root.mkdir()
    (root / "go.mod").write_text(
        f"module {module or root.name}\n\ngo 1.22\n", encoding="utf-8"
    )
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    return root.name


def _edges(
    mock_ingestor: MagicMock, rel: cs.RelationshipType
) -> dict[tuple[str, str], dict]:
    edges: dict[tuple[str, str], dict] = {}
    for c in get_relationships(mock_ingestor, rel.value):
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        edges[(c.args[0][2], c.args[2][2])] = dict(props or {})
    return edges


def _node_labels(mock_ingestor: MagicMock) -> dict[str, str]:
    return {
        str(c.args[1][cs.KEY_QUALIFIED_NAME]): str(c.args[0])
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if cs.KEY_QUALIFIED_NAME in c.args[1]
    }


@pytest.fixture
def shapes(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> str:
    project = _index(
        temp_repo / "goiface", {"shapes.go": SHAPES}, mock_ingestor, monkeypatch
    )
    return f"{project}.shapes"


# --- the three defects the issue names ---------------------------------------


def test_an_interface_method_is_a_method_node_of_its_interface(
    shapes: str, mock_ingestor: MagicMock
) -> None:
    labels = _node_labels(mock_ingestor)
    assert labels.get(f"{shapes}.Shape.Area") == cs.NodeLabel.METHOD
    defines = _edges(mock_ingestor, cs.RelationshipType.DEFINES_METHOD)
    assert (f"{shapes}.Shape", f"{shapes}.Shape.Area") in defines


def test_value_and_pointer_receiver_types_implement_the_interface(
    shapes: str, mock_ingestor: MagicMock
) -> None:
    implements = _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    for implementer in ("Square", "Circle"):
        edge = (f"{shapes}.{implementer}", f"{shapes}.Shape")
        assert edge in implements, implements
        # Only the names and arities were compared, not the signatures.
        assert implements[edge][cs.KEY_RESOLUTION] == cs.EdgeResolution.HEURISTIC
    assert (f"{shapes}.NotAShape", f"{shapes}.Shape") not in implements


def test_each_implementing_method_overrides_the_interface_method(
    shapes: str, mock_ingestor: MagicMock
) -> None:
    overrides = _edges(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert (f"{shapes}.Square.Area", f"{shapes}.Shape.Area") in overrides
    assert (f"{shapes}.Circle.Area", f"{shapes}.Shape.Area") in overrides


def test_a_call_through_the_interface_reaches_every_implementation(
    shapes: str, mock_ingestor: MagicMock
) -> None:
    calls = {
        callee: props[cs.KEY_RESOLUTION]
        for (caller, callee), props in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        ).items()
        if caller == f"{shapes}.Total"
    }
    # The static callee is the interface method; the receiver is typed.
    assert calls.get(f"{shapes}.Shape.Area") == cs.EdgeResolution.EXACT, calls
    # Either implementation can run; each is tied to Shape by a name-and-arity
    # match, so its edge is as much a guess as that match.
    assert calls.get(f"{shapes}.Square.Area") == cs.EdgeResolution.HEURISTIC, calls
    assert calls.get(f"{shapes}.Circle.Area") == cs.EdgeResolution.HEURISTIC, calls


def test_a_call_through_an_interface_parameter_reaches_every_implementation(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = SHAPES + "\nfunc Twice(s Shape) float64 { return 2 * s.Area() }\n"
    project = _index(
        temp_repo / "goparam", {"shapes.go": source}, mock_ingestor, monkeypatch
    )
    pkg = f"{project}.shapes"
    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)
    for callee in ("Shape.Area", "Square.Area", "Circle.Area"):
        assert (f"{pkg}.Twice", f"{pkg}.{callee}") in calls, calls


# --- across files and packages ---------------------------------------------

GEO_SHAPE = """package geo

type Shape interface {
\tArea() float64
}

func Sum(shapes ...Shape) float64 {
\tt := 0.0
\tfor _, s := range shapes {
\t\tt += s.Area()
\t}
\treturn t
}
"""

GEO_SQUARE = """package geo

type Square struct{ S float64 }

func (s Square) Area() float64 { return s.S * s.S }
"""

HEX = """package tiles

type Hex struct{ R float64 }

func (h *Hex) Area() float64 { return 2.6 * h.R * h.R }
"""

SOLIDS = """package solids

import "example.com/goapp/geo"

type Solid interface {
\tgeo.Shape
\tVolume() float64
}

type Cube struct{ S float64 }

func (c Cube) Area() float64   { return 6 * c.S * c.S }
func (c Cube) Volume() float64 { return c.S * c.S * c.S }
"""


@pytest.fixture
def goapp(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> str:
    root = temp_repo / "goapp"
    files = {
        "geo/shape.go": GEO_SHAPE,
        "geo/square.go": GEO_SQUARE,
        "tiles/hex.go": HEX,
        "solids/solid.go": SOLIDS,
    }
    return _index(root, files, mock_ingestor, monkeypatch, "example.com/goapp")


def test_types_in_other_files_and_packages_implement_the_interface(
    goapp: str, mock_ingestor: MagicMock
) -> None:
    # Satisfaction needs no import: `tiles` never mentions `geo`.
    implements = _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    shape = f"{goapp}.geo.shape.Shape"
    assert (f"{goapp}.geo.square.Square", shape) in implements
    assert (f"{goapp}.tiles.hex.Hex", shape) in implements
    assert (f"{goapp}.solids.solid.Cube", shape) in implements


def test_an_embedded_interface_from_another_package_counts(
    goapp: str, mock_ingestor: MagicMock
) -> None:
    solid = f"{goapp}.solids.solid.Solid"
    inherits = _edges(mock_ingestor, cs.RelationshipType.INHERITS)
    assert (solid, f"{goapp}.geo.shape.Shape") in inherits
    implements = _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert (f"{goapp}.solids.solid.Cube", solid) in implements
    # Square has Area but not Volume: it is a Shape, never a Solid.
    assert (f"{goapp}.geo.square.Square", solid) not in implements
    assert (f"{goapp}.tiles.hex.Hex", solid) not in implements


def test_a_variadic_interface_parameter_dispatches_to_every_package(
    goapp: str, mock_ingestor: MagicMock
) -> None:
    calls = {
        callee
        for caller, callee in _edges(mock_ingestor, cs.RelationshipType.CALLS)
        if caller == f"{goapp}.geo.shape.Sum"
    }
    assert calls == {
        f"{goapp}.geo.shape.Shape.Area",
        f"{goapp}.geo.square.Square.Area",
        f"{goapp}.tiles.hex.Hex.Area",
        f"{goapp}.solids.solid.Cube.Area",
    }


# --- what must not implement, or be dispatched to ---------------------------

NEGATIVES = """package shapes

import "fmt"

type Shape interface {
\tArea() float64
\tPerimeter() float64
}

type Scaler interface {
\tScale(f float64)
}

type Anything interface{}

type Whatever interface {
\tany
}

type Named interface {
\tfmt.Stringer
\tName() string
}

type Number interface {
\t~int | ~float64
\tString() string
}

type Fault interface {
\terror
\tCode() int
}

type Square struct{ S float64 }

func (s Square) Area() float64      { return s.S * s.S }
func (s Square) Perimeter() float64 { return 4 * s.S }
func (s Square) Name() string       { return "square" }
func (s Square) String() string     { return "square" }

// Area only: a Shape needs Perimeter too.
type Disc struct{ R float64 }

func (d Disc) Area() float64 { return 3.14 * d.R * d.R }

// Scale with no factor is not Scale(f float64).
type Zoom struct{}

func (z Zoom) Scale() {}

// The same names as Shape's, with other parameter counts.
type Painter struct{}

func (p Painter) Area(layers int) float64    { return 0 }
func (p Painter) Perimeter(dash int) float64 { return 0 }

type Failure struct{}

func (f Failure) Error() string { return "failed" }
func (f Failure) Code() int     { return 1 }

func Measure(s Shape) float64 { return s.Area() + s.Perimeter() }

func Direct() float64 {
\tsq := Square{S: 2}
\treturn sq.Area()
}
"""


@pytest.fixture
def negatives(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> str:
    project = _index(
        temp_repo / "goneg", {"shapes.go": NEGATIVES}, mock_ingestor, monkeypatch
    )
    return f"{project}.shapes"


def _implementers(mock_ingestor: MagicMock, interface_qn: str) -> set[str]:
    return {
        source
        for source, target in _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
        if target == interface_qn
    }


def test_a_type_missing_a_method_or_with_another_arity_does_not_implement(
    negatives: str, mock_ingestor: MagicMock
) -> None:
    assert _implementers(mock_ingestor, f"{negatives}.Shape") == {f"{negatives}.Square"}
    # Zoom's only method is Scaler's name with no parameter.
    assert _implementers(mock_ingestor, f"{negatives}.Scaler") == set()


def test_an_empty_interface_has_no_implementers(
    negatives: str, mock_ingestor: MagicMock
) -> None:
    # Every type satisfies `interface{}` and `any`; an edge from each says
    # nothing about any of them.
    assert _implementers(mock_ingestor, f"{negatives}.Anything") == set()
    assert _implementers(mock_ingestor, f"{negatives}.Whatever") == set()


def test_an_interface_embedding_an_unknown_one_has_no_implementers(
    negatives: str, mock_ingestor: MagicMock
) -> None:
    # fmt.Stringer is not first-party, so Named's method set is not known
    # whole; Square has Name and String, but a match on what is known would
    # be a guess about the rest. A type-set constraint is no method set.
    assert _implementers(mock_ingestor, f"{negatives}.Named") == set()
    assert _implementers(mock_ingestor, f"{negatives}.Number") == set()


def test_the_predeclared_error_interface_counts_when_embedded(
    negatives: str, mock_ingestor: MagicMock
) -> None:
    assert _implementers(mock_ingestor, f"{negatives}.Fault") == {
        f"{negatives}.Failure"
    }


def test_a_same_named_method_of_a_non_implementer_is_no_dispatch_target(
    negatives: str, mock_ingestor: MagicMock
) -> None:
    calls = {
        callee
        for caller, callee in _edges(mock_ingestor, cs.RelationshipType.CALLS)
        if caller == f"{negatives}.Measure"
    }
    assert calls == {
        f"{negatives}.Shape.Area",
        f"{negatives}.Shape.Perimeter",
        f"{negatives}.Square.Area",
        f"{negatives}.Square.Perimeter",
    }
    overrides = _edges(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert not any(
        source.startswith((f"{negatives}.Painter.", f"{negatives}.Disc."))
        for source, _target in overrides
    ), overrides


def test_a_call_on_a_concrete_receiver_stays_on_its_own_method(
    negatives: str, mock_ingestor: MagicMock
) -> None:
    calls = {
        callee
        for caller, callee in _edges(mock_ingestor, cs.RelationshipType.CALLS)
        if caller == f"{negatives}.Direct"
    }
    assert calls == {f"{negatives}.Square.Area"}


GUESSED = """package shapes

type Shape interface {
\tArea() float64
}

func load() []Shape { return nil }

func Guess() float64 {
\tt := 0.0
\tfor _, s := range load() {
\t\tt += s.Area()
\t}
\treturn t
}
"""

IMPLS = """package shapes

type Square struct{ S float64 }

func (s Square) Area() float64 { return s.S * s.S }

type Circle struct{ R float64 }

func (c *Circle) Area() float64 { return 3.14 * c.R * c.R }
"""


def test_a_guessed_interface_callee_is_not_fanned_out(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `s` ranges over a call's result, which the walker does not type, so
    # `s.Area()` binds by name alone, here to the interface method of its
    # own file. One guess must not become an edge to every implementation.
    project = _index(
        temp_repo / "goguess",
        {"shape.go": GUESSED, "impl.go": IMPLS},
        mock_ingestor,
        monkeypatch,
    )
    calls = {
        callee: props[cs.KEY_RESOLUTION]
        for (caller, callee), props in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        ).items()
        if caller == f"{project}.shape.Guess" and callee.endswith(".Area")
    }
    assert calls == {f"{project}.shape.Shape.Area": cs.EdgeResolution.HEURISTIC}
    # The types still implement it; only the guessed call stays unexpanded.
    assert _implementers(mock_ingestor, f"{project}.shape.Shape") == {
        f"{project}.impl.Square",
        f"{project}.impl.Circle",
    }


# --- with the go/types frontend's facts -------------------------------------


def _position(source: str, needle: str, offset: int = 0) -> tuple[int, int]:
    # (1-based line, 0-based byte column) of `needle`'s first occurrence,
    # shifted `offset` characters into it.
    for line_no, line in enumerate(source.splitlines(), 1):
        if (index := line.find(needle)) >= 0:
            return line_no, len(line[: index + offset].encode("utf-8"))
    raise AssertionError(needle)


def _compared(rel: str, source: str) -> frozenset[tuple[str, int, int]]:
    # Every package-level type a go/types run compares in `source`, as the
    # helper reports them: the declaring identifier of each non-generic
    # `type` and its non-empty interfaces alike.
    return frozenset(
        (rel, *_position(source, f"type {name} ", len("type ")))
        for name in re.findall(r"^type (\w+) ", source, re.MULTILINE)
    )


def _gotypes(
    monkeypatch: pytest.MonkeyPatch,
    call_sites: dict[tuple[str, int, int, str], GoCallSite],
    implementers: tuple[str, ...],
) -> None:
    from codebase_rag.parsers.frontends import go as go_fe

    shape_line, shape_col = _position(SHAPES, "Shape interface")
    implements = [
        GoImplements(
            "shapes.go",
            *_position(SHAPES, f"{name} struct"),
            "shapes.go",
            shape_line,
            shape_col,
        )
        for name in implementers
    ]
    facts = GoSemanticFacts(
        call_sites=call_sites,
        external_sites=set(),
        implements=implements,
        analysed_modules=(".",),
        checked_files=frozenset({"shapes.go"}),
        compared_types=_compared("shapes.go", SHAPES),
    )
    monkeypatch.setattr(go_fe, "go_frontend_available", lambda: True)
    monkeypatch.setattr(go_fe, "run_go_frontend", lambda repo_path, ignored_dirs: facts)


def _index_with_gotypes(
    root: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> str:
    root.mkdir()
    (root / "go.mod").write_text(f"module {root.name}\n\ngo 1.22\n", encoding="utf-8")
    (root / "shapes.go").write_text(SHAPES, encoding="utf-8")
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    return f"{root.name}.shapes"


def test_a_go_types_call_through_the_interface_reaches_its_implementers(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # go/types names `s.Area()`'s callee as the interface method, at its name
    # token in Shape. That position had no node, so the fact missed and the
    # call fell back to one implementation picked by name.
    call_line, call_col = _position(SHAPES, "s.Area()", offset=2)
    target_line, target_col = _position(SHAPES, "\tArea() float64", offset=1)
    _gotypes(
        monkeypatch,
        {
            ("shapes.go", call_line, call_col, "Area"): GoCallSite(
                "Area", "shapes.go", target_line, target_col
            )
        },
        ("Square", "Circle"),
    )
    pkg = _index_with_gotypes(temp_repo / "gotypes", mock_ingestor, monkeypatch)
    calls = {
        callee: props[cs.KEY_RESOLUTION]
        for (caller, callee), props in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        ).items()
        if caller == f"{pkg}.Total"
    }
    assert calls == {
        f"{pkg}.Shape.Area": cs.EdgeResolution.EXACT,
        f"{pkg}.Square.Area": cs.EdgeResolution.OVERLOAD,
        f"{pkg}.Circle.Area": cs.EdgeResolution.OVERLOAD,
    }


def test_go_types_pairs_replace_the_method_name_match(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The compiler's pairs are proofs; matching names on top could only add
    # a pair it ruled out. Circle is left out of the facts on purpose.
    _gotypes(monkeypatch, {}, ("Square",))
    pkg = _index_with_gotypes(temp_repo / "gotypes", mock_ingestor, monkeypatch)
    implements = _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert set(implements) == {(f"{pkg}.Square", f"{pkg}.Shape")}
    assert cs.KEY_RESOLUTION not in implements[(f"{pkg}.Square", f"{pkg}.Shape")]
    overrides = _edges(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert (f"{pkg}.Square.Area", f"{pkg}.Shape.Area") in overrides
    assert (f"{pkg}.Circle.Area", f"{pkg}.Shape.Area") not in overrides


# --- incremental runs --------------------------------------------------------

INCREMENTAL = {
    "shape.go": """package shapes

import "fmt"

type Shape interface {
\tArea() float64
}

type Solid interface {
\tShape
\tVolume() float64
}

type Named interface {
\tfmt.Stringer
\tName() string
}
""",
    "square.go": """package shapes

type Square struct{ S float64 }

func (s Square) Area() float64 { return s.S * s.S }
func (s Square) Name() string  { return "square" }

type Cube struct{ S float64 }

func (c *Cube) Area() float64   { return 6 * c.S * c.S }
func (c *Cube) Volume() float64 { return c.S * c.S * c.S }
""",
}


def _incremental_implements(
    store: _StatefulIngestor,
) -> set[tuple[str, str, str]]:
    return {
        (
            str(key[1]),
            str(key[4]),
            str(store.edge_props.get(key, {}).get(cs.KEY_RESOLUTION)),
        )
        for key in store.keyed_edges
        if key[2] == cs.RelationshipType.IMPLEMENTS.value
    }


def _incremental_index(store: _StatefulIngestor, root: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.GO not in parsers:
        pytest.skip("go parser not available")
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


@pytest.mark.parametrize("touched", ["square.go", "shape.go"])
def test_an_incremental_run_keeps_what_a_clean_index_links(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, touched: str
) -> None:
    # Touching the types' file re-parses them against interfaces read back
    # from the graph: their arities from `param_types`, Solid's embedded
    # Shape from its INHERITS edge, and Named's unknown fmt.Stringer from
    # its `embeds`, without which Square would now implement Named.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = temp_repo / "proj"
    root.mkdir()
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in INCREMENTAL.items():
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _incremental_index(store, root, force=True)
    clean = _incremental_implements(store)
    assert {(impl, iface) for impl, iface, _ in clean} == {
        ("proj.square.Square", "proj.shape.Shape"),
        ("proj.square.Cube", "proj.shape.Shape"),
        ("proj.square.Cube", "proj.shape.Solid"),
    }

    path = root / touched
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(path.read_text(encoding="utf-8") + "// touched\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    _incremental_index(store, root, force=False)

    assert _incremental_implements(store) == clean


# --- what `cgr rename` reads from these edges --------------------------------

RENAMED = """package shapes

type Shape interface {
\tArea() float64
}

type Square struct{ S float64 }

func (s Square) Area() float64 { return s.S * s.S }

type Circle struct{ R float64 }

func (c *Circle) Area() float64 { return 3.14 * c.R * c.R }

func Use(s Shape) float64 { return s.Area() }

var _ Shape = Square{}
var _ Shape = &Circle{}
"""


def test_renaming_one_implementation_renames_the_interface_method_set(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rename's member set is the method plus its OVERRIDES hierarchy.
    # Without the edges it renamed Square.Area alone, Square stopped being a
    # Shape and the build broke while the rename reported success.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    _write(temp_repo, "go.mod", "module goren\n\ngo 1.22\n")
    _write(temp_repo, "shapes.go", RENAMED)
    graph = _rename_index(temp_repo, mock_ingestor)
    pkg = f"{graph.project}.shapes"
    query, target = _query(graph), f"{pkg}.Square.Area"
    with pytest.raises(RenameRefused):
        # `s.Area()` reaches each implementation as one candidate of several;
        # rewriting a shared call site is the opt-in it is for a Protocol.
        rename(temp_repo, query, graph.project, target, "Size")
    assert (temp_repo / "shapes.go").read_text(encoding="utf-8") == RENAMED

    report = rename(
        temp_repo,
        _query(graph),
        graph.project,
        f"{pkg}.Square.Area",
        "Size",
        allow_heuristic=True,
    )

    assert report.applied, report.message
    assert set(report.hierarchy) == {
        f"{pkg}.Square.Area",
        f"{pkg}.Shape.Area",
        f"{pkg}.Circle.Area",
    }
    text = (temp_repo / "shapes.go").read_text(encoding="utf-8")
    assert "Area" not in text
    for renamed in (
        "\tSize() float64",
        "func (s Square) Size()",
        "func (c *Circle) Size()",
        "return s.Size()",
    ):
        assert renamed in text, text
    if (go := shutil.which("go")) is not None:
        built = subprocess.run(
            [go, "vet", "./..."],
            cwd=temp_repo,
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
        )
        assert built.returncode == 0, built.stderr


FIELDS = """package logs

type Formatter interface {
\tFormat(e *Entry) ([]byte, error)
}

type Logger struct {
\tFormatter Formatter
}

type Entry struct {
\tLogger *Logger
}

type JSONFormatter struct{}

func (f *JSONFormatter) Format(e *Entry) ([]byte, error) { return nil, nil }

type TextFormatter struct{}

func (f *TextFormatter) Format(e *Entry) ([]byte, error) { return nil, nil }

func (entry *Entry) write() {
\t_, _ = entry.Logger.Formatter.Format(entry)
}

type Handler interface {
\tServeHTTP(code int)
}

type HandlerFunc func(code int)

func (f HandlerFunc) ServeHTTP(code int) { f(code) }
"""


def test_a_field_typed_interface_and_a_func_type_implementer(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # logrus's shape: the formatter is reached through a field typed with the
    # interface, and net/http's: a func type is the implementer.
    project = _index(
        temp_repo / "gofields", {"logs.go": FIELDS}, mock_ingestor, monkeypatch
    )
    pkg = f"{project}.logs"
    calls = {
        callee
        for caller, callee in _edges(mock_ingestor, cs.RelationshipType.CALLS)
        if caller == f"{pkg}.Entry.write"
    }
    assert calls == {
        f"{pkg}.Formatter.Format",
        f"{pkg}.JSONFormatter.Format",
        f"{pkg}.TextFormatter.Format",
    }
    implements = _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert (f"{pkg}.HandlerFunc", f"{pkg}.Handler") in implements


def test_the_real_go_types_facts_reach_every_implementation(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End to end through the bundled tool: its interface-method target joins
    # the node at the name token, and its own IMPLEMENTS pairs drive the
    # fan-out. Skipped where the tool cannot build.
    from codebase_rag.parsers.go_frontend import go_frontend_available

    if shutil.which("go") is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    pkg = _index_with_gotypes(temp_repo / "gotypes", mock_ingestor, monkeypatch)
    implements = _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    if not implements:
        pytest.skip("gotypes frontend could not build in this environment")
    assert set(implements) == {
        (f"{pkg}.Square", f"{pkg}.Shape"),
        (f"{pkg}.Circle", f"{pkg}.Shape"),
    }
    calls = {
        callee: props[cs.KEY_RESOLUTION]
        for (caller, callee), props in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        ).items()
        if caller == f"{pkg}.Total"
    }
    assert calls == {
        f"{pkg}.Shape.Area": cs.EdgeResolution.EXACT,
        f"{pkg}.Square.Area": cs.EdgeResolution.OVERLOAD,
        f"{pkg}.Circle.Area": cs.EdgeResolution.OVERLOAD,
    }


# --- unexported methods keep their package (PR #3360 review) ---------------

STORE = """package store

type Store interface {
\tget(key string) string
}

type mem struct{}

func (m mem) get(key string) string { return key }

func Lookup(s Store) string { return s.get("k") }
"""

API = """package api

import "example.com/unexp/store"

type Named interface {
\tstore.Store
\tName() string
}
"""

STORE_NAMED = """package store

type named struct{}

func (n named) get(key string) string { return key }
func (n named) Name() string          { return "named" }
"""

OTHER = """package other

type Fake struct{}

func (f Fake) get(key string) string { return key }
func (f Fake) Name() string          { return "fake" }
"""


@pytest.fixture
def unexported(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> str:
    files = {
        "store/store.go": STORE,
        "store/named.go": STORE_NAMED,
        "api/api.go": API,
        "other/other.go": OTHER,
    }
    return _index(
        temp_repo / "unexp", files, mock_ingestor, monkeypatch, "example.com/unexp"
    )


def test_an_unexported_method_is_satisfied_only_inside_its_package(
    unexported: str, mock_ingestor: MagicMock
) -> None:
    # `get` in package other is not store's `get`: Go qualifies an
    # unexported name with its package, so Fake is no Store.
    store = f"{unexported}.store.store.Store"
    assert _implementers(mock_ingestor, store) == {
        f"{unexported}.store.store.mem",
        f"{unexported}.store.named.named",
    }
    overrides = _edges(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert (f"{unexported}.store.store.mem.get", f"{store}.get") in overrides
    assert not any(
        source.startswith(f"{unexported}.other.") for source, _target in overrides
    ), overrides
    calls = {
        callee
        for caller, callee in _edges(mock_ingestor, cs.RelationshipType.CALLS)
        if caller == f"{unexported}.store.store.Lookup"
    }
    assert f"{unexported}.other.other.Fake.get" not in calls, calls
    assert f"{unexported}.store.store.mem.get" in calls, calls


def test_an_embedded_unexported_method_keeps_the_embedded_interfaces_package(
    unexported: str, mock_ingestor: MagicMock
) -> None:
    # api.Named gets store's unexported `get` through the embedded Store, so
    # only a type of package store can have it, even though Named is in api.
    assert _implementers(mock_ingestor, f"{unexported}.api.api.Named") == {
        f"{unexported}.store.named.named"
    }


# --- embedded interfaces of one name from two packages ----------------------

CONTRACT_ONE = """package one

type Contract interface {
\tFirst() int
}
"""

CONTRACT_TWO = """package two

type Contract interface {
\tSecond() int
}
"""

BOTH = """package outer

import (
\ta "example.com/both/one"
\tb "example.com/both/two"
)

type Both interface {
\ta.Contract
\tb.Contract
}
"""

PARTS = """package impl

type Full struct{}

func (f Full) First() int  { return 1 }
func (f Full) Second() int { return 2 }

type Half struct{}

func (h Half) First() int { return 1 }
"""


def test_two_embedded_interfaces_of_one_name_stay_apart_after_an_incremental_run(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Read back on an incremental run, `a.Contract` and `b.Contract` must
    # stay two bases: collapsed onto one, Both would ask for First alone and
    # Half would implement it.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = temp_repo / "both"
    files = {
        "one/one.go": CONTRACT_ONE,
        "two/two.go": CONTRACT_TWO,
        "outer/outer.go": BOTH,
        "impl/impl.go": PARTS,
    }
    root.mkdir()
    (root / "go.mod").write_text("module example.com/both\n\ngo 1.22\n")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _incremental_index(store, root, force=True)
    clean = _incremental_implements(store)
    both = "proj.outer.outer.Both"
    assert {impl for impl, iface, _ in clean if iface == both} == {
        "proj.impl.impl.Full"
    }

    path = root / "impl/impl.go"
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(path.read_text(encoding="utf-8") + "// touched\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    _incremental_index(store, root, force=False)

    assert _incremental_implements(store) == clean


# --- whose pairs are the compiler's ------------------------------------------

# Put(Celsius) is not Put(int), which the fallback cannot see: Celsius is a
# name of the package's, and as far as names go it may be `= int`.
SINK = """package sink

type Celsius float64

type Sink interface {
\tPut(v int)
}

type Box struct{}

func (b Box) Put(v Celsius) {}
"""


def _gotypes_facts(monkeypatch: pytest.MonkeyPatch, facts: GoSemanticFacts) -> None:
    from codebase_rag.parsers.frontends import go as go_fe

    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    monkeypatch.setattr(go_fe, "go_frontend_available", lambda: True)
    monkeypatch.setattr(go_fe, "run_go_frontend", lambda repo_path, ignored_dirs: facts)


def test_a_go_types_run_that_proves_no_pair_adds_no_name_match(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Put(Celsius) is not Put(int). The fallback cannot tell, so it would
    # link them; go/types analysed the module and proved no pair, which is
    # the answer, not a missing one.
    root = temp_repo / "gosink"
    root.mkdir()
    (root / "go.mod").write_text("module gosink\n\ngo 1.22\n", encoding="utf-8")
    (root / "sink.go").write_text(SINK, encoding="utf-8")
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[],
            analysed_modules=(".",),
            checked_files=frozenset({"sink.go"}),
            compared_types=_compared("sink.go", SINK),
        ),
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    assert _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS) == {}


def test_without_go_types_the_same_arity_is_still_matched(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fallback's known limit, kept visible: a type of the package's own
    # may be an alias of a predeclared one, so it matches whatever it meets
    # and the edge is labelled heuristic.
    project = _index(
        temp_repo / "gosink", {"sink.go": SINK}, mock_ingestor, monkeypatch
    )
    implements = _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    edge = (f"{project}.sink.Box", f"{project}.sink.Sink")
    assert implements[edge][cs.KEY_RESOLUTION] == cs.EdgeResolution.HEURISTIC


LEGACY = """package legacy

type Shape interface {
\tArea() float64
}

type Tile struct{}

func (t Tile) Area() float64 { return 1 }
"""


def test_a_degraded_module_keeps_the_fallback_and_an_analysed_one_does_not(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # go/types analysed the root module and proved Square alone; its run in
    # the nested `legacy` module failed. Circle gets no edge to the root's
    # Shape, while Tile is matched by name as if go/types were off.
    root = temp_repo / "gomods"
    root.mkdir()
    (root / "go.mod").write_text("module gomods\n\ngo 1.22\n", encoding="utf-8")
    (root / "shapes.go").write_text(SHAPES, encoding="utf-8")
    (root / "legacy").mkdir()
    (root / "legacy" / "go.mod").write_text("module legacy\n\ngo 1.22\n")
    (root / "legacy" / "legacy.go").write_text(LEGACY, encoding="utf-8")
    shape_line, shape_col = _position(SHAPES, "Shape interface")
    square_line, square_col = _position(SHAPES, "Square struct")
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    "shapes.go",
                    square_line,
                    square_col,
                    "shapes.go",
                    shape_line,
                    shape_col,
                )
            ],
            degraded_modules=("legacy",),
            analysed_modules=(".",),
            checked_files=frozenset({"shapes.go"}),
            compared_types=_compared("shapes.go", SHAPES),
        ),
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    implements = {
        pair: props.get(cs.KEY_RESOLUTION)
        for pair, props in _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS).items()
    }
    root_shape, legacy_shape = "gomods.shapes.Shape", "gomods.legacy.legacy.Shape"
    # Inside the analysed module the compiler's pair is the only one.
    assert {impl for impl, iface in implements if iface == root_shape} == {
        "gomods.shapes.Square",
        "gomods.legacy.legacy.Tile",
    }
    assert implements[("gomods.shapes.Square", root_shape)] is None
    assert ("gomods.shapes.Circle", root_shape) not in implements
    # Inside the degraded one, and across the two, which no single go/types
    # run sees, names and arities decide.
    assert implements[("gomods.legacy.legacy.Tile", legacy_shape)] == (
        cs.EdgeResolution.HEURISTIC
    )
    assert implements[("gomods.legacy.legacy.Tile", root_shape)] == (
        cs.EdgeResolution.HEURISTIC
    )
    assert implements[("gomods.shapes.Circle", legacy_shape)] == (
        cs.EdgeResolution.HEURISTIC
    )


def test_the_real_go_types_run_proving_no_pair_adds_none(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same through the bundled tool, which reports the module analysed
    # with no pair. Skipped where it cannot build.
    from codebase_rag.parsers.go_frontend import go_frontend_available, run_go_frontend

    if shutil.which("go") is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    root = temp_repo / "gosink"
    root.mkdir()
    (root / "go.mod").write_text("module gosink\n\ngo 1.22\n", encoding="utf-8")
    (root / "sink.go").write_text(SINK, encoding="utf-8")
    if run_go_frontend(root).analysed_modules != (".",):
        pytest.skip("gotypes frontend could not build in this environment")
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    assert _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS) == {}


# --- guesses stay guesses (PR #3360 review) ---------------------------------

GUESSED_GETTER = """package app

type Label string

type Getter interface {
\tGet() int
}

type Name struct{}

func (n Name) Get() Label { return "name" }
"""


def test_a_guessed_override_is_renamed_only_on_opt_in(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Get() Label does not implement Get() int; the fallback cannot tell, so
    # the OVERRIDES edge is a guess and renaming Name.Get must not rewrite
    # Getter.Get without the opt-in, call sites or none.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    _write(temp_repo, "go.mod", "module goguessed\n\ngo 1.22\n")
    _write(temp_repo, "app.go", GUESSED_GETTER)
    graph = _rename_index(temp_repo, mock_ingestor)
    pkg = f"{graph.project}.app"
    overrides = _edges(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert overrides[(f"{pkg}.Name.Get", f"{pkg}.Getter.Get")] == {
        cs.KEY_RESOLUTION: cs.EdgeResolution.HEURISTIC
    }
    query, target = _query(graph), f"{pkg}.Name.Get"
    with pytest.raises(RenameRefused):
        rename(temp_repo, query, graph.project, target, "Fetch")
    assert (temp_repo / "app.go").read_text(encoding="utf-8") == GUESSED_GETTER

    report = rename(
        temp_repo,
        _query(graph),
        graph.project,
        f"{pkg}.Name.Get",
        "Fetch",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    text = (temp_repo / "app.go").read_text(encoding="utf-8")
    assert "\tFetch() int" in text
    assert "func (n Name) Fetch() Label" in text


PROVEN_GETTER = """package app

type Getter interface {
\tGet() int
}

type Count struct{}

func (c Count) Get() int { return 1 }

func Read(g Getter) int { return g.Get() }
"""


def test_a_proven_override_and_sole_target_rename_without_opt_in(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    getter_line, getter_col = _position(PROVEN_GETTER, "Getter interface")
    count_line, count_col = _position(PROVEN_GETTER, "Count struct")
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    "app.go", count_line, count_col, "app.go", getter_line, getter_col
                )
            ],
            analysed_modules=(".",),
            checked_files=frozenset({"app.go"}),
            compared_types=_compared("app.go", PROVEN_GETTER),
        ),
    )
    _write(temp_repo, "go.mod", "module goproven\n\ngo 1.22\n")
    _write(temp_repo, "app.go", PROVEN_GETTER)
    graph = _rename_index(temp_repo, mock_ingestor)
    pkg = f"{graph.project}.app"
    overrides = _edges(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert overrides[(f"{pkg}.Count.Get", f"{pkg}.Getter.Get")] == {}
    calls = {
        callee: props[cs.KEY_RESOLUTION]
        for (caller, callee), props in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        ).items()
        if caller == f"{pkg}.Read"
    }
    # The compiler proved the sole implementer, so its edge is as exact as
    # the call's own.
    assert calls == {
        f"{pkg}.Getter.Get": cs.EdgeResolution.EXACT,
        f"{pkg}.Count.Get": cs.EdgeResolution.EXACT,
    }

    report = rename(
        temp_repo, _query(graph), graph.project, f"{pkg}.Count.Get", "Fetch"
    )

    assert report.applied, report.message
    text = (temp_repo / "app.go").read_text(encoding="utf-8")
    for renamed in ("\tFetch() int", "func (c Count) Fetch() int", "g.Fetch()"):
        assert renamed in text, text


def test_a_sole_guessed_dispatch_target_is_labelled_heuristic(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = GUESSED_GETTER + "\nfunc Read(g Getter) int { return g.Get() }\n"
    project = _index(
        temp_repo / "gosole", {"app.go": source}, mock_ingestor, monkeypatch
    )
    pkg = f"{project}.app"
    calls = {
        callee: props[cs.KEY_RESOLUTION]
        for (caller, callee), props in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        ).items()
        if caller == f"{pkg}.Read"
    }
    assert calls == {
        f"{pkg}.Getter.Get": cs.EdgeResolution.EXACT,
        f"{pkg}.Name.Get": cs.EdgeResolution.HEURISTIC,
    }


# --- a reused updater forgets what no longer holds ---------------------------

RECONCILED_SHAPE = """package shapes

type Shape interface {
\tArea() float64
}

func Total(shapes []Shape) float64 {
\tt := 0.0
\tfor _, s := range shapes {
\t\tt += s.Area()
\t}
\treturn t
}
"""

RECONCILED_IMPL = """package shapes

type Square struct{}

func (s Square) Area() float64 { return 1 }

type Circle struct{}

func (c Circle) Area() float64      { return 2 }
func (c Circle) Perimeter() float64 { return 3 }
"""


def _interface_edges(store: _StatefulIngestor) -> set[tuple[str, ...]]:
    kinds = {
        cs.RelationshipType.IMPLEMENTS.value,
        cs.RelationshipType.OVERRIDES.value,
        cs.RelationshipType.CALLS.value,
    }
    # Every edge, not only those with properties: a proven pair's edges
    # carry none.
    return {
        (
            str(key[1]),
            str(key[2]),
            str(key[4]),
            str(store.edge_props.get(key, {}).get(cs.KEY_RESOLUTION)),
        )
        for key in store.keyed_edges
        if key[2] in kinds
    }


def _reconciled_repo(root: Path) -> Path:
    root.mkdir()
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    (root / "shape.go").write_text(RECONCILED_SHAPE, encoding="utf-8")
    (root / "impl.go").write_text(RECONCILED_IMPL, encoding="utf-8")
    return root


def _require_perimeter(root: Path) -> None:
    # Shape gains a method Square lacks: Square is no longer a Shape.
    path = root / "shape.go"
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "\tArea() float64\n", "\tArea() float64\n\tPerimeter() float64\n"
        ),
        encoding="utf-8",
    )
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


def _go_updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.GO not in parsers:
        pytest.skip("go parser not available")
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


@pytest.mark.parametrize("how", ["incremental", "reingest"])
def test_a_reused_updater_drops_an_implementer_that_no_longer_matches(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _reconciled_repo(temp_repo / "reused")
    store = _StatefulIngestor()
    updater = _go_updater(store, root)
    updater.run(force=True)
    _require_perimeter(root)
    if how == "incremental":
        updater.run(force=False)
    else:
        updater.reingest(["shape.go"])

    clean_root = _reconciled_repo(temp_repo / "clean")
    (clean_root / "shape.go").write_text(
        (root / "shape.go").read_text(encoding="utf-8"), encoding="utf-8"
    )
    clean = _StatefulIngestor()
    _go_updater(clean, clean_root).run(force=True)

    assert _interface_edges(store) == _interface_edges(clean)
    assert not any("Square" in edge[0] for edge in _interface_edges(store)), sorted(
        _interface_edges(store)
    )


# --- a range's container is the declaration in scope -------------------------

SCOPED = """package shapes

type Shape interface {
\tArea() float64
}

type Square struct{}

func (s Square) Area() float64 { return 1 }

type Other struct{}

func (o Other) Area(scale int) float64 { return float64(scale) }

func Blocked(xs []Shape, ok bool) float64 {
\tif ok {
\t\tvar xs []Other
\t\t_ = xs
\t}
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}

func Nested(xs []Shape) float64 {
\tf := func() {
\t\tvar xs []Other
\t\t_ = xs
\t}
\tf()
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}

func Inner(xs []Shape) float64 {
\tt := 0.0
\tif len(xs) > 0 {
\t\tvar xs []Other
\t\tfor _, o := range xs {
\t\t\tt += o.Area(2)
\t\t}
\t}
\treturn t
}
"""


@pytest.fixture
def scoped(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> str:
    project = _index(
        temp_repo / "goscoped", {"shapes.go": SCOPED}, mock_ingestor, monkeypatch
    )
    return f"{project}.shapes"


def _area_calls(mock_ingestor: MagicMock, caller: str) -> dict[str, str]:
    # `T.Area@3` is a second declaration of `T.Area`, as a build variant.
    return {
        callee: props[cs.KEY_RESOLUTION]
        for (source, callee), props in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        ).items()
        if source == caller and qn_markers.natural_qn(callee).endswith(".Area")
    }


@pytest.mark.parametrize("caller", ["Blocked", "Nested"])
def test_a_shadow_out_of_scope_does_not_retype_the_range(
    scoped: str, mock_ingestor: MagicMock, caller: str
) -> None:
    # The inner `var xs []Other` has ended (its block, or the closure it is
    # declared in) where the loop ranges over the parameter.
    assert _area_calls(mock_ingestor, f"{scoped}.{caller}") == {
        f"{scoped}.Shape.Area": cs.EdgeResolution.EXACT,
        f"{scoped}.Square.Area": cs.EdgeResolution.HEURISTIC,
    }


def test_a_shadow_in_scope_does_retype_the_range(
    scoped: str, mock_ingestor: MagicMock
) -> None:
    assert _area_calls(mock_ingestor, f"{scoped}.Inner") == {
        f"{scoped}.Other.Area": cs.EdgeResolution.EXACT
    }


# --- a pair that changes source on a reused updater (PR #3360 review) -------


@pytest.mark.parametrize("how", ["repeated", "incremental", "reingest"])
@pytest.mark.parametrize("first", ["degraded", "analysed"])
def test_a_pair_that_changes_source_stays_an_implementer(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, first: str, how: str
) -> None:
    # Run 1 matches Count to Getter by name (go/types failed) or proves it;
    # run 2, on the same updater, the other way round. Either way the pair
    # must survive the swap: the guessed and proven sets share one map.
    from codebase_rag.parsers.frontends import go as go_fe

    getter_line, getter_col = _position(PROVEN_GETTER, "Getter interface")
    count_line, count_col = _position(PROVEN_GETTER, "Count struct")
    facts = {
        "degraded": GoSemanticFacts(
            call_sites={}, external_sites=set(), implements=[], degraded_modules=(".",)
        ),
        "analysed": GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    "app.go", count_line, count_col, "app.go", getter_line, getter_col
                )
            ],
            analysed_modules=(".",),
            checked_files=frozenset({"app.go"}),
            compared_types=_compared("app.go", PROVEN_GETTER),
        ),
    }
    second = "analysed" if first == "degraded" else "degraded"
    current = {"facts": facts[first]}
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    monkeypatch.setattr(go_fe, "go_frontend_available", lambda: True)
    monkeypatch.setattr(
        go_fe, "run_go_frontend", lambda repo_path, ignored_dirs: current["facts"]
    )

    def repo(path: Path) -> Path:
        path.mkdir()
        (path / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
        (path / "app.go").write_text(PROVEN_GETTER, encoding="utf-8")
        return path

    root = repo(temp_repo / "reused")
    store = _StatefulIngestor()
    updater = _go_updater(store, root)
    updater.run(force=True)
    current["facts"] = facts[second]
    path = root / "app.go"
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(PROVEN_GETTER + "// touched\n", encoding="utf-8")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    if how == "reingest":
        updater.reingest(["app.go"])
    else:
        updater.run(force=how == "repeated")

    clean_root = repo(temp_repo / "clean")
    (clean_root / "app.go").write_text(PROVEN_GETTER + "// touched\n")
    clean = _StatefulIngestor()
    _go_updater(clean, clean_root).run(force=True)

    edges = _interface_edges(store)
    assert edges == _interface_edges(clean)
    assert ("proj.app.Count.Get", "OVERRIDES", "proj.app.Getter.Get") in {
        edge[:3] for edge in edges
    }
    assert ("proj.app.Read", "CALLS", "proj.app.Count.Get") in {
        edge[:3] for edge in edges
    }


# --- a range variable shadows only inside its loop ---------------------------

LOOP_SCOPED = """package shapes

type Shape interface {
\tArea() float64
}

type Square struct{}

func (s Square) Area() float64 { return 1 }

type Other struct{}

func (o Other) Area(scale int) float64 { return float64(scale) }

func Shadowed(x Other, xs []Shape) float64 {
\tt := 0.0
\tfor _, x := range xs {
\t\tt += x.Area()
\t}
\treturn t + x.Area(2)
}

func Assigned(squares []Square) float64 {
\tvar x Shape
\tt := 0.0
\tfor _, x = range squares {
\t\tt += x.Area()
\t}
\treturn t
}
"""


def _calls_by_line(mock_ingestor: MagicMock, caller: str) -> dict[int, dict[str, str]]:
    by_line: dict[int, dict[str, str]] = {}
    for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value):
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        if c.args[0][2] == caller and str(c.args[2][2]).endswith(".Area"):
            by_line.setdefault(props[cs.KEY_LINE], {})[c.args[2][2]] = props[
                cs.KEY_RESOLUTION
            ]
    return by_line


def test_a_range_variable_shadows_a_parameter_inside_its_loop_only(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _index(
        temp_repo / "goloop", {"shapes.go": LOOP_SCOPED}, mock_ingestor, monkeypatch
    )
    pkg = f"{project}.shapes"
    inside, _ = _position(LOOP_SCOPED, "t += x.Area()")
    after, _ = _position(LOOP_SCOPED, "return t + x.Area(2)")
    calls = _calls_by_line(mock_ingestor, f"{pkg}.Shadowed")
    # `for _, x := range xs` declares a new `x`, a Shape, for the loop.
    assert calls[inside] == {
        f"{pkg}.Shape.Area": cs.EdgeResolution.EXACT,
        f"{pkg}.Square.Area": cs.EdgeResolution.HEURISTIC,
    }
    # Past the loop `x` is the parameter again.
    assert calls[after] == {f"{pkg}.Other.Area": cs.EdgeResolution.EXACT}


def test_a_range_assignment_keeps_the_variables_declared_type(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `for _, x = range squares` assigns each Square to the Shape `x`: no new
    # variable, so `x.Area()` still goes through Shape.
    project = _index(
        temp_repo / "goassign", {"shapes.go": LOOP_SCOPED}, mock_ingestor, monkeypatch
    )
    pkg = f"{project}.shapes"
    line, _ = _position(LOOP_SCOPED, "for _, x = range squares")
    assert _calls_by_line(mock_ingestor, f"{pkg}.Assigned")[line + 1] == {
        f"{pkg}.Shape.Area": cs.EdgeResolution.EXACT,
        f"{pkg}.Square.Area": cs.EdgeResolution.HEURISTIC,
    }


# --- a local that shadows the range variable owns its calls ------------------

LOOP_SHADOWS = """package shapes

type Shape interface {
\tArea() float64
}

type Square struct{}

func (s Square) Area() float64 { return 1 }

type Other struct{}

func (o Other) Area(scale int) float64 { return float64(scale) }

func InLoop(shapes []Shape) float64 {
\tt := 0.0
\tfor _, s := range shapes {
\t\ts := Other{}
\t\tt += s.Area(2)
\t}
\treturn t
}

func InBlock(shapes []Shape, ok bool) float64 {
\tt := 0.0
\tfor _, s := range shapes {
\t\tif ok {
\t\t\ts := Other{}
\t\t\tt += s.Area(3)
\t\t}
\t}
\treturn t
}

func makeOther() Other { return Other{} }

func FromCall(shapes []Shape) float64 {
\tt := 0.0
\tfor _, s := range shapes {
\t\ts := makeOther()
\t\tt += s.Area(6)
\t}
\treturn t
}

func After(shapes []Shape) float64 {
\tt := 0.0
\tfor _, s := range shapes {
\t\tt += s.Area()
\t\ts := Other{}
\t\t_ = s
\t}
\treturn t
}

func Closure(shapes []Shape) float64 {
\tt := 0.0
\tfor _, s := range shapes {
\t\tf := func(s Other) float64 { return s.Area(4) }
\t\tt += f(Other{}) + s.Area()
\t}
\treturn t
}

func Outside(shapes []Shape) float64 {
\ts := Other{}
\tt := 0.0
\tfor _, s := range shapes {
\t\tt += 2 * s.Area()
\t}
\treturn t + s.Area(5)
}
"""

_SHAPE_DISPATCH = {"Shape.Area": "exact", "Square.Area": "heuristic"}
_OTHER = {"Other.Area": "exact"}


@pytest.fixture
def loop_shadows(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, MagicMock]:
    project = _index(
        temp_repo / "goshadow", {"shapes.go": LOOP_SHADOWS}, mock_ingestor, monkeypatch
    )
    return f"{project}.shapes", mock_ingestor


def _calls_at(
    loop_shadows: tuple[str, MagicMock], caller: str, needle: str
) -> dict[str, str]:
    pkg, mock_ingestor = loop_shadows
    line, _ = _position(LOOP_SHADOWS, needle)
    calls = _calls_by_line(mock_ingestor, f"{pkg}.{caller}").get(line, {})
    return {
        callee.removeprefix(f"{pkg}."): str(resolution)
        for callee, resolution in calls.items()
    }


@pytest.mark.parametrize(
    ("caller", "needle", "expected"),
    [
        ("InLoop", "t += s.Area(2)", _OTHER),
        ("InBlock", "t += s.Area(3)", _OTHER),
        ("Closure", "return s.Area(4)", _OTHER),
        ("FromCall", "t += s.Area(6)", _OTHER),
    ],
    ids=["in-loop", "in-block", "closure-parameter", "from-a-call"],
)
def test_a_local_shadowing_the_range_variable_owns_its_calls(
    loop_shadows: tuple[str, MagicMock], caller: str, needle: str, expected: dict
) -> None:
    # The inner `s` is an Other from its declaration on; the loop's Shape
    # `s` must not be laid over it.
    assert _calls_at(loop_shadows, caller, needle) == expected


@pytest.mark.parametrize(
    ("caller", "needle", "expected"),
    [
        ("After", "t += s.Area()", _SHAPE_DISPATCH),
        ("Closure", "t += f(Other{}) + s.Area()", _SHAPE_DISPATCH),
        ("Outside", "t += 2 * s.Area()", _SHAPE_DISPATCH),
        ("Outside", "return t + s.Area(5)", _OTHER),
    ],
    ids=["before-a-later-shadow", "beside-a-closure", "in-loop", "after-loop"],
)
def test_the_range_variable_keeps_the_calls_it_owns(
    loop_shadows: tuple[str, MagicMock], caller: str, needle: str, expected: dict
) -> None:
    assert _calls_at(loop_shadows, caller, needle) == expected


# --- a range element of another package is that package's type ---------------

QUALIFIED_GEO = """package geo

type Shape interface {
\tArea() float64
}

type Square struct{}

func (s Square) Area() float64 { return 1 }
"""

# The local Shape also asks for Name, so only Local implements it, while
# Local, geo's Square and the local Square all implement geo's Shape.
QUALIFIED_APP = """package app

import (
\t"example.com/qual/geo"
\t"github.com/elsewhere/ext"
)

type Shape interface {
\tArea() float64
\tName() string
}

type Local struct{}

func (l Local) Area() float64 { return 2 }
func (l Local) Name() string  { return "local" }

type Square struct{}

func (s Square) Area() float64 { return 3 }

func Slice(xs []geo.Shape) float64 {
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}

func Map(xs map[string]geo.Shape) float64 {
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}

func Pointers(xs []*geo.Square) float64 {
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}

func Own(xs []Shape) float64 {
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}

func External(xs []ext.Shape) float64 {
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}

func Param(s geo.Shape) float64 {
\treturn s.Area()
}

func Var() float64 {
\tvar s geo.Shape
\treturn s.Area()
}
"""

QUALIFIED_OTHER = """package other

import "example.com/qual/geo"

func Slice(xs []geo.Shape) float64 {
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}
"""

_GEO_DISPATCH = {
    "geo.geo.Shape.Area": "exact",
    "geo.geo.Square.Area": "heuristic",
    "app.app.Local.Area": "heuristic",
    "app.app.Square.Area": "heuristic",
}


@pytest.fixture
def qualified(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, MagicMock]:
    files = {
        "geo/geo.go": QUALIFIED_GEO,
        "app/app.go": QUALIFIED_APP,
        "other/other.go": QUALIFIED_OTHER,
    }
    root = temp_repo / "qual"
    return _index(root, files, mock_ingestor, monkeypatch, "example.com/qual"), (
        mock_ingestor
    )


def _area_calls_of(qualified: tuple[str, MagicMock], caller: str) -> dict[str, str]:
    project, mock_ingestor = qualified
    return {
        callee.removeprefix(f"{project}."): str(resolution)
        for callee, resolution in _area_calls(
            mock_ingestor, f"{project}.{caller}"
        ).items()
    }


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("app.app.Slice", _GEO_DISPATCH),
        ("app.app.Map", _GEO_DISPATCH),
        ("app.app.Pointers", {"geo.geo.Square.Area": "exact"}),
        ("other.other.Slice", _GEO_DISPATCH),
    ],
    ids=["slice", "map-value", "pointer-element", "no-local-collision"],
)
def test_a_qualified_range_element_is_the_imported_packages_type(
    qualified: tuple[str, MagicMock], caller: str, expected: dict
) -> None:
    # `geo.Shape` is geo's, whatever the ranging file calls its own Shape.
    assert _area_calls_of(qualified, caller) == expected


@pytest.mark.parametrize("caller", ["app.app.Param", "app.app.Var"])
def test_a_qualified_parameter_or_var_is_the_imported_packages_type(
    qualified: tuple[str, MagicMock], caller: str
) -> None:
    # The flat type map read `geo.Shape` as the bare `Shape` too, which the
    # interface's own method node now turns into an exact edge and a fan-out.
    assert _area_calls_of(qualified, caller) == _GEO_DISPATCH


def test_an_unqualified_range_element_is_the_own_packages_type(
    qualified: tuple[str, MagicMock],
) -> None:
    assert _area_calls_of(qualified, "app.app.Own") == {
        "app.app.Shape.Area": "exact",
        "app.app.Local.Area": "heuristic",
    }


def test_an_unresolved_qualified_range_element_binds_nothing(
    qualified: tuple[str, MagicMock],
) -> None:
    # `ext` is not indexed: its Shape is no type here, and the bare name
    # must not stand in for it.
    calls = _area_calls_of(qualified, "app.app.External")
    assert "app.app.Shape.Area" not in calls
    assert cs.EdgeResolution.EXACT not in calls.values()


# --- a file go/types skipped keeps the fallback (PR #3360 review) -------------

# Built for another OS than the one indexing, so go/packages leaves these
# files out of the build it type-checks while tree-sitter indexes them all.
_OTHER_OS = "plan9" if sys.platform == "win32" else "windows"

BUILT = """package shapes

type Shape interface {
\tArea() float64
}

type Square struct{}

func (s Square) Area() float64 { return 1 }

type Celsius float64

type Wrong struct{}

func (w Wrong) Area() Celsius { return 0 }

type Mixed struct{}

func Total(xs []Shape) float64 {
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}
"""

SKIPPED = {
    # Constrained by its file name alone.
    f"tile_{_OTHER_OS}.go": """package shapes

type Tile struct{}

func (t Tile) Area() float64 { return 2 }
""",
    # Constrained by its build tag alone.
    "tagged.go": f"""//go:build {_OTHER_OS}

package shapes

type Tagged struct{{}}

func (t Tagged) Area() float64 {{ return 3 }}
""",
    # A type the build sees whose method only the other OS's build declares.
    f"mixed_{_OTHER_OS}.go": """package shapes

func (m Mixed) Area() float64 { return 4 }
""",
}


def _skipped_files_repo(root: Path) -> None:
    root.mkdir()
    (root / "go.mod").write_text("module goskip\n\ngo 1.22\n", encoding="utf-8")
    (root / "shapes.go").write_text(BUILT, encoding="utf-8")
    for name, text in SKIPPED.items():
        (root / name).write_text(text, encoding="utf-8")


def _below_file(qn: str) -> str:
    # `goskip.tile_windows.Tile.Area` -> `Tile.Area`: past project and file.
    return qn.split(".", 2)[-1]


def _resolutions(
    mock_ingestor: MagicMock, rel: cs.RelationshipType
) -> dict[tuple[str, str], str | None]:
    return {
        (_below_file(source), _below_file(target)): props.get(cs.KEY_RESOLUTION)
        for (source, target), props in _edges(mock_ingestor, rel).items()
    }


def _real_gotypes_or_skip(root: Path) -> None:
    # The build for this OS reads shapes.go alone.
    _real_gotypes_files_or_skip(root, {"shapes.go"})


# Where the real helper's output is kept, by the Go sources it read (paths
# relative to the module), the GOOS it read them for and the directories it
# left out, for every xdist worker of the session: one run per tree, where
# a fixture shared across parametrizations, a precondition check and the
# index after it, a sync and the clean index it is compared with, and the
# host's build named twice (`real` and `real-linux` on Linux) each ran the
# helper again. Its output holds module-relative paths only.
_REAL_RUNS: list[Path] = []


def _real_go_frontend(
    root: Path, ignored_dirs: frozenset[str] = cs.IGNORE_PATTERNS
) -> GoSemanticFacts:
    from codebase_rag.parsers.go_frontend import frontend

    return frontend.run_go_frontend(root, ignored_dirs=ignored_dirs)


def _real_gotypes_files_or_skip(root: Path, checked: set[str]) -> None:
    from codebase_rag.parsers.go_frontend import go_frontend_available

    if shutil.which("go") is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    facts = _real_go_frontend(root)
    if facts.analysed_modules != (".",):
        pytest.skip("gotypes frontend could not build in this environment")
    assert facts.checked_files == checked


@pytest.fixture
def _one_real_go_run_per_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    from codebase_rag.parsers.go_frontend import frontend

    _REAL_RUNS[:] = [shared_session_dir(tmp_path_factory, "go-helper-runs")]
    run = frontend._run_tool_once

    def once(
        binary: Path, module_root: Path, ignored_dirs: frozenset[str]
    ) -> GoSemanticFacts | None:
        key = hashlib.sha256(
            "\0".join(
                (
                    _go_sources_digest(module_root),
                    _analysed_goos(),
                    *sorted(ignored_dirs),
                )
            ).encode()
        ).hexdigest()
        kept = _REAL_RUNS[0] / f"{key}.json"
        # One worker runs the helper for a tree while the others wait for its
        # copy. Unserialised, two workers that both missed both published,
        # and Windows refuses to open a file mid-replace or to replace one
        # held open: whichever worker came second failed with PermissionError.
        with FileLock(kept.with_suffix(".lock")):
            if kept.is_file():
                stdout, stderr = json.loads(kept.read_text(encoding="utf-8"))
                return frontend._parse_payload(stdout, stderr)
            captured: list[tuple[str, str]] = []
            parse = frontend._parse_payload

            def keep(stdout: str, stderr: str) -> GoSemanticFacts:
                captured.append((stdout, stderr))
                return parse(stdout, stderr)

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(frontend, "_parse_payload", keep)
                facts = run(binary, module_root, ignored_dirs)
            if facts is not None and captured:
                partial = kept.with_suffix(f".{os.getpid()}.part")
                partial.write_text(json.dumps(captured[0]), encoding="utf-8")
                os.replace(partial, kept)
            return facts

    monkeypatch.setattr(frontend, "_run_tool_once", once)


# The GOOS go/packages reads for the helper, by $GOOS: `go env GOOS` once.
_ANALYSED_GOOS: dict[str, str] = {}


def _analysed_goos() -> str:
    # The GOOS go/packages reads for the helper: $GOOS, else the host's.
    go = shutil.which("go")
    if go is None:
        pytest.skip("go toolchain not available")
    wanted = os.environ.get("GOOS", "")
    if wanted not in _ANALYSED_GOOS:
        _ANALYSED_GOOS[wanted] = subprocess.run(
            [go, "env", "GOOS"],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
        ).stdout.strip()
    return _ANALYSED_GOOS[wanted]


def _real_build(
    monkeypatch: pytest.MonkeyPatch, root: Path, files: Iterable[str], frontend: str
) -> str:
    """Index with the real helper on the build `frontend` names (`real` for
    the host's, `real-windows` for GOOS=windows), after checking it read
    that build's files of `files`: the ones not of the other OS. These
    fixtures declare their packages for Linux and Windows alone, so for
    Darwin no package compiles: go/types checks no file and proves no pair,
    and every pair is matched by name. Returns the GOOS it read."""
    if shutil.which("go") is None:
        pytest.skip("go toolchain not available")
    if forced := frontend.removeprefix("real").removeprefix("-"):
        monkeypatch.setenv("GOOS", forced)
    goos = _analysed_goos()
    if goos == "darwin":
        _real_gotypes_files_or_skip(root, set())
    elif goos in ("linux", "windows"):
        other = "windows" if goos == "linux" else "linux"
        _real_gotypes_files_or_skip(root, {rel for rel in files if other not in rel})
    else:
        pytest.skip(f"these builds declare nothing for GOOS={goos}")
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    return goos


def _recorded_gotypes(monkeypatch: pytest.MonkeyPatch) -> None:
    # What the bundled tool reports for the repository on a build that leaves
    # the other OS's files out: it type-checked shapes.go alone and proved
    # Square, while Wrong's Area() Celsius is no Area() float64.
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    "shapes.go",
                    *_position(BUILT, "Square struct"),
                    "shapes.go",
                    *_position(BUILT, "Shape interface"),
                )
            ],
            analysed_modules=(".",),
            checked_files=frozenset({"shapes.go"}),
            compared_types=_compared("shapes.go", BUILT),
        ),
    )


@pytest.mark.parametrize("facts", ["real", "recorded"])
def test_a_type_in_a_file_go_types_skipped_keeps_the_name_match(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    facts: str,
) -> None:
    root = temp_repo / "goskip"
    _skipped_files_repo(root)
    if facts == "real":
        _real_gotypes_or_skip(root)
        monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    else:
        _recorded_gotypes(monkeypatch)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    implements = _resolutions(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    # The compiler's own answer where it type-checked both sides: Square is
    # proven, and Wrong gets no name match.
    assert implements == {
        ("Square", "Shape"): None,
        ("Tile", "Shape"): cs.EdgeResolution.HEURISTIC,
        ("Tagged", "Shape"): cs.EdgeResolution.HEURISTIC,
        ("Mixed", "Shape"): cs.EdgeResolution.HEURISTIC,
    }
    overrides = _resolutions(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert overrides[("Square.Area", "Shape.Area")] is None
    assert ("Wrong.Area", "Shape.Area") not in overrides
    for name in ("Tile", "Tagged", "Mixed"):
        assert overrides[(f"{name}.Area", "Shape.Area")] == cs.EdgeResolution.HEURISTIC
    calls = {
        target: resolution
        for (source, target), resolution in _resolutions(
            mock_ingestor, cs.RelationshipType.CALLS
        ).items()
        if source == "Total"
    }
    assert calls == {
        "Shape.Area": cs.EdgeResolution.EXACT,
        "Square.Area": cs.EdgeResolution.OVERLOAD,
        "Tile.Area": cs.EdgeResolution.HEURISTIC,
        "Tagged.Area": cs.EdgeResolution.HEURISTIC,
        "Mixed.Area": cs.EdgeResolution.HEURISTIC,
    }


def test_without_go_types_every_file_is_matched_by_name(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tree-sitter path reads no build constraint: every method that may
    # be Area() float64 is a match, Wrong's Area() Celsius included.
    _index(
        temp_repo / "goskip",
        {"shapes.go": BUILT, **SKIPPED},
        mock_ingestor,
        monkeypatch,
    )
    implements = _resolutions(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements == {
        (name, "Shape"): cs.EdgeResolution.HEURISTIC
        for name in ("Square", "Wrong", "Tile", "Tagged", "Mixed")
    }


# --- the pairs an incremental sync reads back (PR #3360, after #2503) --------

SYNCED = {
    "shape.go": """package shapes

type Shape interface {
\tArea() float64
}
""",
    "square.go": """package shapes

type Square struct{}
""",
    # Square's method lives apart from its type, as Go allows.
    "square_area.go": """package shapes

func (s Square) Area() float64 { return 1 }
""",
    "circle.go": """package shapes

type Circle struct{}

func (c Circle) Area() float64      { return 2 }
func (c Circle) Perimeter() float64 { return 3 }
""",
    "caller.go": """package shapes

func Total(xs []Shape) float64 {
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}
""",
}

# Each edit, as (file, old, new): a caller-only sync, and an interface that
# Square stops satisfying while the file holding Square.Area is not re-parsed.
SYNC_EDITS = {
    "caller": ("caller.go", "return t\n", "return t // synced\n"),
    "interface": (
        "shape.go",
        "\tArea() float64\n",
        "\tArea() float64\n\tPerimeter() float64\n",
    ),
}


def _synced_repo(root: Path, edit: str | None = None) -> Path:
    root.mkdir()
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in SYNCED.items():
        (root / rel).write_text(text, encoding="utf-8")
    if edit is not None:
        _apply_sync_edit(root, edit)
    return root


def _apply_sync_edit(root: Path, edit: str) -> None:
    rel, old, new = SYNC_EDITS[edit]
    path = root / rel
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.parametrize(
    "sync", ["fresh", "reused", "reingest-fresh", "reingest-reused"]
)
@pytest.mark.parametrize("edit", list(SYNC_EDITS))
def test_a_sync_reading_go_pairs_back_matches_a_clean_index(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, edit: str, sync: str
) -> None:
    # A sync reads every IMPLEMENTS edge back from the graph (#2503), the Go
    # ones too, and restores the edges unchanged files held into re-parsed
    # ones, while link_go_interfaces recomputes the Go pairs from every
    # method set. Neither may add an edge a clean index lacks: no OVERRIDES
    # or IMPLEMENTS the edit took away, and no unlabelled copy.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _synced_repo(temp_repo / "synced")
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    _apply_sync_edit(root, edit)
    rel = SYNC_EDITS[edit][0]
    os.utime(root / rel, (cache_mtime + 1, cache_mtime + 1))
    updater = first if sync.endswith("reused") else _go_updater(store, root)
    if sync.startswith("reingest"):
        updater.reingest([rel])
    else:
        updater.run(force=False)

    clean = _StatefulIngestor()
    _go_updater(clean, _synced_repo(temp_repo / "clean", edit)).run(force=True)
    assert _interface_edges(store) == _interface_edges(clean)


def test_go_pairs_read_back_stay_out_of_the_generic_override_walk(
    temp_repo: Path,
) -> None:
    # link_go_interfaces writes a Go pair's OVERRIDES, labelled with its
    # confidence, from the pairs it recomputes; the read-back copy of the
    # graph's edge may be one the edit took away. A Java pair read back
    # still joins the walk (#2503).
    updater = _go_updater(_StatefulIngestor(), temp_repo)
    dp = updater.factory.definition_processor
    dp.rehydrated_definition_paths.update(
        {"proj.square.Square": "square.go", "proj.Upper.Upper": "Upper.java"}
    )
    dp.rehydrated_interface_implementers.update(
        {
            "proj.shape.Shape": {"proj.square.Square"},
            "proj.Strategy.Strategy": {"proj.Upper.Upper"},
        }
    )
    assert dp._override_walk_implementers() == {
        "proj.Strategy.Strategy": {"proj.Upper.Upper"}
    }


# --- a method each build declares again (PR #3360 review) ---------------------

VARIANT_SHAPE = """package shapes

type Shape interface {
\tArea() float64
}

type T struct{}

type U struct{}

type W struct{}

type Celsius float64

type Square struct{}

func (s Square) Area() float64 { return 3 }

func Total(xs []Shape) float64 {
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}

var _ Shape = T{}
var _ Shape = Square{}
"""

# Two mutually exclusive builds, each declaring T.Area and T.Size again; U's
# Area is a Shape's only on Windows, where it returns a float64. W's is too,
# but only go/types can tell: Celsius may be any type as far as names go.
VARIANT_FILES = {
    "shape.go": VARIANT_SHAPE,
    "t_windows.go": """package shapes

func (t T) Area() float64 { return 1 }

func (t T) Size() float64 { return 1 }

func (u U) Area() float64 { return 1 }

func (w W) Area() float64 { return 1 }
""",
    "t_other.go": """//go:build !windows

package shapes

func (t T) Area() float64 { return 2 }

func (t T) Size() float64 { return 2 }

func (u U) Area() int { return 2 }

func (w W) Area() Celsius { return 2 }
""",
}


def _method_files(mock_ingestor: MagicMock) -> dict[str, str]:
    # Method qn -> the file declaring it, which names a variant (`T.Area` or
    # `T.Area@3`) whichever file the index happened to read first.
    return {
        str(c.args[1][cs.KEY_QUALIFIED_NAME]): str(c.args[1].get(cs.KEY_PATH))
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.METHOD and cs.KEY_QUALIFIED_NAME in c.args[1]
    }


def _declared_in(files: dict[str, str], prefix: str, path: str) -> str:
    (qn,) = [
        qn
        for qn, declared in files.items()
        if declared == path and qn.split("@")[0] == prefix
    ]
    return qn


@pytest.fixture
def variants(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, dict[str, str]]:
    project = _index(temp_repo / "govar", VARIANT_FILES, mock_ingestor, monkeypatch)
    pkg = f"{project}.shape"
    files = _method_files(mock_ingestor)
    return pkg, {
        "T.Area/windows": _declared_in(files, f"{pkg}.T.Area", "t_windows.go"),
        "T.Area/other": _declared_in(files, f"{pkg}.T.Area", "t_other.go"),
        "U.Area/windows": _declared_in(files, f"{pkg}.U.Area", "t_windows.go"),
        "U.Area/other": _declared_in(files, f"{pkg}.U.Area", "t_other.go"),
        "W.Area/windows": _declared_in(files, f"{pkg}.W.Area", "t_windows.go"),
        "W.Area/other": _declared_in(files, f"{pkg}.W.Area", "t_other.go"),
        "Square.Area": f"{pkg}.Square.Area",
    }


def test_every_build_variant_of_a_method_overrides_the_interface_method(
    variants: tuple[str, dict[str, str]], mock_ingestor: MagicMock
) -> None:
    pkg, qns = variants
    overriding = {
        method
        for method, base in _edges(mock_ingestor, cs.RelationshipType.OVERRIDES)
        if base == f"{pkg}.Shape.Area"
    }
    # U's Area() int is no Area() float64, and Size is another method.
    assert overriding == {
        qns["T.Area/windows"],
        qns["T.Area/other"],
        qns["U.Area/windows"],
        qns["W.Area/windows"],
        qns["W.Area/other"],
        qns["Square.Area"],
    }
    implements = _edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert {impl for impl, iface in implements if iface == f"{pkg}.Shape"} == {
        f"{pkg}.T",
        f"{pkg}.U",
        f"{pkg}.W",
        f"{pkg}.Square",
    }


def test_a_call_through_the_interface_reaches_every_build_variant(
    variants: tuple[str, dict[str, str]], mock_ingestor: MagicMock
) -> None:
    pkg, qns = variants
    calls = _area_calls(mock_ingestor, f"{pkg}.Total")
    assert calls == {
        f"{pkg}.Shape.Area": cs.EdgeResolution.EXACT,
        qns["T.Area/windows"]: cs.EdgeResolution.HEURISTIC,
        qns["T.Area/other"]: cs.EdgeResolution.HEURISTIC,
        qns["U.Area/windows"]: cs.EdgeResolution.HEURISTIC,
        qns["W.Area/windows"]: cs.EdgeResolution.HEURISTIC,
        qns["W.Area/other"]: cs.EdgeResolution.HEURISTIC,
        qns["Square.Area"]: cs.EdgeResolution.HEURISTIC,
    }


def _go_build(root: Path, goos: str) -> subprocess.CompletedProcess[str]:
    go = shutil.which("go")
    assert go is not None
    return subprocess.run(
        [go, "build", "./..."],
        cwd=root,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        env={**os.environ, "GOOS": goos, "GOARCH": "amd64", "CGO_ENABLED": "0"},
    )


def test_renaming_the_interface_method_renames_every_build_variant(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Renaming Shape.Area rewrote one of T's two Area declarations: the build
    # holding the other no longer compiled (`T does not implement Shape`)
    # while the rename reported success.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    _write(temp_repo, "go.mod", "module govar\n\ngo 1.22\n")
    for rel, text in VARIANT_FILES.items():
        _write(temp_repo, rel, text)
    graph = _rename_index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo,
        _query(graph),
        graph.project,
        f"{graph.project}.shape.Shape.Area",
        "Measure",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    windows = (temp_repo / "t_windows.go").read_text(encoding="utf-8")
    other = (temp_repo / "t_other.go").read_text(encoding="utf-8")
    assert "func (t T) Measure() float64" in windows
    assert "func (t T) Measure() float64" in other
    assert "func (u U) Measure() float64" in windows
    assert "func (w W) Measure() float64" in windows
    assert "func (w W) Measure() Celsius" in other
    # U's Area() int is no Shape's, but it is the other build's U.Area: a
    # caller both builds compile calls each, so it is renamed with it.
    assert "func (u U) Measure() int" in other
    # Neither Size.
    assert "func (t T) Size() float64" in windows
    assert "func (t T) Size() float64" in other
    if shutil.which("go") is not None:
        for goos in ("linux", "windows"):
            built = _go_build(temp_repo, goos)
            assert built.returncode == 0, (goos, built.stderr)


def test_a_single_declaration_overrides_once(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _index(
        temp_repo / "gosingle", {"shapes.go": SHAPES}, mock_ingestor, monkeypatch
    )
    pkg = f"{project}.shapes"
    overriding = sorted(
        method
        for method, base in _edges(mock_ingestor, cs.RelationshipType.OVERRIDES)
        if base == f"{pkg}.Shape.Area"
    )
    assert overriding == [f"{pkg}.Circle.Area", f"{pkg}.Square.Area"]


@pytest.mark.parametrize(
    "sync", ["fresh", "reused", "reingest-fresh", "reingest-reused"]
)
@pytest.mark.parametrize("touched", ["t_windows.go", "t_other.go", "shape.go"])
def test_a_sync_reparsing_one_build_file_matches_a_clean_index(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, touched: str, sync: str
) -> None:
    # T's methods hang off T in shape.go while two other files declare them.
    # A sync that re-parsed shape.go, the dependent of either, deleted the
    # methods of the file it did not re-parse; one that re-parsed a method's
    # file alone filed its methods under a second name.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)

    def repo(root: Path) -> Path:
        root.mkdir()
        (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
        for rel, text in VARIANT_FILES.items():
            (root / rel).write_text(text, encoding="utf-8")
        return root

    root = repo(temp_repo / "synced")
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / touched
    path.write_text(path.read_text(encoding="utf-8") + "// synced\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    updater = first if sync.endswith("reused") else _go_updater(store, root)
    if sync.startswith("reingest"):
        updater.reingest([touched])
    else:
        updater.run(force=False)

    clean_root = repo(temp_repo / "clean")
    (clean_root / touched).write_text(
        path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    clean = _StatefulIngestor()
    _go_updater(clean, clean_root).run(force=True)
    assert _interface_edges(store) == _interface_edges(clean)


def _recorded_variant_gotypes(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bundled tool's answer on Linux: it read shape.go and t_other.go,
    # and proved Square and T (Area() float64 there), never U (Area() int).
    shape_at = _position(VARIANT_SHAPE, "Shape interface")
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    "shape.go",
                    *_position(VARIANT_SHAPE, f"{name} struct"),
                    "shape.go",
                    *shape_at,
                )
                for name in ("T", "Square")
            ],
            analysed_modules=(".",),
            checked_files=frozenset({"shape.go", "t_other.go"}),
            compared_types=_compared("shape.go", VARIANT_SHAPE),
        ),
    )


@pytest.mark.parametrize("facts", ["real", "recorded"])
def test_go_types_leaves_a_build_it_did_not_read_to_the_name_match(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    facts: str,
) -> None:
    # go/types read the Linux build only and proved T there: T implements
    # Shape for certain, and its Linux Area overrides Shape.Area, while its
    # Windows Area is matched by name. U's Windows Area, the only Area U has
    # that is a Shape's, keeps U matched by name, and so does W's: go/types
    # read W's Linux Area() Celsius and proved nothing, which says nothing
    # of the Windows build.
    root = temp_repo / "govar"
    root.mkdir()
    (root / "go.mod").write_text("module govar\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in VARIANT_FILES.items():
        (root / rel).write_text(text, encoding="utf-8")
    if facts == "real":
        if _analysed_goos() == "windows":
            pytest.skip("the Windows build reads the other files")
        _real_gotypes_files_or_skip(root, {"shape.go", "t_other.go"})
        monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    else:
        _recorded_variant_gotypes(monkeypatch)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    pkg = "govar.shape"
    implements = {
        impl: props.get(cs.KEY_RESOLUTION)
        for (impl, iface), props in _edges(
            mock_ingestor, cs.RelationshipType.IMPLEMENTS
        ).items()
        if iface == f"{pkg}.Shape"
    }
    assert implements == {
        f"{pkg}.Square": None,
        f"{pkg}.T": None,
        f"{pkg}.U": cs.EdgeResolution.HEURISTIC,
        f"{pkg}.W": cs.EdgeResolution.HEURISTIC,
    }
    files = _method_files(mock_ingestor)
    t_windows = _declared_in(files, f"{pkg}.T.Area", "t_windows.go")
    t_other = _declared_in(files, f"{pkg}.T.Area", "t_other.go")
    u_windows = _declared_in(files, f"{pkg}.U.Area", "t_windows.go")
    w_both = {
        _declared_in(files, f"{pkg}.W.Area", path): "heuristic"
        for path in ("t_windows.go", "t_other.go")
    }
    overrides = {
        method: props.get(cs.KEY_RESOLUTION)
        for (method, base), props in _edges(
            mock_ingestor, cs.RelationshipType.OVERRIDES
        ).items()
        if base == f"{pkg}.Shape.Area"
    }
    assert overrides == {
        f"{pkg}.Square.Area": None,
        t_other: None,
        t_windows: "heuristic",
        u_windows: "heuristic",
        **w_both,
    }
    calls = {
        callee: str(resolution)
        for callee, resolution in _area_calls(mock_ingestor, f"{pkg}.Total").items()
    }
    # Total compiles in every build, and t_other.go's Area runs in none of
    # Windows': proven, but a candidate of some of Total's builds only.
    assert calls == {
        f"{pkg}.Shape.Area": "exact",
        f"{pkg}.Square.Area": "overload",
        t_other: "heuristic",
        t_windows: "heuristic",
        u_windows: "heuristic",
        **w_both,
    }


# --- the signature check never rejects what may be the same (PR #3360) --------

SIG_RESULTS = """package results

type Pair interface {
\tBoth() (int, error)
\tNothing()
\tTwo() (int, int)
\tArgs(a int, b string) error
\tGroup(a int, b int)
\tMany(prefix string, xs ...int)
}

type Boths interface {
\tBoth() (
\t\tint,
\t\terror,
\t)
}

type Impl struct{}

func (Impl) Both() (
\tint,
\terror,
) {
\treturn 0, nil
}

func (Impl) Nothing() () {}

func (Impl) Two() (x, y int) { return 0, 0 }

func (Impl) Args(
\ta int,
\tb string,
) error {
\treturn nil
}

func (Impl) Group(a, b int) {}

func (Impl) Many(
\tprefix string,
\txs ...int,
) {
}

type Wrong struct{}

func (Wrong) Both() (float64, error) { return 0, nil }

func Use(p Pair) error {
\tp.Nothing()
\t_, err := p.Both()
\treturn err
}

var _ Pair = Impl{}
"""

# `int` is string in this package: Name(int) int is Name(string) string.
SIG_ALIAS = """package alias

type int = string

type Namer interface {
\tName(s string) string
}

type Impl struct{}

func (Impl) Name(s int) int { return s }

func Call(n Namer) string { return n.Name("x") }

var _ Namer = Impl{}
"""

# `uint8` is a defined type here, so only go/types can tell Size() uint8 from
# Size() int; the name match may not rule it out.
SIG_DEFINED = """package defined

type uint8 int

type Sizer interface {
\tSize() int
}

type T struct{}

func (T) Size() uint8 { return 0 }
"""

# Implements results.Boths from another package, so a sync re-parsing this
# file alone matches it against Boths read back from the graph.
SIG_OTHER = """package other

type Both2 struct{}

func (Both2) Both() (int, error) { return 0, nil }
"""

SIG_FILES = {
    "results/results.go": SIG_RESULTS,
    "alias/alias.go": SIG_ALIAS,
    "defined/defined.go": SIG_DEFINED,
    "other/other.go": SIG_OTHER,
}


def _sig_repo(root: Path) -> None:
    (root / "go.mod").write_text("module gosig\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in SIG_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _sig_frontend(monkeypatch: pytest.MonkeyPatch, frontend: str, root: Path) -> None:
    if frontend == "gotypes":
        from codebase_rag.parsers.go_frontend import (
            go_frontend_available,
            run_go_frontend,
        )

        if shutil.which("go") is None or not go_frontend_available():
            pytest.skip("go toolchain not available")
        if run_go_frontend(root).analysed_modules != (".",):
            pytest.skip("gotypes frontend could not build in this environment")
        monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    else:
        monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)


def _sig_index(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    frontend: str,
) -> str:
    root = temp_repo / "gosig"
    root.mkdir()
    _sig_repo(root)
    _sig_frontend(monkeypatch, frontend, root)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    return "gosig"


def _labels(mock_ingestor: MagicMock, rel: cs.RelationshipType) -> dict:
    return {
        pair: props.get(cs.KEY_RESOLUTION)
        for pair, props in _edges(mock_ingestor, rel).items()
    }


@pytest.mark.parametrize("frontend", ["treesitter", "gotypes"])
def test_a_valid_result_or_parameter_list_keeps_its_links(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    frontend: str,
) -> None:
    # `(int, error,)` over two lines, `()`, `(x, y int)`, `(a int, b
    # string,)`, `(a, b int)` and `(prefix string, xs ...int,)` are the
    # interface's signatures written another way.
    project = _sig_index(temp_repo, mock_ingestor, monkeypatch, frontend)
    pkg = f"{project}.results.results"
    label = cs.EdgeResolution.HEURISTIC if frontend == "treesitter" else None
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[(f"{pkg}.Impl", f"{pkg}.Pair")] == label
    assert implements[(f"{pkg}.Impl", f"{pkg}.Boths")] == label
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    for method in ("Both", "Nothing", "Two", "Args", "Group", "Many"):
        assert overrides[(f"{pkg}.Impl.{method}", f"{pkg}.Pair.{method}")] == label
    calls = {
        callee
        for caller, callee in _edges(mock_ingestor, cs.RelationshipType.CALLS)
        if caller == f"{pkg}.Use"
    }
    assert {f"{pkg}.Impl.Nothing", f"{pkg}.Impl.Both"} <= calls
    # Negative: (float64, error) is no (int, error), built-in types both.
    assert (f"{pkg}.Wrong", f"{pkg}.Boths") not in implements
    assert (f"{pkg}.Wrong.Both", f"{pkg}.Boths.Both") not in overrides
    assert f"{pkg}.Wrong.Both" not in calls


@pytest.mark.parametrize("frontend", ["treesitter", "gotypes"])
def test_a_name_its_package_declares_is_no_builtin(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    frontend: str,
) -> None:
    project = _sig_index(temp_repo, mock_ingestor, monkeypatch, frontend)
    alias, defined = f"{project}.alias.alias", f"{project}.defined.defined"
    label = cs.EdgeResolution.HEURISTIC if frontend == "treesitter" else None
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert implements[(f"{alias}.Impl", f"{alias}.Namer")] == label
    assert overrides[(f"{alias}.Impl.Name", f"{alias}.Namer.Name")] == label
    calls = {
        callee
        for caller, callee in _edges(mock_ingestor, cs.RelationshipType.CALLS)
        if caller == f"{alias}.Call"
    }
    assert f"{alias}.Impl.Name" in calls
    # A defined `uint8` may be anything to the name match; go/types knows
    # Size() uint8 is no Size() int.
    pair = (f"{defined}.T", f"{defined}.Sizer")
    if frontend == "treesitter":
        assert implements[pair] == cs.EdgeResolution.HEURISTIC
    else:
        assert pair not in implements


@pytest.mark.parametrize("frontend", ["treesitter", "gotypes"])
@pytest.mark.parametrize(
    ("target", "renamed", "expected"),
    [
        (
            "results.results.Pair.Both",
            "BothX",
            ("results/results.go", "func (Impl) BothX() ("),
        ),
        (
            "alias.alias.Namer.Name",
            "Title",
            ("alias/alias.go", "func (Impl) Title(s int) int"),
        ),
    ],
    ids=["trailing-comma", "alias"],
)
def test_renaming_through_another_spelling_keeps_the_build(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    frontend: str,
    target: str,
    renamed: str,
    expected: tuple[str, str],
) -> None:
    # Without the implementation in the hierarchy the rename rewrote the
    # interface alone and `var _ Pair = Impl{}` stopped compiling.
    _sig_repo(temp_repo)
    _sig_frontend(monkeypatch, frontend, temp_repo)
    graph = _rename_index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo,
        _query(graph),
        graph.project,
        f"{graph.project}.{target}",
        renamed,
        allow_heuristic=True,
    )
    assert report.applied, report.message
    rel, needle = expected
    assert needle in (temp_repo / rel).read_text(encoding="utf-8")
    if shutil.which("go") is not None:
        built = _go_build(temp_repo, "linux")
        assert built.returncode == 0, built.stderr


def test_a_go_types_proof_is_never_vetoed_by_the_signature_check(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Recorded facts proving U on the build that reads U's Area() int: the
    # compiler's word stands over the name match's, so U's Linux Area keeps
    # its OVERRIDES and dispatch edges, unlabelled.
    shape_at = _position(VARIANT_SHAPE, "Shape interface")
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    "shape.go",
                    *_position(VARIANT_SHAPE, "U struct"),
                    "shape.go",
                    *shape_at,
                )
            ],
            analysed_modules=(".",),
            checked_files=frozenset({"shape.go", "t_other.go"}),
            compared_types=_compared("shape.go", VARIANT_SHAPE),
        ),
    )
    root = temp_repo / "govar"
    root.mkdir()
    (root / "go.mod").write_text("module govar\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in VARIANT_FILES.items():
        (root / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    pkg = "govar.shape"
    u_other = _declared_in(_method_files(mock_ingestor), f"{pkg}.U.Area", "t_other.go")
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert overrides[(u_other, f"{pkg}.Shape.Area")] is None
    assert u_other in _area_calls(mock_ingestor, f"{pkg}.Total")


@pytest.mark.parametrize("sync", ["fresh", "reused"])
def test_a_sync_reads_a_stored_result_list_as_its_parse_did(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, sync: str
) -> None:
    # Re-parsing only other/ reads results/ back from the graph, where
    # Boths.Both's `return_type` holds `(\n\t\tint,\n\t\terror,\n\t)` as
    # written: read as two results, Both2 implements it as a clean index says.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = temp_repo / "synced"
    root.mkdir()
    _sig_repo(root)
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / "other" / "other.go"
    path.write_text(path.read_text(encoding="utf-8") + "// synced\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    (first if sync == "reused" else _go_updater(store, root)).run(force=False)

    clean_root = temp_repo / "clean"
    clean_root.mkdir()
    _sig_repo(clean_root)
    (clean_root / "other" / "other.go").write_text(path.read_text(encoding="utf-8"))
    clean = _StatefulIngestor()
    _go_updater(clean, clean_root).run(force=True)
    assert _interface_edges(store) == _interface_edges(clean)
    assert (
        "proj.other.other.Both2",
        "IMPLEMENTS",
        "proj.results.results.Boths",
        "heuristic",
    ) in _interface_edges(store)


# --- a sync re-parsing only the caller (CodeRabbit review 5484914382) ---------

CALLER_ONLY = {
    "shapes/shape.go": """package shapes

type Shape interface {
\tArea() float64
}
""",
    "shapes/square.go": """package shapes

type Square struct{}

func (s Square) Area() float64 { return 1 }
""",
    "shapes/circle.go": """package shapes

type Circle struct{}

func (c *Circle) Area() float64 { return 2 }
""",
    # Its own package, so a sync touching it re-parses this directory alone
    # and reads every implementer back from the graph.
    "app/app.go": """package app

import "proj/shapes"

func Total(xs []shapes.Shape) float64 {
\tt := 0.0
\tfor _, s := range xs {
\t\tt += s.Area()
\t}
\treturn t
}
""",
}


def _caller_only_repo(root: Path) -> Path:
    root.mkdir()
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in CALLER_ONLY.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _total_calls(store: _StatefulIngestor) -> set[tuple[str, ...]]:
    return {
        edge
        for edge in _interface_edges(store)
        if edge[0] == "proj.app.app.Total" and edge[1] == "CALLS"
    }


@pytest.mark.parametrize(
    "sync", ["fresh", "reused", "reingest-fresh", "reingest-reused"]
)
@pytest.mark.parametrize("frontend", ["treesitter", "gotypes"])
def test_a_sync_reparsing_only_the_caller_keeps_every_dispatch_target(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, frontend: str, sync: str
) -> None:
    # Only app/ is re-parsed: Square and Circle, and their pairs with Shape,
    # come from the unchanged shapes/ files, and `s.Area()` still reaches
    # both, labelled as a clean index labels them.
    root = _caller_only_repo(temp_repo / "synced")
    _sig_frontend(monkeypatch, frontend, root)
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / "app" / "app.go"
    path.write_text(path.read_text(encoding="utf-8") + "// synced\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    updater = first if sync.endswith("reused") else _go_updater(store, root)
    if sync.startswith("reingest"):
        updater.reingest(["app/app.go"])
    else:
        updater.run(force=False)

    clean_root = _caller_only_repo(temp_repo / "clean")
    (clean_root / "app" / "app.go").write_text(path.read_text(encoding="utf-8"))
    clean = _StatefulIngestor()
    _go_updater(clean, clean_root).run(force=True)
    assert _interface_edges(store) == _interface_edges(clean)
    label = "heuristic" if frontend == "treesitter" else "overload"
    assert _total_calls(store) == {
        ("proj.app.app.Total", "CALLS", "proj.shapes.shape.Shape.Area", "exact"),
        ("proj.app.app.Total", "CALLS", "proj.shapes.square.Square.Area", label),
        ("proj.app.app.Total", "CALLS", "proj.shapes.circle.Circle.Area", label),
    }


# --- a local nothing can type hides the outer one (CodeRabbit, PR #3360) ------

UNTYPED_SHADOWS = """package shapes

import "example.com/ext"

type Shape interface {
\tArea() float64
}

type Square struct{}

func (s Square) Area() float64 { return 1 }

type Other struct{}

func (o Other) Area() float64 { return 2 }

type Things []Shape

func FromCall(x Other, xs []Shape) float64 {
\tt := 0.0
\tfor _, x := range xs {
\t\tx := (func() Shape { return x })()
\t\tt += x.Area()
\t}
\treturn t + 3*x.Area()
}

func NamedSlice(x Other, xs Things) float64 {
\tt := 0.0
\tfor _, x := range xs {
\t\tt += 4 * x.Area()
\t}
\treturn t
}

func External(x Other, xs []ext.Shape) float64 {
\tt := 0.0
\tfor _, x := range xs {
\t\tt += 5 * x.Area()
\t}
\treturn t
}
"""


def _untyped_calls(
    mock_ingestor: MagicMock, project: str, caller: str, needle: str
) -> dict[str, str]:
    line, _ = _position(UNTYPED_SHADOWS, needle)
    pkg = f"{project}.shapes"
    return {
        callee.removeprefix(f"{pkg}."): str(resolution)
        for callee, resolution in _calls_by_line(mock_ingestor, f"{pkg}.{caller}")
        .get(line, {})
        .items()
    }


@pytest.mark.parametrize(
    ("caller", "needle"),
    [
        ("FromCall", "t += x.Area()"),
        ("NamedSlice", "t += 4 * x.Area()"),
        ("External", "t += 5 * x.Area()"),
    ],
    ids=["untyped-shadow", "unknown-element", "unresolved-element"],
)
def test_a_local_nothing_can_type_hides_the_outer_one(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    needle: str,
) -> None:
    # Inside the loop `x` is no longer the Other parameter. What it is
    # cannot be read here, so no call through it is pinned to Other.Area.
    project = _index(
        temp_repo / "gountyped",
        {"shapes.go": UNTYPED_SHADOWS},
        mock_ingestor,
        monkeypatch,
    )
    calls = _untyped_calls(mock_ingestor, project, caller, needle)
    assert calls.get("Other.Area") != "exact", calls


def test_the_outer_local_keeps_its_type_past_the_loop(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _index(
        temp_repo / "gountyped",
        {"shapes.go": UNTYPED_SHADOWS},
        mock_ingestor,
        monkeypatch,
    )
    assert _untyped_calls(
        mock_ingestor, project, "FromCall", "return t + 3*x.Area()"
    ) == {"Other.Area": "exact"}


# --- a rename reaching a definition through a guess asks first (CodeRabbit) ---

MODULE_A = """package moda

type I1 interface {
\tM() int
}

type T1 struct{}

func (T1) M() int { return 1 }

var _ I1 = T1{}
"""

MODULE_B = """package modb

type I2 interface {
\tM() int
}

type T2 struct{}

func (T2) M() int { return 2 }

var _ I2 = T2{}
"""


def _two_modules(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # go/types proves each module's own pair; the pairs across the two, which
    # no single run compares, are matched by name.
    _write(root, "go.mod", "module moda\n\ngo 1.22\n")
    _write(root, "a.go", MODULE_A)
    _write(root, "modb/go.mod", "module modb\n\ngo 1.22\n")
    _write(root, "modb/b.go", MODULE_B)
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    "a.go",
                    *_position(MODULE_A, "T1 struct"),
                    "a.go",
                    *_position(MODULE_A, "I1 interface"),
                ),
                GoImplements(
                    "modb/b.go",
                    *_position(MODULE_B, "T2 struct"),
                    "modb/b.go",
                    *_position(MODULE_B, "I2 interface"),
                ),
            ],
            analysed_modules=(".", "modb"),
            checked_files=frozenset({"a.go", "modb/b.go"}),
            compared_types=_compared("a.go", MODULE_A)
            | _compared("modb/b.go", MODULE_B),
        ),
    )


def test_a_hierarchy_reached_through_a_guess_needs_the_opt_in(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every method has a proven edge in its own module, so no definition's
    # edges are all guesses; I2.M and T2.M are still reached from I1.M only
    # through the guessed T2.M -> I1.M and T1.M -> I2.M.
    _two_modules(temp_repo, monkeypatch)
    graph = _rename_index(temp_repo, mock_ingestor)
    target = f"{graph.project}.a.I1.M"
    query = _query(graph)
    with pytest.raises(RenameRefused):
        rename(temp_repo, query, graph.project, target, "N")
    assert (temp_repo / "a.go").read_text(encoding="utf-8") == MODULE_A
    assert (temp_repo / "modb" / "b.go").read_text(encoding="utf-8") == MODULE_B

    report = rename(
        temp_repo,
        _query(graph),
        graph.project,
        target,
        "N",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    for rel in ("a.go", "modb/b.go"):
        text = (temp_repo / rel).read_text(encoding="utf-8")
        assert "\tN() int" in text, text
        assert ") N() int" in text, text
    if (go := shutil.which("go")) is not None:
        for module in (temp_repo, temp_repo / "modb"):
            built = subprocess.run(
                [go, "build", "./..."],
                cwd=module,
                capture_output=True,
                text=True,
                encoding=cs.ENCODING_UTF8,
                check=False,
            )
            assert built.returncode == 0, built.stderr


def test_a_proven_hierarchy_renames_without_the_opt_in(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One module, its pair proven: nothing is reached through a guess.
    _write(temp_repo, "go.mod", "module moda\n\ngo 1.22\n")
    _write(temp_repo, "a.go", MODULE_A)
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    "a.go",
                    *_position(MODULE_A, "T1 struct"),
                    "a.go",
                    *_position(MODULE_A, "I1 interface"),
                )
            ],
            analysed_modules=(".",),
            checked_files=frozenset({"a.go"}),
            compared_types=_compared("a.go", MODULE_A),
        ),
    )
    graph = _rename_index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo, _query(graph), graph.project, f"{graph.project}.a.I1.M", "N"
    )
    assert report.applied, report.message
    text = (temp_repo / "a.go").read_text(encoding="utf-8")
    assert "\tN() int" in text
    assert "func (T1) N() int" in text


# --- a package's own `error` and `any` (Greptile, PR #3360) ------------------

OWN_BUILTINS = """package errs

type error interface {
\tCode() int
}

type any interface {
\tCode() int
}

type I interface {
\terror
\tN()
}

type J interface {
\tany
\tN()
}

type T struct{}

func (T) Code() int { return 0 }
func (T) N()        {}

type U struct{}

func (U) N() {}

var _ I = T{}
"""

# Another package, so a sync touching it reads errs/ back from the graph.
OWN_BUILTINS_CALLER = """package other

import "proj/errs"

func Run(i errs.I) { i.N() }
"""


def _own_builtins_repo(root: Path) -> Path:
    root.mkdir()
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in (
        ("errs/errs.go", OWN_BUILTINS),
        ("other/other.go", OWN_BUILTINS_CALLER),
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_an_embedded_name_its_package_declares_is_its_own_interface(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `error` here asks for Code(), not Error() string, and `any` asks for
    # Code() too: T is an I and a J, and U, lacking Code, is neither.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _own_builtins_repo(temp_repo / "proj")
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    pkg = "proj.errs.errs"
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[(f"{pkg}.T", f"{pkg}.I")] == cs.EdgeResolution.HEURISTIC
    assert implements[(f"{pkg}.T", f"{pkg}.J")] == cs.EdgeResolution.HEURISTIC
    assert (f"{pkg}.U", f"{pkg}.I") not in implements
    assert (f"{pkg}.U", f"{pkg}.J") not in implements
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert overrides[(f"{pkg}.T.N", f"{pkg}.I.N")] == cs.EdgeResolution.HEURISTIC
    assert (f"{pkg}.U.N", f"{pkg}.J.N") not in overrides
    calls = {
        callee
        for caller, callee in _edges(mock_ingestor, cs.RelationshipType.CALLS)
        if caller == "proj.other.other.Run"
    }
    assert f"{pkg}.T.N" in calls
    assert f"{pkg}.U.N" not in calls
    if (go := shutil.which("go")) is not None:
        built = subprocess.run(
            [go, "vet", "./..."],
            cwd=root,
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
        )
        assert built.returncode == 0, built.stderr


@pytest.mark.parametrize("sync", ["fresh", "reused"])
def test_a_sync_reads_a_packages_own_error_back_as_its_parse_did(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, sync: str
) -> None:
    # Re-parsing other/ alone reads I back with its stored embeds, where
    # `error` is errs' own interface, and Run still reaches T.N.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _own_builtins_repo(temp_repo / "synced")
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / "other" / "other.go"
    path.write_text(path.read_text(encoding="utf-8") + "// synced\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    (first if sync == "reused" else _go_updater(store, root)).run(force=False)

    clean_root = _own_builtins_repo(temp_repo / "clean")
    (clean_root / "other" / "other.go").write_text(path.read_text(encoding="utf-8"))
    clean = _StatefulIngestor()
    _go_updater(clean, clean_root).run(force=True)
    assert _interface_edges(store) == _interface_edges(clean)
    assert (
        "proj.other.other.Run",
        "CALLS",
        "proj.errs.errs.T.N",
        "heuristic",
    ) in _interface_edges(store)


# --- a shadow is its package's, not its directory's (Greptile, PR #3360) -----

# Greptile's fixture: `p_test`, the external test package `go test` builds
# beside p, declares an `error` (or `any`) of its own. That name is another
# package's: p's `error` is still the predeclared one, and T is an I.
TEST_SHADOW_PACKAGE = """package p

type I interface {
\tBUILTIN
\tN()
}

type T struct{}

func (T) Error() string { return "" }
func (T) N()            {}

var _ I = T{}

func Run(i I) { i.N() }
"""

TEST_SHADOW = """package p_test

type BUILTIN interface {
\tCode()
}
"""

# p declares the name itself, in a production file or in one of its own
# `_test.go` files. T has Code() as well, so p compiles under `go test`
# (whose I asks for Code()) as under `go build`.
OWN_SHADOW_PACKAGE = """package p

type I interface {
\tBUILTIN
\tN()
}

type T struct{}

func (T) Code()         {}
func (T) Error() string { return "" }
func (T) N()            {}

type U struct{}

func (U) Error() string { return "" }
func (U) N()            {}

var _ I = T{}

func Run(i I) { i.N() }
"""

OWN_SHADOW = """package p

type BUILTIN interface {
\tCode()
}

type K interface {
\tBUILTIN
\tN()
}

type V struct{}

func (V) Code() {}
func (V) N()    {}

type W struct{}

func (W) Error() string { return "" }
func (W) N()            {}

var _ K = V{}
"""

SHADOW_CALLER = """package other

import "proj/p"

func Call(i p.I) { i.N() }
"""

_SHADOW_FILES = {
    "external_test": {"p/p.go": TEST_SHADOW_PACKAGE, "p/shadow_test.go": TEST_SHADOW},
    "production": {"p/p.go": OWN_SHADOW_PACKAGE, "p/own.go": OWN_SHADOW},
    "in_package_test": {"p/p.go": OWN_SHADOW_PACKAGE, "p/own_test.go": OWN_SHADOW},
}


def _shadow_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    root.mkdir()
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.replace("BUILTIN", name), encoding="utf-8")
    return root


def _go_checks(root: Path) -> None:
    # `go vet` on the host's build: it type-checks the test packages as
    # well, which `go build` skips.
    _go_checks_on(root, ("",))


@pytest.mark.parametrize("name", ["error", "any"])
def test_a_test_packages_own_error_leaves_the_package_alone(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    # The directory was taken for the package: p_test's `error` hid the
    # predeclared one from p, I's method set was unknown, and T lost its
    # IMPLEMENTS, OVERRIDES and dispatch edges. Renaming T.N then left I.N,
    # and p no longer compiled (`T does not implement I`).
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _shadow_repo(temp_repo / "proj", name, _SHADOW_FILES["external_test"])
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    pkg = "proj.p.p"
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[(f"{pkg}.T", f"{pkg}.I")] == cs.EdgeResolution.HEURISTIC
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert overrides[(f"{pkg}.T.N", f"{pkg}.I.N")] == cs.EdgeResolution.HEURISTIC
    assert (f"{pkg}.Run", f"{pkg}.T.N") in _edges(
        mock_ingestor, cs.RelationshipType.CALLS
    )

    report = rename(
        root, _query(graph), graph.project, f"{pkg}.T.N", "M", allow_heuristic=True
    )
    assert report.applied, report.message
    text = (root / "p" / "p.go").read_text(encoding="utf-8")
    assert "\tM()\n" in text, text
    assert "func (T) M()" in text, text
    _go_checks_after(root, before, ("",))


@pytest.mark.parametrize("name", ["error", "any"])
@pytest.mark.parametrize("where", ["production", "in_package_test"])
def test_a_package_declaring_error_in_a_test_file_shadows_it_there_alone(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    where: str,
) -> None:
    # p's own `error` is p's in every file of p: K asks for Code(), so V is
    # a K and W is not. Declared in a production file it is I's as well, and
    # U, lacking Code(), is no I. Declared in p's own `_test.go` file only
    # `go test` compiles it: `go build`, and the go/types helper answering a
    # production file from the plain package, read I's `error` as the
    # predeclared one, and U is an I there.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _shadow_repo(temp_repo / "proj", name, _SHADOW_FILES[where])
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    pkg = "proj.p.p"
    own = "proj.p.own" if where == "production" else "proj.p.own_test"
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[(f"{pkg}.T", f"{pkg}.I")] == cs.EdgeResolution.HEURISTIC
    assert ((f"{pkg}.U", f"{pkg}.I") in implements) is (where != "production")
    assert implements[(f"{own}.V", f"{own}.K")] == cs.EdgeResolution.HEURISTIC
    assert (f"{own}.W", f"{own}.K") not in implements

    report = rename(
        root, _query(graph), graph.project, f"{pkg}.T.N", "M", allow_heuristic=True
    )
    assert report.applied, report.message
    text = (root / "p" / "p.go").read_text(encoding="utf-8")
    assert "\tM()\n" in text, text
    assert "func (T) M()" in text, text
    assert ("func (U) M()" in text) is (where != "production"), text
    _go_checks_after(root, before, ("",))


@pytest.mark.parametrize("sync", ["fresh", "reused"])
@pytest.mark.parametrize("name", ["error", "any"])
@pytest.mark.parametrize("where", sorted(_SHADOW_FILES))
def test_a_sync_reads_a_shadow_back_by_its_package(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, sync: str, name: str, where: str
) -> None:
    # Re-parsing other/ alone reads p back: I's stored embeds meet the
    # shadows the graph's own files declare, and they must be read as the
    # parse read them.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {**_SHADOW_FILES[where], "other/other.go": SHADOW_CALLER}
    root = _shadow_repo(temp_repo / "synced", name, files)
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / "other" / "other.go"
    path.write_text(path.read_text(encoding="utf-8") + "// synced\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    (first if sync == "reused" else _go_updater(store, root)).run(force=False)

    clean_root = _shadow_repo(temp_repo / "clean", name, files)
    (clean_root / "other" / "other.go").write_text(path.read_text(encoding="utf-8"))
    clean = _StatefulIngestor()
    _go_updater(clean, clean_root).run(force=True)
    synced = _interface_edges(store)
    assert synced == _interface_edges(clean)
    assert ("proj.p.p.T", "IMPLEMENTS", "proj.p.p.I", "heuristic") in synced
    assert ("proj.other.other.Call", "CALLS", "proj.p.p.T.N", "heuristic") in synced


# An import names a directory's importable package: p_test's Reader, which
# no importer can see, is no candidate for q's `p.Reader`.
IMPORTED_READER = """package p

type Reader interface {
\tRead()
}
"""

TEST_READER = """package p_test

type Reader interface {
\tClose()
}
"""

READER_EMBEDDER = """package q

import "proj/p"

type I interface {
\tp.Reader
\tN()
}

type T struct{}

func (T) Read() {}
func (T) N()    {}

var _ I = T{}
"""


def test_an_embedded_import_names_the_package_not_its_external_tests(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two Readers in p/ made `p.Reader` ambiguous, I's method set unknown,
    # and T no I; renaming T.N then left I.N and q no longer compiled.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _shadow_repo(
        temp_repo / "proj",
        "error",
        {
            "p/p.go": IMPORTED_READER,
            "p/reader_test.go": TEST_READER,
            "q/q.go": READER_EMBEDDER,
        },
    )
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[("proj.q.q.T", "proj.q.q.I")] == cs.EdgeResolution.HEURISTIC
    inherits = _edges(mock_ingestor, cs.RelationshipType.INHERITS)
    assert ("proj.q.q.I", "proj.p.p.Reader") in inherits
    assert ("proj.q.q.I", "proj.p.reader_test.Reader") not in inherits

    report = rename(
        root, _query(graph), graph.project, "proj.q.q.T.N", "M", allow_heuristic=True
    )
    assert report.applied, report.message
    text = (root / "q" / "q.go").read_text(encoding="utf-8")
    assert "\tM()\n" in text, text
    assert "func (T) M()" in text, text
    _go_checks_after(root, before, ("",))


# A method named `error` declares nothing in its package's scope.
METHOD_NAMED_ERROR = """package p

type I interface {
\terror
\tN()
}

type scanner struct{}

func (s *scanner) error(msg string) {}
"""

METHOD_NAMED_ERROR_USER = """package q

import "proj/p"

type T struct{}

func (T) Error() string { return "" }
func (T) N()            {}

var _ p.I = T{}
"""


@pytest.mark.parametrize("sync", ["fresh", "reused"])
def test_a_method_named_error_shadows_nothing(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, sync: str
) -> None:
    # Read back from the graph, `scanner.error` counted as p declaring
    # `error`: I's stored embeds no longer matched, and a sync touching q
    # dropped T's IMPLEMENTS edge that a clean index has.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {"p/p.go": METHOD_NAMED_ERROR, "q/q.go": METHOD_NAMED_ERROR_USER}
    root = _shadow_repo(temp_repo / "synced", "error", files)
    _go_checks(root)
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / "q" / "q.go"
    path.write_text(path.read_text(encoding="utf-8") + "// synced\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    (first if sync == "reused" else _go_updater(store, root)).run(force=False)

    clean_root = _shadow_repo(temp_repo / "clean", "error", files)
    (clean_root / "q" / "q.go").write_text(path.read_text(encoding="utf-8"))
    clean = _StatefulIngestor()
    _go_updater(clean, clean_root).run(force=True)
    synced = _interface_edges(store)
    assert synced == _interface_edges(clean)
    assert ("proj.q.q.T", "IMPLEMENTS", "proj.p.p.I", "heuristic") in synced


# --- a type go/types skips keeps the name match (Greptile, PR #3360) ----------

GENERIC = """package gen

type Speaker interface {
\tSpeak() string
}

type Box[T any] struct{ v T }

func (b Box[T]) Speak() string { return "box" }

type Getter[T any] interface {
\tGet() T
}

type IntBox struct{}

func (IntBox) Get() int { return 1 }

type Label string

type Cat struct{}

func (Cat) Speak() Label { return "" }

func Use(s Speaker) string { return s.Speak() }

var _ Speaker = Box[int]{}
var _ Getter[int] = IntBox{}
"""


@pytest.mark.parametrize("facts", ["real", "recorded", "treesitter"])
def test_a_generic_type_or_interface_keeps_the_name_match(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    facts: str,
) -> None:
    # go/types compares no generic type or interface, so its silence about
    # Box and Getter is no verdict; Cat's Speak() Label is one it gave.
    root = temp_repo / "gogen"
    root.mkdir()
    (root / "go.mod").write_text("module gogen\n\ngo 1.22\n", encoding="utf-8")
    (root / "gen.go").write_text(GENERIC, encoding="utf-8")
    if facts == "real":
        _sig_frontend(monkeypatch, "gotypes", root)
    elif facts == "recorded":
        _gotypes_facts(
            monkeypatch,
            GoSemanticFacts(
                call_sites={},
                external_sites=set(),
                implements=[],
                analysed_modules=(".",),
                checked_files=frozenset({"gen.go"}),
                compared_types=_compared("gen.go", GENERIC),
            ),
        )
    else:
        monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    pkg = "gogen.gen"
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[(f"{pkg}.Box", f"{pkg}.Speaker")] == "heuristic"
    assert implements[(f"{pkg}.IntBox", f"{pkg}.Getter")] == "heuristic"
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert overrides[(f"{pkg}.Box.Speak", f"{pkg}.Speaker.Speak")] == "heuristic"
    calls = {
        callee
        for caller, callee in _edges(mock_ingestor, cs.RelationshipType.CALLS)
        if caller == f"{pkg}.Use"
    }
    assert f"{pkg}.Box.Speak" in calls
    cat = (f"{pkg}.Cat", f"{pkg}.Speaker")
    if facts == "treesitter":
        assert implements[cat] == "heuristic"
    else:
        # A type go/types compared and rejected stays rejected.
        assert cat not in implements
        assert f"{pkg}.Cat.Speak" not in calls


# --- a name each build declares again (Greptile, PR #3360) -------------------

_LINUX_ONLY = "//go:build linux\n\n"
_NOT_LINUX = "//go:build !linux\n\n"

VARIANT_I = """package p

type I interface {
\tM()
}
"""

# Greptile's fixture: `p.I` is declared once per build, the same in both.
VARIANT_EMBEDDER = """package q

import "proj/p"

type J interface {
\tp.I
\tN()
}

type T struct{}

func (T) M() {}
func (T) N() {}

var _ J = T{}
"""

_VARIANT_STYLES = {
    # By file name, and by `//go:build` line; a single declaration is the
    # control both must match.
    "suffix": {"p/i_linux.go": VARIANT_I, "p/i_windows.go": VARIANT_I},
    "tag": {
        "p/i_a.go": _LINUX_ONLY + VARIANT_I,
        "p/i_b.go": _NOT_LINUX + VARIANT_I,
    },
    "single": {"p/i.go": VARIANT_I},
}


def _variant_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir()
    (root / "go.mod").write_text("module proj\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# Where `_go_checks_on` marks the (sources, GOOS) pairs it found compiling,
# shared by every xdist worker of the session.
_VETTED: list[Path] = []


@pytest.fixture
def _shared_vet_cache(tmp_path_factory: pytest.TempPathFactory) -> None:
    _VETTED[:] = [shared_session_dir(tmp_path_factory, "go-vetted")]


def _go_sources_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.suffix == cs.EXT_GO or path.name == "go.mod":
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _go_checks_on(root: Path, goos: tuple[str, ...] = ("linux", "windows")) -> None:
    # `go vet` for each GOOS: a rename must leave every build that compiled
    # compiling, and vet type-checks every package and every test file (a
    # `go build` of these libraries added a second compile and nothing
    # more). Sources already vetted for a GOOS in this session, as a
    # parametrized fixture is, are not vetted again.
    if (go := shutil.which("go")) is None:
        return
    digest = _go_sources_digest(root)
    for target in goos:
        marker = _VETTED[0] / f"{digest}-{target or 'host'}" if _VETTED else None
        if marker is not None and marker.exists():
            continue
        done = subprocess.run(
            [go, "vet", "./..."],
            cwd=root,
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
            # "" is the host's build.
            env=(
                {**os.environ, "GOOS": target, "GOARCH": "amd64", "CGO_ENABLED": "0"}
                if target
                else None
            ),
        )
        assert done.returncode == 0, (target, done.stderr)
        if marker is not None:
            marker.touch()


def _go_sources(root: Path) -> dict[str, bytes]:
    # The Go sources as they stand, for `_go_checks_after`.
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.suffix == cs.EXT_GO or path.name == "go.mod"
    }


def _go_checks_after(
    root: Path,
    before: dict[str, bytes],
    goos: tuple[str, ...] = ("linux", "windows"),
) -> None:
    """`go vet` on each GOOS after a rename: it must leave every build
    compiling. The tree before it (`before`, from `_go_sources`) is vetted
    only when one fails, to tell a fixture that never compiled from a build
    the rename broke; vetting it first as well cost a `go` process per GOOS
    and test, slowest on Windows, and could fail no test this passes."""
    try:
        _go_checks_on(root, goos)
    except AssertionError:
        with tempfile.TemporaryDirectory() as tmp:
            for rel, data in before.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            _go_checks_on(Path(tmp), goos)
        raise


@pytest.mark.parametrize("style", sorted(_VARIANT_STYLES))
def test_an_embedded_name_each_build_declares_keeps_its_method_set(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    style: str,
) -> None:
    # Both builds' `p.I` passed the package filter, so `p.I` was ambiguous,
    # J's method set unknown, and T lost IMPLEMENTS J and T.N OVERRIDES J.N.
    # Renaming T.N then left J.N, and q compiled on neither OS.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {**_VARIANT_STYLES[style], "q/q.go": VARIANT_EMBEDDER}
    root = _variant_repo(temp_repo / "proj", files)
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[("proj.q.q.T", "proj.q.q.J")] == cs.EdgeResolution.HEURISTIC
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert overrides[("proj.q.q.T.N", "proj.q.q.J.N")] == cs.EdgeResolution.HEURISTIC

    report = rename(
        root, _query(graph), graph.project, "proj.q.q.T.N", "O", allow_heuristic=True
    )
    assert report.applied, report.message
    text = (root / "q" / "q.go").read_text(encoding="utf-8")
    assert "\tO()\n" in text, text
    assert "func (T) O()" in text, text
    _go_checks_after(root, before)


# The builds' `I` differ: T and Y are a J on Linux, U and X on Windows, V
# on neither.
DIFFERING_FILES = {
    "p/i_linux.go": VARIANT_I,
    "p/i_windows.go": """package p

type I interface {
\tW()
}
""",
    "p/j.go": """package p

type J interface {
\tI
\tN()
}
""",
    "p/t_linux.go": """package p

type T struct{}

func (T) M() {}
func (T) N() {}

var _ J = T{}
""",
    "p/u_windows.go": """package p

type U struct{}

func (U) W() {}
func (U) N() {}

var _ J = U{}
""",
    "p/v.go": """package p

type V struct{}

func (V) N() {}
""",
    # Files every build compiles, whose types are a J on one OS alone.
    "p/x.go": """package p

type X struct{}

func (X) W() {}
func (X) N() {}
""",
    "p/y.go": """package p

type Y struct{}

func (Y) M() {}
func (Y) N() {}
""",
}


# What the helper proves on each OS: the pairs of the files that build reads.
_DIFFERING_PROOFS = {
    "linux": [
        ("p/t_linux.go", "T", "p/j.go", "J"),
        ("p/t_linux.go", "T", "p/i_linux.go", "I"),
        ("p/y.go", "Y", "p/j.go", "J"),
        ("p/y.go", "Y", "p/i_linux.go", "I"),
    ],
    "windows": [
        ("p/u_windows.go", "U", "p/j.go", "J"),
        ("p/u_windows.go", "U", "p/i_windows.go", "I"),
        ("p/x.go", "X", "p/j.go", "J"),
        ("p/x.go", "X", "p/i_windows.go", "I"),
    ],
}


def _differing_gotypes(monkeypatch: pytest.MonkeyPatch, goos: str) -> None:
    other = "windows" if goos == "linux" else "linux"
    read = {rel: text for rel, text in DIFFERING_FILES.items() if other not in rel}
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    impl_file,
                    *_position(DIFFERING_FILES[impl_file], f"{impl} struct"),
                    iface_file,
                    *_position(DIFFERING_FILES[iface_file], f"{iface} interface"),
                )
                for impl_file, impl, iface_file, iface in _DIFFERING_PROOFS[goos]
            ],
            analysed_modules=(".",),
            checked_files=frozenset(read),
            compared_types=frozenset().union(
                *(_compared(rel, text) for rel, text in read.items())
            ),
        ),
    )


@pytest.mark.parametrize(
    "frontend",
    [
        "treesitter",
        "recorded-linux",
        "recorded-windows",
        "real",
        "real-linux",
        "real-windows",
        "real-darwin",
    ],
)
def test_an_interface_each_build_declares_apart_links_each_builds_implementers(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    frontend: str,
) -> None:
    # J embeds the I of its build. The name match took whichever `I` a set
    # yielded first, so one build's implementers lost their pairs. Each pair
    # holds in one build only: none is proven, even where go/types proved
    # it, and its silence on the other build's (U's file it never read, X
    # and Y it read and compared) rules nothing out.
    root = _variant_repo(temp_repo / "proj", DIFFERING_FILES)
    before = _go_sources(root)
    # The build go/types read: its pairs there are proven, every other
    # build's matched by name.
    proven_on = frontend.removeprefix("recorded-")
    if frontend.startswith("real"):
        proven_on = _real_build(monkeypatch, root, DIFFERING_FILES, frontend)
    elif frontend != "treesitter":
        _differing_gotypes(monkeypatch, proven_on)
    else:
        monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    graph = _rename_index(root, mock_ingestor)
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[("proj.p.t_linux.T", "proj.p.j.J")] == cs.EdgeResolution.HEURISTIC
    assert (
        implements[("proj.p.u_windows.U", "proj.p.j.J")] == cs.EdgeResolution.HEURISTIC
    )
    assert implements[("proj.p.x.X", "proj.p.j.J")] == cs.EdgeResolution.HEURISTIC
    assert implements[("proj.p.y.Y", "proj.p.j.J")] == cs.EdgeResolution.HEURISTIC
    assert ("proj.p.v.V", "proj.p.j.J") not in implements
    # One build's own interface is that build's: go/types' proof stands.
    for owner, own in (("proj.p.t_linux.T", "linux"), ("proj.p.x.X", "windows")):
        expected = (
            None
            if frontend != "treesitter" and own == proven_on
            else cs.EdgeResolution.HEURISTIC
        )
        assert implements[(owner, f"proj.p.i_{own}.I")] == expected
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    for owner in ("proj.p.t_linux.T", "proj.p.u_windows.U"):
        assert overrides[(f"{owner}.N", "proj.p.j.J.N")] == cs.EdgeResolution.HEURISTIC
    assert ("proj.p.v.V.N", "proj.p.j.J.N") not in overrides

    report = rename(
        root,
        _query(graph),
        graph.project,
        "proj.p.t_linux.T.N",
        "O",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    assert "func (U) O()" in (root / "p" / "u_windows.go").read_text(encoding="utf-8")
    assert "func (X) O()" in (root / "p" / "x.go").read_text(encoding="utf-8")
    assert "func (Y) O()" in (root / "p" / "y.go").read_text(encoding="utf-8")
    assert "func (V) N()" in (root / "p" / "v.go").read_text(encoding="utf-8")
    _go_checks_after(root, before)


def _recorded_identical_gotypes(monkeypatch: pytest.MonkeyPatch) -> None:
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    "q/q.go",
                    *_position(VARIANT_EMBEDDER, "T struct"),
                    "q/q.go",
                    *_position(VARIANT_EMBEDDER, "J interface"),
                ),
                GoImplements(
                    "q/q.go",
                    *_position(VARIANT_EMBEDDER, "T struct"),
                    "p/i_linux.go",
                    *_position(VARIANT_I, "I interface"),
                ),
            ],
            analysed_modules=(".",),
            checked_files=frozenset({"p/i_linux.go", "q/q.go"}),
            compared_types=_compared("p/i_linux.go", VARIANT_I)
            | _compared("q/q.go", VARIANT_EMBEDDER),
        ),
    )


@pytest.mark.parametrize(
    "frontend", ["recorded", "real", "real-linux", "real-windows", "real-darwin"]
)
def test_builds_that_agree_keep_the_go_types_proof(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    frontend: str,
) -> None:
    # Each build's `p.I` asks for M(): J is one method set, and the pair
    # go/types proved stays proven. Darwin declares no `p.I`: go/types
    # proves nothing there and the pair is matched by name.
    files = {**_VARIANT_STYLES["suffix"], "q/q.go": VARIANT_EMBEDDER}
    root = _variant_repo(temp_repo / "proj", files)
    goos = "linux"
    if frontend.startswith("real"):
        goos = _real_build(monkeypatch, root, files, frontend)
    else:
        _recorded_identical_gotypes(monkeypatch)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    expected = None if goos != "darwin" else cs.EdgeResolution.HEURISTIC
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[("proj.q.q.T", "proj.q.q.J")] == expected
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert overrides[("proj.q.q.T.N", "proj.q.q.J.N")] == expected


# Files one build compiles together: a redeclaration there, so `I` names
# no one interface, and J's method set stays unknown.
_OVERLAPPING = {
    "arch": {"p/i_linux.go": VARIANT_I, "p/i_amd64.go": VARIANT_I},
    "plain": {"p/a.go": VARIANT_I, "p/b.go": VARIANT_I},
    "tag": {"p/i_a.go": _LINUX_ONLY + VARIANT_I, "p/i_b.go": VARIANT_I},
}

LOCAL_EMBEDDER = """package p

type J interface {
\tI
\tN()
}

type T struct{}

func (T) M() {}
func (T) N() {}
"""


@pytest.mark.parametrize("embed", ["imported", "local"])
@pytest.mark.parametrize("overlap", sorted(_OVERLAPPING))
def test_two_declarations_one_build_compiles_stay_ambiguous(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    overlap: str,
    embed: str,
) -> None:
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = dict(_OVERLAPPING[overlap])
    if embed == "imported":
        files["q/q.go"] = VARIANT_EMBEDDER.replace("\nvar _ J = T{}\n", "")
        pkg = "proj.q.q"
    else:
        files["p/j.go"] = LOCAL_EMBEDDER
        pkg = "proj.p.j"
    root = _variant_repo(temp_repo / "proj", files)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert (f"{pkg}.T", f"{pkg}.J") not in implements


# A type each build declares again, its methods in a file both compile.
TYPE_VARIANT_FILES = {
    "p/t_linux.go": "package p\n\ntype T struct{ a int }\n",
    "p/t_windows.go": "package p\n\ntype T struct{ b int }\n",
    "p/t.go": """package p

func (T) M() {}
func (T) N() {}
""",
    "p/m_windows.go": """package p

func (T) W() {}

type I2 interface {
\tM()
\tW()
}

var _ I2 = T{}

func Run2(i I2) { i.M() }
""",
    "p/i.go": """package p

type I interface {
\tM()
\tN()
}

var _ I = T{}

func Run(i I) { i.M() }
""",
}


def test_a_type_each_build_declares_again_has_the_methods_both_compile(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `func (T) M()` in t.go was filed under whichever `T` a set yielded
    # first, and the other build's T had no M: no IMPLEMENTS I, and on
    # Windows no I2 either, so renaming I2.M left t.go's M.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _variant_repo(temp_repo / "proj", TYPE_VARIANT_FILES)
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    linux, windows = "proj.p.t_linux.T", "proj.p.t_windows.T"
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[(linux, "proj.p.i.I")] == cs.EdgeResolution.HEURISTIC
    assert implements[(windows, "proj.p.i.I")] == cs.EdgeResolution.HEURISTIC
    assert implements[(windows, "proj.p.m_windows.I2")] == cs.EdgeResolution.HEURISTIC
    assert (linux, "proj.p.m_windows.I2") not in implements
    # The shared file's methods sit under the first `T` by name; the
    # Windows file's under Windows' T.
    nodes = _node_labels(mock_ingestor)
    assert {f"{linux}.M", f"{linux}.N", f"{windows}.W"} <= nodes.keys()
    assert f"{windows}.M" not in nodes
    assert f"{linux}.W" not in nodes
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert (f"{linux}.M", "proj.p.m_windows.I2.M") in overrides
    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)
    assert ("proj.p.m_windows.Run2", f"{linux}.M") in calls
    assert ("proj.p.i.Run", f"{linux}.M") in calls

    report = rename(
        root,
        _query(graph),
        graph.project,
        "proj.p.m_windows.I2.M",
        "Y",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    assert "func (T) Y()" in (root / "p" / "t.go").read_text(encoding="utf-8")
    _go_checks_after(root, before)


# `error` is p's own on Windows alone: elsewhere it is the predeclared one.
PARTIAL_SHADOW_FILES = {
    "p/err_windows.go": """package p

type error interface {
\tCode()
}
""",
    "p/i.go": """package p

type I interface {
\terror
\tN()
}
""",
    "p/t_linux.go": """package p

type T struct{}

func (T) Error() string { return "" }
func (T) N()            {}

var _ I = T{}
""",
    "p/u_windows.go": """package p

type U struct{}

func (U) Code() {}
func (U) N()    {}

var _ I = U{}
""",
}

PARTIAL_SHADOW_CALLER = """package other

import "proj/p"

func Call(i p.I) { i.N() }
"""


@pytest.mark.parametrize("sync", ["parse", "fresh", "reused"])
def test_an_error_one_build_declares_shadows_it_in_that_build_alone(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, sync: str
) -> None:
    # p's Windows-only `error` hid the predeclared one from every build: I
    # asked for Code() alone, and T, an I on Linux, lost its pair.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {**PARTIAL_SHADOW_FILES, "other/other.go": PARTIAL_SHADOW_CALLER}
    root = _variant_repo(temp_repo / "synced", files)
    _go_checks_on(root)
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    if sync != "parse":
        cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
        path = root / "other" / "other.go"
        path.write_text(path.read_text(encoding="utf-8") + "// synced\n")
        os.utime(path, (cache_mtime + 1, cache_mtime + 1))
        (first if sync == "reused" else _go_updater(store, root)).run(force=False)
        clean_root = _variant_repo(temp_repo / "clean", files)
        (clean_root / "other" / "other.go").write_text(path.read_text(encoding="utf-8"))
        clean = _StatefulIngestor()
        _go_updater(clean, clean_root).run(force=True)
        assert _interface_edges(store) == _interface_edges(clean)
    edges = _interface_edges(store)
    for owner in ("proj.p.t_linux.T", "proj.p.u_windows.U"):
        assert (owner, "IMPLEMENTS", "proj.p.i.I", "heuristic") in edges
        assert ("proj.other.other.Call", "CALLS", f"{owner}.N", "heuristic") in edges


@pytest.mark.parametrize("sync", ["fresh", "reused"])
def test_a_sync_reads_build_variant_embeds_back_as_its_parse_did(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, sync: str
) -> None:
    # Re-parsing other/ alone reads p and q back: J's stored embed names one
    # build's `p.I`, and both builds' are still J's.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {
        **_VARIANT_STYLES["suffix"],
        "p/i_windows.go": DIFFERING_FILES["p/i_windows.go"],
        # T is a J on Linux alone, U on Windows alone.
        "q/q.go": VARIANT_EMBEDDER.replace("\nvar _ J = T{}\n", ""),
        "q/u_windows.go": """package q

type U struct{}

func (U) W() {}
func (U) N() {}
""",
        "other/other.go": """package other

import "proj/q"

func Call(j q.J) { j.N() }
""",
    }
    root = _variant_repo(temp_repo / "synced", files)
    _go_checks_on(root)
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / "other" / "other.go"
    path.write_text(path.read_text(encoding="utf-8") + "// synced\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    (first if sync == "reused" else _go_updater(store, root)).run(force=False)
    clean_root = _variant_repo(temp_repo / "clean", files)
    (clean_root / "other" / "other.go").write_text(path.read_text(encoding="utf-8"))
    clean = _StatefulIngestor()
    _go_updater(clean, clean_root).run(force=True)
    synced = _interface_edges(store)
    assert synced == _interface_edges(clean)
    for owner in ("proj.q.q.T", "proj.q.u_windows.U"):
        assert (owner, "IMPLEMENTS", "proj.q.q.J", "heuristic") in synced
        assert ("proj.other.other.Call", "CALLS", f"{owner}.N", "heuristic") in synced


@pytest.mark.parametrize("sync", ["fresh", "reused"])
def test_a_sync_reads_a_local_build_variant_embed_back_as_its_parse_did(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, sync: str
) -> None:
    # p read back from the graph: J's unqualified `I` is each build's own
    # again, so both builds' implementers keep their pairs.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {
        **DIFFERING_FILES,
        "other/other.go": """package other

import "proj/p"

func Call(j p.J) { j.N() }
""",
    }
    root = _variant_repo(temp_repo / "synced", files)
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / "other" / "other.go"
    path.write_text(path.read_text(encoding="utf-8") + "// synced\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    (first if sync == "reused" else _go_updater(store, root)).run(force=False)
    clean_root = _variant_repo(temp_repo / "clean", files)
    (clean_root / "other" / "other.go").write_text(path.read_text(encoding="utf-8"))
    clean = _StatefulIngestor()
    _go_updater(clean, clean_root).run(force=True)
    synced = _interface_edges(store)
    assert synced == _interface_edges(clean)
    for owner in ("proj.p.t_linux.T", "proj.p.u_windows.U"):
        assert (owner, "IMPLEMENTS", "proj.p.j.J", "heuristic") in synced
        assert ("proj.other.other.Call", "CALLS", f"{owner}.N", "heuristic") in synced
    assert not any(edge[0] == "proj.p.v.V" for edge in synced)


# --- builds combine only with builds (Greptile, PR #3360) ---------------------

# A asks for M() on Linux and W() on Windows, B for L() and R(): J is
# {M, L, N} on Linux and {W, R, N} on Windows, and never {M, R, N}.
COMBINED_FILES = {
    "p/a_linux.go": "package p\n\ntype A interface {\n\tM()\n}\n",
    "p/a_windows.go": "package p\n\ntype A interface {\n\tW()\n}\n",
    "p/b_linux.go": "package p\n\ntype B interface {\n\tL()\n}\n",
    "p/b_windows.go": "package p\n\ntype B interface {\n\tR()\n}\n",
    "p/j.go": "package p\n\ntype J interface {\n\tA\n\tB\n\tN()\n}\n",
    "p/t.go": """package p

type T struct{}

func (T) M() {}
func (T) L() {}
func (T) N() {}
""",
    "p/t_linux.go": "package p\n\nvar _ J = T{}\n",
    "p/v.go": """package p

type V struct{}

func (V) W() {}
func (V) R() {}
func (V) N() {}
""",
    "p/v_windows.go": "package p\n\nvar _ J = V{}\n",
    # M from Linux's A, R from Windows' B: a J in no build.
    "p/u.go": """package p

type U struct{}

func (U) M() {}
func (U) R() {}
func (U) N() {}
""",
}


def _combined_gotypes(monkeypatch: pytest.MonkeyPatch) -> None:
    linux = {rel: text for rel, text in COMBINED_FILES.items() if "windows" not in rel}
    t_file, j_file = COMBINED_FILES["p/t.go"], COMBINED_FILES["p/j.go"]
    _gotypes_facts(
        monkeypatch,
        GoSemanticFacts(
            call_sites={},
            external_sites=set(),
            implements=[
                GoImplements(
                    "p/t.go",
                    *_position(t_file, "T struct"),
                    "p/j.go",
                    *_position(j_file, "J interface"),
                )
            ],
            analysed_modules=(".",),
            checked_files=frozenset(linux),
            compared_types=frozenset().union(
                *(_compared(rel, text) for rel, text in linux.items())
            ),
        ),
    )


@pytest.mark.parametrize(
    "frontend",
    ["treesitter", "recorded", "real", "real-linux", "real-windows", "real-darwin"],
)
def test_embeds_combine_only_the_builds_one_build_compiles(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    frontend: str,
) -> None:
    # J's method sets were every product of A's and B's: {M, R, N} too, and
    # U, a J in no build, got IMPLEMENTS and OVERRIDES that pulled its
    # methods into J's rename hierarchy.
    root = _variant_repo(temp_repo / "proj", COMBINED_FILES)
    before = _go_sources(root)
    if frontend.startswith("real"):
        _real_build(monkeypatch, root, COMBINED_FILES, frontend)
    elif frontend == "recorded":
        _combined_gotypes(monkeypatch)
    else:
        monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    graph = _rename_index(root, mock_ingestor)
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert ("proj.p.t.T", "proj.p.j.J") in implements
    assert ("proj.p.v.V", "proj.p.j.J") in implements
    assert ("proj.p.u.U", "proj.p.j.J") not in implements
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert ("proj.p.u.U.N", "proj.p.j.J.N") not in overrides
    assert ("proj.p.t.T.N", "proj.p.j.J.N") in overrides

    report = rename(
        root, _query(graph), graph.project, "proj.p.t.T.N", "O", allow_heuristic=True
    )
    assert report.applied, report.message
    assert "func (V) O()" in (root / "p" / "v.go").read_text(encoding="utf-8")
    assert "func (U) N()" in (root / "p" / "u.go").read_text(encoding="utf-8")
    _go_checks_after(root, before)


# --- a call binds the build its caller compiles in (Greptile, PR #3360) -------

CALLER_BUILD_FILES = {
    "p/i_linux.go": "package p\n\ntype I interface {\n\tM() int\n}\n",
    "p/i_windows.go": "package p\n\ntype I interface {\n\tM() string\n\tX()\n}\n",
    "p/j.go": "package p\n\ntype J interface {\n\tI\n\tN()\n}\n",
    "p/t_linux.go": """package p

type T struct{}

func (T) M() int { return 0 }
func (T) N()     {}

var _ J = T{}
""",
    "p/u_windows.go": """package p

type U struct{}

func (U) M() string { return "" }
func (U) X()        {}
func (U) N()        {}

var _ J = U{}
""",
    "p/c_windows.go": """package p

func CallM(j J) { j.M() }

func CallX(j J) { j.X() }
""",
    "p/c.go": "package p\n\nfunc CallAny(j J) { j.M() }\n",
}


def _calls_from(mock_ingestor: MagicMock, caller: str) -> dict[str, str | None]:
    return {
        callee: props.get(cs.KEY_RESOLUTION)
        for (source, callee), props in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        ).items()
        if source == caller
    }


def test_a_call_through_an_embed_binds_the_callers_builds(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # J's one INHERITS edge named Linux's I: a Windows-only caller's `j.M()`
    # got an exact edge to Linux's M and its Linux implementation, and
    # `j.X()`, which only Windows' I declares, reached no implementation.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _variant_repo(temp_repo / "proj", CALLER_BUILD_FILES)
    _go_checks_on(root)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    linux, windows = "proj.p.i_linux.I", "proj.p.i_windows.I"
    t_m, u_m, u_x = "proj.p.t_linux.T.M", "proj.p.u_windows.U.M", "proj.p.u_windows.U.X"
    windows_m = _calls_from(mock_ingestor, "proj.p.c_windows.CallM")
    assert windows_m[f"{windows}.M"] == cs.EdgeResolution.EXACT
    assert windows_m[u_m] == cs.EdgeResolution.HEURISTIC
    assert f"{linux}.M" not in windows_m
    assert t_m not in windows_m
    windows_x = _calls_from(mock_ingestor, "proj.p.c_windows.CallX")
    assert windows_x == {
        f"{windows}.X": cs.EdgeResolution.EXACT,
        u_x: cs.EdgeResolution.HEURISTIC,
    }
    # Every build compiles c.go, and each has its own M: no edge is exact.
    assert _calls_from(mock_ingestor, "proj.p.c.CallAny") == {
        f"{linux}.M": cs.EdgeResolution.HEURISTIC,
        f"{windows}.M": cs.EdgeResolution.HEURISTIC,
        t_m: cs.EdgeResolution.HEURISTIC,
        u_m: cs.EdgeResolution.HEURISTIC,
    }


def test_renaming_one_builds_interface_method_renames_every_builds(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Renaming Linux's I.M rewrote c.go's `j.M()`, which Windows compiles
    # against its own I.M, left unrenamed: Windows no longer built.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _variant_repo(temp_repo / "proj", CALLER_BUILD_FILES)
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    report = rename(
        root,
        _query(graph),
        graph.project,
        "proj.p.i_linux.I.M",
        "Z",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    assert "\tZ() string\n" in (root / "p" / "i_windows.go").read_text(encoding="utf-8")
    assert "j.Z()" in (root / "p" / "c.go").read_text(encoding="utf-8")
    _go_checks_after(root, before)


# Each build declares T.M at the same line and column.
SAME_PLACE_FILES = {
    "p/i.go": "package p\n\ntype I interface {\n\tM()\n}\n\nvar _ I = T{}\n",
    "p/t.go": "package p\n\ntype T struct{}\n",
    "p/m_linux.go": "package p\n\nfunc (T) M() {}\n",
    "p/m_windows.go": "package p\n\nfunc (T) M() {}\n",
    "p/m_darwin.go": "package p\n\nfunc (T) M() {}\n",
}


def test_a_method_each_build_declares_at_one_place_keeps_every_declaration(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `T.M@3` named the Windows and the Darwin declaration both: one node
    # stood for two, the other build's M had no OVERRIDES, and renaming I.M
    # left it, so that build no longer compiled.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _variant_repo(temp_repo / "proj", SAME_PLACE_FILES)
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    files = _method_files(mock_ingestor)
    declared = {
        files[qn] for qn in files if qn_markers.natural_qn(qn) == "proj.p.t.T.M"
    }
    assert declared == {"p/m_linux.go", "p/m_windows.go", "p/m_darwin.go"}
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert {
        files[method] for method, base in overrides if base == "proj.p.i.I.M"
    } == declared

    report = rename(
        root, _query(graph), graph.project, "proj.p.i.I.M", "Z", allow_heuristic=True
    )
    assert report.applied, report.message
    for rel in ("p/m_linux.go", "p/m_windows.go", "p/m_darwin.go"):
        assert "func (T) Z()" in (root / rel).read_text(encoding="utf-8"), rel
    _go_checks_after(root, before, ("linux", "windows", "darwin"))


# A header Go's own reader takes, nested past any depth this one reads.
DEEP_HEADER = "//go:build " + "(" * 300 + "linux" + ")" * 300 + "\n\n"


@pytest.mark.parametrize("how", ["index", "reingest"])
def test_a_build_line_too_deep_to_read_stops_no_index(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    # Reading the header recursed once per parenthesis: RecursionError out
    # of deferred resolution aborted the index, and a reingest after it had
    # changed the graph.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {
        "p/i_a.go": DEEP_HEADER + VARIANT_I,
        "p/i_b.go": _NOT_LINUX + VARIANT_I,
        "p/j.go": LOCAL_EMBEDDER.replace("package p\n", "package p\n", 1),
    }
    root = _variant_repo(temp_repo / "proj", files)
    store = _StatefulIngestor()
    updater = _go_updater(store, root)
    updater.run(force=True)
    if how == "reingest":
        path = root / "p" / "i_a.go"
        path.write_text(path.read_text(encoding="utf-8") + "// edited\n")
        updater.reingest([path])
    edges = _interface_edges(store)
    # The deep header holds in every build as far as this can tell, so the
    # two `I` may build together: J's method set stays unknown.
    assert not any(
        edge[1] == "IMPLEMENTS" and edge[2] == "proj.p.j.J" for edge in edges
    )


# --- a build variant is the same package's, and apart (Greptile, PR #3360) ----

VARIANT_NEIGHBOURS = {
    "p/h_linux.go": "package p\n\nfunc Helper() {}\n",
    "p/h_windows.go": "package p\n\nfunc Helper() {}\n",
    "p/c.go": "package p\n\nfunc Run() { Helper() }\n",
    # Same name, same directory, never the same declaration.
    "p/helper.py": "def Helper():\n    return 1\n\n\ndef use():\n    return Helper()\n",
    "p/helper.js": "function Helper() {\n  return 1;\n}\n\nHelper();\n",
    "p/ext_test.go": "package p_test\n\nfunc Helper() {}\n\nfunc use() { Helper() }\n",
    "r/r.go": "package r\n\nfunc Helper() {}\n",
}


@pytest.mark.parametrize("opt_in", [False, True])
def test_a_rename_reaches_only_the_same_packages_other_builds(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    opt_in: bool,
) -> None:
    # Every same-named Function in the directory counted as a build variant,
    # proven: the Python and JavaScript `Helper`, and `p_test`'s, were
    # rewritten unasked, and the Python module lost the name its API
    # exported.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _variant_repo(temp_repo / "proj", VARIANT_NEIGHBOURS)
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    query, target = _query(graph), "proj.p.h_linux.Helper"
    if opt_in:
        report = rename(
            root, query, graph.project, target, "Assist", allow_heuristic=True
        )
        assert report.applied, report.message
        for rel in ("p/h_linux.go", "p/h_windows.go"):
            assert "func Assist()" in (root / rel).read_text(encoding="utf-8"), rel
        assert "Assist()" in (root / "p" / "c.go").read_text(encoding="utf-8")
        _go_checks_after(root, before)
    else:
        # c.go reaches each build's Helper, a guess the caller opts into.
        with pytest.raises(RenameRefused):
            rename(root, query, graph.project, target, "Assist")
        _go_checks_after(root, before)
    for rel in ("p/helper.py", "p/helper.js", "p/ext_test.go", "r/r.go"):
        assert (root / rel).read_text(encoding="utf-8") == VARIANT_NEIGHBOURS[rel], rel


def test_two_declarations_one_build_compiles_are_no_rename_variants(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `s/a.go` and `s/b_linux.go` both build on Linux: a redeclaration, not
    # one name each build declares, so the other is left alone.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {
        "s/a.go": "package s\n\nfunc Helper() {}\n",
        "s/b_linux.go": "package s\n\nfunc Helper() {}\n",
    }
    root = _variant_repo(temp_repo / "proj", files)
    graph = _rename_index(root, mock_ingestor)
    report = rename(
        root,
        _query(graph),
        graph.project,
        "proj.s.a.Helper",
        "Assist",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    assert (root / "s" / "b_linux.go").read_text(encoding="utf-8") == files[
        "s/b_linux.go"
    ]


PROBE_FILES = {
    "p/p.go": "package p\n",
    "p/probe_linux_test.go": "package p\n\nfunc Probe() {}\n",
    "p/probe_windows_test.go": "package p\n\nfunc Probe() {}\n",
    "p/use_test.go": "package p\n\nfunc use() { Probe() }\n",
    # `p_test`: same directory, a `_test.go` file, the other build, another
    # package.
    "p/ext_windows_test.go": "package p_test\n\nfunc Probe() {}\n",
}


def test_a_tests_own_build_variant_is_renamed_and_the_external_tests_is_not(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Any same-named Function of a `_test.go` file in the directory counted
    # as a variant of `p`'s own test's: `p_test`'s Probe was renamed too.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _variant_repo(temp_repo / "proj", PROBE_FILES)
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    report = rename(
        root,
        _query(graph),
        graph.project,
        "proj.p.probe_linux_test.Probe",
        "Assist",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    for rel in ("p/probe_linux_test.go", "p/probe_windows_test.go", "p/use_test.go"):
        assert "Assist()" in (root / rel).read_text(encoding="utf-8"), rel
    assert (root / "p" / "ext_windows_test.go").read_text(
        encoding="utf-8"
    ) == PROBE_FILES["p/ext_windows_test.go"]
    _go_checks_after(root, before)


# --- a diamond of embeds is walked once per interface (Greptile, PR #3360) ---


def _diamond(levels: int) -> dict[str, str]:
    # A0 and B0, then per level A<k> and B<k>, each embedding both of the
    # level below; T implements every one, and Call calls through the top.
    body = ["package p\n", "type A0 interface{ M0() }\n", "type B0 interface{ N0() }\n"]
    for k in range(1, levels):
        for name, method in (("A", "M"), ("B", "N")):
            body.append(
                f"type {name}{k} interface {{\n\tA{k - 1}\n\tB{k - 1}\n"
                f"\t{method}{k}()\n}}\n"
            )
    top = levels - 1
    methods = "".join(
        f"func (T) {method}{k}() {{}}\n" for k in range(levels) for method in "MN"
    )
    return {
        "p/d.go": "\n".join(body),
        "p/t.go": f"package p\n\ntype T struct{{}}\n\n{methods}",
        "p/call.go": f"package p\n\nfunc Call(a A{top}) {{\n\ta.M{top}()\n}}\n",
    }


@pytest.mark.parametrize("levels", [16, 40])
def test_a_diamond_of_embeds_is_walked_once_per_interface(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    levels: int,
) -> None:
    # Each embedded interface was walked again on every path to it: two per
    # level, each embedding both below, doubled the work per level (262,108
    # walks at 16 levels; 40 never finished). Each interface's method set
    # is now walked once, combining its two embeds once each.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    combined: list[int] = []
    combine = go_ifaces._combined

    def counted(
        sets: list[go_ifaces.GoBuildSet], options: list[go_ifaces.GoBuildSet]
    ) -> list[go_ifaces.GoBuildSet]:
        combined.append(1)
        return combine(sets, options)

    monkeypatch.setattr(go_ifaces, "_combined", counted)
    root = _variant_repo(temp_repo / "proj", _diamond(levels))
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    interfaces = 2 * levels
    assert len(combined) <= 2 * interfaces
    top = levels - 1
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert ("proj.p.t.T", f"proj.p.d.A{top}") in implements
    assert ("proj.p.t.T", "proj.p.d.B0") in implements
    assert f"proj.p.t.T.M{top}" in _calls_from(mock_ingestor, "proj.p.call.Call")


# --- a long chain of embeds indexes (Greptile, PR #3360) ----------------------


def _chain(links: int, order: str) -> dict[str, str]:
    # `forward`: I0 embeds I1, declared after it, ... and the last asks for
    # M(); `backward`: each embeds the one declared before it.
    decls = []
    for k in range(links):
        below = k + 1 if order == "forward" else k - 1
        body = "\tM()\n" if below in (-1, links) else f"\tI{below}\n"
        decls.append(f"type I{k} interface {{\n{body}}}\n")
    top = "I0" if order == "forward" else f"I{links - 1}"
    return {
        "p/chain.go": "package p\n\n" + "\n".join(decls),
        "p/t.go": f"package p\n\ntype T struct{{}}\n\nfunc (T) M() {{}}\n\nvar _ {top} = T{{}}\n",
        "p/call.go": f"package p\n\nfunc Call(i {top}) {{\n\ti.M()\n}}\n",
        "other/other.go": "package other\n\nfunc Other() {}\n",
    }


@pytest.mark.parametrize("sync", ["fresh", "incremental"])
@pytest.mark.parametrize("order", ["forward", "backward"])
def test_a_long_chain_of_embeds_indexes(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, order: str, sync: str
) -> None:
    # The method-set walk took three Python frames per embed: a chain of
    # 400 interfaces declared before the ones they embed raised
    # RecursionError, and the whole index failed. The walk keeps its own
    # stack now; each interface's set is combined once, and the generic
    # override walks, quadratic up a chain, leave Go's interfaces to
    # link_go_interfaces.
    from codebase_rag.parsers.class_ingest import method_override

    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    # One chain of 1000 links; the others, 600, are half again what
    # overflowed. The combines counted and the override walks left out of
    # it bound the work whatever the length.
    links = 1000 if (order, sync) == ("forward", "fresh") else 600
    combined: list[int] = []
    combine = go_ifaces._combined

    def counted(
        sets: list[go_ifaces.GoBuildSet], options: list[go_ifaces.GoBuildSet]
    ) -> list[go_ifaces.GoBuildSet]:
        combined.append(1)
        return combine(sets, options)

    walked: list[str] = []
    linearized = method_override._linearized_ancestors

    def linearize(class_qn: str, inheritance: dict[str, list[str]]) -> list[str]:
        walked.append(class_qn)
        return linearized(class_qn, inheritance)

    monkeypatch.setattr(go_ifaces, "_combined", counted)
    monkeypatch.setattr(method_override, "_linearized_ancestors", linearize)
    files = _chain(links, order)
    root = _variant_repo(temp_repo / "proj", files)
    store = _StatefulIngestor()
    updater = _go_updater(store, root)
    started = time.monotonic()
    updater.run(force=True)
    if sync == "incremental":
        cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
        path = root / "other" / "other.go"
        path.write_text(files["other/other.go"] + "// synced\n", encoding="utf-8")
        os.utime(path, (cache_mtime + 1, cache_mtime + 1))
        combined.clear()
        started = time.monotonic()
        _go_updater(store, root).run(force=False)
    elapsed = time.monotonic() - started
    assert len(combined) <= links
    assert [qn for qn in walked if qn.startswith("proj.p.chain.")] == []
    assert elapsed < 60, elapsed
    top = "I0" if order == "forward" else f"I{links - 1}"
    edges = _interface_edges(store)
    assert ("proj.p.t.T", "IMPLEMENTS", f"proj.p.chain.{top}", "heuristic") in edges
    assert ("proj.p.call.Call", "CALLS", "proj.p.t.T.M", "heuristic") in edges


def _cascade_files(count: int) -> dict[str, str]:
    # Greptile's fixture (PR #3360): each `A<i>` declares M; `C0` embeds A0,
    # and each `C<i>` embeds `C<i-1>` and `A<i>`, so `C<i>` holds i + 1
    # declarations of M, and F converts A0 to the last C.
    decls = ["type A0 interface {\n\tM()\n}\n", "type C0 interface {\n\tA0\n}\n"]
    for index in range(1, count):
        decls.append(f"type A{index} interface {{\n\tM()\n}}\n")
        decls.append(f"type C{index} interface {{\n\tC{index - 1}\n\tA{index}\n}}\n")
    return {
        "p/cascade.go": "package p\n\n" + "\n".join(decls),
        "p/use.go": f"package p\n\nfunc F(a A0) C{count - 1} {{\n\treturn a\n}}\n",
        "other/other.go": "package other\n\nfunc Other() {}\n",
    }


def _cascade_work(
    root: Path, files: dict[str, str], sync: str, decided: list[int]
) -> tuple[int, int, float]:
    # The decisions the interface passes made and the links between
    # interface methods the index wrote, fresh or in a sync after a
    # comment-only edit, and how long that run took.
    store = _StatefulIngestor()
    decided.clear()
    started = time.monotonic()
    _go_updater(store, root).run(force=True)
    if sync == "incremental":
        cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
        path = root / "other" / "other.go"
        path.write_text(files["other/other.go"] + "// synced\n", encoding="utf-8")
        os.utime(path, (cache_mtime + 1, cache_mtime + 1))
        decided.clear()
        started = time.monotonic()
        _go_updater(store, root).run(force=False)
    elapsed = time.monotonic() - started
    joins = {
        (source, target)
        for source, kind, target, _ in _interface_edges(store)
        if kind == cs.RelationshipType.OVERRIDES.value
        and source.startswith("proj.p.cascade.A")
        and target.startswith("proj.p.cascade.A")
    }
    return len(decided), len(joins), elapsed


@pytest.mark.parametrize("sync", ["fresh", "incremental"])
def test_a_cascade_of_gathered_declarations_indexes_in_linear_work(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, sync: str
) -> None:
    # Each combining interface checked every pair of declarations gathered
    # below it again and linked them all: n**3/6 decisions and n**2/2
    # OVERRIDES, fresh and in a comment-only sync; 2000 interfaces did not
    # finish linking in 12 s. Now one link per declaration, and the
    # decisions grow as the cascade does.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    decide = go_ifaces.decide
    decided: list[int] = []

    def counted(exprs: Iterable[Expr]) -> bool | None:
        decided.append(1)
        return decide(exprs)

    monkeypatch.setattr(go_ifaces, "decide", counted)
    work = {}
    for count in (150, 300, 2000) if sync == "fresh" else (150, 300):
        files = _cascade_files(count)
        (temp_repo / f"c{count}").mkdir()
        root = _variant_repo(temp_repo / f"c{count}" / "proj", files)
        work[count] = _cascade_work(root, files, sync, decided)
        assert work[count][1] == count - 1, (count, work[count])
    assert work[300][0] <= 3 * max(work[150][0], 1), work
    if sync == "fresh":
        assert work[2000][2] < 60, work


def test_a_long_chain_of_embeds_indexes_with_go_types(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same chain, 600 links, read by the real go/types helper first.
    files = _chain(600, "forward")
    root = _variant_repo(temp_repo / "proj", files)
    _real_gotypes_files_or_skip(root, set(files))
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert implements[("proj.p.t.T", "proj.p.chain.I0")] is None
    assert "proj.p.t.T.M" in _calls_from(mock_ingestor, "proj.p.call.Call")


def test_a_long_chain_into_a_diamond_indexes(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 600 links feeding the bottom of a 12-level diamond. Not handed to the
    # Go toolchain, whose own type checker takes minutes over this shape.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = _chain(600, "forward")
    diamond = _diamond(12)
    files["p/d.go"] = diamond["p/d.go"].replace(
        "type A0 interface{ M0() }", "type A0 interface {\n\tI0\n\tM0()\n}"
    )
    files["p/t.go"] = diamond["p/t.go"] + "\nfunc (T) M() {}\n\nvar _ A11 = T{}\n"
    files["p/call.go"] = "package p\n\nfunc Call(a A11) {\n\ta.M()\n}\n"
    root = _variant_repo(temp_repo / "proj", files)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert ("proj.p.t.T", "proj.p.d.A11") in implements
    assert ("proj.p.t.T", "proj.p.chain.I599") in implements
    assert "proj.p.t.T.M" in _calls_from(mock_ingestor, "proj.p.call.Call")


# Interfaces alone, no type implementing any: each conversion compiles only
# while every declaration of `M` keeps one name (Greptile, PR #3360).
_INTERFACE_ONLY: dict[str, dict[str, str]] = {
    "redeclare": {
        "p/i.go": (
            "package p\n\ntype A interface {\n\tM()\n}\n\n"
            "type B interface {\n\tA\n\tM()\n}\n\n"
            "func F(a A) B {\n\treturn a\n}\n\n"
            "func Use(b B) {\n\tb.M()\n}\n"
        ),
    },
    "chain": {
        "p/i.go": (
            "package p\n\ntype A interface {\n\tM()\n}\n\n"
            "type B interface {\n\tA\n\tM()\n}\n\n"
            "type C interface {\n\tB\n\tM()\n}\n\n"
            "func F(a A) C {\n\treturn a\n}\n"
        ),
    },
    "chain-gap": {
        "p/i.go": (
            "package p\n\ntype A interface {\n\tM()\n}\n\n"
            "type B interface {\n\tA\n}\n\n"
            "type C interface {\n\tB\n\tM()\n}\n\n"
            "func F(a A) C {\n\treturn a\n}\n"
        ),
    },
    "siblings": {
        "p/i.go": (
            "package p\n\ntype B interface {\n\tM()\n}\n\n"
            "type C interface {\n\tM()\n}\n\n"
            "type D interface {\n\tB\n\tC\n}\n\n"
            "func F(b B) D {\n\treturn b\n}\n\n"
            "func G(c C) D {\n\treturn c\n}\n"
        ),
    },
    "diamond-redeclare": {
        "p/i.go": (
            "package p\n\ntype B interface {\n\tM()\n}\n\n"
            "type C interface {\n\tM()\n}\n\n"
            "type D interface {\n\tB\n\tC\n\tM()\n}\n\n"
            "func F(c C) D {\n\treturn c\n}\n"
        ),
    },
    "diamond": {
        "p/i.go": (
            "package p\n\ntype A interface {\n\tM()\n}\n\n"
            "type B interface {\n\tA\n\tM()\n}\n\n"
            "type C interface {\n\tA\n\tM()\n}\n\n"
            "type D interface {\n\tB\n\tC\n\tM()\n}\n\n"
            "func F(a A) D {\n\treturn a\n}\n"
        ),
    },
    # Greptile's cascade, small: renaming one A renames every A.
    "cascade": {
        "p/i.go": _cascade_files(5)["p/cascade.go"]
        + "\nfunc F(a A0) C4 {\n\treturn a\n}\n",
    },
    "per-build": {
        "p/a_linux.go": "package p\n\ntype A interface {\n\tM()\n}\n",
        "p/a_other.go": (
            "//go:build !linux\n\npackage p\n\ntype A interface {\n\tM()\n}\n"
        ),
        "p/b.go": (
            "package p\n\ntype B interface {\n\tA\n\tM()\n}\n\n"
            "func F(a A) B {\n\treturn a\n}\n"
        ),
    },
}


# Per frontend, each rename: the shape, in a package of its own, and the
# declaration renamed.
_INTERFACE_ONLY_RENAMES = {
    "treesitter": (
        ("redeclare", "i.A.M"),
        ("redeclare", "i.B.M"),
        ("chain", "i.C.M"),
        ("chain-gap", "i.A.M"),
        ("siblings", "i.B.M"),
        ("diamond-redeclare", "i.C.M"),
        ("diamond", "i.A.M"),
        ("cascade", "i.A2.M"),
        ("per-build", "a_other.A.M"),
    ),
    "gotypes": (("redeclare", "i.A.M"), ("per-build", "a_other.A.M")),
}


@pytest.mark.parametrize("frontend", ["treesitter", "gotypes"])
def test_an_interface_only_hierarchy_renames_every_declaration(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    frontend: str,
) -> None:
    # The generic override walk linked `B.M` to `A.M` where B embeds A and
    # declares M again; leaving Go's interfaces out of it (for the chain's
    # sake) left B.M behind a rename of A.M with no type to tie them, and F
    # stopped compiling. Siblings two embeds bring together, and every
    # build's declaration of an embed, were never all linked. Each rename
    # is in a package of its own, so one index serves them all and one
    # `go vet` per GOOS checks them all.
    monkeypatch.setattr(
        gu.settings,
        "GO_FRONTEND",
        cs.GoFrontend.GOTYPES if frontend == "gotypes" else cs.GoFrontend.TREESITTER,
    )
    renames = _INTERFACE_ONLY_RENAMES[frontend]
    files = {
        f"r{index}/{rel.removeprefix('p/')}": text.replace(
            "package p\n", f"package r{index}\n", 1
        )
        for index, (shape, _) in enumerate(renames)
        for rel, text in _INTERFACE_ONLY[shape].items()
    }
    root = _variant_repo(temp_repo / "proj", files)
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)

    for index, (shape, target) in enumerate(renames):
        report = rename(
            root, _query(graph), graph.project, f"proj.r{index}.{target}", "N"
        )
        assert report.applied, (shape, target, report.message)
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8")
        assert "M()" not in text, (rel, text)
    _go_checks_after(root, before)


def test_a_rename_hierarchy_of_many_members_is_collected_linearly() -> None:
    # The rename hierarchy asked a list whether it had seen each member:
    # quadratic in a hierarchy as long as a long chain's. 50,000 members,
    # each overriding the next.
    from codebase_rag import cypher_queries as cq
    from codebase_rag.editing.sites import hierarchy

    members = 50_000

    def fetch_all(query: str, params: dict | None) -> list[dict]:
        if query != cq.CYPHER_GRAPH_OVERRIDES:
            return []
        k = int(str((params or {})[cs.KEY_QN]).rpartition("m")[2])
        return [
            {cs.KEY_QUALIFIED_NAME: f"proj.p.m{other}", cs.KEY_RESOLUTION: None}
            for other in (k - 1, k + 1)
            if 0 <= other < members
        ]

    started = time.monotonic()
    found = hierarchy(fetch_all, "proj", "proj.p.m0")
    assert time.monotonic() - started < 10
    assert len(found.members) == members
    assert found.guessed == frozenset()


# --- a license comment before the build line (Greptile, PR #3360) ------------

_LICENSES = {
    "block": "/* Copyright 2026 The Authors. */\n\n",
    "multi-line-block": "/*\n * Copyright 2026 The Authors.\n * SPDX-License-Identifier: MIT\n */\n\n",
    "line-then-block": "// Code by the Authors.\n/* Copyright 2026 */\n\n",
}


@pytest.mark.parametrize("license_style", sorted(_LICENSES))
def test_a_license_comment_does_not_hide_the_build_line(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    license_style: str,
) -> None:
    # The reader stopped at the license's `/*` line: both Getters seemed to
    # build everywhere, a redeclaration on Windows, so J's embed was
    # ambiguous and T lost its pairs.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    head = _LICENSES[license_style]
    files = {
        "p/getter_a.go": f"{head}//go:build linux\n\npackage p\n\n"
        "type Getter interface {\n\tGet() int\n}\n",
        "p/getter_b.go": f"{head}//go:build !linux\n\npackage p\n\n"
        "type Getter interface {\n\tGet() string\n}\n",
        "p/j.go": "package p\n\ntype J interface {\n\tGetter\n\tN()\n}\n",
        "p/t.go": f"{head}//go:build linux\n\npackage p\n\ntype T struct{{}}\n\n"
        "func (T) Get() int { return 0 }\n\nfunc (T) N() {}\n\nvar _ J = T{}\n",
    }
    root = _variant_repo(temp_repo / "proj", files)
    _go_checks_on(root)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    assert ("proj.p.t.T", "proj.p.getter_a.Getter") in implements
    assert ("proj.p.t.T", "proj.p.j.J") in implements
    assert ("proj.p.t.T", "proj.p.getter_b.Getter") not in implements


# --- past the alternatives cap the method set is unknown (Greptile, PR #3360) -


def _tagged_embeds(count: int) -> dict[str, str]:
    # E1..En each ask for A<k>() under tag t<k> and B<k>() without it: J has
    # 2**n method sets, and the build without tags asks for every B<k>.
    files: dict[str, str] = {}
    for index in range(1, count + 1):
        for header, method in ((f"t{index}", "A"), (f"!t{index}", "B")):
            name = "a" if method == "A" else "b"
            files[f"p/e{index}{name}.go"] = (
                f"//go:build {header}\n\npackage p\n\n"
                f"type E{index} interface {{\n\t{method}{index}()\n}}\n"
            )
    embeds = "".join(f"\tE{index}\n" for index in range(1, count + 1))
    files["p/j.go"] = f"package p\n\ntype J interface {{\n{embeds}\tN()\n}}\n"
    methods = "".join(f"func (T) B{index}() {{}}\n" for index in range(1, count + 1))
    files["p/t.go"] = f"package p\n\ntype T struct{{}}\n\n{methods}func (T) N() {{}}\n"
    files["p/u.go"] = "package p\n\ntype U struct{}\n\nfunc (U) N() {}\n"
    files["p/run.go"] = "package p\n\nfunc Run(j J) { j.N() }\n"
    return files


@pytest.mark.parametrize("count", [6, 7])
def test_an_interface_past_the_alternatives_cap_links_nothing(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    # Past 64 method sets J fell back to the methods every set shares, {N}:
    # U, with N alone, got IMPLEMENTS, OVERRIDES and dispatch, and a guessed
    # rename of U.N rewrote J's N. Past the cap the set is unknown now; at
    # 64 (six embeds) it still resolves, and T, a J in the build without
    # tags, is linked while U is not.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = _tagged_embeds(count)
    root = _variant_repo(temp_repo / "proj", files)
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    calls = _calls_from(mock_ingestor, "proj.p.run.Run")
    assert ("proj.p.u.U", "proj.p.j.J") not in implements
    assert ("proj.p.u.U.N", "proj.p.j.J.N") not in overrides
    assert "proj.p.u.U.N" not in calls
    linked = count == 6
    assert (("proj.p.t.T", "proj.p.j.J") in implements) is linked
    assert ("proj.p.t.T.N" in calls) is linked

    # Renaming U.N rewrote J.N on a guess. At 64 sets U is no J and renames
    # alone; past them nothing tells whether U is a J, so the rename refuses
    # (see the tests below).
    query = _query(graph)
    if linked:
        report = rename(
            root, query, graph.project, "proj.p.u.U.N", "M", allow_heuristic=True
        )
        assert report.applied, report.message
    else:
        with pytest.raises(RenameRefused, match=r"proj\.p\.j\.J"):
            rename(
                root, query, graph.project, "proj.p.u.U.N", "M", allow_heuristic=True
            )
    assert (root / "p" / "j.go").read_text(encoding="utf-8") == files["p/j.go"]
    _go_checks_after(root, before)


def _changed(root: Path, files: dict[str, str]) -> list[str]:
    return [
        rel
        for rel, text in files.items()
        if (root / rel).read_text(encoding="utf-8") != text
    ]


def _unknown_marks(store: _StatefulIngestor) -> dict[str, object]:
    return {
        str(uid): props.get(cs.KEY_IMPLEMENTERS_UNKNOWN)
        for (label, uid), props in store.nodes.items()
        if label == cs.NodeLabel.INTERFACE
    }


@pytest.mark.parametrize("opt_in", [False, True])
@pytest.mark.parametrize("target", ["proj.p.j.J.N", "proj.p.t.T.N", "proj.p.u.U.N"])
def test_a_name_an_interface_of_unknown_implementers_declares_is_never_renamed(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    opt_in: bool,
) -> None:
    # Past the cap J links nothing, so the hierarchy showed nothing to take
    # along: renaming T.N left J.N and `var _ J = T{}` stopped compiling;
    # renaming J.N left T.N. Whichever method of that name is asked for,
    # with or without the opt-in, the rename refuses, names J and says why,
    # and writes nothing.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {**_tagged_embeds(7), "p/use.go": "package p\n\nvar _ J = T{}\n"}
    root = _variant_repo(temp_repo / "proj", files)
    _go_checks_on(root)
    graph = _rename_index(root, mock_ingestor)
    assert graph.nodes["proj.p.j.J"][cs.KEY_IMPLEMENTERS_UNKNOWN] == "builds"
    query = _query(graph)
    with pytest.raises(RenameRefused) as refused:
        rename(root, query, graph.project, target, "M", allow_heuristic=opt_in)
    assert "proj.p.j.J" in str(refused.value)
    assert "differs between builds" in str(refused.value)
    assert _changed(root, files) == []


@pytest.mark.parametrize(
    ("count", "target"), [(6, "proj.p.t.T.N"), (7, "proj.p.t.T.B1")]
)
def test_a_name_no_interface_of_unknown_implementers_declares_still_renames(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    target: str,
) -> None:
    # Negatives: at 64 sets J is known and T.N renames with J.N; past them
    # T.B1 still renames, since E1, which declares B1, is known and J
    # embeds it. `go build` and `go vet` pass after either rename.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {**_tagged_embeds(count), "p/use.go": "package p\n\nvar _ J = T{}\n"}
    root = _variant_repo(temp_repo / "proj", files)
    graph = _rename_index(root, mock_ingestor)
    report = rename(
        root, _query(graph), graph.project, target, "Renamed", allow_heuristic=True
    )
    assert report.applied, report.message
    assert "Renamed()" in (root / "p" / "t.go").read_text(encoding="utf-8")
    _go_checks_on(root)


UNRESOLVED_EMBED_FILES = {
    # K embeds io.Closer, which cgr does not read: K's method set is unknown.
    "q/k.go": 'package q\n\nimport "io"\n\ntype K interface {\n\tio.Closer\n\tn()\n}\n',
    "q/v.go": (
        "package q\n\ntype V struct{}\n\nfunc (V) n() {}\n\n"
        "func (V) Close() error { return nil }\n\nvar _ K = V{}\n"
    ),
    # Another package's `n` is another name: no K is satisfied through it.
    "r/w.go": "package r\n\ntype W struct{}\n\nfunc (W) n() {}\n\nfunc use() { W{}.n() }\n",
}


@pytest.mark.parametrize("opt_in", [False, True])
def test_an_unexported_name_refuses_only_in_its_interfaces_package(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    opt_in: bool,
) -> None:
    # On 44784617d renaming V.n left K.n, and `var _ K = V{}` broke.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _variant_repo(temp_repo / "proj", UNRESOLVED_EMBED_FILES)
    before = _go_sources(root)
    graph = _rename_index(root, mock_ingestor)
    assert graph.nodes["proj.q.k.K"][cs.KEY_IMPLEMENTERS_UNKNOWN] == "embeds"
    query = _query(graph)
    with pytest.raises(RenameRefused, match="embeds an interface cgr cannot resolve"):
        rename(root, query, graph.project, "proj.q.v.V.n", "m", allow_heuristic=opt_in)
    assert _changed(root, UNRESOLVED_EMBED_FILES) == []
    report = rename(
        root, query, graph.project, "proj.r.w.W.n", "m", allow_heuristic=opt_in
    )
    assert report.applied, report.message
    assert "func (W) m()" in (root / "r" / "w.go").read_text(encoding="utf-8")
    _go_checks_after(root, before)


# Thirteen clauses, each tag but the ends both asked for and ruled out: no
# tags satisfy it, and twelve tags are more than cgr enumerates.
UNDECIDED_HEADER = "//go:build " + " && ".join(
    f"(t{k} || !t{k + 1})" for k in range(13)
)
UNDECIDED_FILES = {
    "p/i.go": "package p\n\ntype I interface{ M() }\n",
    "p/t.go": "package p\n\ntype T struct{}\n\nfunc (T) M() {}\n\nfunc (T) Other() {}\n",
    "p/v.go": f"{UNDECIDED_HEADER}\n\npackage p\n\ntype V struct{{}}\n\nfunc (V) M() {{}}\n",
    "p/use.go": "package p\n\nvar _ I = T{}\n\nvar _ I = V{}\n",
}


@pytest.mark.parametrize("target", ["proj.p.i.I.M", "proj.p.t.T.M", "proj.p.v.V.M"])
def test_an_interface_a_type_may_implement_under_an_undecided_constraint_refuses(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    # Whether V's build holds V with I is undecided, so V gets no edge. On
    # 44784617d renaming I.M took T.M along, left V.M, and `var _ I = V{}`
    # broke.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _variant_repo(temp_repo / "proj", UNDECIDED_FILES)
    _go_checks_on(root)
    graph = _rename_index(root, mock_ingestor)
    assert graph.nodes["proj.p.i.I"][cs.KEY_IMPLEMENTERS_UNKNOWN] == "constraint"
    query = _query(graph)
    with pytest.raises(RenameRefused, match="build constraint cgr cannot read"):
        rename(root, query, graph.project, target, "N", allow_heuristic=True)
    assert _changed(root, UNDECIDED_FILES) == []


def test_a_name_the_undecided_interface_does_not_declare_still_renames(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _variant_repo(temp_repo / "proj", UNDECIDED_FILES)
    graph = _rename_index(root, mock_ingestor)
    report = rename(root, _query(graph), graph.project, "proj.p.t.T.Other", "Else")
    assert report.applied, report.message
    _go_checks_on(root)


@pytest.mark.parametrize("sync", ["fresh", "reused"])
@pytest.mark.parametrize("edit", ["other", "interface"])
def test_a_sync_marks_unknown_implementers_as_a_clean_index_does(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, sync: str, edit: str
) -> None:
    # J read back from the graph keeps its mark; J edited to embed six
    # interfaces loses it.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    files = {
        **_tagged_embeds(7),
        "other/other.go": "package other\n\nfunc Other() {}\n",
    }
    root = _variant_repo(temp_repo / "synced", files)
    store = _StatefulIngestor()
    first = _go_updater(store, root)
    first.run(force=True)
    assert _unknown_marks(store)["proj.p.j.J"] == "builds"
    assert _unknown_marks(store)["proj.p.e1b.E1"] == ""
    edited = dict(files)
    if edit == "other":
        edited["other/other.go"] = files["other/other.go"] + "// synced\n"
    else:
        edited["p/j.go"] = files["p/j.go"].replace("\tE7\n", "")
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    for rel, text in edited.items():
        if text != files[rel]:
            (root / rel).write_text(text, encoding="utf-8")
            os.utime(root / rel, (cache_mtime + 1, cache_mtime + 1))
    (first if sync == "reused" else _go_updater(store, root)).run(force=False)
    clean = _StatefulIngestor()
    _go_updater(clean, _variant_repo(temp_repo / "clean", edited)).run(force=True)
    assert _unknown_marks(store) == _unknown_marks(clean)
    assert _unknown_marks(store)["proj.p.j.J"] == ("builds" if edit == "other" else "")


def test_the_implementers_unknown_mark_travels_in_a_protobuf_export(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A graph imported from the export must refuse the same renames.
    from codebase_rag.services.protobuf_service import ProtobufFileIngestor
    from codec import schema_pb2 as pb

    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    root = _variant_repo(temp_repo / "proj", _tagged_embeds(7))
    out = temp_repo / "export"
    out.mkdir()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ProtobufFileIngestor(str(out), split_index=False),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
        skip_embeddings=True,
    ).run(force=True)
    index = pb.GraphCodeIndex()
    index.ParseFromString((out / cs.PROTOBUF_INDEX_FILE).read_bytes())
    marks = {
        node.interface_node.qualified_name: node.interface_node.implementers_unknown
        for node in index.nodes
        if node.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF) == "interface_node"
    }
    assert marks["proj.p.j.J"] == "builds"
    assert marks["proj.p.e1b.E1"] == ""


@pytest.mark.parametrize(
    ("count", "goos"), [(6, "host"), (7, "host"), (7, "darwin"), (7, "windows")]
)
def test_a_pair_go_types_proves_on_an_interface_of_many_method_sets_is_a_guess(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    goos: str,
) -> None:
    # go/types proves T a J in the one build it reads, the one without tags,
    # and J asks for other methods in every other build. Past the cap the
    # proven pair stayed the compiler's word for every build, exact, and so
    # did T.N's OVERRIDES and Run's dispatch to it. At the cap the names
    # tell where it holds; past it nothing is known: a guess either way.
    # No file names a GOOS: whichever build go/types reads, it proves T.
    # Without the toolchain nothing is proven and, past the cap, nothing is
    # linked at all (macOS CI has no Go).
    if goos != "host":
        monkeypatch.setenv("GOOS", goos)
    files = _tagged_embeds(count)
    root = _variant_repo(temp_repo / "proj", files)
    # The build without tags reads every file but the `t<k>` ones.
    _real_gotypes_files_or_skip(
        root, {rel for rel, text in files.items() if "//go:build t" not in text}
    )
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    implements = _labels(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    overrides = _labels(mock_ingestor, cs.RelationshipType.OVERRIDES)
    calls = _calls_from(mock_ingestor, "proj.p.run.Run")
    heuristic = cs.EdgeResolution.HEURISTIC
    assert implements[("proj.p.t.T", "proj.p.j.J")] == heuristic
    assert overrides[("proj.p.t.T.N", "proj.p.j.J.N")] == heuristic
    assert calls["proj.p.t.T.N"] == heuristic
    assert ("proj.p.u.U", "proj.p.j.J") not in implements
    assert "proj.p.u.U.N" not in calls


def test_a_pair_resting_on_the_build_go_types_read_survives_many_tags(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Windows J embeds an I asking for `M() string`, which T's `M() int`
    # is not, yet go/types proved T a J: where the pair holds rests on the
    # build go/types read, which rules out the tag of each `!t<k>` file it
    # type-checked. Twelve such tags were more than a decision enumerates,
    # so that was unknown and Run's dispatch to T.N was dropped. A tag only
    # ever ruled out is unset in the one build that matters, and is not
    # enumerated.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    files = {
        "p/i.go": "//go:build !windows\n\npackage p\n\ntype I interface{ M() int }\n",
        "p/i_windows.go": "package p\n\ntype I interface{ M() string }\n",
        "p/t.go": (
            "package p\n\ntype T struct{}\n\nfunc (T) M() int { return 0 }\n\n"
            "func (T) N() {}\n"
        ),
        "p/j.go": "package p\n\ntype J interface {\n\tI\n\tN()\n}\n",
        "p/run.go": "package p\n\nfunc Run(j J) { j.N() }\n",
        **{f"p/y{k}.go": f"//go:build !t{k}\n\npackage p\n" for k in range(12)},
    }
    root = _variant_repo(temp_repo / "proj", files)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="go")
    assert ("proj.p.t.T", "proj.p.j.J") in _labels(
        mock_ingestor, cs.RelationshipType.IMPLEMENTS
    )
    assert "proj.p.t.T.N" in _calls_from(mock_ingestor, "proj.p.run.Run")
