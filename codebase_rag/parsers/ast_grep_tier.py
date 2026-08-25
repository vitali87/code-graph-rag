# ast-grep pattern-driven language tier (issue #414). For languages with no
# tree-sitter LanguageSpec, this extracts Module/Function/Class nodes and
# DEFINES/IMPORTS edges from per-language YAML pattern configs, so adding a
# new language is a config file rather than a hand-written tree-sitter
# traversal. It is a BASIC structural tier: names are flat (no nested
# namespace qualification) and there is no call-graph resolution.
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
from .flat_module import emit_flat_module

if TYPE_CHECKING:
    from ast_grep_py import SgNode

    from ..services import IngestorProtocol

logger = logging.getLogger(__name__)

_PATTERNS_DIR = Path(__file__).parent / "ast_grep_patterns"
# leading bare name of a captured signature, for `name_head` rules
_LEADING_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*[!?]?")
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

    @property
    def label(self) -> str:
        return self.pattern if self.pattern is not None else f"kind={self.kind}"


def _parse_rule(raw: object, path_name: str, section: str) -> _Rule:
    if isinstance(raw, str):
        return _Rule(pattern=raw)
    if isinstance(raw, Mapping):
        fields = cast("Mapping[str, object]", raw)
        pattern = fields.get("pattern")
        kind = fields.get("kind")
        if bool(pattern) == bool(kind):
            raise ValueError(
                f"{path_name}: each {section} rule needs exactly one of "
                f"'pattern' or 'kind'"
            )
        name_child = fields.get("name_child")
        has_child = fields.get("has_child")
        name_head = bool(fields.get("name_head"))
        if name_head and not pattern:
            raise ValueError(
                f"{path_name}: 'name_head' applies to 'pattern' rules only"
            )
        if has_child and not kind:
            raise ValueError(f"{path_name}: 'has_child' applies to 'kind' rules only")
        if name_child and not kind:
            raise ValueError(f"{path_name}: 'name_child' applies to 'kind' rules only")
        return _Rule(
            pattern=str(pattern) if pattern else None,
            kind=str(kind) if kind else None,
            name_child=str(name_child) if name_child else None,
            has_child=str(has_child) if has_child else None,
            name_head=name_head,
        )
    raise ValueError(f"{path_name}: {section} rules must be a string or a mapping")


def _parse_rules(raw: object, path_name: str, section: str) -> tuple[_Rule, ...]:
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{path_name}: '{section}' must be a list of rules")
    return tuple(_parse_rule(item, path_name, section) for item in raw)


@dataclass(frozen=True)
class _LangConfig:
    ast_grep_id: str
    functions: tuple[_Rule, ...]
    classes: tuple[_Rule, ...]
    imports: tuple[_Rule, ...]


def load_pattern_configs() -> dict[str, _LangConfig]:
    """Load every ast_grep_patterns/*.yaml, keyed by file extension."""
    import yaml

    configs: dict[str, _LangConfig] = {}
    for path in sorted(_PATTERNS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        extensions = data.get("extensions")
        ast_grep_id = data.get("ast_grep_id")
        if not extensions or not ast_grep_id:
            raise ValueError(
                f"{path.name}: 'extensions' and 'ast_grep_id' are required"
            )
        if isinstance(extensions, str):
            extensions = [ext.strip() for ext in extensions.split(",") if ext.strip()]
        config = _LangConfig(
            ast_grep_id=str(ast_grep_id),
            functions=_parse_rules(data.get("functions"), path.name, "functions"),
            classes=_parse_rules(data.get("classes"), path.name, "classes"),
            imports=_parse_rules(data.get("imports"), path.name, "imports"),
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


def _leading_identifier(text: str) -> str | None:
    """The bare name at the head of a captured signature.

    `guarded(x) when is_integer(x)` -> `guarded`, `zero_arg` -> `zero_arg`.
    Returns None when the capture does not start with an identifier, so a
    stray match is dropped rather than emitted under a junk name.
    """
    match = _LEADING_IDENTIFIER_RE.match(text.strip())
    return match.group(0) if match else None


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

        # Functions then classes; dedupe by start line PER label so a specific
        # pattern (def self.$NAME) wins over a general one (def $NAME) on the
        # same line, while a class and function sharing a line both still land.
        for label, rules in (
            (cs.NodeLabel.FUNCTION, config.functions),
            (cs.NodeLabel.CLASS, config.classes),
        ):
            self._extract_definitions(
                root,
                label,
                rules,
                file_path,
                module_qn,
                relative_path,
                absolute_path,
            )
        self._extract_imports(root, config.imports, file_path, module_qn)

    def _extract_definitions(
        self,
        root: SgNode,
        label: cs.NodeLabel,
        rules: tuple[_Rule, ...],
        file_path: Path,
        module_qn: str,
        relative_path: str,
        absolute_path: str,
    ) -> None:
        claimed: set[tuple[int, int]] = set()
        for rule in rules:
            for node in self._find_all(root, rule, file_path):
                name = self._definition_name(node, rule)
                if name is None:
                    continue
                start = node.range().start
                # Keyed on (line, column), not line alone: overlapping rules
                # for the SAME declaration start at the same column, so the
                # specific rule still wins over the general one, while two
                # distinct declarations sharing a line (`fun a() {}; fun b()
                # {}`) keep their own nodes instead of the second vanishing.
                position = (start.line, start.column)
                if position in claimed:
                    continue
                claimed.add(position)
                self._emit_definition(
                    label,
                    name,
                    node,
                    module_qn,
                    relative_path,
                    absolute_path,
                )

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
        """Emit the file's Module node and return its qualified name."""
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
        label: cs.NodeLabel,
        name: str,
        node: SgNode,
        module_qn: str,
        relative_path: str,
        absolute_path: str,
    ) -> None:
        """Emit one definition node and its DEFINES edge from the module."""
        qualified_name = f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        node_range = node.range()
        self._ingestor.ensure_node_batch(
            label,
            {
                cs.KEY_QUALIFIED_NAME: qualified_name,
                cs.KEY_NAME: name,
                cs.KEY_MODIFIERS: [],
                cs.KEY_DECORATORS: [],
                cs.KEY_START_LINE: node_range.start.line + 1,
                cs.KEY_END_LINE: node_range.end.line + 1,
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
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            cs.RelationshipType.DEFINES,
            (label, cs.KEY_QUALIFIED_NAME, qualified_name),
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
