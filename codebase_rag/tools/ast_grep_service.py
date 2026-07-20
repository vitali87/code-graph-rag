# (H) Structural search/replace over the project using ast-grep patterns (#415).
# (H) Purely additive: no graph, parser, or index dependency. Language is chosen
# (H) per file from its extension; languages ast-grep has no grammar for (scala,
# (H) dart) are skipped. Metavariables in a rewrite are interpolated here because
# (H) ast-grep's node.replace() does not substitute them.
from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from .. import constants as cs
from ..language_spec import get_language_for_extension
from ..types_defs import StructuralReplaceChange, StructuralSearchMatch
from ..utils.path_utils import should_skip_path

if TYPE_CHECKING:
    from ast_grep_py import SgNode


class AstGrepService:
    __slots__ = ("project_root",)

    def __init__(self, project_root: str = ".") -> None:
        self.project_root = Path(project_root).resolve()

    def _resolve_language(self, language: str) -> str:
        try:
            supported = cs.SupportedLanguage(language)
        except ValueError:
            supported = None
        ast_grep_lang = (
            cs.AST_GREP_LANGUAGES.get(supported) if supported is not None else None
        )
        if ast_grep_lang is None:
            raise ValueError(
                cs.AST_GREP_UNKNOWN_LANGUAGE.format(
                    language=language,
                    supported=", ".join(lang.value for lang in cs.AST_GREP_LANGUAGES),
                )
            )
        return ast_grep_lang

    def _iter_source_files(self, language: str | None) -> list[tuple[Path, str, str]]:
        # (H) (abs_path, rel_posix, ast_grep_lang) for every file ast-grep can
        # (H) parse, honouring the same ignore rules as graph ingestion.
        wanted = self._resolve_language(language) if language else None
        out: list[tuple[Path, str, str]] = []
        for dirpath, dirnames, filenames in os.walk(self.project_root):
            dir_path = Path(dirpath)
            dirnames[:] = [
                d
                for d in dirnames
                if not should_skip_path(dir_path / d, self.project_root, is_file=False)
            ]
            for fname in filenames:
                abs_path = dir_path / fname
                lang = get_language_for_extension(abs_path.suffix)
                if lang is None:
                    continue
                ast_grep_lang = cs.AST_GREP_LANGUAGES.get(lang)
                if ast_grep_lang is None:
                    continue
                if wanted is not None and ast_grep_lang != wanted:
                    continue
                if should_skip_path(abs_path, self.project_root, is_file=True):
                    continue
                rel = abs_path.relative_to(self.project_root).as_posix()
                out.append((abs_path, rel, ast_grep_lang))
        out.sort(key=lambda item: item[1])
        return out

    @staticmethod
    def _read(path: Path) -> str | None:
        try:
            return path.read_text(encoding=cs.ENCODING_UTF8)
        except (OSError, UnicodeDecodeError):
            return None

    @staticmethod
    def _root(source: str, ast_grep_lang: str) -> SgNode:
        from ast_grep_py import SgRoot

        return SgRoot(source, ast_grep_lang).root()

    def search(
        self,
        pattern: str,
        language: str | None = None,
        max_results: int = cs.AST_GREP_MAX_RESULTS,
    ) -> list[StructuralSearchMatch]:
        results: list[StructuralSearchMatch] = []
        for abs_path, rel, ast_grep_lang in self._iter_source_files(language):
            source = self._read(abs_path)
            if source is None:
                continue
            for match in self._root(source, ast_grep_lang).find_all(pattern=pattern):
                rng = match.range()
                results.append(
                    StructuralSearchMatch(
                        file=rel,
                        line=rng.start.line + 1,
                        column=rng.start.column,
                        end_line=rng.end.line + 1,
                        end_column=rng.end.column,
                        text=match.text(),
                    )
                )
                if len(results) >= max_results:
                    logger.info(cs.AST_GREP_TRUNCATED.format(limit=max_results))
                    return results
        return results

    def _interpolate(self, rewrite: str, match: SgNode) -> str:
        # (H) $$$NAME -> joined text of the multi-capture, $NAME -> the single
        # (H) capture. Unknown metavars are left literal. ponytail: multi-capture
        # (H) joins matched node texts, so inter-token spacing can normalise
        # (H) (e.g. "a, b" -> "a,b"); upgrade to a source-span slice only if
        # (H) exact whitespace fidelity is ever required.
        def _sub(m: re.Match[str]) -> str:
            multi, single = m.group(1), m.group(2)
            if multi is not None:
                return "".join(n.text() for n in match.get_multiple_matches(multi))
            node = match.get_match(single) if single is not None else None
            return node.text() if node is not None else m.group(0)

        return cs.AST_GREP_METAVAR_RE.sub(_sub, rewrite)

    def replace(
        self,
        pattern: str,
        rewrite: str,
        language: str | None = None,
        dry_run: bool = True,
    ) -> list[StructuralReplaceChange]:
        changes: list[StructuralReplaceChange] = []
        for abs_path, rel, ast_grep_lang in self._iter_source_files(language):
            source = self._read(abs_path)
            if source is None:
                continue
            root = self._root(source, ast_grep_lang)
            matches = root.find_all(pattern=pattern)
            if not matches:
                continue
            edits = [m.replace(self._interpolate(rewrite, m)) for m in matches]
            new_source = root.commit_edits(edits)
            if new_source == source:
                continue
            diff = "".join(
                difflib.unified_diff(
                    source.splitlines(keepends=True),
                    new_source.splitlines(keepends=True),
                    fromfile=rel,
                    tofile=rel,
                )
            )
            if not dry_run:
                abs_path.write_text(new_source, encoding=cs.ENCODING_UTF8)
            changes.append(
                StructuralReplaceChange(
                    file=rel,
                    matches=len(matches),
                    diff=diff,
                    applied=not dry_run,
                )
            )
        return changes
