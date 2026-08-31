"""Concrete-syntax-preserving patchers (issue #1529).

A rewrite that re-emits a file from an AST loses comments, whitespace and
style, and turns every rename into a diff nobody wants to review. The
patcher never re-emits: it replaces exact byte spans of the original file
and leaves everything around them untouched.

Every edit is expressed against the ORIGINAL bytes of the file, so a batch
of edits to one file needs no offset bookkeeping by the caller: the batch
is applied in one pass from the end of the file backwards, which makes the
result independent of the order the edits were queued in. Overlapping spans
are refused rather than guessed.

After patching, the file is re-parsed with its tree-sitter grammar and the
result records whether it still parses; for Go and Rust a formatter check
(`gofmt -l`, `rustfmt --check`) runs when the tool is installed, so a patch
that broke the language's canonical formatting is reported (not rewritten:
the patcher's promise is to touch only what it was asked to touch).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple, Protocol

from tree_sitter import Node, Parser

from .. import constants as cs
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers


class SpanEdit(NamedTuple):
    """Replace `source[start:end]` (byte offsets into the original) with `text`."""

    start: int
    end: int
    text: bytes


class PatchResult(NamedTuple):
    """One file after its batch of edits was applied in memory."""

    path: str
    content: bytes
    edits: int
    parses: bool | None
    formatter: str | None
    formatted: bool | None
    message: str


class PatcherError(ValueError):
    """An edit could not be queued or applied as asked."""


class _Stager(Protocol):
    def stage(self, rel_path: str | Path, content: str | bytes | None) -> object: ...


# --- offsets ------------------------------------------------------------------


def line_col_to_byte(source: bytes, line: int, col: int) -> int:
    """Byte offset of (1-based line, 0-based byte column), the graph's convention."""
    if line < 1:
        raise PatcherError(cs.PATCH_BAD_POSITION.format(line=line, col=col))
    offset = 0
    for _ in range(line - 1):
        newline = source.find(b"\n", offset)
        if newline == -1:
            raise PatcherError(cs.PATCH_BAD_POSITION.format(line=line, col=col))
        offset = newline + 1
    line_end = source.find(b"\n", offset)
    line_len = (len(source) if line_end == -1 else line_end) - offset
    if col < 0 or col > line_len:
        raise PatcherError(cs.PATCH_BAD_POSITION.format(line=line, col=col))
    return offset + col


def byte_to_line_col(source: bytes, offset: int) -> tuple[int, int]:
    """(1-based line, 0-based byte column) of a byte offset."""
    if offset < 0 or offset > len(source):
        raise PatcherError(cs.PATCH_BAD_OFFSET.format(offset=offset))
    line = source.count(b"\n", 0, offset) + 1
    line_start = source.rfind(b"\n", 0, offset) + 1
    return line, offset - line_start


def apply_span_edits(source: bytes, edits: Iterable[SpanEdit]) -> bytes:
    """Apply a batch of original-offset edits in one pass, any queue order.

    Sorted by start descending so earlier spans are never shifted by later
    replacements; two edits touching the same byte are an overlap and refused
    (an insertion at a boundary shared with a deletion is the one exception
    worth allowing, so equal `start == end` inserts are kept, applied after
    the replacement that starts there).
    """
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    previous_end = -1
    for edit in ordered:
        if edit.start < 0 or edit.end > len(source) or edit.start > edit.end:
            raise PatcherError(cs.PATCH_BAD_SPAN.format(start=edit.start, end=edit.end))
        if edit.start < previous_end:
            raise PatcherError(cs.PATCH_OVERLAP.format(start=edit.start, end=edit.end))
        previous_end = max(previous_end, edit.end)
    out = bytearray(source)
    for edit in reversed(ordered):
        out[edit.start : edit.end] = edit.text
    return bytes(out)


# --- validation ---------------------------------------------------------------

_IDENTIFIER_TYPES = frozenset(
    {
        cs.TS_IDENTIFIER,
        cs.TS_TYPE_IDENTIFIER,
        cs.TS_PROPERTY_IDENTIFIER,
        cs.TS_SHORTHAND_PROPERTY_IDENTIFIER,
        cs.TS_PRIVATE_PROPERTY_IDENTIFIER,
        cs.TS_GO_FIELD_IDENTIFIER,
        cs.TS_GO_PACKAGE_IDENTIFIER,
        cs.TS_RS_FIELD_IDENTIFIER,
        cs.TS_PY_IDENTIFIER,
        cs.PATCH_TS_FIELD_IDENTIFIER,
        cs.PATCH_TS_NAMESPACE_IDENTIFIER,
        cs.PATCH_TS_STATEMENT_IDENTIFIER,
    }
)

