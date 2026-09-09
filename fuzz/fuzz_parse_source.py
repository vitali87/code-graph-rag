"""Fuzz the tree-sitter parse, query and node-extraction pass.

The main target. Every file in an indexed checkout reaches a grammar and then
the per-language query and qualification code, so malformed, truncated and
adversarial sources are the normal case rather than the exception. tree-sitter
itself recovers from anything by producing ERROR nodes; what has not been
exercised is the code that walks the resulting tree -- the name extraction in
`language_spec.py` and the capture handling that assumes a shape the grammar
only guarantees for valid input.

Replaces the previous `codebase_rag/tests/fuzz_test_parsers.py`, which despite
its name fed random strings to two dictionary lookups and never touched a
parser.

Run locally (Linux; atheris does not build against Apple Clang):

    uv run --extra fuzz python fuzz/fuzz_parse_source.py -max_total_time=60
"""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from tree_sitter import QueryCursor

    from codebase_rag import constants as cs
    from codebase_rag.language_spec import LANGUAGE_FQN_SPECS, LANGUAGE_SPECS
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.utils import sorted_captures

# Built once: loading a grammar per iteration would dominate the run and leave
# the fuzzer exercising the loader instead of the parsers.
_PARSERS, _QUERIES = load_parsers()
_LANGUAGES = tuple(sorted(_PARSERS))

if not _LANGUAGES:
    raise SystemExit(
        "no tree-sitter grammars are available; install the treesitter-full "
        "extra before fuzzing"
    )


def _walk(node: object) -> int:
    """Visit every node, mirroring the traversals the ingest passes run."""
    count = 0
    stack = [node]
    while stack:
        current = stack.pop()
        count += 1
        # Touch the attributes the extraction code reads. A grammar that
        # returns a malformed node fails here rather than deep inside ingest.
        _ = current.type  # type: ignore[attr-defined]
        _ = current.start_byte, current.end_byte  # type: ignore[attr-defined]
        stack.extend(current.children)  # type: ignore[attr-defined]
    return count


def _extract_names(language: cs.SupportedLanguage, root: object, source: bytes) -> None:
    """Run the language's own name extractor over every captured node.

    This is the qualification code the issue names: it indexes children,
    follows named fields and decodes `node.text`, all on shapes the grammar
    guarantees only for well-formed input.

    Two failure modes, and only one of them raises. The loud one (#1797) is a
    strict decode blowing up on an undecodable byte. The quiet one (#1810) is
    tree-sitter treating that byte as a token boundary: the `name` node covers
    only the bytes AFTER it, the extractor decodes that shortened node
    perfectly successfully, and `alpha` is indexed as `pha` with no exception,
    no log line and nothing to catch. A wrong name is worse than a missing
    one -- `pha` reads as a real definition nobody calls, so dead-code and
    impact queries get a confidently wrong answer.
    """
    fqn_spec = LANGUAGE_FQN_SPECS.get(language)
    spec = LANGUAGE_SPECS.get(language)
    if fqn_spec is None or spec is None:
        return

    wanted = set(spec.function_node_types) | set(spec.class_node_types)
    wanted |= set(fqn_spec.function_node_types) | set(fqn_spec.scope_node_types)
    if not wanted:
        return

    stack = [root]
    while stack:
        current = stack.pop()
        if current.type in wanted:  # type: ignore[attr-defined]
            # A None return is a legitimate "no name here"; a raise is not.
            # #1797's UnicodeDecodeError suppression was removed with the fix:
            # the extractors decode with errors="replace", so an undecodable
            # byte can no longer raise here and a regression would surface as
            # a crash rather than being silently tolerated.
            name = fqn_spec.get_name(current)  # type: ignore[arg-type]
            if name is not None:
                _check_truncated_name(language, current, name, source)
        stack.extend(current.children)  # type: ignore[attr-defined]


def _check_truncated_name(
    language: cs.SupportedLanguage, node: object, name: str, source: bytes
) -> None:
    """Fail when a name was truncated by an invalid byte beside it.

    Mirrors `parsers.utils.warn_if_name_truncated`: the discriminator is the
    byte IMMEDIATELY ADJACENT to the NAME's span, the one the grammar split
    on. Two narrower-sounding shapes are both wrong -- the name node's own
    bytes never contain the bad byte (tree-sitter excludes it, so they decode
    cleanly either way and detect nothing), and the enclosing DEFINITION's
    span contains it but also spans the whole body, so it fires on a bad byte
    in any body comment or string while the symbol is indexed correctly.

    Deliberately NOT `name.encode() in raw`: containment is satisfied BY this
    corruption, because the truncated name is a substring of the corrupt bytes
    (`pha` really is inside `al\xffpha`), so that oracle returns True on the
    exact defect it would be written for and can never fire.
    """
    span = _name_span(node, name)
    if span is None:
        return
    start, end = span
    for probe, at_end in (
        (source[max(0, start - _UTF8_MAX_SEQUENCE_BYTES) : start], False),
        (source[end : end + _UTF8_MAX_SEQUENCE_BYTES], True),
    ):
        if probe and _has_invalid_byte(probe, at_end=at_end):
            raise AssertionError(
                f"{language}: the name {name!r} was extracted from a token the "
                f"grammar split at an invalid byte, so the symbol is indexed "
                f"under a truncated name (#1810)"
            )


