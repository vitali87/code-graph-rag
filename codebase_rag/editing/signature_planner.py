"""Planning layer for change_signature."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..graph_query import QueryFn
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from .contract import Reingest
from .patcher import Patcher
from .rename import _hierarchy, _name_token
from .signature_literals import _compatible, _literal_kind
from .signature_params import (
    _RECEIVERS,
    _VARIADIC_PARAM_TYPES,
    _definition_node,
    _Param,
    _parameters,
    _refuse_duplicate_sources,
)
from .signature_rewrite import rewrite_definition, rewrite_site
from .signature_types import (
    ParamSpec,
    RewrittenSite,
    SignatureRefused,
    SignatureReport,
    UnmappedSite,
)
from .transaction import StagedTree, VerificationResult


class SignaturePlanner:
    def __init__(
        self,
        repo_root: Path,
        fetch_all: QueryFn,
        project_name: str,
        verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
        reingest: Reingest | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.fetch_all = fetch_all
        self.project = project_name
        self.verify = verify
        self.reingest = reingest
        self._parsers = load_parsers()[0]
        self._allow_heuristic = False
        self._keyword_only: frozenset[str] = frozenset()

    def _parse(
        self, path: str, source: bytes
    ) -> tuple[cs.SupportedLanguage | None, Node | None]:
        language = get_language_for_extension(Path(path).suffix)
        parser = self._parsers.get(language) if language is not None else None
        if parser is None:
            return language, None
        return language, parser.parse(source).root_node

    def _definition(
        self, qn: str, patcher: Patcher
    ) -> tuple[str, Node, cs.SupportedLanguage | None, bytes]:
        row = graph_query.definition(self.fetch_all, self.project, qn, self.repo_root)
        if not row["found"] or not row["path"]:
            raise SignatureRefused(cs.SIGNATURE_UNKNOWN.format(qn=qn))
        path = row["path"]
        source = patcher.source(path)
        language, root = self._parse(path, source)
        if root is None:
            raise SignatureRefused(cs.SIGNATURE_NO_GRAMMAR.format(path=path))
        name = row["name"] or qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        token = _name_token(
            source,
            language,
            row["start_line"] or 1,
            row["end_line"] or 1,
            name.split("(")[0],
        )
        node = _definition_node(root, *token) if token else None
        if node is None:
            raise SignatureRefused(
                cs.SIGNATURE_NO_DEFINITION_TOKEN.format(qn=qn, path=path)
            )
        return path, node, language, source

    def _resolve_specs(
        self, specs: Iterable[ParamSpec], old: list[_Param]
    ) -> list[ParamSpec]:
        names = [p.name for p in old if not p.receiver and not p.keyword_only]
        resolved: list[ParamSpec] = []
        seen: set[str] = set()
        for spec in specs:
            if spec.name in seen:
                raise SignatureRefused(
                    cs.SIGNATURE_DUPLICATE_PARAM.format(name=spec.name)
                )
            seen.add(spec.name)
            index = spec.from_index
            if spec.from_name is not None:
                if spec.from_name not in names:
                    raise SignatureRefused(
                        cs.SIGNATURE_UNKNOWN_SOURCE.format(
                            source=spec.from_name, names=", ".join(names)
                        )
                    )
                index = names.index(spec.from_name)
            if index is not None and not 0 <= index < len(names):
                raise SignatureRefused(
                    cs.SIGNATURE_UNKNOWN_SOURCE.format(
                        source=str(index), names=", ".join(names)
                    )
                )
            resolved.append(spec._replace(from_index=index, from_name=None))
        _refuse_duplicate_sources(resolved)
        return resolved

    def _check_literals(
        self, qn: str, specs: list[ParamSpec], old_names: list[str]
    ) -> None:
        """Refuse a default literal the declared parameter type cannot hold."""
        declared = self._declared_types(qn)
        for spec in specs:
            if spec.literal is None:
                continue
            annotation = spec.annotation
            if (
                annotation is None
                and spec.from_index is not None
                and spec.from_index < len(declared)
            ):
                annotation = declared[spec.from_index] or None
            if annotation is None:
                continue
            kind = _literal_kind(spec.literal)
            if kind is not None and not _compatible(kind, annotation):
                raise SignatureRefused(
                    cs.SIGNATURE_LITERAL_TYPE.format(
                        literal=spec.literal, name=spec.name, annotation=annotation
                    )
                )

    def _declared_types(self, qn: str) -> list[str]:
        rows = self.fetch_all(
            cq.CYPHER_GRAPH_SIGNATURE,
            {cs.KEY_PROJECT_PREFIX: f"{self.project}{cs.SEPARATOR_DOT}", cs.KEY_QN: qn},
        )
        for row in rows:
            types = row.get(cs.KEY_PARAM_TYPES)
            positional = row.get(cs.KEY_POSITIONAL_PARAMS)
            if isinstance(types, list):
                out = [str(t) for t in types]
                # The receiver carries no annotation slot worth keeping.
                if (
                    isinstance(positional, list)
                    and positional
                    and positional[0] in _RECEIVERS
                ):
                    out = out[1:] if len(out) == len(positional) else out
                return out
        return []

    # --- planning ---------------------------------------------------------------------

    def plan(
        self, qn: str, specs: Iterable[ParamSpec], allow_heuristic: bool = False
    ) -> tuple[SignatureReport, Patcher]:
        patcher = Patcher(self.repo_root)
        hierarchy = _hierarchy(self.fetch_all, self.project, qn)
        path, node, language, _source = self._definition(qn, patcher)
        old = _parameters(node, language)
        if any(p.node.type in _VARIADIC_PARAM_TYPES for p in old):
            raise SignatureRefused(cs.SIGNATURE_VARIADIC.format(qn=qn))
        old_names = [p.name for p in old if not p.receiver and not p.keyword_only]
        self._keyword_only = frozenset(p.name for p in old if p.keyword_only)
        resolved = self._resolve_specs(specs, old)
        self._check_literals(qn, resolved, old_names)
        # Definitions across the hierarchy. Each member's own pre-edit
        # parameter names are kept: an override may spell them differently
        # from the member the request names, and its callers bind by ITS
        # names, not the selected definition's.
        member_names: dict[str, list[str]] = {}
        member_keyword_only: dict[str, frozenset[str]] = {}
        for member in hierarchy:
            m_path, m_node, m_language, _ = (
                (path, node, language, _source)
                if member == qn
                else self._definition(member, patcher)
            )
            m_old = _parameters(m_node, m_language)
            member_names[member] = [
                p.name for p in m_old if not p.receiver and not p.keyword_only
            ]
            member_keyword_only[member] = frozenset(
                p.name for p in m_old if p.keyword_only
            )
            rewrite_definition(patcher, m_path, m_node, m_language, resolved)
        # Every call site of every member.
        sites: list[RewrittenSite] = []
        unmapped: list[UnmappedSite] = []
        for member in hierarchy:
            # `Square().area(factor=2)` is spelled with Square's parameter
            # name; matching it against Base's would read as an unknown
            # keyword and leave the call unrewritten against a rewritten
            # definition.
            self._keyword_only = member_keyword_only.get(member, frozenset())
            for row in graph_query.callers(self.fetch_all, self.project, member):
                rewrite_site(
                    patcher,
                    row,
                    member,
                    member_names.get(member, old_names),
                    resolved,
                    allow_heuristic,
                    self._parse,
                    self._keyword_only,
                    sites,
                    unmapped,
                )
        self._keyword_only = frozenset(p.name for p in old if p.keyword_only)
        report = SignatureReport(
            qualified_name=qn,
            hierarchy=tuple(hierarchy),
            old_params=tuple(old_names),
            new_params=tuple(spec.name for spec in resolved),
            applied=False,
            transaction_id="",
            files=tuple(sorted(patcher.pending)),
            sites=tuple(sorted(sites, key=lambda s: (s.path, s.line, s.col))),
            unmapped=tuple(sorted(unmapped, key=lambda s: (s.path, s.line, s.col))),
            diff="",
            message=cs.SIGNATURE_PLANNED.format(
                count=len(sites), unmapped=len(unmapped)
            ),
        )
        return report, patcher