_FORMATTERS: dict[cs.SupportedLanguage, tuple[str, tuple[str, ...]]] = {
    # gofmt -l prints the file name when it would reformat it.
    cs.SupportedLanguage.GO: (cs.PATCH_GOFMT, (cs.PATCH_GOFMT_LIST_FLAG,)),
    cs.SupportedLanguage.RUST: (
        cs.PATCH_RUSTFMT,
        (
            cs.PATCH_RUSTFMT_CHECK_FLAG,
            cs.PATCH_RUSTFMT_EDITION_FLAG,
            cs.PATCH_RUSTFMT_EDITION,
        ),
    ),
}


def _language_for(path: Path) -> cs.SupportedLanguage | None:
    language = get_language_for_extension(path.suffix)
    return language if isinstance(language, cs.SupportedLanguage) else None


def _identifier_at(root: Node, start: int, end: int) -> Node | None:
    """The identifier node spanning exactly [start, end), or None.

    A wrapper with the same span (a scoped or dotted name around the token)
    is walked up so the identifier inside it is what gets checked; any other
    node at that span (a string's content, a keyword) is not an identifier.
    """
    # tree-sitter's range end is inclusive; `end` here is exclusive.
    node = root.named_descendant_for_byte_range(start, max(start, end - 1))
    while node is not None and (node.start_byte, node.end_byte) == (start, end):
        if node.type in _IDENTIFIER_TYPES:
            return node
        node = node.parent
    return None


