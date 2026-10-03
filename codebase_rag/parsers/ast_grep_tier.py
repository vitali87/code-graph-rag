# ast-grep pattern-driven language tier (issue #414). For languages with no
# tree-sitter LanguageSpec, this extracts Module/Function/Class nodes and
# DEFINES/IMPORTS edges from per-language YAML pattern configs, so adding a
# new language is a config file rather than a hand-written tree-sitter
# traversal. It is a BASIC structural tier: there is no call-graph
# resolution, and names are flat unless a config opts into `scoped_names`
# (members under their type, overloads kept apart; see ast_grep_scope).
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .. import constants as cs
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from .ast_grep_scope import (
    Declaration,
    DeclKind,
    PlacedDefinition,
    place_declarations,
    place_flat,
    type_path,
)
from .flat_module import emit_flat_module

if TYPE_CHECKING:
    from ast_grep_py import SgNode

    from ..services import IngestorProtocol

logger = logging.getLogger(__name__)

_PATTERNS_DIR = Path(__file__).parent / "ast_grep_patterns"
# leading bare name of a captured signature, for `name_head` rules
_LEADING_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*[!?]?", re.ASCII)
# Metavar conventions contributors must follow in the YAML patterns.
_NAME_METAVAR = "NAME"
_PATH_METAVAR = "PATH"
# Child node kinds that carry a declaration's name, tried in order when a
# kind rule's node exposes no `name` field. Grammars differ on which one
# they use (kotlin: simple_identifier, haskell/solidity: identifier or a
# *_id node), so the fallback list spans them rather than per-language.
_NAME_CHILD_KINDS = (
    "name",
    "identifier",
    "simple_identifier",
    "type_identifier",
    "constructor",
    "module_id",
    "variable",
)


@dataclass(frozen=True)
class _Rule:
    """One matcher from a config: either an ast-grep pattern or a node kind.

    A pattern captures its result in the $NAME/$PATH metavar. A `kind` rule
    matches a node type instead, which is what grammars need when modifiers
    sit outside the matched construct (`private suspend fun f()` is still a
    kotlin `function_declaration`, but no fixed pattern spells every modifier
    combination). Kind rules take the name from the node's `name` field, or
    else its first `_NAME_CHILD_KINDS` child.
    """

    pattern: str | None = None
    kind: str | None = None
    # for kind rules whose name lives on a non-standard child kind
    name_child: str | None = None
    # kind rules only: skip matches without a child of this kind. Needed when
    # one grammar node covers several concepts (a nix `binding` is a function
    # only when its value is a `function_expression`; without this every
    # attribute in a set would be emitted as a Function).
    has_child: str | None = None
    # pattern rules only: keep just the leading identifier of the capture.
    # Elixir defs are macro calls, so the only pattern that matches a
    # zero-arg or guarded `def` captures the whole signature
    # (`guarded(x) when is_integer(x)`); this trims it to `guarded`.
    name_head: bool = False
    # kind rules only: the child naming the type a declaration adds itself to
    # (kotlin `fun Client.get()` has a receiver_type child), so it is named
    # under that type instead of colliding with every other `get`.
    receiver_child: str | None = None

    @property
    def label(self) -> str:
        return self.pattern if self.pattern is not None else f"kind={self.kind}"


def _require_exactly_one_selector(
    pattern: object, kind: object, path_name: str, section: str
) -> None:
    # A rule selects nodes by `pattern` OR by `kind`, never both and never
    # neither. Compared as bools so an empty string counts as absent.
    if bool(pattern) == bool(kind):
        raise ValueError(
            f"{path_name}: each {section} rule needs exactly one of 'pattern' or 'kind'"
        )


def _require_selector_applies(
    fields: Mapping[str, object], pattern: object, kind: object, path_name: str
) -> None:
    # Each modifier is meaningful for only one kind of selector.
    for field, needs, needed_by in (
        ("name_head", pattern, "pattern"),
        ("has_child", kind, "kind"),
        ("name_child", kind, "kind"),
        ("receiver_child", kind, "kind"),
    ):
        if fields.get(field) and not needs:
            raise ValueError(
                f"{path_name}: '{field}' applies to '{needed_by}' rules only"
            )


