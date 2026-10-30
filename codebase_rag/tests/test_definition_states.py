"""The definition pass's shared state: `DefinitionContext` and the six
`<language>/definition_state.py` containers.

Apart from Go's `link_state` view they are bare records, so what is pinned
is what they promise their readers: every collaborator holds the
processor's own objects, which are refilled in place and never rebound, so
a write through any one reaches every reader. Their fields and empty
defaults are not pinned on their own; `LanguageStates`, a bare tuple, is
covered through the `DefinitionProcessor` that builds and hands it out."""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp.definition_state import CppDefinitionState
from codebase_rag.parsers.csharp.class_ingest import CSharpClassIngest
from codebase_rag.parsers.csharp.definition_state import CSharpDefinitionState
from codebase_rag.parsers.dart.definition_state import DartDefinitionState
from codebase_rag.parsers.definition_context import DefinitionContext
from codebase_rag.parsers.definition_facts import DefinitionFacts
from codebase_rag.parsers.definition_processor import DefinitionProcessor
from codebase_rag.parsers.go.builds import GoBuilds
from codebase_rag.parsers.go.definition_state import GoDefinitionState
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.java.definition_state import JavaDefinitionState
from codebase_rag.parsers.rust.definition_state import RustDefinitionState
from codebase_rag.tests.collaborator_support import first, parse
from evals.recorded_graph import RecordingIngestor

_State = (
    CppDefinitionState
    | CSharpDefinitionState
    | DartDefinitionState
    | GoDefinitionState
    | JavaDefinitionState
    | RustDefinitionState
)
_STATES = [
    CppDefinitionState,
    CSharpDefinitionState,
    DartDefinitionState,
    JavaDefinitionState,
    RustDefinitionState,
    GoDefinitionState,
]


def _state(kind: type) -> _State:
    if kind is GoDefinitionState:
        return GoDefinitionState(GoBuilds(Path("/repo")))
    return kind()


def _processor(repo: Path) -> DefinitionProcessor:
    registry = FunctionRegistryTrie()
    ingestor = RecordingIngestor()
    return DefinitionProcessor(
        ingestor=ingestor,
        repo_path=repo,
        project_name="proj",
        function_registry=registry,
        simple_name_lookup=defaultdict(set),
        import_processor=ImportProcessor(
            repo, "proj", ingestor=ingestor, function_registry=registry
        ),
        module_qn_to_file_path={},
    )


@pytest.mark.parametrize("kind", _STATES, ids=lambda kind: kind.__name__)
def test_a_states_containers_cannot_be_rebound(kind: type) -> None:
    # Collaborators hold the containers themselves, not the state: a rebound
    # field would leave every one of them reading the old object.
    state = _state(kind)
    for field in dataclasses.fields(state):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(state, field.name, getattr(state, field.name))


@pytest.mark.parametrize("kind", _STATES, ids=lambda kind: kind.__name__)
def test_two_states_never_share_a_container(kind: type) -> None:
    # Two processors in one process (two updaters) must never record into
    # one container.
    first, second = _state(kind), _state(kind)
    for field in dataclasses.fields(first):
        if field.default_factory is not dataclasses.MISSING:
            assert getattr(first, field.name) is not getattr(second, field.name)


def test_the_go_link_state_is_a_view_of_the_states_own_containers() -> None:
    state = GoDefinitionState(GoBuilds(Path("/repo")))
    variants: dict[str, tuple[str, ...]] = {}
    view = state.link_state(variants)
    assert view.methods is state.methods
    assert view.builtin_shadows is state.builtin_shadows
    assert view.variant_methods is state.variant_methods
    assert view.pair_builds is state.pair_builds
    assert view.builds is state.builds
    assert view.declaration_variants is variants
    # Call resolution labels a pair heuristic from this set, so it is the
    # name-matched pairs alone, never the go/types proofs.
    assert view.guessed_implementations is state.guessed_pairs
    state.proven_pairs.add(("proj.I", "proj.Proven"))
    state.guessed_pairs.add(("proj.I", "proj.Guessed"))
    assert view.guessed_implementations == {("proj.I", "proj.Guessed")}


