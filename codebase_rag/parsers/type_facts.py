"""Return and parameter types as graph facts (issue #1527).

`parameters`-shaped facts existed only as inference caches; "what does this
return / what can I pass" was a source read. Each function or method node
now carries the annotation text as written (`return_type`, `param_types`),
read from the tree-sitter node already in hand, and a deferred pass turns the
names inside those annotations into `RETURNS` / `ACCEPTS` edges when they
resolve to a type defined in the project.

Contract for the two properties:

- `return_type` is absent when the definition carries no return annotation
  (a Python `def f():`, a JS function), so "unknown" and "annotated as None"
  stay distinguishable.
- `param_types` is parallel to the declared parameters in source order, one
  entry per parameter, `""` for an unannotated one. It is absent, not empty,
  for languages this module does not read, so a consumer can tell "no
  parameters" from "kinds unknown" (the `positional_params` rule).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..services import IngestorProtocol
from ..types_defs import FunctionRegistryTrieProtocol, PendingTypeFact
from .utils import safe_decode_with_fallback

# Identifiers, dotted paths and Rust `::` paths inside an annotation. Generic
# brackets, pointers, arrays and unions fall away; every candidate is then
# checked against the registry, so builtins simply resolve to nothing. The
# identifier start is any word character but a digit, so a Unicode class
# name (`class Δ`) is a candidate exactly as an ASCII one is.
_TYPE_NAME_RE = re.compile(r"[^\W\d]\w*(?:(?:\.|::)[^\W\d]\w*)*")

# A Rust path from the crate root. The registry keys modules by their file
# path under the project (`proj.src.model.Item`), never by `crate`, so the
# marker is dropped and the rest resolves by scope and unique suffix.
_RUST_CRATE_ROOT = "crate" + cs.SEPARATOR_DOT

# Parameter-list children that declare no parameter (`*` and `/` separators).
_PY_SEPARATORS = frozenset({cs.TS_PY_KEYWORD_SEPARATOR, cs.TS_PY_POSITIONAL_SEPARATOR})
_PY_TYPED = frozenset({cs.TS_PY_TYPED_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER})
_TS_TYPED = frozenset({cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER})
_GO_PARAMS = frozenset(
    {cs.TS_GO_PARAMETER_DECLARATION, cs.TS_GO_VARIADIC_PARAMETER_DECLARATION}
)
_JAVA_PARAMS = frozenset({cs.TS_FORMAL_PARAMETER, cs.TS_SPREAD_PARAMETER})

TYPE_NODE_TYPES = frozenset(
    {
        cs.NodeLabel.CLASS.value,
        cs.NodeLabel.INTERFACE.value,
        cs.NodeLabel.ENUM.value,
        cs.NodeLabel.TYPE.value,
        cs.NodeLabel.UNION.value,
    }
)


class TypeFacts(NamedTuple):
    return_type: str | None
    param_types: list[str] | None


NO_TYPE_FACTS = TypeFacts(None, None)


def _text(node: Node | None) -> str:
    return safe_decode_with_fallback(node).strip() if node is not None else ""


def _annotation_text(node: Node | None) -> str:
    # A JS/TS `type_annotation` is `: T`; the type is what follows the colon.
    text = _text(node)
    if node is not None and node.type == cs.TS_TYPE_ANNOTATION:
        text = text.lstrip(cs.CHAR_COLON).strip()
    return text


def _field_type(node: Node) -> str:
    return _text(node.child_by_field_name(cs.FIELD_TYPE))


def _extract_python_type_facts(node: Node) -> TypeFacts:
    ret = _text(node.child_by_field_name(cs.FIELD_RETURN_TYPE)) or None
    params = node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return TypeFacts(ret, None)
    types: list[str] = []
    for child in params.named_children:
        if child.type in _PY_SEPARATORS:
            continue
        types.append(_field_type(child) if child.type in _PY_TYPED else "")
    return TypeFacts(ret, types)


def _extract_js_ts_type_facts(node: Node) -> TypeFacts:
    ret = _annotation_text(node.child_by_field_name(cs.FIELD_RETURN_TYPE)) or None
    params = node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return TypeFacts(ret, None)
    types = [
        _annotation_text(child.child_by_field_name(cs.FIELD_TYPE))
        if child.type in _TS_TYPED
        else ""
        for child in params.named_children
    ]
    return TypeFacts(ret, types)


def _extract_go_type_facts(node: Node) -> TypeFacts:
    ret = _text(node.child_by_field_name(cs.FIELD_RESULT)) or None
    params = node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return TypeFacts(ret, None)
    types: list[str] = []
    for child in params.named_children:
        if child.type not in _GO_PARAMS:
            continue
        type_text = _field_type(child)
        if child.type == cs.TS_GO_VARIADIC_PARAMETER_DECLARATION:
            type_text = f"{cs.LANG_ELLIPSIS}{type_text}"
        # `a, b int` declares two parameters of one type; `int` alone one.
        names = [c for c in child.children if c.type == cs.TS_IDENTIFIER]
        types.extend([type_text] * max(1, len(names)))
    return TypeFacts(ret, types)


def _extract_java_type_facts(node: Node) -> TypeFacts:
    ret = _field_type(node) or None
    params = node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return TypeFacts(ret, None)
    types: list[str] = []
    for child in params.named_children:
        if child.type not in _JAVA_PARAMS:
            continue
        if child.type == cs.TS_SPREAD_PARAMETER:
            # `String... names`: the type is the first named child.
            inner = next(iter(child.named_children), None)
            types.append(f"{_text(inner)}{cs.LANG_ELLIPSIS}")
        else:
            types.append(_field_type(child))
    return TypeFacts(ret, types)


def _extract_rust_type_facts(node: Node) -> TypeFacts:
    ret = _text(node.child_by_field_name(cs.FIELD_RETURN_TYPE)) or None
    params = node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return TypeFacts(ret, None)
    types: list[str] = []
    for child in params.named_children:
        if child.type == cs.TS_RS_PARAMETER:
            types.append(_field_type(child))
        elif child.type == cs.TS_RS_SELF_PARAMETER:
            types.append("")
    return TypeFacts(ret, types)


def _extract_csharp_type_facts(node: Node) -> TypeFacts:
    returns = node.child_by_field_name(cs.TS_CSHARP_FIELD_RETURNS)
    if returns is None:
        returns = node.child_by_field_name(cs.FIELD_TYPE)
    ret = _text(returns) or None
    params = node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return TypeFacts(ret, None)
    types: list[str] = []
    for child in params.named_children:
        if child.type == cs.TS_CSHARP_PARAMETER:
            types.append(_field_type(child))
        elif child.type == cs.TS_CSHARP_ARRAY_TYPE:
            # `params Item[] items`: the grammar puts the modifier, the
            # array_type and the identifier straight under the list.
            types.append(f"{cs.CSHARP_PARAMS_PREFIX}{_text(child)}")
    return TypeFacts(ret, types)


def _extract_c_cpp_type_facts(node: Node) -> TypeFacts:
    # Best effort: the declared type specifier, without declarator-level
    # pointers or references; parameters are left to the C++ frontend.
    return TypeFacts(_field_type(node) or None, None)


_EXTRACTORS = {
    cs.SupportedLanguage.PYTHON: _extract_python_type_facts,
    cs.SupportedLanguage.JS: _extract_js_ts_type_facts,
    cs.SupportedLanguage.TS: _extract_js_ts_type_facts,
    cs.SupportedLanguage.TSX: _extract_js_ts_type_facts,
    cs.SupportedLanguage.GO: _extract_go_type_facts,
    cs.SupportedLanguage.JAVA: _extract_java_type_facts,
    cs.SupportedLanguage.RUST: _extract_rust_type_facts,
    cs.SupportedLanguage.CSHARP: _extract_csharp_type_facts,
    cs.SupportedLanguage.C: _extract_c_cpp_type_facts,
    cs.SupportedLanguage.CPP: _extract_c_cpp_type_facts,
}


def extract_type_facts(node: Node, language: cs.SupportedLanguage | None) -> TypeFacts:
    """The annotation text on a function or method definition node."""
    extractor = _EXTRACTORS.get(language) if language is not None else None
    if extractor is None:
        return NO_TYPE_FACTS
    try:
        return extractor(node)
    except (AttributeError, ValueError):
        return NO_TYPE_FACTS


def type_facts_props(facts: TypeFacts) -> dict[str, str | list[str]]:
    props: dict[str, str | list[str]] = {}
    if facts.return_type is not None:
        props[cs.KEY_RETURN_TYPE] = facts.return_type
    if facts.param_types is not None:
        props[cs.KEY_PARAM_TYPES] = facts.param_types
    return props


def type_reference_names(annotation: str) -> list[str]:
    """Distinct dotted names an annotation mentions, in order of appearance.

    `Optional[list[pkg.Item]]` yields `Optional`, `list`, `pkg.Item`; Rust
    `crate::model::Item` yields `crate.model.Item`. Resolution decides which
    of them name a project type.
    """
    seen: dict[str, None] = {}
    for match in _TYPE_NAME_RE.finditer(annotation):
        seen.setdefault(
            match.group(0).replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT), None
        )
    return list(seen)


def queue_type_facts(
    sink: list[PendingTypeFact] | None,
    label: str,
    qualified_name: str,
    module_qn: str | None,
    facts: TypeFacts,
) -> None:
    """Hold a definition's annotations until every file's types are registered.

    A return type may name a class the pass has not reached yet, so the
    edges resolve after Pass 2 (the deferred-parent-link precedent).
    """
    if sink is None or module_qn is None:
        return
    if facts.return_type is None and not facts.param_types:
        return
    sink.append(
        PendingTypeFact(
            label, qualified_name, module_qn, facts.return_type, facts.param_types
        )
    )


class TypeReferenceResolver:
    """Resolve the names an annotation mentions to project type nodes."""

    def __init__(
        self,
        function_registry: FunctionRegistryTrieProtocol,
        import_mapping: Mapping[str, Mapping[str, str]],
        project_name: str,
    ) -> None:
        self._registry = function_registry
        self._imports = import_mapping
        self._prefix = f"{project_name}{cs.SEPARATOR_DOT}"

    def _is_type(self, qn: str) -> bool:
        node_type = self._registry.get(qn)
        return node_type is not None and str(node_type) in TYPE_NODE_TYPES

    def _scoped_candidates(self, name: str, module_qn: str) -> list[str]:
        head, _sep, rest = name.partition(cs.SEPARATOR_DOT)
        imports = self._imports.get(module_qn, {})
        # 1. Through the module's imports: `Item` -> pkg.models.Item, or
        #    `models.Item` when `models` is an imported module.
        candidates: list[str] = []
        if head in imports:
            bound = imports[head]
            candidates.append(f"{bound}{cs.SEPARATOR_DOT}{rest}" if rest else bound)
        # 2. Defined in the same module, or an enclosing module of it.
        scope = module_qn
        while scope.startswith(self._prefix):
            candidates.append(f"{scope}{cs.SEPARATOR_DOT}{name}")
            scope = scope.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return candidates

    def _nearest_unique(self, matches: list[str], module_qn: str) -> str | None:
        # 3. A unique project type with that name, preferring the nearest
        #    package; two equally near candidates stay unresolved rather
        #    than guessed.
        if len(matches) == 1:
            return matches[0]
        module_parts = module_qn.split(cs.SEPARATOR_DOT)

        def shared(qn: str) -> int:
            n = 0
            for a, b in zip(module_parts, qn.split(cs.SEPARATOR_DOT), strict=False):
                if a != b:
                    break
                n += 1
            return n

        ranked = sorted(matches, key=lambda qn: (-shared(qn), len(qn), qn))
        if shared(ranked[0]) > shared(ranked[1]):
            return ranked[0]
        return None

    def resolve(self, name: str, module_qn: str) -> str | None:
        # A crate-root path is absolute: it must not bind to a nearer module
        # that happens to share the tail, so the scoped walk is skipped and
        # only the unique-suffix match below decides.
        absolute = name.startswith(_RUST_CRATE_ROOT)
        if absolute:
            name = name[len(_RUST_CRATE_ROOT) :]
        for candidate in () if absolute else self._scoped_candidates(name, module_qn):
            if self._is_type(candidate):
                return candidate
        matches = [
            qn
            for qn in self._registry.find_ending_with(name)
            if qn.startswith(self._prefix) and self._is_type(qn)
        ]
        if not matches:
            return None
        if absolute:
            return self._root_nearest_unique(matches)
        return self._nearest_unique(matches, module_qn)

    @staticmethod
    def _root_nearest_unique(matches: list[str]) -> str | None:
        # An absolute path names the type closest to the crate root: the
        # shortest matching qn wins, and two at the same depth stay
        # unresolved rather than guessed.
        ranked = sorted(matches, key=lambda qn: (qn.count(cs.SEPARATOR_DOT), qn))
        if len(ranked) > 1 and ranked[0].count(cs.SEPARATOR_DOT) == ranked[1].count(
            cs.SEPARATOR_DOT
        ):
            return None
        return ranked[0]

    def resolve_annotation(self, annotation: str, module_qn: str) -> list[str]:
        found: dict[str, None] = {}
        for name in type_reference_names(annotation):
            qn = self.resolve(name, module_qn)
            if qn is not None:
                found.setdefault(qn, None)
        return list(found)


def _target_spec(
    resolver: TypeReferenceResolver, target_qn: str
) -> tuple[str, str, str]:
    return (str(resolver._registry[target_qn]), cs.KEY_QUALIFIED_NAME, target_qn)


def _emit_returns(
    fact: PendingTypeFact,
    resolver: TypeReferenceResolver,
    ingestor: IngestorProtocol,
    source: tuple[str, str, str],
) -> int:
    if fact.return_type is None:
        return 0
    targets = resolver.resolve_annotation(fact.return_type, fact.module_qn)
    for target_qn in targets:
        ingestor.ensure_relationship_batch(
            source, cs.RelationshipType.RETURNS, _target_spec(resolver, target_qn)
        )
    return len(targets)


def _emit_accepts(
    fact: PendingTypeFact,
    resolver: TypeReferenceResolver,
    ingestor: IngestorProtocol,
    source: tuple[str, str, str],
) -> int:
    accepted: dict[str, None] = {}
    for annotation in fact.param_types or ():
        if not annotation:
            continue
        for target_qn in resolver.resolve_annotation(annotation, fact.module_qn):
            accepted.setdefault(target_qn, None)
    for target_qn in accepted:
        ingestor.ensure_relationship_batch(
            source, cs.RelationshipType.ACCEPTS, _target_spec(resolver, target_qn)
        )
    return len(accepted)


def emit_type_edges(
    pending: list[PendingTypeFact],
    resolver: TypeReferenceResolver,
    ingestor: IngestorProtocol,
) -> int:
    """RETURNS / ACCEPTS edges for every queued fact.

    Runs after Pass 2 with the full registry, so an annotation naming a type
    defined in a later file still resolves. Returns the number of edges.
    """
    emitted = 0
    for fact in pending:
        source = (fact.label, cs.KEY_QUALIFIED_NAME, fact.qualified_name)
        emitted += _emit_returns(fact, resolver, ingestor, source)
        emitted += _emit_accepts(fact, resolver, ingestor, source)
    pending.clear()
    return emitted
