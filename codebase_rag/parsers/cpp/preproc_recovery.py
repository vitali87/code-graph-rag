# Recovery for the conditional-brace preprocessor pattern that collapses a
# whole C/C++ file into one tree-sitter ERROR node. A branch can open a
# brace that a LATER branch closes (nlohmann binary_reader.hpp:
# `#ifdef __cpp_lib_byteswap ... else { #endif` then
# `#ifdef __cpp_lib_byteswap } #endif`); the preprocessor keeps braces
# balanced under every configuration, but tree-sitter keeps EVERY branch's
# tokens, sees an imbalance, and reinterprets the whole translation unit
# (methods degrade to free functions or vanish, orphaning dead-code
# clusters). On a catastrophic ERROR, blank the net-unbalanced LEAF
# conditional branches (space-filled, so byte offsets and line numbers
# survive) and re-parse, preferring the SMALLEST blanked subset: each
# candidate alone first, the full set as a last resort, keeping a retry only
# when it strictly shrinks the worst ERROR span.
import re

from tree_sitter import Node, Parser, Tree

from ... import constants as cs

_DIRECTIVE = re.compile(cs.CPP_PREPROC_CONDITIONAL_PATTERN)
# a line holding nothing but an ALL_CAPS identifier: a scope-marker macro
# invocation without a trailing semicolon (NLOHMANN_JSON_NAMESPACE_BEGIN,
# FMT_BEGIN_NAMESPACE)
_MACRO_MARKER = re.compile(rb"^[A-Z_][A-Z0-9_]{2,}$")
# node types whose content is payload, not code: a marker-shaped line
# inside one must never be blanked
_PAYLOAD_NODE_TYPES = frozenset(
    (
        "comment",
        "string_literal",
        "raw_string_literal",
        "raw_string_content",
        "string_content",
        "char_literal",
        "concatenated_string",
        "system_lib_string",
    )
)
_CHAR_OPEN_BRACE = b"{"
_CHAR_CLOSE_BRACE = b"}"
_CHAR_SPACE = b" "
_CHAR_NEWLINE = b"\n"
_LINE_COMMENT = b"//"

# A branch list plus whether the conditional contains nested conditionals:
# only LEAF conditionals are candidates. An outer region (an include guard
# spanning the whole file) legitimately holds unbalanced fragments between
# its nested directives, so blanking is restricted to branches whose entire
# content is directive-free.
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
    # a brace after `//` is prose, not structure (a doc note `// { see
    # design` must not mark its branch unbalanced); block comments and
    # string literals are left to the strict-improvement guard
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
            if not has_nested[0]:
                candidates.extend(_brace_unbalanced(lines, branches))
    return candidates