def test_every_collaborator_holds_the_processors_own_state(tmp_path: Path) -> None:
    processor = _processor(tmp_path)
    classes, functions = processor.class_ingest, processor.function_ingest
    assert classes.csharp.methods is processor.csharp.methods
    assert classes.csharp.type_locations is processor.csharp.type_locations
    assert classes.cpp.module_interfaces is processor.cpp.module_interfaces
    assert functions.cpp.cpp_definition_spans is processor.cpp.definition_spans
    assert classes.rust.impl_method_traits is processor.rust.impl_method_traits
    assert classes.go.interfaces is processor.go.interfaces
    # The method pass records Dart overrides through the processor; the Dart
    # class collaborator judges them from its own reference.
    assert classes.dart.annotated_overrides is processor.dart.annotated_overrides
    # Both Java collaborators record into one list.
    assert classes.java.anon_overrides is functions.java.anon_overrides
    assert classes.java.anon_overrides is processor.java.anon_overrides
    assert processor.go_link_state().declaration_variants is (
        processor.facts.go_declaration_variants
    )


def test_every_collaborator_reads_the_processors_own_context(tmp_path: Path) -> None:
    processor = _processor(tmp_path)
    for collaborator in (processor.class_ingest, processor.class_ingest.csharp):
        assert collaborator.function_registry is processor.function_registry
        assert collaborator.facts is processor.facts
    assert processor.class_ingest.ingestor is processor.ingestor
    assert processor.class_ingest.import_processor is processor.import_processor


def test_a_write_through_a_collaborator_reaches_the_processor(tmp_path: Path) -> None:
    processor = _processor(tmp_path)
    _, queries = load_parsers()
    processor.class_ingest.ingest_classes_and_methods(
        parse(cs.SupportedLanguage.CSHARP, "class A { public override void M() {} }"),
        "proj.a",
        cs.SupportedLanguage.CSHARP,
        queries,
    )
    assert processor.csharp.override_methods == {"proj.a.A.M"}
    assert processor.function_registry.get("proj.a.A") == cs.NodeLabel.CLASS


def test_a_new_run_empties_a_shared_set_without_replacing_it(tmp_path: Path) -> None:
    processor = _processor(tmp_path)
    held = processor.class_ingest.cpp.interfaces_parsed_this_run
    held.add("proj.shapes.core")
    processor.begin_run()
    assert processor.cpp.interfaces_parsed_this_run is held
    assert held == set()


def test_a_collaborator_writes_into_the_context_and_state_it_is_given(
    tmp_path: Path,
) -> None:
    registry = FunctionRegistryTrie()
    ingestor = RecordingIngestor()
    facts = DefinitionFacts()
    state = CSharpDefinitionState()
    ingest = CSharpClassIngest(
        DefinitionContext(
            ingestor=ingestor,
            repo_path=tmp_path,
            project_name="proj",
            function_registry=registry,
            simple_name_lookup=defaultdict(set),
            module_qn_to_file_path={},
            import_processor=ImportProcessor(
                tmp_path, "proj", ingestor=ingestor, function_registry=registry
            ),
            facts=facts,
        ),
        state,
    )
    node = first(
        parse(
            cs.SupportedLanguage.CSHARP, "namespace N { partial class W { int n; } }"
        ),
        "class_declaration",
    )
    path = tmp_path / "W.cs"
    ingest.record_type_location("proj.W.N.W", cs.NodeLabel.CLASS, path, 1, 14)
    ingest.record_members(
        node, node, "proj.W.N.W", "proj.W", cs.NodeLabel.CLASS, ["partial"], path
    )
    assert state.type_locations == {("W.cs", 1): "proj.W.N.W"}
    assert facts.class_field_types == {"proj.W.N.W": {"n": "int"}}
    # The facts' partial group and the state's index are one list.
    (group,) = state.partial_index.values()
    assert facts.csharp_partial_groups["proj.W.N.W"] is group
