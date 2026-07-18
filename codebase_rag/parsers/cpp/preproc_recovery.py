# (H) Recovery for the conditional-brace preprocessor pattern that collapses a
# (H) whole C/C++ file into one tree-sitter ERROR node. A branch can open a
# (H) brace that a LATER branch closes (nlohmann binary_reader.hpp:
# (H) `#ifdef __cpp_lib_byteswap ... else { #endif` followed by
# (H) `#ifdef __cpp_lib_byteswap } #endif`); the preprocessor keeps the braces
# (H) balanced under every configuration, but tree-sitter keeps EVERY branch's
# (H) tokens and sees an imbalance, which at file scale makes error recovery
# (H) reinterpret the entire translation unit (3,125 lines -> one ERROR;
# (H) methods degrade to free functions or vanish, orphaning whole dead-code
# (H) clusters). When a parse comes back with a catastrophic ERROR, blank the
# (H) net-unbalanced LEAF conditional branches (space-filled, so every byte
# (H) offset and line number of the surviving code is preserved) and re-parse,
# (H) preferring the SMALLEST blanked subset: each candidate alone first, the
# (H) full set as a last resort, keeping the retry only when it strictly
# (H) shrinks the worst ERROR span.
import re

from tree_sitter import Node, Parser, Tree

from ... import constants as cs

_DIRECTIVE = re.compile(cs.CPP_PREPROC_CONDITIONAL_PATTERN)
_CHAR_OPEN_BRACE = b"{"
_CHAR_CLOSE_BRACE = b"}"
_CHAR_SPACE = b" "
_CHAR_NEWLINE = b"\n"
_LINE_COMMENT = b"//"

# (H) A branch list plus whether the conditional contains nested conditionals:
# (H) only LEAF conditionals are candidates. An outer region (an include guard
# (H) spans the whole file) legitimately holds unbalanced fragments between its
# (H) nested directives (a class body split by an inner #if), so blanking is
# (H) restricted to branches whose entire content is directive-free.
_Frame = tuple[list[bool], list[tuple[int, int]], list[int]]


def _max_error_span(root: Node) -> int:
    return max(
        (
            child.end_point[0] - child.start_point[0] + 1
            for child in root.children
            if child.type == cs.TS_ERROR
        ),
        default=0,
    )


def _code_brace_delta(line: bytes) -> int:
    # (H) a brace after `//` is prose, not structure (a doc note `// { see
    # (H) design` must not mark its branch unbalanced); block comments and
    # (H) string literals are left to the strict-improvement guard
    code = line.split(_LINE_COMMENT, 1)[0]
    return code.count(_CHAR_OPEN_BRACE) - code.count(_CHAR_CLOSE_BRACE)


def _unbalanced_leaf_branches(lines: list[bytes]) -> list[tuple[int, int]]:
    candidates: list[tuple[int, int]] = []
    stack: list[_Frame] = []
    for index, line in enumerate(lines):
        match = _DIRECTIVE.match(line)
        if match is None:
            continue
        keyword = match.group(1)
        if keyword in cs.CPP_PREPROC_OPEN_DIRECTIVES:
            if stack:
                stack[-1][0][0] = True
            stack.append(([False], [], [index + 1]))
        elif not stack:
            continue
        elif keyword in cs.CPP_PREPROC_SPLIT_DIRECTIVES:
            _, branches, current = stack[-1]
            branches.append((current[0], index - 1))
            current[0] = index + 1
        else:
            has_nested, branches, current = stack.pop()
            branches.append((current[0], index - 1))
            if has_nested[0]:
                continue
            for start, end in branches:
                if start > end:
                    continue
                delta = sum(_code_brace_delta(lines[i]) for i in range(start, end + 1))
                if delta != 0:
                    candidates.append((start, end))
    return candidates


def _blank(lines: list[bytes], ranges: list[tuple[int, int]]) -> bytes:
    out = list(lines)
    for start, end in ranges:
        for i in range(start, end + 1):
            out[i] = _CHAR_SPACE * len(out[i])
    return _CHAR_NEWLINE.join(out)


_CSHARP_DIRECTIVE_PREFIXES = (b"#if", b"#elif", b"#else", b"#endif")


def _count_error_nodes(root: Node) -> int:
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_ERROR:
            count += 1
        if node.has_error:
            stack.extend(node.children)
    return count


def _blank_csharp_directives(source_bytes: bytes) -> bytes:
    lines = source_bytes.split(_CHAR_NEWLINE)
    for i, line in enumerate(lines):
        if line.lstrip().startswith(_CSHARP_DIRECTIVE_PREFIXES):
            lines[i] = b""
    return _CHAR_NEWLINE.join(lines)


def parse_with_preproc_recovery(
    parser: Parser, source_bytes: bytes, language: cs.SupportedLanguage
) -> Tree:
    tree = parser.parse(source_bytes)
    if language == cs.SupportedLanguage.CSHARP:
        # (H) A C# conditional directive interleaved with declaration syntax
        # (H) (`void M() #if X => Impl() #endif ;`, Serilog's ILogger default
        # (H) interface bodies) can shatter a whole type into an ERROR node:
        # (H) members register as module-level Functions and the directive
        # (H) CONDITION becomes a phantom node. On any parse error, retry with
        # (H) the conditional-directive LINES blanked (line-count preserving,
        # (H) both branches kept -- the duplicate-qn machinery absorbs twin
        # (H) branch definitions) and keep the tree with the smaller error.
        # (H) has_error, not the line-span metric: the shatter often yields
        # (H) SINGLE-LINE inner ERROR nodes inside a plausibly-shaped wrong
        # (H) declaration (a property named after the directive condition),
        # (H) which a span measure scores as zero.
        if not tree.root_node.has_error or b"#if" not in source_bytes:
            return tree
        retry = parser.parse(_blank_csharp_directives(source_bytes))
        if _count_error_nodes(retry.root_node) < _count_error_nodes(tree.root_node):
            return retry
        return tree
    if language not in (cs.SupportedLanguage.CPP, cs.SupportedLanguage.C):
        return tree
    worst = _max_error_span(tree.root_node)
    total_lines = source_bytes.count(_CHAR_NEWLINE) + 1
    # (H) local errors recover fine through query matching; only a collapse
    # (H) covering most of the file warrants the blank-and-retry pass
    if worst * 2 < total_lines:
        return tree
    lines = source_bytes.split(_CHAR_NEWLINE)
    candidates = _unbalanced_leaf_branches(lines)
    if not candidates:
        return tree
    # (H) prefer the smallest blanked subset: an unrelated branch whose textual
    # (H) imbalance survives comment stripping (a brace in a string or macro
    # (H) payload) must not lose its definitions when a single real offender
    # (H) explains the collapse; the full set stays as the last resort
    subsets: list[list[tuple[int, int]]] = [[c] for c in candidates]
    if len(candidates) > 1:
        subsets.append(candidates)
    best = tree
    best_worst = worst
    for subset in subsets:
        retry = parser.parse(_blank(lines, subset))
        retry_worst = _max_error_span(retry.root_node)
        if retry_worst < best_worst:
            best = retry
            best_worst = retry_worst
        if best_worst == 0:
            break
    return best
