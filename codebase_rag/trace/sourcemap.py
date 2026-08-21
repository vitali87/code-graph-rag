"""Source Map v3 support for remapping transpiled-JS trace frames to sources.

V8 CPU profiles reference the JavaScript that actually executed, so a TypeScript
project built to ``dist/`` produces frames pointing at ``dist/*.js`` rather than
the ``src/*.ts`` the graph indexes. When the build emits source maps (``tsc
--sourceMap``, bundlers by default), each generated ``(line, column)`` can be
mapped back to an original ``(source, line, column)``; this module parses the
``mappings`` VLQ payload and resolves a generated position to its source file
and line so the frame lands on the TypeScript node.

Only the subset needed to relocate a frame is implemented: the ``mappings``
grammar (semicolon-separated generated lines, comma-separated Base64-VLQ
segments) and nearest-preceding-segment lookup. ``names`` are irrelevant to
position resolution and ignored.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterable


class SourceMapOutcome(StrEnum):
    """The result of trying to relocate a generated frame to its source."""

    RESOLVED = "resolved"  # a source map covered the position
    NO_MAP = "no_map"  # no source map referenced or found beside the file
    UNCOVERED = "uncovered"  # a map loaded but no segment covers the position
    MALFORMED = "malformed"  # a map file was found but could not be parsed


_B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_INDEX = {char: index for index, char in enumerate(_B64_ALPHABET)}
_VLQ_CONTINUATION = 0x20
_VLQ_VALUE_MASK = 0x1F
_VLQ_SHIFT = 5

# A source-mapping segment: the generated column and the original source file
# index, line, and column it maps to. Segments carrying only a generated column
# (no source) are unmapped and dropped.
_Segment = tuple[int, int, int, int]

_SOURCE_MAP_URL_MARKER = "//# sourceMappingURL="
_INLINE_MAP_PREFIX = "data:application/json"

# The shape of a decoded JSON object; source maps are plain JSON documents.
_JsonObject = dict[str, object]


def _decode_vlq(segment: str) -> list[int]:
    """Decode a Base64-VLQ segment into its signed integer fields."""
    values: list[int] = []
    accumulator = 0
    shift = 0
    for char in segment:
        digit = _B64_INDEX.get(char)
        if digit is None:
            raise ValueError(segment)
        accumulator += (digit & _VLQ_VALUE_MASK) << shift
        if digit & _VLQ_CONTINUATION:
            shift += _VLQ_SHIFT
            continue
        # The least significant bit of the assembled value is the sign.
        magnitude = accumulator >> 1
        values.append(-magnitude if accumulator & 1 else magnitude)
        accumulator = 0
        shift = 0
    if shift:
        # The final digit set the continuation bit but no digit followed: the
        # segment is truncated, so the whole map is malformed rather than a
        # field silently dropped.
        raise ValueError(segment)
    return values


def _parse_mappings(mappings: str) -> list[list[_Segment]]:
    """Per generated line, its sorted ``(gen_col, src, src_line, src_col)``.

    Source index, source line, and source column are delta-encoded across the
    whole payload; only the generated column resets at each line (spec v3).
    """
    lines: list[list[_Segment]] = []
    source_index = source_line = source_column = 0
    for line_field in mappings.split(";"):
        generated_column = 0
        segments: list[_Segment] = []
        for raw_segment in line_field.split(","):
            if not raw_segment:
                continue
            fields = _decode_vlq(raw_segment)
            generated_column += fields[0]
            if len(fields) >= 4:
                source_index += fields[1]
                source_line += fields[2]
                source_column += fields[3]
                segments.append(
                    (generated_column, source_index, source_line, source_column)
                )
        segments.sort()
        lines.append(segments)
    return lines


@dataclass(slots=True)
class SourceMap:
    """A parsed source map able to relocate a generated position."""

    sources: list[str]
    lines: list[list[_Segment]]
    base_dir: Path

    def original_position(
        self, generated_line: int, generated_column: int
    ) -> tuple[str, int] | None:
        """The absolute source path and 1-based line for a 0-based position.

        Returns None when the line has no mapping at or before the column (the
        generated position is synthetic, e.g. runtime-injected glue).
        """
        if not 0 <= generated_line < len(self.lines):
            return None
        segments = self.lines[generated_line]
        if not segments:
            return None
        # Segments are sorted by generated column; bisect on that key directly
        # so a minified line's many segments cost no per-lookup allocation.
        index = (
            bisect.bisect_right(segments, generated_column, key=lambda seg: seg[0]) - 1
        )
        if index < 0:
            return None
        _column, source_index, source_line, _source_column = segments[index]
        if not 0 <= source_index < len(self.sources):
            return None
        source_path = (self.base_dir / self.sources[source_index]).resolve()
        return source_path.as_posix(), source_line + 1


def load_source_map(map_path: Path) -> SourceMap | None:
    """Parse a source map file, or None if it is missing or malformed."""
    try:
        raw = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _from_document(raw, map_path.parent)


def _nonneg_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_section(
    raw_section: object, base_dir: Path
) -> tuple[int, int, SourceMap] | None:
    """The generated line, column, and inner map of one index-map section."""
    if not isinstance(raw_section, dict):
        return None
    section = cast(_JsonObject, raw_section)
    offset = section.get("offset")
    if not isinstance(offset, dict):
        return None
    offset_fields = cast(_JsonObject, offset)
    offset_line = offset_fields.get("line")
    offset_column = offset_fields.get("column")
    if not _nonneg_int(offset_line) or not _nonneg_int(offset_column):
        return None
    inner = _from_document(section.get("map"), base_dir)
    if inner is None:
        return None
    return cast("int", offset_line), cast("int", offset_column), inner


def _place_section(
    inner: SourceMap,
    offset_line: int,
    offset_column: int,
    source_base: int,
    combined_lines: list[list[_Segment]],
) -> None:
    """Copy ``inner``'s segments into ``combined_lines`` at the section offset.

    The column offset applies only to the section's first generated line; later
    lines start a fresh generated line and are unshifted.
    """
    for index, segments in enumerate(inner.lines):
        generated_line = offset_line + index
        while len(combined_lines) <= generated_line:
            combined_lines.append([])
        column_shift = offset_column if index == 0 else 0
        for generated_column, source_index, source_line, source_column in segments:
            combined_lines[generated_line].append(
                (
                    generated_column + column_shift,
                    source_base + source_index,
                    source_line,
                    source_column,
                )
            )


def _from_sections(sections: list[object], base_dir: Path) -> SourceMap | None:
    """Flatten a Source Map v3 index map's ``sections`` into one map.

    Each section places an inner map at a generated ``offset`` (line, column)
    and its sources are absolutised, so the combined map resolves a generated
    position exactly as a flat map would.
    """
    combined_sources: list[str] = []
    combined_lines: list[list[_Segment]] = []
    for raw_section in sections:
        parsed = _parse_section(raw_section, base_dir)
        if parsed is None:
            return None
        offset_line, offset_column, inner = parsed
        source_base = len(combined_sources)
        for source in inner.sources:
            combined_sources.append((inner.base_dir / source).resolve().as_posix())
        _place_section(inner, offset_line, offset_column, source_base, combined_lines)
    for line in combined_lines:
        line.sort()
    return SourceMap(sources=combined_sources, lines=combined_lines, base_dir=base_dir)


def _from_document(raw: object, base_dir: Path) -> SourceMap | None:
    if not isinstance(raw, dict):
        return None
    document = cast(_JsonObject, raw)
    sections = document.get("sections")
    if isinstance(sections, list):
        # A Source Map v3 index map (bundler output) nests maps under sections.
        return _from_sections(cast("list[object]", sections), base_dir)
    mappings = document.get("mappings")
    sources = document.get("sources")
    if not isinstance(mappings, str) or not isinstance(sources, list):
        return None
    source_root = document.get("sourceRoot")
    resolved_base = base_dir
    if isinstance(source_root, str) and source_root:
        resolved_base = base_dir / source_root
    try:
        lines = _parse_mappings(mappings)
    except (ValueError, IndexError):
        return None
    return SourceMap(
        sources=[str(source) for source in sources],
        lines=lines,
        base_dir=resolved_base,
    )


def _sniff_map_reference(js_path: Path) -> Path | None:
    """The ``sourceMappingURL`` target of a generated file, if any.

    An inline ``data:`` map is not followed here (the profile's own file paths
    already point at the generated file on disk); only external ``.map`` files
    beside the generated output are resolved.
    """
    try:
        tail = js_path.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
    except OSError:
        return None
    for line in reversed(tail):
        stripped = line.strip()
        if stripped.startswith(_SOURCE_MAP_URL_MARKER):
            url = stripped[len(_SOURCE_MAP_URL_MARKER) :].strip()
            if url.startswith(_INLINE_MAP_PREFIX):
                return None
            # The reference is a URL: drop any ?query / #fragment and
            # percent-decode before treating it as a filesystem path. A
            # malformed URL (urlsplit raises on bad brackets) is simply ignored.
            try:
                relative = unquote(urlsplit(url).path)
            except ValueError:
                return None
            if not relative:
                return None
            return (js_path.parent / relative).resolve()
    return None


class SourceMapIndex:
    """Lazily loads and caches the source map for each generated file."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[SourceMap | None, SourceMapOutcome]] = {}

    def _map_for(self, js_path: str) -> tuple[SourceMap | None, SourceMapOutcome]:
        if js_path not in self._cache:
            self._cache[js_path] = self._load(Path(js_path))
        return self._cache[js_path]

    @staticmethod
    def _load(path: Path) -> tuple[SourceMap | None, SourceMapOutcome]:
        referenced = _sniff_map_reference(path)
        candidates: Iterable[Path] = (
            [referenced] if referenced else [path.with_name(path.name + ".map")]
        )
        found = False
        for candidate in candidates:
            if candidate.is_file():
                found = True
                loaded = load_source_map(candidate)
                if loaded is not None:
                    return loaded, SourceMapOutcome.RESOLVED
        # A map file was present but every candidate failed to parse; otherwise
        # no map is referenced or sitting beside the generated file at all.
        return None, (SourceMapOutcome.MALFORMED if found else SourceMapOutcome.NO_MAP)

    def remap_detailed(
        self, path: str, line: int, column: int
    ) -> tuple[tuple[str, int] | None, SourceMapOutcome]:
        """Remap a generated ``(path, line, column)`` and report the outcome.

        ``line`` and ``column`` are 0-based (as V8 reports them); the returned
        line is 1-based. The position is None (and the caller keeps the generated
        frame) whenever the outcome is not ``RESOLVED``.
        """
        source_map, load_outcome = self._map_for(path)
        if source_map is None:
            return None, load_outcome
        position = source_map.original_position(line, column)
        if position is None:
            return None, SourceMapOutcome.UNCOVERED
        return position, SourceMapOutcome.RESOLVED

    def remap(self, path: str, line: int, column: int) -> tuple[str, int] | None:
        """The remapped source position, or None when no map covers it."""
        return self.remap_detailed(path, line, column)[0]
