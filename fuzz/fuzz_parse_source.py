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

    # The whole file, once: a name is suspect when the bytes it was extracted
    # from are not valid UTF-8, and the cheap file-level question decides
    # whether any per-node work is worth doing at all.
    try:
        source.decode(cs.ENCODING_UTF8)
    except UnicodeDecodeError:
        file_is_valid_utf8 = False
    else:
        file_is_valid_utf8 = True

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
                _check_truncated_name(language, current, name)
        stack.extend(current.children)  # type: ignore[attr-defined]


def _check_truncated_name(
    language: cs.SupportedLanguage, node: object, name: str
) -> None:
    """Fail when a name was extracted from bytes that are not valid UTF-8.

    Deliberately NOT `name.encode() in raw`: that containment check is
    satisfied by the very corruption it looks for, because the truncated name
    is a SUBSTRING of the corrupt bytes -- `pha` really is inside
    `al\xffpha`, so the oracle returns True on the exact defect it was
    written for and could never fire. What discriminates is whether the
    SOURCE round-trips, not whether the result is contained in it.

    Scoped honestly: this detects a superset (any name extracted from a span
    that is not valid UTF-8), it needs the enclosing node's bytes to carry the
    bad byte, and a payload placing that byte outside every captured node
    stays invisible. It is silent on legitimate non-ASCII identifiers such as
    `café`, which a naive "is it ASCII" check would flag on every non-English
    codebase.
    """
    raw = _node_bytes(node)
    if raw is None:
        return
    try:
        raw.decode(cs.ENCODING_UTF8)
    except UnicodeDecodeError:
        raise AssertionError(
            f"{language}: extracted the name {name!r} from bytes that are not "
            f"valid UTF-8 ({raw!r}); tree-sitter split the token at the bad "
            f"byte and the symbol is indexed under a truncated name (#1810)"
        ) from None


def _node_bytes(node: object) -> bytes | None:
    """The raw source bytes a node covers, or None when unavailable."""
    text = getattr(node, "text", None)
    return text if isinstance(text, bytes) else None


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
