"""`change_signature(qn, new_params)`: edit-algebra operation 2 (issue #1533).

The definition changes, its callers in other packages do not: that is the
most-cited cross-package breakage. The graph knows every call site with
its argument shape (issue #1522), how each edge was resolved (issue #1526)
and the declared parameter types (issue #1527), so every site can be
rewritten per an explicit mapping or listed as unmapped.

`new_params` describes the new parameter list in order. Each entry says
where its value comes from at a call site: an old positional index
(`from_index`, receiver excluded), an old parameter name (`from_name`),
a default literal (`literal`, inserted where the site passes nothing), or
nothing at all (unmapped: sites that pass no value are left untouched and
listed). Every definition in the override hierarchy is rewritten too.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..graph_query import QueryFn
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..parsers.call_processor import _find_call_arguments_node, _split_call_arguments
from ..types_defs import ResultRow
from .contract import Reingest, Verdict, change_signature_expectation, measure, verify
from .patcher import Patcher, PatcherError
from .rename import _hierarchy, _name_token
from .transaction import EditTransaction, StagedTree, VerificationResult, undo_last

_AMBIGUOUS = frozenset(
    {
        cs.EdgeResolution.HEURISTIC.value,
        cs.EdgeResolution.OVERLOAD.value,
        cs.EdgeResolution.DYNAMIC.value,
    }
)
_RECEIVERS = frozenset({cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS})
_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?::(?P<annotation>[^=@]+))?"
    r"(?:=(?P<default>[^@]+))?"
    r"(?:@(?P<source>.+))?$"
)
_SPLAT_TYPES = frozenset(
    {
        cs.TS_PY_LIST_SPLAT,
        cs.TS_PY_DICTIONARY_SPLAT,
        cs.TS_SPREAD_ELEMENT,
    }
)
_COMMENT_TYPES = frozenset({cs.TS_COMMENT})


class ParamSpec(NamedTuple):
    """One parameter of the new signature and where its value comes from."""

    name: str
    from_index: int | None = None
    from_name: str | None = None
    literal: str | None = None
    annotation: str | None = None
    default: str | None = None

    @property
    def unmapped(self) -> bool:
        return (
            self.from_index is None and self.from_name is None and self.literal is None
        )


def parse_param_spec(text: str) -> ParamSpec:
    """`name[:annotation][=default][@source]` as the CLI and MCP spell it.

    `@2` maps from old positional index 2, `@old` from the old parameter
    `old`; a `=default` without a source is the literal every site that
    passes nothing gains (and the definition's default); a bare name with
    neither is unmapped.
    """
    match = _SPEC_RE.match(text.strip())
    if match is None:
        raise SignatureRefused(cs.SIGNATURE_BAD_SPEC.format(spec=text))
    name = match.group("name")
    annotation = (match.group("annotation") or "").strip() or None
    default = (match.group("default") or "").strip() or None
    source = (match.group("source") or "").strip() or None
    from_index = from_name = None
    if source is not None:
        if source.isdigit():
            from_index = int(source)
        else:
            from_name = source
    literal = default if source is None and default is not None else None
    return ParamSpec(name, from_index, from_name, literal, annotation, default)


class SignatureRefused(ValueError):
    """The change cannot be planned as asked."""


class UnmappedSite(NamedTuple):
    path: str
    line: int
    col: int
    owner: str
    reason: str


class RewrittenSite(NamedTuple):
    path: str
    line: int
    col: int
    owner: str
    resolution: str | None
    before: str
    after: str


class SignatureReport(NamedTuple):
    qualified_name: str
    hierarchy: tuple[str, ...]
    old_params: tuple[str, ...]
    new_params: tuple[str, ...]
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    sites: tuple[RewrittenSite, ...]
    unmapped: tuple[UnmappedSite, ...]
    diff: str
    message: str
    verdict: Verdict | None = None


# --- parameters of a definition -----------------------------------------------------


class _Param(NamedTuple):
    name: str
    node: Node
    receiver: bool


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode(cs.ENCODING_UTF8, errors="replace")


def _identifier_in(node: Node) -> str:
    """The first identifier token inside a parameter node, any grammar."""
    named = node.child_by_field_name(cs.FIELD_NAME)
    if named is not None and named.type in (cs.TS_IDENTIFIER, cs.TS_PY_IDENTIFIER):
        return _text(named)
    pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
    if pattern is not None and pattern.type == cs.TS_IDENTIFIER:
        return _text(pattern)
    if node.type in (cs.TS_IDENTIFIER, cs.TS_PY_IDENTIFIER):
        return _text(node)
    stack = list(reversed(node.children))
    while stack:
        child = stack.pop()
        if child.type in (cs.TS_IDENTIFIER, cs.TS_PY_IDENTIFIER):
            return _text(child)
        stack.extend(reversed(child.children))
    return ""


def _definition_node(root: Node, line: int, col: int) -> Node | None:
    """The definition whose own name token starts at (line, col)."""
    stack = [root]
    while stack:
        node = stack.pop()
        named = node.child_by_field_name(cs.FIELD_NAME)
        if (
            named is not None
            and named.start_point == (line - 1, col)
            and node.child_by_field_name(cs.FIELD_PARAMETERS) is not None
        ):
            return node
        if node.start_point[0] <= line - 1 <= node.end_point[0]:
            stack.extend(node.children)
    return None


def _parameters(
    definition: Node, language: cs.SupportedLanguage | None
) -> list[_Param]:
    params = definition.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return []
    out: list[_Param] = []
    for child in params.named_children:
        if child.type in _COMMENT_TYPES:
            continue
        name = _identifier_in(child)
        receiver = (
            language == cs.SupportedLanguage.PYTHON and not out and name in _RECEIVERS
        ) or child.type == cs.TS_RS_SELF_PARAMETER
        out.append(_Param(name, child, receiver))
    return out


def _render_param(spec: ParamSpec, language: cs.SupportedLanguage | None) -> str:
    """A brand-new parameter in the target grammar's spelling."""
    annotation = spec.annotation
    default = spec.default if spec.default is not None else spec.literal
    if language == cs.SupportedLanguage.GO:
        return f"{spec.name} {annotation}" if annotation else spec.name
    if language == cs.SupportedLanguage.JAVA:
        return f"{annotation} {spec.name}" if annotation else spec.name
    if language == cs.SupportedLanguage.RUST:
        return f"{spec.name}: {annotation}" if annotation else spec.name
    text = spec.name
    if annotation:
        text += f": {annotation}"
    if default is not None:
        text += f" = {default}" if annotation else f"={default}"
    return text


def _rendered_old(
    param: _Param, spec: ParamSpec, language: cs.SupportedLanguage | None
) -> str:
    """An old parameter carried into the new signature, renamed if asked."""
    text = _text(param.node)
    if spec.name != param.name:
        text = re.sub(rf"\b{re.escape(param.name)}\b", spec.name, text, count=1)
    if spec.annotation is None and spec.default is None:
        return text
    return _render_param(spec, language)


# --- the operation -----------------------------------------------------------------


class SignatureChanger:
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
        names = [p.name for p in old if not p.receiver]
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
        old_names = [p.name for p in old if not p.receiver]
        resolved = self._resolve_specs(specs, old)
        self._check_literals(qn, resolved, old_names)
        # Definitions across the hierarchy.
        for member in hierarchy:
            m_path, m_node, m_language, _ = (
                (path, node, language, _source)
                if member == qn
                else self._definition(member, patcher)
            )
            self._rewrite_definition(patcher, m_path, m_node, m_language, resolved)
        # Every call site of every member.
        sites: list[RewrittenSite] = []
        unmapped: list[UnmappedSite] = []
        for member in hierarchy:
            for row in graph_query.callers(self.fetch_all, self.project, member):
                self._rewrite_site(
                    patcher,
                    row,
                    member,
                    old_names,
                    resolved,
                    allow_heuristic,
                    sites,
                    unmapped,
                )
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

    def _rewrite_definition(
        self,
        patcher: Patcher,
        path: str,
        node: Node,
        language: cs.SupportedLanguage | None,
        specs: list[ParamSpec],
    ) -> None:
        params_node = node.child_by_field_name(cs.FIELD_PARAMETERS)
        assert params_node is not None
        old = _parameters(node, language)
        positional = [p for p in old if not p.receiver]
        rendered = [_text(p.node) for p in old if p.receiver]
        for spec in specs:
            if spec.from_index is not None:
                rendered.append(
                    _rendered_old(positional[spec.from_index], spec, language)
                )
            else:
                rendered.append(_render_param(spec, language))
        if language == cs.SupportedLanguage.PYTHON:
            _check_default_order(positional, specs)
        text = "(" + ", ".join(rendered) + ")"
        patcher.replace_span(path, (params_node.start_byte, params_node.end_byte), text)

    def _rewrite_site(
        self,
        patcher: Patcher,
        row: graph_query.CallSiteRow | ResultRow,
        owner_qn: str,
        old_names: list[str],
        specs: list[ParamSpec],
        allow_heuristic: bool,
        sites: list[RewrittenSite],
        unmapped: list[UnmappedSite],
    ) -> None:
        path, line, col = row.get("path"), row.get("line"), row.get("col")
        caller = str(row.get("qualified_name") or "")
        resolution = row.get("resolution")
        resolution_text = resolution if isinstance(resolution, str) else None
        if (
            not isinstance(path, str)
            or not isinstance(line, int)
            or not isinstance(col, int)
        ):
            unmapped.append(
                UnmappedSite(
                    path if isinstance(path, str) else "",
                    0,
                    0,
                    caller,
                    cs.SIGNATURE_UNLOCATABLE,
                )
            )
            return
        if resolution_text in _AMBIGUOUS and not allow_heuristic:
            unmapped.append(
                UnmappedSite(
                    path,
                    line,
                    col,
                    caller,
                    cs.SIGNATURE_GUESSED.format(resolution=resolution_text),
                )
            )
            return
        try:
            source = patcher.source(path)
        except PatcherError:
            unmapped.append(
                UnmappedSite(path, line, col, caller, cs.SIGNATURE_MISSING_FILE)
            )
            return
        language, root = self._parse(path, source)
        call = _call_at(root, line, col) if root is not None else None
        args_node = _find_call_arguments_node(call) if call is not None else None
        if args_node is None:
            unmapped.append(UnmappedSite(path, line, col, caller, cs.SIGNATURE_NO_CALL))
            return
        new_args = _map_arguments(args_node, old_names, specs, language)
        if isinstance(new_args, str):
            unmapped.append(UnmappedSite(path, line, col, caller, new_args))
            return
        before = _text(args_node)
        after = "(" + ", ".join(new_args) + ")"
        if after != before:
            patcher.replace_span(
                path, (args_node.start_byte, args_node.end_byte), after
            )
        sites.append(
            RewrittenSite(path, line, col, caller, resolution_text, before, after)
        )

    # --- applying ---------------------------------------------------------------------

    def apply(
        self, qn: str, specs: Iterable[ParamSpec], allow_heuristic: bool = False
    ) -> SignatureReport:
        report, patcher = self.plan(qn, specs, allow_heuristic)
        tx = EditTransaction(self.repo_root)
        results = patcher.stage_into(tx)
        broken = [key for key, result in results.items() if result.parses is False]
        if broken:
            tx.rollback()
            return report._replace(
                files=tuple(sorted(results)),
                message=cs.SIGNATURE_PARSE_FAILED.format(files=", ".join(broken)),
            )

        def verifier(tree: StagedTree) -> VerificationResult | bool | None:
            return self.verify(tree) if self.verify is not None else True

        outcome = tx.commit(verifier)
        report = report._replace(
            applied=outcome.applied,
            transaction_id=outcome.transaction_id,
            files=outcome.files,
            diff=outcome.diff,
            message=outcome.message,
        )
        if outcome.applied and self.reingest is not None:
            report = self._enforce_contract(report)
        return report

    def _enforce_contract(self, report: SignatureReport) -> SignatureReport:
        assert self.reingest is not None
        delta = measure(
            self.fetch_all, self.project, self.repo_root, report.files, self.reingest
        )
        verdict = verify(
            change_signature_expectation(f"{u.path}:{u.line}" for u in report.unmapped),
            delta,
            rewritten=[(f"{s.path}:{s.line}", s.resolution) for s in report.sites],
        )
        if verdict.ok:
            return report._replace(verdict=verdict)
        undo_last(self.repo_root)
        self.reingest(list(report.files))
        return report._replace(
            applied=False,
            verdict=verdict,
            message=cs.SIGNATURE_CONTRACT_FAILED.format(
                reasons="; ".join(verdict.failures)
            ),
        )


_PY_DEFAULTED = frozenset(
    {cs.TS_PY_DEFAULT_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER}
)


def _check_default_order(positional: list[_Param], specs: list[ParamSpec]) -> None:
    """Python: no required parameter may follow one with a default."""
    seen_default = False
    for spec in specs:
        if spec.from_index is not None:
            node = positional[spec.from_index].node
            has_default = spec.default is not None or node.type in _PY_DEFAULTED
        else:
            has_default = spec.default is not None or spec.literal is not None
        if seen_default and not has_default:
            raise SignatureRefused(cs.SIGNATURE_DEFAULT_ORDER.format(name=spec.name))
        seen_default = seen_default or has_default


# --- call-site mapping ----------------------------------------------------------


def _call_at(root: Node, line: int, col: int) -> Node | None:
    """The outermost node starting at (line, col) that carries arguments."""
    stack = [root]
    found: Node | None = None
    while stack:
        node = stack.pop()
        if (
            node.start_point == (line - 1, col)
            and _find_call_arguments_node(node) is not None
        ):
            if found is None or node.end_byte > found.end_byte:
                found = node
        if node.start_point[0] <= line - 1 <= node.end_point[0]:
            stack.extend(node.children)
    return found


def _map_arguments(
    args_node: Node,
    old_names: list[str],
    specs: list[ParamSpec],
    language: cs.SupportedLanguage | None,
) -> list[str] | str:
    """The site's new argument texts, or the reason it cannot be mapped."""
    positional, keyword = _split_call_arguments(args_node)
    if any(child.type in _SPLAT_TYPES for child in args_node.named_children):
        return cs.SIGNATURE_SPLAT
    values: dict[int, tuple[str, bool]] = {}
    for index, node in enumerate(positional):
        values[index] = (_text(node), False)
    for name, node in keyword.items():
        if name not in old_names:
            return cs.SIGNATURE_UNKNOWN_KEYWORD.format(name=name)
        values[old_names.index(name)] = (_text(node), True)
    keywords_ok = language == cs.SupportedLanguage.PYTHON
    if not positional and _keywords_already_fit(values, old_names, specs):
        # A keyword-only site binds by name: reordering would change
        # nothing but the spelling, so it is left exactly as written.
        return [_text(child) for child in args_node.named_children]
    out: list[str] = []
    keyword_mode = False
    for spec in specs:
        if spec.from_index is not None:
            found = values.pop(spec.from_index, None)
            if found is None:
                # The site relied on the old default; every later value
                # must be spelled by name so positions do not shift.
                keyword_mode = True
                continue
            text, was_keyword = found
            if was_keyword or keyword_mode:
                if not keywords_ok:
                    return cs.SIGNATURE_NEEDS_KEYWORDS
                out.append(f"{spec.name}={text}")
                keyword_mode = True
            else:
                out.append(text)
        elif spec.literal is not None:
            if keyword_mode:
                if not keywords_ok:
                    return cs.SIGNATURE_NEEDS_KEYWORDS
                out.append(f"{spec.name}={spec.literal}")
            else:
                out.append(spec.literal)
        else:
            return cs.SIGNATURE_UNMAPPED_PARAM.format(name=spec.name)
    return out


def _keywords_already_fit(
    values: dict[int, tuple[str, bool]], old_names: list[str], specs: list[ParamSpec]
) -> bool:
    """Every passed keyword keeps its name and nothing new must be inserted."""
    kept = {spec.from_index: spec.name for spec in specs if spec.from_index is not None}
    if any(index not in kept or kept[index] != old_names[index] for index in values):
        return False
    return all(spec.from_index is not None for spec in specs)


# --- literal type checks --------------------------------------------------------


_KIND_OF = {
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
    list: "list",
    dict: "dict",
    set: "set",
    tuple: "tuple",
    type(None): "None",
}
_ACCEPTS: dict[str, frozenset[str]] = {
    "int": frozenset({"int"}),
    "float": frozenset({"int", "float"}),
    "str": frozenset({"str"}),
    "bool": frozenset({"bool"}),
    "list": frozenset({"list"}),
    "dict": frozenset({"dict"}),
    "set": frozenset({"set"}),
    "tuple": frozenset({"tuple"}),
    "bytes": frozenset(),
}


def _literal_kind(literal: str) -> str | None:
    try:
        value = ast.literal_eval(literal)
    except (ValueError, SyntaxError):
        return None
    return _KIND_OF.get(type(value))


def _compatible(kind: str, annotation: str) -> bool:
    """Whether a literal of `kind` fits `annotation` (builtin names only)."""
    text = annotation.strip()
    if kind == "None":
        return "None" in text or text.startswith("Optional")
    members = [m.strip() for m in re.split(r"\s*\|\s*", text)]
    if len(members) > 1:
        return any(_compatible(kind, member) for member in members)
    base = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)", text)
    if base is None:
        return True
    accepted = _ACCEPTS.get(base.group(1))
    if accepted is None:
        # Not a builtin: the graph cannot tell, so do not accuse.
        return True
    return kind in accepted


def change_signature(
    repo_root: Path,
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    new_params: Iterable[ParamSpec | str],
    allow_heuristic: bool = False,
    dry_run: bool = False,
    verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
    reingest: Reingest | None = None,
) -> SignatureReport:
    """The op: plan (rewrites definitions and mapped sites) or plan and apply."""
    specs = [parse_param_spec(p) if isinstance(p, str) else p for p in new_params]
    changer = SignatureChanger(
        repo_root, fetch_all, project_name, verify=verify, reingest=reingest
    )
    if dry_run:
        report, _patcher = changer.plan(qualified_name, specs, allow_heuristic)
        return report
    return changer.apply(qualified_name, specs, allow_heuristic)


def sites_for(sites: Iterable[NamedTuple]) -> list[dict[str, object]]:
    return [dict(site._asdict()) for site in sites]
