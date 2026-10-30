"""The non-C++ INHERITS/IMPLEMENTS edges held until every class is
registered, and the base each one lands on.

Parse time sees a partial registry, so `ClassIngest` queues a base it
cannot yet trust and hands the queue to the `DeferredInherits` it owns once
every file is parsed. A base resolves first through the language's own
scopes (a C# namespace, its `using`s), then through the import that named
it, the registry, a re-export, a second spelling of the written path, a
unique suffix match for a project path written from another root, and a
package's re-export of the name. A base that resolves to no indexed class is
defined outside the index by construction and keeps its edge onto an
ExternalModule under its written name. A resolved base also replaces the
guess the class's inheritance list carries, so method resolution and the
override walk follow the real hierarchy. It calls back through
`InheritHost` for a language's class collaborator and the parse-time base
lookup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ... import constants as cs
from ...types_defs import DeferredInherit
from ..language_definitions import LanguageClassIngest
from ..utils import written_simple_name
from . import base_targets as bt
from . import relationships as rel

if TYPE_CHECKING:
    from ..definition_context import DefinitionContext


class InheritHost(Protocol):
    """What the deferred inherits call on the class pass that owns them: the
    class collaborator of a base's language, and the parse-time lookup of a
    written base name through the import map and the registry."""

    def _for(self, language: cs.SupportedLanguage | None) -> LanguageClassIngest: ...

    def _resolve_base_name(
        self, name: str, module_qn: str, language: cs.SupportedLanguage | None
    ) -> str | None: ...


class DeferredInherits:
    """Resolves a queue of held-back bases and writes their edges.

    Built and owned by `ClassIngest`, whose registry, graph and import map
    the bases resolve against."""

    __slots__ = (
        "_host",
        "ingestor",
        "project_name",
        "function_registry",
        "import_processor",
        "facts",
    )

    def __init__(
        self,
        host: InheritHost,
        context: DefinitionContext,
    ) -> None:
        self._host = host
        self.ingestor = context.ingestor
        self.project_name = context.project_name
        self.function_registry = context.function_registry
        self.import_processor = context.import_processor
        self.facts = context.facts

    def resolve(self, deferred: list[DeferredInherit]) -> int:
        """Write the edge of every held-back base that resolves; the number
        written."""
        emitted = 0
        for entry in deferred:
            child_type = self._deferred_child_label(entry)
            if child_type is None:
                continue
            language_ingest = self._host._for(entry.language)
            # Scope first (issue #2534); a base no scope reaches falls to the
            # name search, whose `PB`/`PB<T>` pick the written arity settles
            # (issue #2579).
            scoped = language_ingest.scoped_base(entry)
            resolved = (
                (scoped, False)
                if scoped is not None
                else language_ingest.base_by_arity(entry, self.resolve_parent_qn(entry))
            )
            if resolved is None or resolved[1]:
                # Resolved nowhere, or to a node outside the index: the file
                # defining this base may be added later (issue #1568).
                self.import_processor.note_unresolved(
                    entry.module_qn, written_simple_name(entry.parent_qn)
                )
            if resolved is None:
                continue
            # A C# base no scope or `using` reaches, bound anyway by a
            # project-wide name match, is a guess; say so on the edge.
            heuristic = (
                scoped is None and entry.written_ref is not None and not resolved[1]
            )
            self._emit_resolved_inherit(entry, child_type, resolved, heuristic)
            emitted += 1
        return emitted

    def _deferred_child_label(self, entry: DeferredInherit) -> str | None:
        held = self.function_registry.get(entry.child_qn)
        if held is None:
            return None
        # Where a TS type shares its name with a value (issue #2520) the
        # registry holds the value's kind for both, so the label the child was
        # declared with decides which of the two nodes the edge leaves.
        if (
            entry.child_label is not None
            and self.function_registry.type_kind(entry.child_qn) != held
        ):
            return entry.child_label
        return str(held)

    def resolve_parent_qn(self, entry: DeferredInherit) -> tuple[str, bool] | None:
        """Resolve a deferred parent to (qn, is_external), or None for no edge.

        First-party wins; a qn outside the project prefix is positive external
        knowledge (import-mapped or ::-qualified) and keeps its edge onto an
        ExternalModule node. A project-prefixed qn that is not a real class
        may still name one through a src-root layout (setup.py maps src/ to
        the distribution name), recovered by a unique whole-segment suffix
        match. A module-anchored guess that re-resolves nowhere externalizes
        its WRITTEN name (canonicalized for JS globals and java.lang): a base
        name resolving to no indexed class is by construction defined outside
        the indexed tree, and dropping it would lose a syntactic inheritance
        fact the source declares.
        """
        if entry.parent_qn == entry.child_qn:
            return self._resolve_self_edge_parent(entry)
        if (registered := self._registered_base(entry)) is not None:
            return registered, False
        if entry.alt_parent_qn is not None:
            # The written path was exact and still landed on nothing, which a
            # re-export does: `crate::Base` names the entry module while the
            # trait registers where it is declared. The second spelling gets
            # the whole chain, not just the registry probe above.
            return self.resolve_parent_qn(
                entry._replace(parent_qn=entry.alt_parent_qn, alt_parent_qn=None)
            )
        project_prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        if not entry.parent_qn.startswith(project_prefix):
            external = entry.parent_qn.replace(
                cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT
            )
            return external, True
        prefix = f"{entry.module_qn}{cs.SEPARATOR_DOT}"
        if not entry.parent_qn.startswith(prefix):
            return self._resolve_unanchored_project_parent(entry, project_prefix)
        # The module-anchored fallback shape carries the raw written name
        # as the remainder after the module qn.
        return self._resolve_module_anchored_parent(
            entry, entry.parent_qn[len(prefix) :]
        )

    def _registered_base(self, entry: DeferredInherit) -> str | None:
        # The registered type a parent names as written: through the import
        # that bound it, the registry itself, or the re-export it leads to.
        # Only a type declaration is a base: a registered member of the same
        # name (a C# property, a method) is not, however it was reached.
        target_kinds = bt.base_target_kinds(entry.language)
        language_ingest = self._host._for(entry.language)
        if (
            imported := language_ingest.imported_base(entry)
        ) is not None and self.function_registry.get(imported) in target_kinds:
            return imported
        if self.function_registry.get(entry.parent_qn) in target_kinds:
            return entry.parent_qn
        return language_ingest.reexport_target(entry)

    def _resolve_module_anchored_parent(
        self, entry: DeferredInherit, raw_name: str
    ) -> tuple[str, bool] | None:
        if (
            bound := self._host._for(entry.language).namespace_member(raw_name, entry)
        ) is not None:
            return bound, False
        resolved = self._resolve_class_name(raw_name, entry.module_qn, entry.language)
        if (
            resolved is not None
            # A simple-name sweep can land on the child itself; a
            # self-INHERITS is never real.
            and resolved != entry.child_qn
            and self.function_registry.get(resolved)
            in bt.base_target_kinds(entry.language)
        ):
            return resolved, False
        return self._externalize_written_base(raw_name, entry.language)

    def _resolve_self_edge_parent(
        self, entry: DeferredInherit
    ) -> tuple[str, bool] | None:
        # Parse-time resolution can land on the child ITSELF. A
        # self-edge is never real. In C# the written base can be an
        # ARITY sibling (`class Foo : Foo<object>`, a different type
        # sharing the simple name); recover it before falling back.
        # Otherwise the written base must refer to a SHADOWED outer name
        # (thrift's `pub enum Error` implementing the std `Error` trait):
        # when the module-anchored remainder is a bare single segment it
        # IS the written name and externalizes. A dotted remainder (a
        # nested child like SimpleHashMap.Entry) was never written as
        # such; derivation would be a lie, so no edge.
        if (
            sibling := self._host._for(entry.language).arity_sibling(entry)
        ) is not None:
            return sibling, False
        self_prefix = f"{entry.module_qn}{cs.SEPARATOR_DOT}"
        if entry.parent_qn.startswith(self_prefix):
            raw = entry.parent_qn[len(self_prefix) :]
            if raw and cs.SEPARATOR_DOT not in raw:
                return self._externalize_written_base(raw, entry.language)
        return None

    def _resolve_unanchored_project_parent(
        self, entry: DeferredInherit, project_prefix: str
    ) -> tuple[str, bool] | None:
        # Project-prefixed but not module-anchored: an import-mapped
        # qn whose written path skips real directories (thrift's
        # setup.py maps lib/py/src -> package `thrift`, so the import
        # says thrift.Thrift while the class qn says
        # thrift.src.Thrift). A UNIQUE whole-segment suffix match
        # recovers the real node; ambiguity means no edge.
        tail = entry.parent_qn[len(project_prefix) :]
        simple = tail.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        suffix = f"{cs.SEPARATOR_DOT}{tail}"
        candidates = self.function_registry.find_ending_with(simple)
        target_kinds = bt.base_target_kinds(entry.language)
        matches = {
            qn
            for qn in candidates
            if qn.endswith(suffix)
            and qn != entry.child_qn
            and self.function_registry.get(qn) in target_kinds
        }
        if len(matches) == 1:
            return matches.pop(), False
        # A base written as a PACKAGE attribute (`forms.ModelForm` via
        # `from django import forms`) names the re-exporting package, not
        # the defining module (django.forms.models.ModelForm behind the
        # package __init__'s star import), so the suffix match cannot
        # bridge the missing segment. A UNIQUE same-named class UNDER the
        # written package path is that re-export; ambiguity means no edge.
        package_prefix = (
            entry.parent_qn.rsplit(cs.SEPARATOR_DOT, 1)[0] + cs.SEPARATOR_DOT
        )
        # The registry also holds functions/methods with the same simple
        # name; only a TYPE declaration is a valid inheritance target, so
        # filter before the uniqueness check (a same-named factory function
        # under the package must not corrupt the class hierarchy). The
        # package must also EXPOSE the name (its __init__ imports it
        # explicitly or star-imports the defining module); a same-named
        # internal class the package never re-exports is not the referent.
        type_decls = (cs.NodeLabel.CLASS, cs.NodeLabel.INTERFACE, cs.NodeLabel.ENUM)
        package_qn = package_prefix[: -len(cs.SEPARATOR_DOT)]
        package_matches = {
            qn
            for qn in candidates
            if qn.startswith(package_prefix)
            and qn != entry.child_qn
            and self.function_registry.get(qn) in type_decls
            and self._package_exposes(package_qn, simple, qn)
        }
        if len(package_matches) == 1:
            return package_matches.pop(), False
        return None

    def _package_exposes(self, package_qn: str, simple: str, class_qn: str) -> bool:
        # True when the package __init__ makes `simple` an attribute of the
        # package: an explicit import binding the name to this class (or its
        # defining module member), or a star import of the module that
        # defines it. Star-import keys carry a leading GLOB_ALL marker,
        # matching the call resolver's wildcard convention.
        imports = self.import_processor.import_mapping.get(package_qn)
        if not imports:
            return False
        if imports.get(simple) == class_qn:
            return True
        star_member = f"{cs.SEPARATOR_DOT}{simple}"
        return any(
            key.startswith(cs.GLOB_ALL) and class_qn == f"{target}{star_member}"
            for key, target in imports.items()
        )

    def _externalize_written_base(
        self, raw_name: str, language: cs.SupportedLanguage
    ) -> tuple[str, bool]:
        if (
            canonical := self._host._for(language).externalized_base(raw_name)
        ) is not None:
            return canonical, True
        # Language-agnostic fallback: the written base name resolves to no
        # indexed class, so the base is external to the index by
        # construction (Python `object`, Rust `Default`, a generated Java
        # `Iface`); keep the fact under the written name.
        return raw_name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT), True

    def _emit_resolved_inherit(
        self,
        entry: DeferredInherit,
        child_type: str,
        resolved: tuple[str, bool],
        heuristic: bool = False,
    ) -> None:
        parent_qn, is_external = resolved
        resolution = cs.EdgeResolution.HEURISTIC if heuristic else None
        if not is_external:
            self._host._for(entry.language).record_resolved_base(entry, parent_qn)
        external_label: str | None = None
        if is_external:
            # The import pass mints the same node for IMPORTS edges, so
            # this MERGEs idempotently when the base was imported.
            self.import_processor.ensure_external_module_node(parent_qn)
            external_label = cs.NodeLabel.EXTERNAL_MODULE.value
        if entry.rel_type != cs.RelationshipType.IMPLEMENTS:
            bases = self.facts.class_inheritance.get(entry.child_qn)
            if bases is not None and entry.base_index < len(bases):
                bases[entry.base_index] = parent_qn
            rel.create_inheritance_relationship(
                child_type,
                entry.child_qn,
                parent_qn,
                self.function_registry,
                self.ingestor,
                entry.base_index,
                parent_label=external_label,
                resolution=resolution,
            )
            return
        # Dart has no `interface` keyword: `implements X` targets a
        # concrete class, so a hardcoded Interface label would dangle.
        # Resolve the target's real registered label (Interface for a
        # true interface, Class/Enum for a Dart type); external stays
        # EXTERNAL_MODULE.
        interface_label = external_label or rel.get_node_type_for_inheritance(
            parent_qn, self.function_registry, type_position=True
        )
        rel.create_implements_relationship(
            child_type,
            entry.child_qn,
            parent_qn,
            self.ingestor,
            interface_label=interface_label,
            resolution=resolution,
        )
        self.facts.interface_implementers.setdefault(parent_qn, set()).add(
            entry.child_qn
        )

    def _resolve_class_name(
        self,
        class_name: str,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> str | None:
        resolved = self._host._resolve_base_name(class_name, module_qn, language)
        if resolved is not None:
            return resolved
        return self._host._for(language).namespaced_class(class_name)