def _parse_rule(raw: object, path_name: str, section: str) -> _Rule:
    if isinstance(raw, str):
        return _Rule(pattern=raw)
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path_name}: {section} rules must be a string or a mapping")
    fields = cast("Mapping[str, object]", raw)
    pattern = fields.get("pattern")
    kind = fields.get("kind")
    _require_exactly_one_selector(pattern, kind, path_name, section)
    _require_selector_applies(fields, pattern, kind, path_name)
    name_child = fields.get("name_child")
    has_child = fields.get("has_child")
    receiver_child = fields.get("receiver_child")
    return _Rule(
        pattern=str(pattern) if pattern else None,
        kind=str(kind) if kind else None,
        name_child=str(name_child) if name_child else None,
        has_child=str(has_child) if has_child else None,
        name_head=bool(fields.get("name_head")),
        receiver_child=str(receiver_child) if receiver_child else None,
    )


def _parse_rules(raw: object, path_name: str, section: str) -> tuple[_Rule, ...]:
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{path_name}: '{section}' must be a list of rules")
    return tuple(_parse_rule(item, path_name, section) for item in raw)


@dataclass(frozen=True)
class _LangConfig:
    # the config's `language`, falling back to the file stem; only displayed
    language: str
    ast_grep_id: str
    functions: tuple[_Rule, ...]
    classes: tuple[_Rule, ...]
    imports: tuple[_Rule, ...]
    # Declarations that add members to a type named elsewhere (swift
    # `extension T`) rather than declaring one.
    type_extensions: tuple[_Rule, ...] = ()
    # Opt-in, per language: an Elixir multi-clause def or a Haskell
    # multi-equation function is ONE function matched several times, which
    # the scoped naming would split into `@line` variants.
    scoped_names: bool = False


def _require_scoped_names(
    scoped_names: bool,
    functions: tuple[_Rule, ...],
    type_extensions: tuple[_Rule, ...],
    path_name: str,
) -> None:
    # Extended and receiver types only exist as a scope to name members
    # under; with flat names they would be silently ignored.
    if scoped_names:
        return
    if type_extensions or any(rule.receiver_child for rule in functions):
        raise ValueError(
            f"{path_name}: 'type_extensions' and 'receiver_child' need 'scoped_names: true'"
        )


def _pattern_config_paths() -> list[Path]:
    return sorted(_PATTERNS_DIR.glob("*.yaml"))


def load_pattern_configs() -> dict[str, _LangConfig]:
    """Load every ast_grep_patterns/*.yaml, keyed by file extension."""
    import yaml

    configs: dict[str, _LangConfig] = {}
    for path in _pattern_config_paths():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        extensions = data.get("extensions")
        ast_grep_id = data.get("ast_grep_id")
        if not extensions or not ast_grep_id:
            raise ValueError(
                f"{path.name}: 'extensions' and 'ast_grep_id' are required"
            )
        if isinstance(extensions, str):
            extensions = [ext.strip() for ext in extensions.split(",") if ext.strip()]
        functions = _parse_rules(data.get("functions"), path.name, "functions")
        type_extensions = _parse_rules(
            data.get("type_extensions"), path.name, "type_extensions"
        )
        scoped_names = bool(data.get("scoped_names"))
        _require_scoped_names(scoped_names, functions, type_extensions, path.name)
        config = _LangConfig(
            language=str(data.get("language") or path.stem),
            ast_grep_id=str(ast_grep_id),
            functions=functions,
            classes=_parse_rules(data.get("classes"), path.name, "classes"),
            imports=_parse_rules(data.get("imports"), path.name, "imports"),
            type_extensions=type_extensions,
            scoped_names=scoped_names,
        )
        for extension in extensions:
            configs[extension] = config
    return configs


