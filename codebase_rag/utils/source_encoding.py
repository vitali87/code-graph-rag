"""Python source decoding as PEP 263 defines it (issue #2445).

tree-sitter reads bytes as UTF-8. A Python file may declare another encoding
on its first or second line (`# -*- coding: latin-1 -*-`), and CPython decodes
it that way, so `def café():` in such a file defines `café`. Handed its raw
bytes, the grammar split the identifier at the undecodable byte and the
function was indexed as `caf`.

The indexer therefore re-encodes a declared-encoding source to UTF-8 before
parsing. Newlines are ASCII in every encoding PEP 263 admits, so the re-encoded
text has the same lines as the file on disk and every recorded start/end line
still addresses it; only byte columns inside a line move, and those are read
back from the parsed tree, never from the file. Readers that slice the file by
line (`extract_source_lines`) decode it with the same declaration.
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs as ls

_CODING_COOKIE = re.compile(cs.PY_CODING_COOKIE_PATTERN)
_BLANK_OR_COMMENT_LINE = re.compile(cs.PY_CODING_BLANK_LINE_PATTERN)
_ASCII_TEXT = cs.ASCII_BYTES.decode(cs.ENCODING_ASCII)


def _coding_cookie(source: bytes) -> str | None:
    """The encoding a PEP 263 declaration names, as the file spells it."""
    first, _, rest = source.partition(b"\n")
    if match := _CODING_COOKIE.match(first):
        return match.group(1).decode(cs.ENCODING_ASCII)
    # Line 2 is a declaration only under a line 1 that holds no code: a
    # shebang, another comment, or nothing.
    if not _BLANK_OR_COMMENT_LINE.match(first):
        return None
    if match := _CODING_COOKIE.match(rest.partition(b"\n")[0]):
        return match.group(1).decode(cs.ENCODING_ASCII)
    return None


def _is_utf8(codec: str) -> bool:
    try:
        return codecs.lookup(codec).name in cs.UTF8_CODEC_NAMES
    except LookupError:
        return False


def _declared_codec(source: bytes, path: Path) -> str | None:
    """The codec `source` declares it is written in, or None.

    None when it declares nothing, which means UTF-8, and when the declaration
    cannot be honoured: an unknown name, a codec that is not a text encoding
    (`base64`), or one that does not read ASCII as ASCII (`utf-16`). Those are
    warned about and fall back to the UTF-8 reading every file had before,
    rather than failing the run over one file.
    """
    if source.startswith(codecs.BOM_UTF8):
        # CPython refuses a BOM beside a different declaration; the bytes
        # after a BOM are UTF-8 whatever the comment says.
        cookie = _coding_cookie(source[len(codecs.BOM_UTF8) :])
        if cookie is not None and not _is_utf8(cookie):
            logger.warning(ls.PY_ENCODING_BOM_CONFLICT.format(path=path, codec=cookie))
        return cs.ENCODING_UTF8_SIG
    if (cookie := _coding_cookie(source)) is None:
        return None
    try:
        codecs.lookup(cookie)
    except LookupError:
        logger.warning(ls.PY_ENCODING_UNKNOWN.format(path=path, codec=cookie))
        return None
    # A codec that is not a text encoding raises LookupError on `bytes.decode`
    # even though `codecs.lookup` knows it.
    try:
        ascii_compatible = cs.ASCII_BYTES.decode(cookie) == _ASCII_TEXT
    except (LookupError, UnicodeError):
        ascii_compatible = False
    if not ascii_compatible:
        logger.warning(ls.PY_ENCODING_UNSUPPORTED.format(path=path, codec=cookie))
        return None
    return cookie


def _decode(source: bytes, codec: str, path: Path) -> str | None:
    try:
        return source.decode(codec)
    except UnicodeError as exc:
        logger.warning(
            ls.PY_ENCODING_UNDECODABLE.format(path=path, codec=codec, error=exc)
        )
        return None


# Checked longest first: the UTF-32 LE mark begins with the UTF-16 LE one.
# Each mark with the codec of the text after it and the BOM-aware codec a
# reader opens the whole file with.
_WIDE_BOMS = (
    (codecs.BOM_UTF32_LE, cs.ENCODING_UTF32_LE, cs.ENCODING_UTF32),
    (codecs.BOM_UTF32_BE, cs.ENCODING_UTF32_BE, cs.ENCODING_UTF32),
    (codecs.BOM_UTF16_LE, cs.ENCODING_UTF16_LE, cs.ENCODING_UTF16),
    (codecs.BOM_UTF16_BE, cs.ENCODING_UTF16_BE, cs.ENCODING_UTF16),
)
_WIDE_BOM_MAX = max(len(bom) for bom, _, _ in _WIDE_BOMS)


def _wide_bom(source: bytes) -> tuple[bytes, str, str] | None:
    return next((entry for entry in _WIDE_BOMS if source.startswith(entry[0])), None)


def decode_wide_source(source: bytes, path: Path) -> str | None:
    """The text of a UTF-16 or UTF-32 source marked by its BOM.

    tree-sitter read such a file's NUL-interleaved bytes as UTF-8 and found no
    declarations, so a UTF-16 `.cs`, `.java` or `.cpp` was indexed as an
    empty module (issue #3153). The mark picks the byte order and is not part
    of the text. Newlines decode to newlines, so the text has the file's
    lines. None for any other file, or one that fails to decode.
    """
    if (wide := _wide_bom(source)) is None:
        return None
    bom, codec, _reader_codec = wide
    return _decode(source[len(bom) :], codec, path)


def encode_source(text: str, codec: str) -> bytes:
    """`text` written back in the codec `python_source_view` reported.

    A wide source gets the BOM it was read with in front again: the codec
    names one byte order, and the mark is not part of the text.
    """
    bom = next((mark for mark, wide, _ in _WIDE_BOMS if wide == codec), b"")
    return bom + text.encode(codec)


def decode_python_source(source: bytes, path: Path) -> str | None:
    """The text of a Python source that declares its encoding, BOM included.

    None when it declares none, or one that cannot decode it; the caller then
    reads it as it always has.
    """
    if (codec := _declared_codec(source, path)) is None:
        return None
    return _decode(source, codec, path)


def source_codec(path: Path) -> str:
    """The codec to read `path` as text with (issue #2901).

    A Python file's PEP 263 declaration (only its first two lines can hold
    one), else UTF-8: every other reader of a source then sees the text the
    indexer parsed.
    """
    python = path.suffix in cs.PY_EXTENSIONS
    try:
        with path.open("rb") as handle:
            head = (
                handle.readline() + handle.readline()
                if python
                else handle.read(_WIDE_BOM_MAX)
            )
    except OSError:
        return cs.ENCODING_UTF8
    if not python:
        # A UTF-16/32 source was indexed as the text its BOM marks (#3153).
        wide = _wide_bom(head)
        return cs.ENCODING_UTF8 if wide is None else wide[2]
    return _declared_codec(head, path) or cs.ENCODING_UTF8


def decode_source(source: bytes, path: Path) -> str:
    """`source` as text for display, in a Python file's declared encoding or
    the one a wide source's BOM marks."""
    declared = (
        decode_python_source(source, path)
        if path.suffix in cs.PY_EXTENSIONS
        else decode_wide_source(source, path)
    )
    if declared is not None:
        return declared
    return source.decode(cs.ENCODING_UTF8, errors="replace")


def python_source_view(
    source: bytes, language: cs.SupportedLanguage | None, path: Path
) -> tuple[bytes, str | None]:
    """`source` as the UTF-8 bytes the grammar parsed, plus the codec to
    write an edit of it back in (None when that is UTF-8 already).

    The graph's byte columns address this view (issue #2901), so an editor
    patches it and re-encodes only the result.
    """
    view = grammar_bytes(source, language, path)
    if view is source:
        return source, None
    if language != cs.SupportedLanguage.PYTHON and (wide := _wide_bom(source)):
        return view, wide[1]
    return view, _declared_codec(source, path)


def grammar_bytes(
    source: bytes, language: cs.SupportedLanguage | None, path: Path
) -> bytes:
    """The bytes to hand tree-sitter for `source`.

    A UTF-16 or UTF-32 source with a BOM in any language but Python, and a
    Python source declaring a non-UTF-8 encoding, come back re-encoded as
    UTF-8. Everything else is returned as the same object: other languages
    have no encoding declaration, and a UTF-8 file (BOM or not) is already
    what the grammar reads. CPython refuses a UTF-16/32 source, which is not
    ASCII-compatible (the reason `_declared_codec` refuses a `utf-16`
    cookie), so a wide `.py` is left as the bytes it was before (#3153 review).
    """
    if language != cs.SupportedLanguage.PYTHON:
        if (wide := decode_wide_source(source, path)) is not None:
            return wide.encode(cs.ENCODING_UTF8, errors="surrogatepass")
        return source
    codec = _declared_codec(source, path)
    if codec is None or _is_utf8(codec):
        return source
    if (text := _decode(source, codec, path)) is None:
        return source
    # Only an escape codec can decode to a lone surrogate, which strict UTF-8
    # refuses to encode; passing it through leaves those bytes invalid, as the
    # raw reading would have, instead of failing the file.
    return text.encode(cs.ENCODING_UTF8, errors="surrogatepass")