# Longest UTF-8 sequence: a window this size either side of a name spans any
# single character that could legitimately sit next to it.
_UTF8_MAX_SEQUENCE_BYTES = 4
# A definition's own name is at most a step or two above it (the JS/TS
# field-definition wrapper is one).
_NAME_SPAN_ANCESTOR_LIMIT = 3
# Sigils an ingestor prepends to a synthesized name (a C# destructor).
_SYNTHETIC_NAME_PREFIXES = "~"


def _has_invalid_byte(window: bytes, *, at_end: bool) -> bool:
    """Whether `window` holds a byte no valid UTF-8 sequence explains.

    The window is cut at an arbitrary offset, so it can begin or end
    mid-character even when the source is well-formed (`\u00a9` is c2 a9, and
    either half alone fails). The partial sequence at the CUT edge is trimmed
    before decoding; `at_end` says which edge that is.
    """
    for trim in range(len(window)):
        candidate = window[trim:] if not at_end else window[: len(window) - trim]
        if not candidate:
            return False
        try:
            candidate.decode(cs.ENCODING_UTF8)
        except UnicodeDecodeError:
            continue
        return False
    return True


def _name_span(node: object, name: str) -> tuple[int, int] | None:
    """Byte span the extracted `name` came from, or None."""
    # A synthesized name need not appear in the source: C# destructor ingestion
    # builds `~Greeter` while the source leaf is bare. Mirrors production's
    # strip so the oracle exercises that path too.
    name = name.lstrip(_SYNTHETIC_NAME_PREFIXES)
    if not name:
        return None
    encoded = name.encode(cs.ENCODING_UTF8)
    direct = node.child_by_field_name("name")  # type: ignore[attr-defined]
    if direct is not None and direct.text == encoded:
        return direct.start_byte, direct.end_byte

    stack = [node]
    while stack:
        current = stack.pop(0)
        if current is not node and current.text == encoded and not current.children:  # type: ignore[attr-defined]
            return current.start_byte, current.end_byte  # type: ignore[attr-defined]
        stack.extend(current.children)  # type: ignore[attr-defined]

    ancestor = node.parent  # type: ignore[attr-defined]
    for _ in range(_NAME_SPAN_ANCESTOR_LIMIT):
        if ancestor is None:
            break
        field = ancestor.child_by_field_name("name")
        if field is not None and field.text == encoded:
            return field.start_byte, field.end_byte
        ancestor = ancestor.parent
    return None


def _run_queries(language: cs.SupportedLanguage, tree: object) -> None:
    """Execute the language's compiled queries against the tree.

    Goes through `QueryCursor` and `sorted_captures`, the same route the ingest
    passes take, so a capture-handling bug is reached the way production
    reaches it rather than through a shortcut the real code never uses.
    """
    queries = _QUERIES.get(language)
    if queries is None:
        return
    # LanguageQueries is a TypedDict; the query slots may each be None.
    for key in (cs.QUERY_FUNCTIONS, cs.QUERY_CLASSES, cs.QUERY_CALLS, cs.QUERY_IMPORTS):
        query = queries.get(key)
        if query is None:
            continue
        captures = sorted_captures(QueryCursor(query), tree.root_node)  # type: ignore[attr-defined]
        for nodes in captures.values():
            for node in nodes:
                _ = node.type, node.start_byte


def fuzz_parse_source(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    language = _LANGUAGES[fdp.ConsumeIntInRange(0, len(_LANGUAGES) - 1)]
    source = fdp.ConsumeBytes(fdp.remaining_bytes())

    parser = _PARSERS[language]
    tree = parser.parse(source)
    if tree is None or tree.root_node is None:
        raise AssertionError(f"{language} parser returned no tree for {source!r}")

    _walk(tree.root_node)
    _run_queries(language, tree)
    _extract_names(language, tree.root_node, source)


def main() -> None:
    atheris.Setup(sys.argv, atheris.instrument_func(fuzz_parse_source))
    atheris.Fuzz()


if __name__ == "__main__":
    main()