@cache
def structural_tier_extensions() -> frozenset[str]:
    """File extensions handled by this tier, for consumers that must skip them.

    Analyses built on the call graph (dead code) cannot say anything about a
    language parsed here, since the tier emits no CALLS edges. Reading the
    shipped configs keeps those consumers correct as languages are added.
    Returns empty if the [ast-grep] extra is absent, matching the disabled tier.
    """
    try:
        return frozenset(load_pattern_configs())
    except Exception:  # noqa: BLE001
        return frozenset()


def structural_tier_languages() -> dict[str, tuple[str, ...]]:
    """Each configured language's name, mapped to the extensions routed to it.

    Grouped from the same loader the tier uses, so a language listed here is
    exactly one the tier parses once the [ast-grep] extra is installed.
    Raises what `load_pattern_configs` raises: a caller that lists languages
    decides for itself how to report a config it cannot read.
    """
    languages: dict[str, list[str]] = {}
    for extension, config in load_pattern_configs().items():
        languages.setdefault(config.language, []).append(extension)
    return {name: tuple(extensions) for name, extensions in languages.items()}


def structural_tier_config_names() -> tuple[str, ...]:
    """Each configured language's name, read from the config file names.

    Needs no YAML parser: PyYAML comes with the [ast-grep] extra, and a
    listing must still name the languages that extra adds when it is missing
    (Greptile review of PR 2508). The file stem is also the name
    `load_pattern_configs` falls back to.
    """
    return tuple(path.stem for path in _pattern_config_paths())


def _leading_identifier(text: str) -> str | None:
    """The bare name at the head of a captured signature.

    `guarded(x) when is_integer(x)` -> `guarded`, `zero_arg` -> `zero_arg`.
    Returns None when the capture does not start with an identifier, so a
    stray match is dropped rather than emitted under a junk name.
    """
    match = _LEADING_IDENTIFIER_RE.match(text.strip())
    return match.group(0) if match else None


def _declaration(
    kind: DeclKind, name: str, node: SgNode, rule: _Rule
) -> Declaration | None:
    """A matched node as a Declaration; None for an unreadable extension."""
    receiver: str | None = None
    if kind == DeclKind.EXTENSION:
        # Its position is claimed even when this returns None, so an
        # extension of a type this cannot name (`extension [String]`) never
        # falls through to the classes rule as a Class of that name.
        extended = type_path(name)
        if extended is None:
            return None
        name = extended
    elif rule.receiver_child:
        for child in node.children():
            if child.kind() == rule.receiver_child:
                receiver = type_path(child.text())
                break
    node_range = node.range()
    return Declaration(
        kind=kind,
        name=name,
        start=node_range.start.index,
        end=node_range.end.index,
        start_line=node_range.start.line + 1,
        end_line=node_range.end.line + 1,
        start_col=node_range.start.column,
        receiver=receiver,
    )


def _strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    return text


