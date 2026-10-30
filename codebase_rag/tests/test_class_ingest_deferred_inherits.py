"""`DeferredInherits`: how a held-back base resolves once every class is
registered (the registry, a second spelling, a project path written from
another root, a package's re-export, the parse-time name lookup) or is
externalized under its written name, and the edge each one writes.

The host here answers with the neutral language hooks, so what is pinned is
the language-agnostic chain itself; the per-language hooks have their own
collaborator tests."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.parsers.class_ingest.deferred_inherits import DeferredInherits
from codebase_rag.parsers.definition_context import DefinitionContext
from codebase_rag.parsers.definition_facts import DefinitionFacts
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.language_definitions import LanguageClassIngest
from codebase_rag.tests.conftest import get_nodes, get_relationships
from codebase_rag.types_defs import DeferredInherit
from evals.recorded_graph import RecordingIngestor

_L = cs.SupportedLanguage
_CHILD = "proj.m.C"


class _Host:
    """The class pass as `DeferredInherits` sees it: neutral language hooks
    and a parse-time base lookup answering from `bases`."""

    def __init__(self, bases: dict[str, str]) -> None:
        self._bases = bases
        self._neutral = LanguageClassIngest()

    def _for(self, language: cs.SupportedLanguage | None) -> LanguageClassIngest:
        return self._neutral

    def _resolve_base_name(
        self, name: str, module_qn: str, language: cs.SupportedLanguage | None
    ) -> str | None:
        return self._bases.get(name)


class _Pass:
    def __init__(self, bases: dict[str, str] | None = None) -> None:
        self.ingestor = RecordingIngestor()
        self.registry = FunctionRegistryTrie()
        self.imports = ImportProcessor(
            Path("/repo"),
            "proj",
            ingestor=self.ingestor,
            function_registry=self.registry,
        )
        self.facts = DefinitionFacts()
        self.inherits = DeferredInherits(
            _Host(bases or {}),
            DefinitionContext(
                ingestor=self.ingestor,
                repo_path=Path("/repo"),
                project_name="proj",
                function_registry=self.registry,
                simple_name_lookup=defaultdict(set),
                module_qn_to_file_path={},
                import_processor=self.imports,
                facts=self.facts,
            ),
        )

    def parent(self, parent_qn: str, **fields: str | None) -> tuple[str, bool] | None:
        return self.inherits.resolve_parent_qn(_entry(parent_qn, **fields))


def _entry(
    parent_qn: str,
    *,
    child_qn: str = _CHILD,
    language: cs.SupportedLanguage = _L.PYTHON,
    rel_type: cs.RelationshipType = cs.RelationshipType.INHERITS,
    base_index: int = 0,
    alt_parent_qn: str | None = None,
    written_ref: str | None = None,
    child_label: str | None = None,
) -> DeferredInherit:
    return DeferredInherit(
        rel_type=rel_type,
        child_qn=child_qn,
        parent_qn=parent_qn,
        module_qn="proj.m",
        base_index=base_index,
        language=language,
        alt_parent_qn=alt_parent_qn,
        written_ref=written_ref,
        child_label=child_label,
    )


def test_a_registered_base_replaces_the_guess_and_an_unknown_one_externalizes() -> None:
    run = _Pass()
    run.registry[_CHILD] = cs.NodeLabel.CLASS
    run.registry["proj.other.Base"] = cs.NodeLabel.CLASS
    run.facts.class_inheritance[_CHILD] = ["proj.m.Base", "proj.m.object"]
    emitted = run.inherits.resolve(
        [_entry("proj.other.Base"), _entry("proj.m.object", base_index=1)]
    )
    assert emitted == 2
    # Method resolution walks this list, so it must name the real bases.
    assert run.facts.class_inheritance[_CHILD] == ["proj.other.Base", "object"]
    assert [
        (str(r.target), r.target_label, r.properties)
        for r in get_relationships(run.ingestor, cs.RelationshipType.INHERITS)
    ] == [
        ("proj.other.Base", cs.NodeLabel.CLASS, {cs.KEY_BASE_INDEX: 0}),
        ("object", cs.NodeLabel.EXTERNAL_MODULE, {cs.KEY_BASE_INDEX: 1}),
    ]
    # The external base gets its node, and is remembered as unresolved so a
    # file added later that defines it re-parses this one.
    assert [n.properties[cs.KEY_QUALIFIED_NAME] for n in get_nodes(run.ingestor)] == [
        "object"
    ]
    assert run.imports.unresolved_references == {"proj.m": {"object"}}


def test_a_base_whose_child_never_registered_writes_nothing() -> None:
    run = _Pass()
    run.registry["proj.other.Base"] = cs.NodeLabel.CLASS
    assert (
        run.inherits.resolve([_entry("proj.other.Base", child_qn="proj.m.Gone")]) == 0
    )
    assert get_relationships(run.ingestor) == []


def test_a_self_edge_externalizes_only_a_written_single_segment() -> None:
    run = _Pass()
    # `pub enum Error` implementing the std `Error` trait shadows the name.
    assert run.parent("proj.m.Error", child_qn="proj.m.Error") == ("Error", True)
    # A nested `Map.Entry` was never written as such: no edge at all.
    assert run.parent("proj.m.Map.Entry", child_qn="proj.m.Map.Entry") is None


def test_a_base_outside_the_project_keeps_its_path_as_an_external() -> None:
    assert _Pass().parent("std::fmt::Display", language=_L.RUST) == (
        "std.fmt.Display",
        True,
    )


def test_a_same_named_function_is_a_base_in_js_alone() -> None:
    run = _Pass()
    run.registry["proj.m.Base"] = cs.NodeLabel.FUNCTION
    # An ES5 constructor function; in Python a function is never a base.
    assert run.parent("proj.m.Base", language=_L.JS) == ("proj.m.Base", False)
    assert run.parent("proj.m.Base") == ("Base", True)


def test_a_second_spelling_gets_the_whole_chain() -> None:
    run = _Pass()
    run.registry["proj.crate.base.Base"] = cs.NodeLabel.CLASS
    assert run.parent("proj.crate.Base", alt_parent_qn="proj.crate.base.Base") == (
        "proj.crate.base.Base",
        False,
    )


def test_a_path_written_from_another_root_needs_a_unique_suffix_match() -> None:
    run = _Pass()
    # setup.py maps lib/src to `thrift`, so the import says proj.thrift.Thrift.
    run.registry["proj.lib.src.thrift.Thrift"] = cs.NodeLabel.CLASS
    assert run.parent("proj.thrift.Thrift") == ("proj.lib.src.thrift.Thrift", False)
    run.registry["proj.vendor.src.thrift.Thrift"] = cs.NodeLabel.CLASS
    assert run.parent("proj.thrift.Thrift") is None


def test_a_package_attribute_resolves_only_through_what_the_package_exposes() -> None:
    run = _Pass()
    # `from django import forms` then `class F(forms.ModelForm)`.
    run.registry["proj.django.forms.models.ModelForm"] = cs.NodeLabel.CLASS
    assert run.parent("proj.django.forms.ModelForm") is None
    run.imports.import_mapping["proj.django.forms"] = {
        f"{cs.GLOB_ALL}models": "proj.django.forms.models"
    }
    assert run.parent("proj.django.forms.ModelForm") == (
        "proj.django.forms.models.ModelForm",
        False,
    )


def test_a_module_anchored_guess_tries_the_parse_time_lookup_first() -> None:
    run = _Pass({"Base": "proj.other.Base", "Loop": _CHILD})
    run.registry["proj.other.Base"] = cs.NodeLabel.CLASS
    run.registry[_CHILD] = cs.NodeLabel.CLASS
    assert run.parent("proj.m.Base") == ("proj.other.Base", False)
    # A lookup landing on the child itself is no base; the written name is.
    assert run.parent("proj.m.Loop") == ("Loop", True)
    assert run.parent("proj.m.std::Default") == ("std.Default", True)


def test_a_csharp_base_no_scope_reached_is_marked_heuristic() -> None:
    run = _Pass()
    run.registry[_CHILD] = cs.NodeLabel.CLASS
    run.registry["proj.n.Base"] = cs.NodeLabel.CLASS
    run.registry["proj.n.Shape"] = cs.NodeLabel.CLASS
    run.inherits.resolve(
        [
            _entry("proj.n.Base", language=_L.CSHARP, written_ref="Base"),
            # Dart's `implements` names a class; the edge must not dangle on
            # an Interface label.
            _entry(
                "proj.n.Shape",
                language=_L.DART,
                rel_type=cs.RelationshipType.IMPLEMENTS,
            ),
        ]
    )
    (inherits,) = get_relationships(run.ingestor, cs.RelationshipType.INHERITS)
    assert inherits.properties == {
        cs.KEY_BASE_INDEX: 0,
        cs.KEY_RESOLUTION: cs.EdgeResolution.HEURISTIC,
    }
    (implements,) = get_relationships(run.ingestor, cs.RelationshipType.IMPLEMENTS)
    assert (implements.target_label, implements.properties) == (cs.NodeLabel.CLASS, {})
    assert run.facts.interface_implementers == {"proj.n.Shape": {_CHILD}}


def test_a_ts_type_sharing_a_values_name_leaves_from_its_own_label() -> None:
    run = _Pass()
    run.registry.register_unique_qn("proj.m.Box", 1, kind=cs.NodeLabel.INTERFACE)
    run.registry["proj.m.Box"] = cs.NodeLabel.INTERFACE
    run.registry.register_unique_qn("proj.m.Box", 2, kind=cs.NodeLabel.FUNCTION)
    run.registry["proj.m.Box"] = cs.NodeLabel.FUNCTION
    run.registry["proj.m.Base"] = cs.NodeLabel.INTERFACE
    run.inherits.resolve(
        [
            _entry(
                "proj.m.Base",
                child_qn="proj.m.Box",
                language=_L.TS,
                child_label=cs.NodeLabel.INTERFACE,
            )
        ]
    )
    (edge,) = get_relationships(run.ingestor, cs.RelationshipType.INHERITS)
    assert (edge.source_label, edge.target_label) == (
        cs.NodeLabel.INTERFACE,
        cs.NodeLabel.INTERFACE,
    )
