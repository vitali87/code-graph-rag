# (H) ast-grep pattern-driven language tier (issue #414). For languages with no
# (H) tree-sitter LanguageSpec, this extracts Module/Function/Class nodes and
# (H) DEFINES/IMPORTS edges from per-language YAML pattern configs, so adding a
# (H) new language is a config file rather than a hand-written tree-sitter
# (H) traversal. It is a BASIC structural tier: names are flat (no nested
# (H) namespace qualification) and there is no call-graph resolution.
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import constants as cs
from ..utils.path_utils import cached_relative_path, cached_resolve_posix

if TYPE_CHECKING:
    from ast_grep_py import SgNode

    from ..services import IngestorProtocol

logger = logging.getLogger(__name__)

_PATTERNS_DIR = Path(__file__).parent / "ast_grep_patterns"
# (H) Metavar conventions contributors must follow in the YAML patterns.
_NAME_METAVAR = "NAME"
_PATH_METAVAR = "PATH"


@dataclass(frozen=True)
class _LangConfig:
    ast_grep_id: str
    functions: tuple[str, ...]
    classes: tuple[str, ...]
    imports: tuple[str, ...]


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
            functions=tuple(data.get("functions") or ()),
            classes=tuple(data.get("classes") or ()),
            imports=tuple(data.get("imports") or ()),
        )
        for extension in extensions:
            configs[extension] = config
    return configs


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
            # (H) ast-grep/pyyaml are the [ast-grep] extra; no-op if absent.
            logger.warning("ast-grep-py unavailable; ast-grep language tier disabled")
            self._configs = {}
        except Exception as exc:  # noqa: BLE001
            # (H) a malformed shipped config must not crash GraphUpdater
            # (H) construction; disable the tier and surface the reason.
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

        # (H) Functions then classes; dedupe by start line PER label so a specific
        # (H) pattern (def self.$NAME) wins over a general one (def $NAME) on the
        # (H) same line, while a class and a function sharing a line (one-liners)
        # (H) both still land.
        for label, patterns in (
            (cs.NodeLabel.FUNCTION, config.functions),
            (cs.NodeLabel.CLASS, config.classes),
        ):
            self._extract_definitions(
                root,
                label,
                patterns,
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
        patterns: tuple[str, ...],
        file_path: Path,
        module_qn: str,
        relative_path: str,
        absolute_path: str,
    ) -> None:
        claimed: set[int] = set()
        for pattern in patterns:
            for node in self._find_all(root, pattern, file_path):
                name_node = node.get_match(_NAME_METAVAR)
                if name_node is None:
                    continue
                line = node.range().start.line
                if line in claimed:
                    continue
                claimed.add(line)
                self._emit_definition(
                    label,
                    name_node.text(),
                    node,
                    module_qn,
                    relative_path,
                    absolute_path,
                )

    def _extract_imports(
        self,
        root: SgNode,
        patterns: tuple[str, ...],
        file_path: Path,
        module_qn: str,
    ) -> None:
        for pattern in patterns:
            for node in self._find_all(root, pattern, file_path):
                target_node = node.get_match(_PATH_METAVAR)
                if target_node is not None:
                    self._emit_import(_strip_quotes(target_node.text()), module_qn)

    def _find_all(self, root: SgNode, pattern: str, file_path: Path) -> list[SgNode]:
        try:
            return root.find_all(pattern=pattern)
        except RuntimeError as exc:
            logger.warning(
                "bad ast-grep pattern %r for %s: %s", pattern, file_path, exc
            )
            return []

    def _emit_module(
        self, file_path: Path, structural_elements: dict[Path, str | None]
    ) -> str:
        relative_path = cached_relative_path(file_path, self._repo_path)
        # (H) flat module qn, no init/mod special-case or stem
        # (H) disambiguation; add if a config language collides with another
        # (H) file's stem in the same directory.
        module_qn = cs.SEPARATOR_DOT.join(
            [self._project_name, *relative_path.with_suffix("").parts]
        )
        self._ingestor.ensure_node_batch(
            cs.NodeLabel.MODULE,
            {
                cs.KEY_QUALIFIED_NAME: module_qn,
                cs.KEY_NAME: file_path.name,
                cs.KEY_PATH: relative_path.as_posix(),
                cs.KEY_ABSOLUTE_PATH: cached_resolve_posix(file_path),
            },
        )
        parent_rel_path = relative_path.parent
        parent_container_qn = structural_elements.get(parent_rel_path)
        if parent_container_qn:
            parent = (cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, parent_container_qn)
        elif parent_rel_path != Path("."):
            parent = (cs.NodeLabel.FOLDER, cs.KEY_PATH, parent_rel_path.as_posix())
        else:
            parent = (cs.NodeLabel.PROJECT, cs.KEY_NAME, self._project_name)
        self._ingestor.ensure_relationship_batch(
            parent,
            cs.RelationshipType.CONTAINS_MODULE,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
        )
        return module_qn

    def _emit_definition(
        self,
        label: cs.NodeLabel,
        name: str,
        node: SgNode,
        module_qn: str,
        relative_path: str,
        absolute_path: str,
    ) -> None:
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
                # (H) no visibility analysis for these languages; mark
                # (H) exported so dead-code does not false-flag everything.
                cs.KEY_IS_EXPORTED: True,
                cs.KEY_PATH: relative_path,
                cs.KEY_ABSOLUTE_PATH: absolute_path,
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
        # (H) every require target is treated as an external module; local
        # (H) require_relative resolution needs path handling this tier skips.
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