class AstGrepTier:
    """Structural extractor for languages without a tree-sitter LanguageSpec."""

    __slots__ = ("_ingestor", "_repo_path", "_project_name", "_configs")

    def __init__(
        self, ingestor: IngestorProtocol, repo_path: Path, project_name: str
    ) -> None:
        self._ingestor = ingestor
        self._repo_path = repo_path
        self._project_name = project_name
        try:
            import ast_grep_py  # noqa: F401

            self._configs = load_pattern_configs()
        except ImportError:
            # ast-grep/pyyaml are the [ast-grep] extra; no-op if absent.
            logger.warning("ast-grep-py unavailable; ast-grep language tier disabled")
            self._configs = {}
        except Exception as exc:  # noqa: BLE001
            # a malformed shipped config must not crash GraphUpdater
            # construction; disable the tier and surface the reason.
            logger.warning("ast-grep language tier disabled: %s", exc)
            self._configs = {}

    def handles(self, suffix: str) -> bool:
        return suffix in self._configs

    def process_file(
        self, file_path: Path, structural_elements: dict[Path, str | None]
    ) -> None:
        config = self._configs.get(file_path.suffix)
        if config is None:
            return
        try:
            source = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        from ast_grep_py import SgRoot

        try:
            root = SgRoot(source, config.ast_grep_id).root()
        except (RuntimeError, ValueError) as exc:
            logger.warning("ast-grep failed to parse %s: %s", file_path, exc)
            return

        module_qn = self._emit_module(file_path, structural_elements)
        relative_path = cached_relative_path(file_path, self._repo_path).as_posix()
        absolute_path = cached_resolve_posix(file_path)

        declarations = self._collect_declarations(root, config, file_path)
        placed = (
            place_declarations(declarations, module_qn)
            if config.scoped_names
            else place_flat(declarations, module_qn)
        )
        for definition in placed:
            self._emit_definition(definition, relative_path, absolute_path)
        self._extract_imports(root, config.imports, file_path, module_qn)

    def _collect_declarations(
        self, root: SgNode, config: _LangConfig, file_path: Path
    ) -> list[Declaration]:
        # Functions, then types, each group deduped on its own so a class and
        # a function sharing a line both land. Extensions share the types'
        # group and go first: swift spells `extension T` with the same kind as
        # `class T`, and the classes rule must not read it as a second T.
        functions = ((DeclKind.FUNCTION, config.functions),)
        types = (
            (DeclKind.EXTENSION, config.type_extensions),
            (DeclKind.TYPE, config.classes),
        )
        return [
            *self._collect_group(root, functions, file_path),
            *self._collect_group(root, types, file_path),
        ]

    def _collect_group(
        self,
        root: SgNode,
        group: tuple[tuple[DeclKind, tuple[_Rule, ...]], ...],
        file_path: Path,
    ) -> list[Declaration]:
        declarations: list[Declaration] = []
        claimed: set[tuple[int, int]] = set()
        for kind, rules in group:
            for rule in rules:
                for node in self._find_all(root, rule, file_path):
                    if declaration := self._claim(kind, rule, node, claimed):
                        declarations.append(declaration)
        return declarations

    def _claim(
        self,
        kind: DeclKind,
        rule: _Rule,
        node: SgNode,
        claimed: set[tuple[int, int]],
    ) -> Declaration | None:
        name = self._definition_name(node, rule)
        if name is None:
            return None
        start = node.range().start
        # Keyed on (line, column), not line alone: overlapping rules for the
        # SAME declaration start at the same column, so a specific pattern
        # (def self.$NAME) still wins over a general one (def $NAME), while
        # two distinct declarations sharing a line (`fun a() {}; fun b() {}`)
        # keep their own nodes instead of the second vanishing.
        position = (start.line, start.column)
        if position in claimed:
            return None
        claimed.add(position)
        return _declaration(kind, name, node, rule)

    def _definition_name(self, node: SgNode, rule: _Rule) -> str | None:
        """The declared name of a matched node, or None if it has none."""
        if rule.pattern is not None:
            name_node = node.get_match(_NAME_METAVAR)
            if name_node is None:
                return None
            text = name_node.text()
            return _leading_identifier(text) if rule.name_head else text
        # kind rule: prefer the grammar's `name` field, then a named child of
        # a known identifier kind. A config may pin an explicit child kind
        # when a grammar puts something else first.
        if rule.name_child:
            for child in node.children():
                if child.kind() == rule.name_child:
                    return child.text()
            return None
        field = node.field("name")
        if field is not None:
            return field.text()
        for child in node.children():
            if child.kind() in _NAME_CHILD_KINDS:
                return child.text()
        return None

    def _extract_imports(
        self,
        root: SgNode,
        rules: tuple[_Rule, ...],
        file_path: Path,
        module_qn: str,
    ) -> None:
        for rule in rules:
            for node in self._find_all(root, rule, file_path):
                if rule.pattern is not None:
                    target_node = node.get_match(_PATH_METAVAR)
                    target = target_node.text() if target_node is not None else None
                else:
                    target = self._definition_name(node, rule)
                if target:
                    self._emit_import(_strip_quotes(target), module_qn)

    def _find_all(self, root: SgNode, rule: _Rule, file_path: Path) -> list[SgNode]:
        try:
            if rule.pattern is not None:
                return root.find_all(pattern=rule.pattern)
            # `_parse_rule` gives every rule exactly one selector; a rule built
            # with neither matches nothing rather than failing the whole file.
            if rule.kind is None:
                return []
            matches = root.find_all(kind=rule.kind)
            if rule.has_child:
                matches = [
                    node
                    for node in matches
                    if any(c.kind() == rule.has_child for c in node.children())
                ]
            return matches
        except RuntimeError as exc:
            logger.warning(
                "bad ast-grep rule %s for %s: %s", rule.label, file_path, exc
            )
            return []

    def _emit_module(
        self, file_path: Path, structural_elements: dict[Path, str | None]
    ) -> str:
        """Emit the file's Module node and return its qualified name.

        The name carries the extension (`app.rb` -> `<project>.app_rb`) for
        every tier language, not only the ones that can collide. Graphs
        indexed before this carry the unsuffixed names and keep them until
        re-indexed, so a graph holding both vintages holds both shapes; a
        query written against the old shape will not match a re-indexed
        module (issue #1429).
        """
        return emit_flat_module(
            self._ingestor,
            self._repo_path,
            self._project_name,
            file_path,
            structural_elements,
            # Several tier languages declare two extensions (.sh/.bash,
            # .kt/.kts, .ex/.exs), and Module is keyed on qualified_name, so
            # dropping the suffix merges a colliding pair onto one node
            # (issue #1429).
            distinguish_suffix=True,
        )

    def _emit_definition(
        self,
        definition: PlacedDefinition,
        relative_path: str,
        absolute_path: str,
    ) -> None:
        """Emit one definition node and the edge from what defines it."""
        self._ingestor.ensure_node_batch(
            definition.label,
            {
                cs.KEY_QUALIFIED_NAME: definition.qualified_name,
                cs.KEY_NAME: definition.name,
                cs.KEY_MODIFIERS: [],
                cs.KEY_DECORATORS: [],
                cs.KEY_START_LINE: definition.start_line,
                cs.KEY_END_LINE: definition.end_line,
                cs.KEY_DOCSTRING: None,
                # no visibility analysis for these languages; mark exported
                # so dead-code does not false-flag everything.
                cs.KEY_IS_EXPORTED: True,
                cs.KEY_PATH: relative_path,
                cs.KEY_ABSOLUTE_PATH: absolute_path,
                # No ast_fingerprint props: SgNode carries no tree-sitter
                # tree, so structural clone detection cannot cover the
                # pattern-tier languages; `cgr duplicates` reports them as
                # skipped rather than analyzed.
            },
        )
        self._ingestor.ensure_relationship_batch(
            (definition.parent_label, cs.KEY_QUALIFIED_NAME, definition.parent_qn),
            definition.relationship,
            (definition.label, cs.KEY_QUALIFIED_NAME, definition.qualified_name),
        )

    def _emit_import(self, target: str, module_qn: str) -> None:
        if not target:
            return
        # every require target is treated as an external module; local
        # require_relative resolution needs path handling this tier skips.
        self._ingestor.ensure_node_batch(
            cs.NodeLabel.EXTERNAL_MODULE,
            {
                cs.KEY_NAME: target,
                cs.KEY_QUALIFIED_NAME: target,
                cs.KEY_PATH: target,
            },
        )
        self._ingestor.ensure_relationship_batch(
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            cs.RelationshipType.IMPORTS,
            (cs.NodeLabel.EXTERNAL_MODULE, cs.KEY_QUALIFIED_NAME, target),
        )
