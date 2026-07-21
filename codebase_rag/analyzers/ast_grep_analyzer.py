# (H) ast-grep finding analyzer (issue #413). A post-pass over indexed source
# (H) files that runs categorized ast-grep YAML rules and emits
# (H) Pattern/CodeSmell/SecurityIssue nodes linked to each file's Module. Rules
# (H) live in ast_grep_rules/{patterns,smells,security}/<lang>.yaml; a new rule is
# (H) a YAML entry, no code. The FINDINGS capture group is opt-in, so the analyzer
# (H) no-ops entirely unless a finding relationship is enabled. Findings link to
# (H) the Module (not the enclosing Class/Function); the finding node carries the
# (H) line so the site is still locatable. Symbol-level linkage is a follow-up.
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import constants as cs
from ..utils.path_utils import cached_relative_path

if TYPE_CHECKING:
    from ..capture import CaptureSelection
    from ..services import IngestorProtocol

logger = logging.getLogger(__name__)

_RULES_DIR = Path(__file__).parent / "ast_grep_rules"
_SNIPPET_MAX = 200

# (H) rule-category directory name -> (finding node label, relationship type).
_CATEGORY_MAP: dict[str, tuple[cs.NodeLabel, cs.RelationshipType]] = {
    "patterns": (cs.NodeLabel.PATTERN, cs.RelationshipType.IMPLEMENTS_PATTERN),
    "smells": (cs.NodeLabel.CODE_SMELL, cs.RelationshipType.HAS_SMELL),
    "security": (cs.NodeLabel.SECURITY_ISSUE, cs.RelationshipType.HAS_VULNERABILITY),
}


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    message: str
    body: dict[str, Any]  # (H) ast-grep rule body, splatted into find_all(**body)
    node_label: cs.NodeLabel
    rel_type: cs.RelationshipType


@dataclass(frozen=True)
class _LangRules:
    ast_grep_id: str
    rules: tuple[_Rule, ...]


def load_finding_rules() -> dict[str, _LangRules]:
    """Load ast_grep_rules/<category>/*.yaml, merged per file extension."""
    lang_id_by_ext: dict[str, str] = {}
    rules_by_ext: dict[str, list[_Rule]] = {}
    for category_dir in sorted(_RULES_DIR.iterdir()):
        mapping = _CATEGORY_MAP.get(category_dir.name)
        if not category_dir.is_dir() or mapping is None:
            continue
        node_label, rel_type = mapping
        for path in sorted(category_dir.glob("*.yaml")):
            ast_grep_id, extensions, rules = _parse_rule_file(
                path, node_label, rel_type
            )
            for ext in extensions:
                lang_id_by_ext[ext] = ast_grep_id
                rules_by_ext.setdefault(ext, []).extend(rules)
    return {
        ext: _LangRules(lang_id_by_ext[ext], tuple(rules))
        for ext, rules in rules_by_ext.items()
    }


def _parse_rule_file(
    path: Path, node_label: cs.NodeLabel, rel_type: cs.RelationshipType
) -> tuple[str, list[str], list[_Rule]]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    ast_grep_id = data.get("ast_grep_id")
    extensions = data.get("extensions")
    if isinstance(extensions, str):
        extensions = [ext.strip() for ext in extensions.split(",") if ext.strip()]
    if not ast_grep_id or not extensions:
        raise ValueError(f"{path.name}: 'ast_grep_id' and 'extensions' are required")
    rules = [
        _Rule(
            rule_id=str(entry["id"]),
            message=str(entry.get("message", entry["id"])),
            body=entry["rule"],
            node_label=node_label,
            rel_type=rel_type,
        )
        for entry in (data.get("rules") or [])
    ]
    return str(ast_grep_id), list(extensions), rules


class FindingAnalyzer:
    """Runs ast-grep finding rules over indexed files and emits finding nodes."""

    __slots__ = ("_ingestor", "_repo_path", "_enabled", "_rules")

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        selection: CaptureSelection,
    ) -> None:
        self._ingestor = ingestor
        self._repo_path = repo_path
        finding_rels = cs.CAPTURE_GROUP_RELS[cs.CaptureGroup.FINDINGS]
        self._enabled = any(selection.rel_enabled(rel) for rel in finding_rels)
        self._rules: dict[str, _LangRules] = {}
        if not self._enabled:
            return
        try:
            import ast_grep_py  # noqa: F401

            self._rules = load_finding_rules()
        except ImportError:
            # (H) ast-grep/pyyaml are the [ast-grep] extra; no-op if absent.
            logger.warning("ast-grep-py unavailable; finding analyzer disabled")
        except Exception as exc:  # noqa: BLE001
            # (H) a malformed shipped rule file must not crash indexing.
            logger.warning("ast-grep finding analyzer disabled: %s", exc)

    def analyze(self, module_qn_to_file_path: dict[str, Path]) -> None:
        if not self._enabled or not self._rules:
            return
        from ast_grep_py import SgRoot

        for module_qn, file_path in module_qn_to_file_path.items():
            lang_rules = self._rules.get(file_path.suffix)
            if lang_rules is None:
                continue
            try:
                raw = file_path.read_bytes()
            except OSError:
                continue
            # (H) decode with replacement so a few malformed bytes don't skip the
            # (H) whole file; ast-grep tolerates U+FFFD in the source.
            source = raw.decode("utf-8", errors="replace")
            try:
                root = SgRoot(source, lang_rules.ast_grep_id).root()
            except (RuntimeError, ValueError) as exc:
                logger.warning("ast-grep failed to parse %s: %s", file_path, exc)
                continue
            relative_path = cached_relative_path(file_path, self._repo_path).as_posix()
            for rule in lang_rules.rules:
                self._emit_rule(root, rule, module_qn, relative_path, file_path)

    def _emit_rule(
        self,
        root: Any,
        rule: _Rule,
        module_qn: str,
        relative_path: str,
        file_path: Path,
    ) -> None:
        try:
            matches = root.find_all(**rule.body)
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.warning(
                "bad ast-grep rule %r for %s: %s", rule.rule_id, file_path, exc
            )
            return
        for node in matches:
            self._emit_finding(rule, node, module_qn, relative_path)

    def _emit_finding(
        self, rule: _Rule, node: Any, module_qn: str, relative_path: str
    ) -> None:
        node_range = node.range()
        start_line = node_range.start.line + 1
        end_line = node_range.end.line + 1
        # (H) qn scopes the finding to file+line+column+rule so two matches of one
        # (H) rule on the same line stay distinct, while re-indexing merges the
        # (H) same site idempotently.
        qualified_name = cs.SEPARATOR_DOT.join(
            [module_qn, str(start_line), str(node_range.start.column), rule.rule_id]
        )
        snippet = node.text()
        if len(snippet) > _SNIPPET_MAX:
            snippet = snippet[:_SNIPPET_MAX]
        self._ingestor.ensure_node_batch(
            rule.node_label,
            {
                cs.KEY_QUALIFIED_NAME: qualified_name,
                cs.KEY_NAME: rule.rule_id,
                cs.KEY_MESSAGE: rule.message,
                cs.KEY_START_LINE: start_line,
                cs.KEY_END_LINE: end_line,
                cs.KEY_PATH: relative_path,
                cs.KEY_SNIPPET: snippet,
            },
        )
        self._ingestor.ensure_relationship_batch(
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            rule.rel_type,
            (rule.node_label, cs.KEY_QUALIFIED_NAME, qualified_name),
        )
