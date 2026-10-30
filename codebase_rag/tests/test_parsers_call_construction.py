"""`CallConstruction`: the edges a call bound to a class writes. Each class
variant is INSTANTIATED and the constructors the language runs are called
(a declared Java one, a PHP class's inherited `__construct`), or the
constructor member is handed back for the call's own edge (Python's
`__init__`); a Julia function named like the type shadows it. One target
keeps the call node's verdict, several mark the call overloaded."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.call_construction import CallConstruction
from codebase_rag.parsers.call_processor import CallProcessor
from codebase_rag.parsers.language_calls import CallScan
from codebase_rag.tests.collaborator_support import index, parse
from evals.recorded_graph import RecordingIngestor

pytestmark = pytest.mark.usefixtures("fingerprint_inputs_once_per_session")

_L = cs.SupportedLanguage
_FILES = {
    "m.py": (
        "class Box:\n    def __init__(self):\n        pass\n\n"
        "class Bare:\n    pass\n\n"
        "class Dup:\n    pass\n\n"
        "class Dup:\n    pass\n\n"
        "def make():\n    Box()\n\n"
        "class Twin:\n    pass\n\n"
        "def Twin():\n    pass\n"
    ),
    "Shop.java": (
        "class Item {\n  Item() {}\n  Item(int n) {}\n}\n"
        "class One {\n  One() {}\n}\n"
        "class Shop {\n  void run() { new Item(); new One(); }\n}\n"
    ),
    "a.php": (
        "<?php\nclass Base { public function __construct() {} }\n"
        "class Child extends Base {}\n"
        "function mk() { return new Child(); }\n"
    ),
    "j.jl": (
        "struct P\n  v\n  P() = new(1)\nend\n"
        "struct Q\n  v\nend\n"
        "Q(x, y) = Q(x)\n"
        "function go()\n  P()\n  Q(1, 2)\nend\n"
    ),
}
_LANGUAGES = {
    "m.py": _L.PYTHON,
    "Shop.java": _L.JAVA,
    "a.php": _L.PHP,
    "j.jl": _L.JULIA,
}


@dataclass
class _Written:
    caller_spec: tuple[str, str, str]
    edges: list[tuple[str, str, str]] = field(default_factory=list)

    def __call__(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: object = None,
    ) -> None:
        assert from_spec == self.caller_spec
        self.edges.append((str(rel_type), str(to_spec[0]), to_spec[2]))


@pytest.fixture
def processor(temp_repo: Path, mock_ingestor: RecordingIngestor) -> CallProcessor:
    updater = index(temp_repo / "proj", mock_ingestor, _FILES)
    processor = updater.factory.call_processor
    assert isinstance(processor.construction, CallConstruction)
    # The verdict the scan noted for the call node being bound; a
    # construction with one target carries it on.
    processor.verdict = cs.EdgeResolution.HEURISTIC
    processor.resolution = cs.EdgeResolution.EXACT
    return processor


def _construct(
    processor: CallProcessor,
    rel_file: str,
    class_qn: str,
    ctor_rel: cs.RelationshipType = cs.RelationshipType.CALLS,
) -> tuple[tuple[str, str] | None, list[tuple[str, str, str]]]:
    # The scan of the file's module-level calls, as the call pass builds it
    # for the call node whose callee resolved to `class_qn`.
    language = _LANGUAGES[rel_file]
    module_qn = f"proj.{Path(rel_file).stem}"
    caller_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
    written = _Written(caller_spec)
    ctx = CallScan(
        caller_node=parse(language, _FILES[rel_file]),
        caller_qn=module_qn,
        caller_spec=caller_spec,
        module_qn=module_qn,
        language=language,
        queries={},
        calls=processor.calls_for(language),
        class_context=None,
        call_name_cache=None,
        local_var_types=None,
        span_bindings=[],
        caller_params=frozenset(),
        cpp_template_params=frozenset(),
        resolve_func=processor._resolve_noting,
        resolve_builtin=None,
        resolve_cpp_op=None,
        ensure_rel=written,
        arg_ref_rel=cs.RelationshipType.CALLS,
    )
    redirected = processor.construction.redirect_construction(ctx, class_qn, ctor_rel)
    return redirected, written.edges


def test_a_python_class_hands_back_its_init_for_the_calls_edge(
    processor: CallProcessor,
) -> None:
    redirected, edges = _construct(processor, "m.py", "proj.m.Box")
    assert redirected == (cs.NodeLabel.METHOD, "proj.m.Box.__init__")
    assert edges == [("INSTANTIATES", "Class", "proj.m.Box")]
    assert processor.resolution == cs.EdgeResolution.HEURISTIC


def test_a_class_with_no_init_is_only_instantiated(processor: CallProcessor) -> None:
    redirected, edges = _construct(processor, "m.py", "proj.m.Bare")
    assert redirected is None
    assert edges == [("INSTANTIATES", "Class", "proj.m.Bare")]


def test_every_variant_of_a_redefined_class_is_instantiated_as_an_overload(
    processor: CallProcessor,
) -> None:
    _redirected, edges = _construct(processor, "m.py", "proj.m.Dup")
    assert edges == [
        ("INSTANTIATES", "Class", "proj.m.Dup"),
        ("INSTANTIATES", "Class", "proj.m.Dup@11"),
    ]
    assert processor.resolution == cs.EdgeResolution.OVERLOAD


def test_a_java_construction_calls_every_declared_constructor(
    processor: CallProcessor,
) -> None:
    redirected, edges = _construct(processor, "Shop.java", "proj.Shop.Item")
    # Written here, so the call's own edge has nothing left to go to.
    assert redirected is None
    assert edges == [
        ("INSTANTIATES", "Class", "proj.Shop.Item"),
        ("CALLS", "Method", "proj.Shop.Item.Item()"),
        ("CALLS", "Method", "proj.Shop.Item.Item(int)"),
    ]
    # No argument-type selection: one call, several candidates (#1526).
    assert processor.resolution == cs.EdgeResolution.OVERLOAD


def test_a_single_java_constructor_keeps_the_verdict(processor: CallProcessor) -> None:
    _redirected, edges = _construct(processor, "Shop.java", "proj.Shop.One")
    assert edges == [
        ("INSTANTIATES", "Class", "proj.Shop.One"),
        ("CALLS", "Method", "proj.Shop.One.One()"),
    ]
    assert processor.resolution == cs.EdgeResolution.HEURISTIC


def test_a_constructor_reference_takes_the_relationship_it_is_given(
    processor: CallProcessor,
) -> None:
    # A Java `One::new` runs no constructor yet, so it references one.
    _redirected, edges = _construct(
        processor, "Shop.java", "proj.Shop.One", cs.RelationshipType.REFERENCES
    )
    assert ("REFERENCES", "Method", "proj.Shop.One.One()") in edges
    assert ("CALLS", "Method", "proj.Shop.One.One()") not in edges


def test_a_php_class_runs_the_constructor_it_inherits(processor: CallProcessor) -> None:
    redirected, edges = _construct(processor, "a.php", "proj.a.Child")
    assert redirected == (cs.NodeLabel.METHOD, "proj.a.Base.__construct")
    assert edges == [("INSTANTIATES", "Class", "proj.a.Child")]


def test_a_julia_type_runs_its_inner_constructor(processor: CallProcessor) -> None:
    redirected, edges = _construct(processor, "j.jl", "proj.j.P")
    assert redirected is None
    assert edges == [
        ("INSTANTIATES", "Class", "proj.j.P"),
        ("CALLS", "Function", "proj.j.P.P"),
    ]


def test_a_julia_function_named_like_its_type_shadows_it(
    processor: CallProcessor,
) -> None:
    redirected, edges = _construct(processor, "j.jl", "proj.j.Q")
    # The call dispatches in the function namespace: the outer method is
    # called and the type is not instantiated.
    assert redirected is None
    assert edges == [("CALLS", "Function", "proj.j.Q@8")]
    assert processor.resolution == cs.EdgeResolution.HEURISTIC


def test_a_same_named_variant_of_another_kind_is_not_instantiated(
    processor: CallProcessor,
) -> None:
    _redirected, edges = _construct(processor, "m.py", "proj.m.Twin")
    # An INSTANTIATES edge to a Function would be a phantom the database
    # drops (issue #652); the language writes the twin's own edge instead.
    assert edges == [
        ("INSTANTIATES", "Class", "proj.m.Twin"),
        ("CALLS", "Function", "proj.m.Twin@20"),
    ]
    assert processor.resolution == cs.EdgeResolution.OVERLOAD