def _brace_unbalanced(
    lines: list[bytes], branches: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    # The non-empty branches of one leaf conditional whose code braces do not
    # net to zero.
    return [
        (start, end)
        for start, end in branches
        if start <= end
        and sum(_code_brace_delta(lines[i]) for i in range(start, end + 1)) != 0
    ]


def _blank(lines: list[bytes], ranges: list[tuple[int, int]]) -> bytes:
    out = list(lines)
    for start, end in ranges:
        for i in range(start, end + 1):
            out[i] = _CHAR_SPACE * len(out[i])
    return _CHAR_NEWLINE.join(out)


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


def _retry_without_macro_markers(
    parser: Parser, tree: Tree, source_bytes: bytes
) -> tuple[Tree, bytes]:
    # A bare scope-marker macro line (`NLOHMANN_JSON_NAMESPACE_BEGIN`, no
    # semicolon) glues onto the NEXT top-level construct: macro +
    # `template <...> struct ordered_map : base` parses as ONE declaration
    # whose struct head sinks into an init_declarator, and macro +
    # `namespace detail {` as a function_definition named `namespace`. The
    # damage yields only a couple of SMALL error nodes, so the whole-file
    # pass never fires, yet the class/namespace and its members are lost or
    # leak to module scope. Blanking the marker lines (offsets preserved) and
    # re-parsing recovers the structure; the strict error-count guard keeps
    # benign marker uses untouched. A `//` comment after the marker is prose.
    # All gated markers blank TOGETHER: a marker glued into a well-formed
    # neighbour (an attribute macro between `template<...>` and the
    # declarator) damages the tree SILENTLY with no error node, so no
    # per-subset metric can see which single marker repairs it. Collateral is
    # prevented structurally: a marker-shaped line whose covering node is
    # string or comment payload is never a candidate.
    if not tree.root_node.has_error:
        return tree, source_bytes
    lines = source_bytes.split(_CHAR_NEWLINE)
    markers: list[tuple[int, int]] = []
    offset = 0
    for i, line in enumerate(lines):
        code = line.split(_LINE_COMMENT, 1)[0]
        stripped = code.strip()
        if _MACRO_MARKER.match(stripped):
            start = offset + code.index(stripped)
            covering = tree.root_node.named_descendant_for_byte_range(
                start, start + len(stripped)
            )
            if covering is None or covering.type not in _PAYLOAD_NODE_TYPES:
                markers.append((i, i))
        offset += len(line) + 1
    if not markers:
        return tree, source_bytes
    blanked = _blank(lines, markers)
    retry = parser.parse(blanked)
    if _count_error_nodes(retry.root_node) < _count_error_nodes(tree.root_node):
        return retry, blanked
    return tree, source_bytes


_PREPROC_LINE = re.compile(cs.CPP_PREPROC_LINE_PATTERN)
_BLOCK_COMMENT = re.compile(cs.CPP_BLOCK_COMMENT_PATTERN)
_CTOR_INIT_COLON = re.compile(cs.CPP_CTOR_INIT_COLON_PATTERN)

# One conditional group: the line ranges of its directives (a directive
# continued with a trailing backslash spans several lines) and of its
# branches, both in source order.
_Conditional = tuple[list[tuple[int, int]], list[tuple[int, int]]]


def _directive_end(lines: list[bytes], index: int) -> int:
    while index + 1 < len(lines) and lines[index].rstrip().endswith(
        cs.CPP_PREPROC_CONTINUATION
    ):
        index += 1
    return index


def _preproc_line_flags(lines: list[bytes]) -> list[bool]:
    # every line of every directive, continuations included: the last line
    # of `#define LIST a, b,` must not read as a list left open
    flags: list[bool] = []
    index = 0
    while index < len(lines):
        if _PREPROC_LINE.match(lines[index]) is None:
            flags.append(False)
            index += 1
            continue
        end = _directive_end(lines, index)
        flags.extend([True] * (end - index + 1))
        index = end + 1
    return flags


def _conditional_groups(lines: list[bytes]) -> list[_Conditional]:
    groups: list[_Conditional] = []
    stack: list[_Conditional] = []
    index = 0
    while index < len(lines):
        match = _DIRECTIVE.match(lines[index])
        if match is None:
            index += 1
            continue
        end = _directive_end(lines, index)
        keyword = match.group(1)
        if keyword in cs.CPP_PREPROC_OPEN_DIRECTIVES:
            stack.append(([(index, end)], []))
        elif stack:
            directives, branches = stack[-1]
            branches.append((directives[-1][1] + 1, index - 1))
            directives.append((index, end))
            if keyword not in cs.CPP_PREPROC_SPLIT_DIRECTIVES:
                groups.append(stack.pop())
        index = end + 1
    return groups


def _code_text(line: bytes) -> bytes:
    return _BLOCK_COMMENT.sub(b"", line).split(_LINE_COMMENT, 1)[0].strip()


def _neighbour_code(
    lines: list[bytes], preproc: list[bool], index: int, step: int
) -> bytes:
    index += step
    while 0 <= index < len(lines):
        if not preproc[index]:
            code = _code_text(lines[index])
            if code:
                return code
        index += step
    return b""


def _in_list_context(before: bytes, after: bytes) -> bool:
    return (
        before.endswith(cs.CPP_LIST_OPEN_SUFFIXES)
        or _CTOR_INIT_COLON.search(before) is not None
        or (
            after.startswith(cs.CPP_LIST_ITEM_PREFIXES)
            and not after.startswith(cs.CPP_SCOPE_RESOLUTION_PREFIX)
        )
    )


def _list_context_conditionals(lines: list[bytes]) -> list[_Conditional]:
    preproc = _preproc_line_flags(lines)
    return [
        group
        for group in _conditional_groups(lines)
        if any(
            _in_list_context(
                _neighbour_code(lines, preproc, start, -1),
                _neighbour_code(lines, preproc, end, 1),
            )
            for start, end in group[0]
        )
    ]


def _directive_misparsed(root: Node, start: int, line: bytes) -> bool:
    # A directive the grammar accepted where it stands (an `#if` between
    # enumerators, an `#ifdef` under an access label) parses into an
    # error-free preproc node; only one tree-sitter choked on is a target.
    hash_byte = start + len(line) - len(line.lstrip())
    node = root.descendant_for_byte_range(hash_byte, hash_byte + 1)
    if node is None:
        return False
    return node.has_error or (node.parent is not None and node.parent.has_error)


def _count_syntax_faults(root: Node) -> int:
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_ERROR or node.is_missing:
            count += 1
        if node.has_error:
            stack.extend(node.children)
    return count


def _retry_without_list_directives(
    parser: Parser, tree: Tree, source_bytes: bytes
) -> tuple[Tree, bytes]:
    # A conditional inside a comma-separated list (snappy's member
    # initialiser `#if !defined(NDEBUG) begin_(iov), #endif`, issue #2614)
    # stands where the grammar admits no directive. tree-sitter opens a
    # preproc_if inside the class body that never meets its `#endif`, and
    # the class swallows the rest of the file: later classes nest in it,
    # free functions become its methods, the constructor is lost and the
    # enclosing namespace turns into an ERROR. Blanking the directive lines
    # (offsets preserved) splices the branch back into the list. Keeping
    # every branch loses nothing, so it is tried first; branches without
    # their own separator (`#ifdef A x_(1) #else x_(2) #endif {`) still
    # misparse that way, so the second try keeps only the FIRST branch, as a
    # compiler with the condition set would. A retry survives only when it
    # strictly reduces the ERROR and MISSING nodes.
    if not tree.root_node.has_error:
        return tree, source_bytes
    lines = source_bytes.split(_CHAR_NEWLINE)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    groups = [
        group
        for group in _list_context_conditionals(lines)
        if any(
            _directive_misparsed(tree.root_node, offsets[start], lines[start])
            for start, _ in group[0]
        )
    ]
    if not groups:
        return tree, source_bytes
    directives = [span for group_directives, _ in groups for span in group_directives]
    alternatives = [span for _, branches in groups for span in branches[1:]]
    attempts = [directives]
    if alternatives:
        attempts.append(directives + alternatives)
    best, best_source = tree, source_bytes
    best_faults = _count_syntax_faults(tree.root_node)
    for ranges in attempts:
        blanked = _blank(lines, ranges)
        retry = parser.parse(blanked)
        faults = _count_syntax_faults(retry.root_node)
        if faults < best_faults:
            best, best_source, best_faults = retry, blanked, faults
        if best_faults == 0:
            break
    return best, best_source


def _track_csharp_directive(stripped: bytes, skip_stack: list[bool]) -> bool:
    # Mutates skip_stack for the directive on this line; True means the
    # line IS a directive (always blanked). `#elif`/`#else` flip the
    # current group to skipping so only the FIRST branch survives; an
    # `#if` inside a skipped branch inherits the skip.
    if stripped.startswith(b"#if"):
        skip_stack.append(any(skip_stack))
        return True
    if stripped.startswith((b"#elif", b"#else")):
        if skip_stack:
            skip_stack[-1] = True
        return True
    if stripped.startswith(b"#endif"):
        if skip_stack:
            skip_stack.pop()
        return True
    return False


def _blank_csharp_directives(source_bytes: bytes) -> bytes:
    # Keep only the FIRST branch of each conditional group: retaining both
    # branches of an `#if X bodyA #else bodyB #endif` around an
    # expression-bodied member leaves an orphaned second body that
    # misparses into a phantom bare-name declaration (Polly's
    # DelegatingComponent grew a parameterless ExecuteComponent).
    lines = source_bytes.split(_CHAR_NEWLINE)
    skip_stack: list[bool] = []
    for i, line in enumerate(lines):
        if _track_csharp_directive(line.lstrip(), skip_stack) or (
            skip_stack and skip_stack[-1]
        ):
            lines[i] = b""
    return _CHAR_NEWLINE.join(lines)


def parse_with_preproc_recovery(
    parser: Parser, source_bytes: bytes, language: cs.SupportedLanguage
) -> Tree:
    tree = parser.parse(source_bytes)
    if language == cs.SupportedLanguage.CSHARP:
        # A C# conditional directive interleaved with declaration syntax
        # (`void M() #if X => Impl() #endif ;`, Serilog's ILogger default
        # interface bodies) can shatter a whole type into an ERROR node:
        # members register as module-level Functions and the directive
        # CONDITION becomes a phantom node. On any parse error, retry with
        # the conditional-directive LINES blanked (line-count preserving,
        # only the FIRST branch kept, since an orphaned alternative body would
        # misparse into a phantom bare-name declaration) and keep the tree
        # with the smaller error. Gate on has_error, not the line-span metric:
        # the shatter often yields SINGLE-LINE inner ERROR nodes inside a
        # plausibly-shaped wrong declaration (a property named after the
        # directive condition), which a span measure scores as zero.
        if not tree.root_node.has_error or b"#if" not in source_bytes:
            return tree
        retry = parser.parse(_blank_csharp_directives(source_bytes))
        if _count_error_nodes(retry.root_node) < _count_error_nodes(tree.root_node):
            return retry
        return tree
    if language not in (cs.SupportedLanguage.CPP, cs.SupportedLanguage.C):
        return tree
    tree, source_bytes = _retry_without_list_directives(parser, tree, source_bytes)
    tree, source_bytes = _retry_without_macro_markers(parser, tree, source_bytes)
    worst = _max_error_span(tree.root_node)
    total_lines = source_bytes.count(_CHAR_NEWLINE) + 1
    # local errors recover fine through query matching; only a collapse
    # covering most of the file warrants the blank-and-retry pass
    if worst * 2 < total_lines:
        return tree
    lines = source_bytes.split(_CHAR_NEWLINE)
    candidates = _unbalanced_leaf_branches(lines)
    if not candidates:
        return tree
    # prefer the smallest blanked subset: an unrelated branch whose textual
    # imbalance survives comment stripping (a brace in a string or macro
    # payload) must not lose its definitions when a single real offender
    # explains the collapse; the full set stays as the last resort
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