def formatter_check(
    language: cs.SupportedLanguage | None, content: bytes, suffix: str
) -> tuple[str | None, bool | None]:
    """(tool name, formatted?) for languages with a canonical formatter.

    `None, None` when the language has none or the tool is not installed;
    the check reports, it never rewrites.
    """
    if language is None or language not in _FORMATTERS:
        return None, None
    tool, flags = _FORMATTERS[language]
    executable = shutil.which(tool)
    if executable is None:
        return tool, None
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    try:
        completed = subprocess.run(
            [executable, *flags, str(temp_path)],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
            timeout=cs.PATCH_FORMATTER_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return tool, None
    finally:
        temp_path.unlink(missing_ok=True)
    if language == cs.SupportedLanguage.GO:
        return tool, completed.returncode == 0 and not completed.stdout.strip()
    return tool, completed.returncode == 0


# --- the patcher --------------------------------------------------------------


class Patcher:
    """Queue span and identifier edits per file, then apply them in memory.

    Sources are read once per file (from `overlay` when given, so a batch
    can build on an `EditTransaction`'s staged content, else from disk).
    `apply()` returns the patched bytes per file together with the parse and
    formatter verdicts; `stage_into(tx)` hands them to a transaction.
    """

    def __init__(
        self,
        repo_root: Path,
        parsers: dict[cs.SupportedLanguage, Parser] | None = None,
        overlay: dict[str, bytes] | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self._parsers = parsers
        self._overlay = overlay or {}
        self._sources: dict[str, bytes] = {}
        self._edits: dict[str, list[SpanEdit]] = {}

    def _relative(self, file: str | Path) -> str:
        path = Path(file)
        if not path.is_absolute():
            path = self.repo_root / path
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except (OSError, ValueError) as error:
            raise PatcherError(cs.PATCH_OUTSIDE_ROOT.format(path=file)) from error

    def source(self, file: str | Path) -> bytes:
        key = self._relative(file)
        if key not in self._sources:
            staged = self._overlay.get(key)
            if staged is not None:
                self._sources[key] = staged
            else:
                path = self.repo_root / key
                if not path.is_file():
                    raise PatcherError(cs.PATCH_NO_FILE.format(path=key))
                self._sources[key] = path.read_bytes()
        return self._sources[key]

    def _parser(self, key: str) -> tuple[cs.SupportedLanguage | None, Parser | None]:
        language = _language_for(Path(key))
        if language is None:
            return None, None
        if self._parsers is None:
            parsers, _queries = load_parsers()
            self._parsers = dict(parsers)
        return language, self._parsers.get(language)

    def replace_span(
        self, file: str | Path, span: tuple[int, int], text: str | bytes
    ) -> None:
        """Replace original bytes `span=(start, end)` of `file` with `text`."""
        key = self._relative(file)
        source = self.source(key)
        start, end = span
        if start < 0 or end > len(source) or start > end:
            raise PatcherError(cs.PATCH_BAD_SPAN.format(start=start, end=end))
        payload = text.encode(cs.ENCODING_UTF8) if isinstance(text, str) else text
        self._edits.setdefault(key, []).append(SpanEdit(start, end, payload))

    def replace_identifier_at(
        self, file: str | Path, line: int, col: int, old: str, new: str
    ) -> None:
        """Rename the identifier that starts at (line, col) from `old` to `new`.

        The position is checked against the source (and, when a grammar is
        available, against the syntax tree): the bytes there must be exactly
        `old` as a whole identifier, or the edit is refused. So a stale
        location from the graph can never rename the wrong token.
        """
        key = self._relative(file)
        source = self.source(key)
        start = line_col_to_byte(source, line, col)
        old_bytes = old.encode(cs.ENCODING_UTF8)
        end = start + len(old_bytes)
        if source[start:end] != old_bytes:
            raise PatcherError(
                cs.PATCH_IDENTIFIER_MISMATCH.format(
                    path=key,
                    line=line,
                    col=col,
                    expected=old,
                    found=source[start:end].decode(cs.ENCODING_UTF8, errors="replace"),
                )
            )
        _language, parser = self._parser(key)
        if parser is not None:
            root = parser.parse(source).root_node
            node = _identifier_at(root, start, end)
            if node is None or (node.start_byte, node.end_byte) != (start, end):
                raise PatcherError(
                    cs.PATCH_NOT_AN_IDENTIFIER.format(path=key, line=line, col=col)
                )
        self._edits.setdefault(key, []).append(
            SpanEdit(start, end, new.encode(cs.ENCODING_UTF8))
        )

    @property
    def pending(self) -> dict[str, int]:
        return {key: len(edits) for key, edits in self._edits.items()}

    def apply(self) -> dict[str, PatchResult]:
        """Every queued file patched in memory, with its parse and format verdicts."""
        results: dict[str, PatchResult] = {}
        for key in sorted(self._edits):
            edits = self._edits[key]
            content = apply_span_edits(self.source(key), edits)
            language, parser = self._parser(key)
            parses: bool | None = None
            if parser is not None:
                parses = not parser.parse(content).root_node.has_error
            tool, formatted = formatter_check(language, content, Path(key).suffix)
            if parses is False:
                message = cs.PATCH_PARSE_FAILED.format(path=key)
            elif formatted is False:
                message = cs.PATCH_FORMAT_DRIFT.format(path=key, tool=tool)
            elif parses is None:
                # Three states, three messages. `parses` is `bool | None`, and
                # folding None into the OK branch told the caller a file had
                # been checked when nothing had checked it (issue #1580).
                message = cs.PATCH_UNVERIFIED.format(path=key, count=len(edits))
            else:
                message = cs.PATCH_OK.format(path=key, count=len(edits))
            results[key] = PatchResult(
                key, content, len(edits), parses, tool, formatted, message
            )
        return results

    def stage_into(self, transaction: _Stager) -> dict[str, PatchResult]:
        """Apply, then stage every result that still parses into `transaction`.

        A file whose patch no longer parses is not staged: the batch's
        transaction then has nothing from it and the caller sees the
        result's message. Formatter drift is staged (it is the caller's
        rename that changed the alignment) and reported.

        `parses` is `bool | None` and this gate is two-valued, so the third
        state has to be assigned deliberately rather than by omission. It
        is assigned to STAGING, and the reason is that refusing would be
        worse: `None` means no grammar was loaded for the language, which
        on a base install is the ordinary case for Rust and Go, and a
        refusal there turns a working edit into a silent no-op.

        That is a decision about what to WRITE, and it is safe only because
        it is paired with a decision about what to SAY: `apply` reports
        those files as `PATCH_UNVERIFIED`, not `PATCH_OK`. Staging a file
        checked by nothing is defensible; telling the caller it was checked
        is not (issue #1580).
        """
        results = self.apply()
        for key, result in results.items():
            if result.parses is not False:
                transaction.stage(key, result.content)
        return results
