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


def _extract_names(language: cs.SupportedLanguage, root: object) -> None:
    """Run the language's own name extractor over every captured node.

    This is the qualification code the issue names: it indexes children,
    follows named fields and decodes `node.text`, all on shapes the grammar
    guarantees only for well-formed input.
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
            fqn_spec.get_name(current)  # type: ignore[arg-type]
        stack.extend(current.children)  # type: ignore[attr-defined]


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
    _extract_names(language, tree.root_node)


def main() -> None:
    atheris.Setup(sys.argv, atheris.instrument_func(fuzz_parse_source))
    atheris.Fuzz()


if __name__ == "__main__":
    main()
