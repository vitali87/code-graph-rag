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
# (H) offset and line number of the surviving code is preserved) and re-parse;
# (H) the retry is kept only when it strictly shrinks the worst ERROR span.
import re

from tree_sitter import Node, Parser, Tree

from ... import constants as cs

_DIRECTIVE = re.compile(cs.CPP_PREPROC_CONDITIONAL_PATTERN)
_CHAR_OPEN_BRACE = b"{"
_CHAR_CLOSE_BRACE = b"}"
_CHAR_SPACE = b" "
_CHAR_NEWLINE = b"\n"

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


def _blank_unbalanced_leaf_branches(source: bytes) -> bytes | None:
    lines = source.split(_CHAR_NEWLINE)
    blanks: list[tuple[int, int]] = []
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
                delta = sum(
                    lines[i].count(_CHAR_OPEN_BRACE) - lines[i].count(_CHAR_CLOSE_BRACE)
                    for i in range(start, end + 1)
                )
                # (H) brace counting is textual (a brace inside a comment or
                # (H) string literal counts too); a false trigger only blanks a
                # (H) branch of a file that already parses as one ERROR, and
                # (H) the strict-improvement guard below discards a bad retry
                if delta != 0:
                    blanks.append((start, end))
    if not blanks:
        return None
    for start, end in blanks:
        for i in range(start, end + 1):
            lines[i] = _CHAR_SPACE * len(lines[i])
    return _CHAR_NEWLINE.join(lines)


def parse_with_preproc_recovery(
    parser: Parser, source_bytes: bytes, language: cs.SupportedLanguage
) -> Tree:
    tree = parser.parse(source_bytes)
    if language not in (cs.SupportedLanguage.CPP, cs.SupportedLanguage.C):
        return tree
    worst = _max_error_span(tree.root_node)
    total_lines = source_bytes.count(_CHAR_NEWLINE) + 1
    # (H) local errors recover fine through query matching; only a collapse
    # (H) covering most of the file warrants the blank-and-retry pass
    if worst * 2 < total_lines:
        return tree
    blanked = _blank_unbalanced_leaf_branches(source_bytes)
    if blanked is None:
        return tree
    retry = parser.parse(blanked)
    if _max_error_span(retry.root_node) < worst:
        return retry
    return tree
