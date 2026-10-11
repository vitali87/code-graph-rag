"""Go implicit interface satisfaction on the tree-sitter path (issue #2483).

A Go type implements an interface by having its methods, with no clause
naming it. The graph used to record none of that: the interface's method set
had no nodes, no type IMPLEMENTS any interface, and `s.Area()` through a
`Shape` bound to one implementation picked by name, leaving the others with no
caller. Every case indexes a small module through the real parsers with the
go/types frontend off, which is what most installs run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.checkout_state import state_file
from codebase_rag.editing.rename import RenameRefused, rename
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.go_frontend import GoCallSite, GoImplements, GoSemanticFacts
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from codebase_rag.tests.test_rename_op import _index as _rename_index
from codebase_rag.tests.test_rename_op import _write
from evals.cgr_graph import _StatefulIngestor

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
    # Either implementation can run, so each is one candidate of several.
    assert calls.get(f"{shapes}.Square.Area") == cs.EdgeResolution.OVERLOAD, calls
    assert calls.get(f"{shapes}.Circle.Area") == cs.EdgeResolution.OVERLOAD, calls


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
        call_sites=call_sites, external_sites=set(), implements=implements
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
) -> set[tuple[str, str, str | None]]:
    return {
        (str(key[1]), str(key[4]), props.get(cs.KEY_RESOLUTION))
        for key, props in store.edge_props.items()
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
    with pytest.raises(RenameRefused):
        # `s.Area()` reaches each implementation as one candidate of several;
        # rewriting a shared call site is the opt-in it is for a Protocol.
        rename(temp_repo, graph.fetch_all, graph.project, f"{pkg}.Square.Area", "Size")
    assert (temp_repo / "shapes.go").read_text(encoding="utf-8") == RENAMED

    report = rename(
        temp_repo,
        graph.fetch_all,
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
