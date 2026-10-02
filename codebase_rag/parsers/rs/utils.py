import ntpath
import posixpath
import re
from collections.abc import Iterable, Sequence

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol
from ..utils import safe_decode_text

# `#[path = "support/helpers.rs"]` redirects the mod declaration it sits on
# to an arbitrary file. Only the plain string form is read: a cfg_attr
# wrapper is conditional and no single target speaks for it.
_RS_PATH_ATTRIBUTE = re.compile(r'^#\[\s*path\s*=\s*"([^"]+)"\s*\]$')


def path_attribute_target(decorators: Iterable[object]) -> str | None:
    """The file a `#[path = "..."]` attribute points the declaration at."""
    for decorator in decorators:
        if match := _RS_PATH_ATTRIBUTE.match(str(decorator).strip()):
            return match.group(1)
    return None


def path_attribute_qn_parts(
    dir_parts: Sequence[str], redirect: str
) -> list[str] | None:
    """Qn segments for a `#[path]` target, or None when none can be named.

    `dir_parts` is the directory the path counts from, and a `mod.rs`
    target names that directory rather than a file beside it. An absolute
    path, a Windows separator, a non-`.rs` target, or a climb above the
    repository root names a file the qn scheme never keys, so nothing is
    claimed rather than a spelling guessed at.
    """
    if (
        not redirect.endswith(cs.EXT_RS)
        or redirect.startswith("/")
        or ntpath.splitdrive(redirect)[0]
        or "\\" in redirect
    ):
        # Only a `.rs` file backs a module, and a rooted path names one the
        # qn scheme never keys. Both rooted forms are spelled out here: a
        # leading slash, and a `C:` drive that `ntpath` is what recognises
        # whatever platform the indexer itself runs on. `ntpath.isabs` is
        # not the check, since 3.13 changed it to call a single leading
        # slash relative (to the current drive), which is true of Windows
        # but says nothing about whether the repository holds the file.
        return None
    parts = posixpath.normpath(posixpath.join(*dir_parts, redirect)).split("/")
    stem = parts.pop()[: -len(cs.EXT_RS)]
    if not stem:
        return None
    if stem != cs.INDEX_MOD:
        parts.append(stem)
    cleaned = [part for part in parts if part not in ("", ".")]
    return None if not cleaned or ".." in cleaned else cleaned


def _collect_path_parts(node: Node, parts: list[str]) -> None:
    match node.type:
        case cs.TS_IDENTIFIER | cs.TS_TYPE_IDENTIFIER:
            if part := safe_decode_text(node):
                parts.append(part)
        case cs.TS_SCOPED_IDENTIFIER | cs.TS_RS_SCOPED_TYPE_IDENTIFIER:
            for child in node.children:
                if child.type != cs.SEPARATOR_DOUBLE_COLON:
                    _collect_path_parts(child, parts)
        case cs.TS_RS_CRATE | cs.KEYWORD_SUPER | cs.KEYWORD_SELF:
            if part := safe_decode_text(node):
                parts.append(part)


def _extract_path_from_node(node: Node) -> str:
    match node.type:
        case cs.TS_IDENTIFIER | cs.TS_TYPE_IDENTIFIER:
            return safe_decode_text(node) or ""
        case cs.TS_SCOPED_IDENTIFIER | cs.TS_RS_SCOPED_TYPE_IDENTIFIER:
            parts: list[str] = []
            _collect_path_parts(node, parts)
            return cs.SEPARATOR_DOUBLE_COLON.join(parts)
        case cs.TS_RS_CRATE | cs.KEYWORD_SUPER | cs.KEYWORD_SELF:
            return safe_decode_text(node) or ""
        case _:
            return ""


def _process_use_tree(node: Node, base_path: str, imports: dict[str, str]) -> None:
    match node.type:
        case cs.TS_IDENTIFIER | cs.TS_TYPE_IDENTIFIER:
            if name := safe_decode_text(node):
                imports[name] = _join_use_path(base_path, name)

        case cs.TS_SCOPED_IDENTIFIER | cs.TS_RS_SCOPED_TYPE_IDENTIFIER:
            # Inside a brace list the scoped path is RELATIVE to the
            # list's base (`use crate::flags::{hiargs::HiArgs}` names
            # crate::flags::hiargs::HiArgs); stored bare it can never be
            # followed, and every consumer of the re-export silently
            # loses resolution (ripgrep's HiArgs surface, issue #1039).
            if path := _extract_path_from_node(node):
                full_path = _join_use_path(base_path, path)
                imports[full_path.split(cs.SEPARATOR_DOUBLE_COLON)[-1]] = full_path

        case cs.TS_RS_USE_AS_CLAUSE:
            _process_use_as_clause(node, base_path, imports)

        case cs.TS_RS_USE_WILDCARD:
            _process_use_wildcard(node, base_path, imports)

        case cs.TS_RS_USE_LIST:
            for child in node.children:
                if child.type not in cs.RS_USE_LIST_DELIMITERS:
                    _process_use_tree(child, base_path, imports)

        case cs.TS_RS_SCOPED_USE_LIST:
            _process_scoped_use_list(node, base_path, imports)

        case cs.KEYWORD_SELF:
            _process_use_self(base_path, imports)

        case _:
            for child in node.children:
                _process_use_tree(child, base_path, imports)


def _join_use_path(base_path: str, path: str) -> str:
    return f"{base_path}{cs.SEPARATOR_DOUBLE_COLON}{path}" if base_path else path


def _process_use_self(base_path: str, imports: dict[str, str]) -> None:
    # `use std::io::{self, Write}` imports the base path ITSELF, in scope
    # under its own last segment (`io`), which is the name every later
    # `io::...` spelling in the file is written against. Keyed on the keyword
    # the binding is unreachable, the real name is absent, and those
    # spellings resolve by trie luck instead (issue #1054).
    if not base_path:
        return
    name = base_path.rsplit(cs.SEPARATOR_DOUBLE_COLON, maxsplit=1)[-1]
    # A relative base (`use super::{self, x}`) leaves a keyword where the name
    # would be, and rustc rejects that spelling outright ("imports need to be
    # explicitly named"), so there is no binding to record.
    if name not in cs.RS_PATH_KEYWORDS:
        # Marked weak: the name comes from the path rather than the source,
        # and it must not evict a name another `use` bound in the other
        # namespace (see the constant).
        imports[f"{cs.RS_SELF_MODULE_PREFIX}{name}"] = base_path


def _process_use_as_clause(node: Node, base_path: str, imports: dict[str, str]) -> None:
    original_path = ""
    alias_name = ""

    children = [c for c in node.children if c.type != cs.TS_RS_KEYWORD_AS]
    if len(children) == 2:
        path_node, alias_node = children

        if path_node.type == cs.KEYWORD_SELF:
            original_path = base_path or cs.KEYWORD_SELF
        else:
            original_path = _extract_path_from_node(path_node)
            if base_path and original_path:
                original_path = f"{base_path}{cs.SEPARATOR_DOUBLE_COLON}{original_path}"
            elif base_path:
                original_path = base_path

        alias_name = safe_decode_text(alias_node) or ""

    if alias_name and original_path:
        imports[alias_name] = original_path


def _process_use_wildcard(node: Node, base_path: str, imports: dict[str, str]) -> None:
    if wildcard_base := next(
        (
            _extract_path_from_node(child)
            for child in node.children
            if child.type != cs.RS_WILDCARD_PREFIX
        ),
        "",
    ):
        # Same base-path rule as scoped identifiers: inside a brace list
        # the glob's path is relative (`use crate::flags::{hiargs::*}`
        # globs crate::flags::hiargs), so the list's base joins it.
        if base_path:
            wildcard_base = f"{base_path}{cs.SEPARATOR_DOUBLE_COLON}{wildcard_base}"
        wildcard_key = f"{cs.RS_WILDCARD_PREFIX}{wildcard_base}"
        imports[wildcard_key] = wildcard_base
    elif base_path:
        wildcard_key = f"{cs.RS_WILDCARD_PREFIX}{base_path}"
        imports[wildcard_key] = base_path


def _process_scoped_use_list(
    node: Node, base_path: str, imports: dict[str, str]
) -> None:
    new_base_path = ""

    for child in node.children:
        match child.type:
            case (
                cs.TS_IDENTIFIER
                | cs.TS_SCOPED_IDENTIFIER
                | cs.TS_RS_CRATE
                | cs.KEYWORD_SUPER
                | cs.KEYWORD_SELF
            ):
                new_base_path = _extract_path_from_node(child)
            case cs.TS_RS_USE_LIST:
                final_base = (
                    f"{base_path}{cs.SEPARATOR_DOUBLE_COLON}{new_base_path}"
                    if base_path
                    else new_base_path
                )
                _process_use_tree(child, final_base, imports)


def _impl_field_type_name(impl_node: Node, field: str) -> str | None:
    for i in range(impl_node.child_count):
        if impl_node.field_name_for_child(i) != field:
            continue
        type_node = impl_node.child(i)
        if type_node is None:
            continue
        found, name = _impl_type_node_name(type_node)
        if found:
            return name
    return None


def _impl_type_node_name(type_node: Node) -> tuple[bool, str | None]:
    # (decided, name) for one impl target/trait type node; not decided means
    # the node carries no name here and the caller keeps scanning.
    match type_node.type:
        case cs.TS_GENERIC_TYPE:
            ident = next(
                (c for c in type_node.children if c.type == cs.TS_TYPE_IDENTIFIER),
                None,
            )
            return ident is not None, safe_decode_text(ident) if ident else None
        case cs.TS_TYPE_IDENTIFIER | cs.TS_RS_PRIMITIVE_TYPE:
            return True, safe_decode_text(type_node)
        case cs.TS_RS_SCOPED_TYPE_IDENTIFIER:
            name = next(
                (
                    decoded
                    for c in type_node.children
                    if c.type == cs.TS_TYPE_IDENTIFIER
                    and (decoded := safe_decode_text(c))
                ),
                None,
            )
            return name is not None, name
    return False, None


def extract_return_type_name(func_node: Node, impl_target: str | None) -> str | None:
    # Bare name of a Rust fn's return type, for chained-call and `let x =
    # Type::assoc()` resolution: `Self` -> the impl target, `Result<T>`/`Option<T>`
    # -> their inner `T` (what a `?`/`.unwrap()` yields), a scoped type -> its last
    # segment. Returns None when no return type.
    return_type = func_node.child_by_field_name(cs.FIELD_RETURN_TYPE)
    if return_type is None:
        return None
    return _rust_return_type_name(return_type, impl_target)


def tuple_group_inner(node: Node) -> Node | None:
    # The single grouped type inside a tuple_type, or None for a real tuple.
    # Grouping parens (`&(dyn Svc + Send)`) have one typed child and NO comma; a
    # 1-ary tuple `(T,)` also has one typed child, so the comma, not the child
    # count, separates the two.
    if any(c.type == cs.CHAR_COMMA for c in node.children):
        return None
    inners = [c for c in node.children if c.type in cs.RS_RETURN_TYPE_NODE_TYPES]
    return inners[0] if len(inners) == 1 else None


def _rust_return_type_name(node: Node, impl_target: str | None) -> str | None:
    match node.type:
        case cs.TS_TYPE_IDENTIFIER | cs.TS_RS_PRIMITIVE_TYPE:
            name = safe_decode_text(node)
            return impl_target if name == cs.RS_SELF_TYPE else name
        case cs.TS_GENERIC_TYPE:
            return _rust_generic_type_name(node, impl_target)
        case cs.TS_RS_SCOPED_TYPE_IDENTIFIER:
            name_node = node.child_by_field_name(cs.FIELD_NAME)
            return safe_decode_text(name_node) if name_node else None
        case cs.TS_RS_TUPLE_TYPE:
            inner = tuple_group_inner(node)
            return _rust_return_type_name(inner, impl_target) if inner else None
        case _:
            # reference_type (`&Frame`) / pointer_type / dyn / impl / bounded:
            # descend to the first typed child (a bounded type's first bound
            # is the principal trait; the rest are auto-trait markers).
            for child in node.children:
                if child.type in cs.RS_RETURN_TYPE_NODE_TYPES:
                    return _rust_return_type_name(child, impl_target)
            return None


def _rust_generic_type_name(node: Node, impl_target: str | None) -> str | None:
    outer = node.child_by_field_name(cs.FIELD_TYPE)
    outer_name = _rust_return_type_name(outer, impl_target) if outer else None
    if outer_name not in cs.RS_RETURN_STRIP_WRAPPERS:
        return outer_name
    # Result<T>/Option<T>/Arc<T>: the useful type is the inner argument.
    args = node.child_by_field_name(cs.TS_RS_TYPE_ARGUMENTS)
    if args is None:
        return outer_name
    inner = next(
        (c for c in args.children if c.type in cs.RS_RETURN_TYPE_NODE_TYPES), None
    )
    return _rust_return_type_name(inner, impl_target) if inner else outer_name


def extract_impl_target(impl_node: Node) -> str | None:
    if impl_node.type != cs.TS_IMPL_ITEM:
        return None
    return _impl_field_type_name(impl_node, cs.FIELD_TYPE)


def extract_impl_trait(impl_node: Node) -> str | None:
    # The `trait` field of `impl Trait for Type` -> the implemented trait's
    # simple name (a trait impl means Type IMPLEMENTS Trait).
    if impl_node.type != cs.TS_IMPL_ITEM:
        return None
    if name := _impl_field_type_name(impl_node, cs.FIELD_TRAIT):
        return name
    # A generic wrapping a SCOPED path (`std::ops::Add<u32>`) is the one
    # trait spelling the field walk reads no name off, and the whole block
    # then read as no trait impl at all: no IMPLEMENTS edge, no implementer,
    # no override for its methods. The written path's last segment is the
    # simple name (issue #1080).
    path = extract_impl_trait_path(impl_node)
    return path.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1] if path else None


def extract_impl_trait_path(impl_node: Node) -> str | None:
    # The trait AS WRITTEN (`std::io::Read`, `serde::de::Visitor`), which the
    # simple name discards. Only the written head says which crate speaks for
    # the trait, and that decides whether its dispatch is external (#1048).
    if impl_node.type != cs.TS_IMPL_ITEM:
        return None
    for i in range(impl_node.child_count):
        if impl_node.field_name_for_child(i) != cs.FIELD_TRAIT:
            continue
        if (trait_node := impl_node.child(i)) is None:
            continue
        if (text := safe_decode_text(trait_node)) is None:
            continue
        # Type arguments (`FromIterator<u8>`) belong to the instantiation,
        # not the path.
        return text.split(cs.CHAR_ANGLE_OPEN, 1)[0].strip()
    return None


def extract_use_imports(use_node: Node) -> dict[str, str]:
    if use_node.type != cs.TS_USE_DECLARATION:
        return {}

    imports: dict[str, str] = {}

    argument_node = use_node.child_by_field_name(cs.RS_FIELD_ARGUMENT)
    if argument_node:
        _process_use_tree(argument_node, "", imports)

    return imports


def rust_use_scope(node: Node) -> tuple[Node | None, list[str] | None, bool]:
    """Classify a `use` declaration's storage scope.

    Returns (fn_node, None, True) when the use sits directly in a
    function body: it keys on that function's REGISTERED qn, resolved by
    span after ingestion (never re-derived from source names, which
    diverge on qn collisions). Returns (None, parts, pure) when the
    nearest scope is a module chain, with parts mirroring the
    registered-qn path of the items inside it (mod names, impl targets,
    and class names, with functions contributing nothing; [] is file
    scope). Returns (block_node, None, True) when the use sits inside a
    const/static initializer block, wherever the item is declared (mod
    level, file level, or a class body): the use is scoped to its
    innermost enclosing block alone (E0425 outside it), no qn scope
    corresponds to the block, and any key would serve a REAL scope's
    readers, so it stores span-gated and answers only calls written
    inside the block. `pure` is False when a mod chain passes through a
    function or a const/static initializer: such a mod forges a qn
    namespace it does not own, so on a key collision the pure writer's
    map wins.
    """
    block_scoped, nearest = _rust_use_nearest_scope(node)
    if block_scoped:
        return nearest, None, True
    if nearest is None:
        return None, [], True
    if nearest.type == cs.TS_RS_FUNCTION_ITEM:
        return nearest, None, True
    if nearest.type != cs.TS_RS_MOD_ITEM:
        return None, None, True
    parts, pure = _rust_mod_chain_parts(node)
    return None, parts, pure


def _rust_use_nearest_scope(node: Node) -> tuple[bool, Node | None]:
    # (True, block) when the use is scoped to one block alone (a
    # const/static initializer, an inner fn block, or a class-body
    # expression); else (False, the nearest fn/class-like/mod scope or None).
    block = None
    current = node.parent
    while current and current.type != cs.TS_RS_SOURCE_FILE:
        if block is None and current.type == cs.TS_RS_BLOCK:
            block = current
        if current.type in (cs.TS_RS_CONST_ITEM, cs.TS_RS_STATIC_ITEM):
            return True, block
        if (
            current.type == cs.TS_RS_FUNCTION_ITEM
            or current.type in cs.FQN_RS_SCOPE_TYPES
        ):
            return (
                (True, block)
                if _rust_use_in_inner_block(current, block)
                else (False, current)
            )
        current = current.parent
    return False, None


def _rust_use_in_inner_block(scope: Node, block: Node | None) -> bool:
    if block is None:
        return False
    if scope.type == cs.TS_RS_FUNCTION_ITEM:
        # A use in an INNER block of the fn body is in scope for that block
        # alone (rustc-verified): store it span-gated like an
        # initializer-block use, never fn-wide.
        body = scope.child_by_field_name(cs.FIELD_BODY)
        if body is None or block.id != body.id:
            return True
    # An expression block in a class-body position (enum discriminant, array
    # length, const-generic default) is scoped to that block alone, exactly
    # like a const/static initializer: a class type has no qn a use could key
    # on, so store it span-gated instead of dropping it (#1016).
    return scope.type in cs.FQN_RS_SCOPE_TYPES


def _rust_mod_chain_parts(node: Node) -> tuple[list[str], bool]:
    parts: list[str] = []
    pure = True
    current = node.parent
    while current and current.type != cs.TS_RS_SOURCE_FILE:
        if current.type in (
            cs.TS_RS_FUNCTION_ITEM,
            cs.TS_RS_CONST_ITEM,
            cs.TS_RS_STATIC_ITEM,
        ):
            pure = False
        elif (segment := _rust_scope_segment(current)) is not None:
            parts.append(segment)
        current = current.parent
    parts.reverse()
    return parts, pure


def _rust_scope_segment(scope: Node) -> str | None:
    # The qn segment an impl block or class-like/mod scope contributes.
    if scope.type == cs.TS_IMPL_ITEM:
        return extract_impl_target(scope) or None
    if scope.type not in cs.FQN_RS_SCOPE_TYPES:
        return None
    name_node = scope.child_by_field_name(cs.FIELD_NAME)
    if name_node is None or name_node.text is None:
        return None
    return name_node.text.decode(cs.RS_ENCODING_UTF8)


def block_item_at(
    block_items: Iterable[tuple[int, int, dict[str, str]]],
    registry: FunctionRegistryTrieProtocol,
    name: str,
    call_point: int | None,
) -> str | None:
    """The item `name` binds to in the innermost block holding this site.

    Innermost wins: nested blocks may each declare the name, and only the
    tightest one is in scope where the site is written. Keyed by FILE, not by
    caller: a fn declared inside the block is inside the block, so its own
    body binds the block's items too.
    """
    if call_point is None:
        return None
    best: tuple[int, str] | None = None
    for start, end, items in block_items:
        item_qn = items.get(name)
        if item_qn is None or not (start <= call_point < end):
            continue
        if (best is None or end - start < best[0]) and item_qn in registry:
            best = (end - start, item_qn)
    return best[1] if best else None


def literal_receiver_types(receiver: str) -> frozenset[str] | None:
    """The primitive types a literal method-call receiver can have.

    None when `receiver` is not a literal at all. A literal's type is a
    primitive (or an array, which yields the empty set), so only an impl
    block on that primitive can hold the method it calls (issue #2543).
    """
    if cs.RS_BYTE_STRING_LITERAL.match(receiver):
        return frozenset()
    if cs.RS_STRING_LITERAL.match(receiver):
        return frozenset({cs.RS_STR_TYPE})
    if receiver.startswith(cs.RS_BYTE_LITERAL_PREFIX):
        return frozenset({cs.RS_BYTE_TYPE})
    if receiver.startswith(cs.RS_CHAR_LITERAL_PREFIX):
        return frozenset({cs.RS_CHAR_TYPE})
    if receiver in cs.RS_BOOL_LITERALS:
        return frozenset({cs.RS_BOOL_TYPE})
    if receiver[:1].isdigit():
        if suffix := cs.RS_NUMERIC_SUFFIX.search(receiver):
            return frozenset({suffix.group()})
        return cs.RS_NUMERIC_TYPES
    return None


def is_body_local(node: Node) -> bool:
    """True when *node* sits directly in a function body or an initializer.

    Such an item is in scope for that body alone: no path from another module
    names it, so it owns none of the qn its enclosing module or impl target
    hands out (issue #1037). A type declared in the body is a different
    matter, and the walk stops there: an `impl` written in a method body is
    still the owner of the methods inside it, whatever encloses the impl.
    """
    current = node.parent
    while current is not None and current.type != cs.TS_RS_SOURCE_FILE:
        if current.type in cs.SPEC_RS_CLASS_TYPES:
            return False
        if current.type in cs.RS_BODY_LOCAL_CONTAINERS:
            return True
        current = current.parent
    return False


def enclosing_mod_names(node: Node) -> frozenset[str]:
    """Names of `mod` items in scope at the node's own module level.

    A uniform-path use head binds any `mod` in scope before an extern
    crate, whether file-backed (`mod pool;`) or inline (`mod pool { }`),
    so callers gate crate-name rewriting on this set (issue #1033). The
    walk stops at the first module boundary: a parent module's items,
    including the enclosing mod's OWN name, are not in scope inside it
    (rustc binds `use util::x;` within `mod util { }` to the crate).
    """
    names: set[str] = set()
    current = node.parent
    while current is not None:
        for sibling in current.named_children:
            if (
                sibling.type == cs.TS_RS_MOD_ITEM
                and (name_node := sibling.child_by_field_name(cs.FIELD_NAME))
                is not None
                and (text := name_node.text) is not None
            ):
                names.add(text.decode(cs.RS_ENCODING_UTF8))
        if current.type == cs.TS_RS_SOURCE_FILE or (
            current.parent is not None and current.parent.type == cs.TS_RS_MOD_ITEM
        ):
            break
        current = current.parent
    return frozenset(names)


def rust_block_scope_holes(
    block: Node,
) -> tuple[
    list[tuple[int, int]],
    list[tuple[int, int]],
    list[tuple[int, int, dict[str, tuple[int, int]]]],
]:
    """Byte spans of the scope-forming items nested inside a block.

    A use declared in a const/static initializer block reaches code in
    the block itself and in nested closures, but a nested `mod` is a
    hard name-resolution boundary (its members never see the block's
    use), and a nested `fn` is an inner scope whose OWN body uses win
    before the block's (though it inherits the block's use when it has
    none). Returns (mod spans, fn spans, item scopes): every PLAIN
    block in the subtree is an item scope in Rust (a fn body, a let or
    const initializer, an if/match/loop body alike), so each nested
    block declaring function items as DIRECT children is recorded with
    those items by name and span, and a call inside it naming one binds
    that local item,
    never the initializer block's use (rustc-verified; items deeper
    inside are invisible at that block's level, and methods in impl or
    trait bodies are not bare names at all). The walk descends
    everywhere since a mod nested inside a fn still needs its boundary
    recorded.
    """
    mod_holes: list[tuple[int, int]] = []
    fn_holes: list[tuple[int, int]] = []
    item_scopes: list[tuple[int, int, dict[str, tuple[int, int]]]] = []
    stack = list(block.children)
    while stack:
        current = stack.pop()
        if current.type == cs.TS_RS_MOD_ITEM:
            mod_holes.append((current.start_byte, current.end_byte))
        elif current.type == cs.TS_RS_FUNCTION_ITEM:
            fn_holes.append((current.start_byte, current.end_byte))
        elif current.type == cs.TS_RS_BLOCK and (
            items := _rust_direct_block_items(current)
        ):
            item_scopes.append((current.start_byte, current.end_byte, items))
        stack.extend(current.children)
    return mod_holes, fn_holes, item_scopes


def rust_block_item_scopes(
    root: Node,
) -> list[tuple[int, int, dict[str, tuple[int, int]]]]:
    """Every plain block in the file that declares function items directly.

    A block item is in scope for the block alone, yet it registers flat in
    the enclosing module beside the module's own items, so the span is the
    only thing that says which calls may reach it (issue #1061).
    """
    scopes: list[tuple[int, int, dict[str, tuple[int, int]]]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_RS_BLOCK and (
            items := _rust_direct_block_items(current)
        ):
            scopes.append((current.start_byte, current.end_byte, items))
        stack.extend(current.children)
    return scopes


def _rust_direct_block_items(block_node: Node) -> dict[str, tuple[int, int]]:
    """Function items declared directly in a block, by name and span key.

    The span (1-based line, column) is the item's identity: a block item
    and a same-named item at module level register flat in one module, so
    only the span tells a caller which of them a call in the block binds.
    """
    items: dict[str, tuple[int, int]] = {}
    for child in block_node.children:
        if (
            child.type == cs.TS_RS_FUNCTION_ITEM
            and (name_node := child.child_by_field_name(cs.FIELD_NAME))
            and (text := name_node.text) is not None
        ):
            items[text.decode(cs.RS_ENCODING_UTF8)] = (
                child.start_point[0] + 1,
                child.start_point[1],
            )
    return items


def enclosing_mod_fn_spans(node: Node) -> list[tuple[int, int]]:
    """Spans of every fn declared in the mod block this use sits DIRECTLY in.

    A mod-level `use` applies mod-wide, so its imports must reach the
    functions declared there (through impl/trait blocks and nested fns)
    even when the mod's shared qn key is lost to a same-named twin. A use
    nested any deeper (a const/static initializer block or a fn body
    inside the mod) is scoped to that block by Rust and reaches no mod
    function, and a nested mod is its own scope and does not inherit the
    use, so descent stops at mod boundaries.
    """
    body = node.parent
    mod_node = body.parent if body is not None else None
    if body is None or mod_node is None or mod_node.type != cs.TS_RS_MOD_ITEM:
        return []
    spans: list[tuple[int, int]] = []
    stack = list(body.children)
    while stack:
        current = stack.pop()
        if current.type == cs.TS_RS_MOD_ITEM:
            continue
        if current.type == cs.TS_RS_FUNCTION_ITEM:
            spans.append((current.start_point[0] + 1, current.start_point[1]))
        stack.extend(current.children)
    return spans


def record_effective_module(
    store: dict[str, str],
    qn: str,
    node: Node,
    module_qn: str | None,
    language: cs.SupportedLanguage | None,
) -> None:
    """Record the MODULE a Rust item is contained by, keyed by its registered qn.

    `super::`/`self::` count from the innermost enclosing `mod`, which an impl
    block is NOT, and qn space cannot tell the two apart: an inline mod and an
    impl target are both a bare segment, and an impl target the graph never
    registers (`impl Serialize for Vec<u8>`) is indistinguishable from a mod by
    lookup alone. Guessing wrong climbs one level too few and mints a WRONG
    edge, which revives dead code, so the answer is taken from the AST here --
    the same walk that builds the qn -- rather than re-derived at resolve time
    (issue #1086).
    """
    if language != cs.SupportedLanguage.RUST or module_qn is None:
        # No module to count from: recording the mod chain alone would key a
        # relative fragment the resolver would read as an absolute qn.
        return
    mod_parts = build_module_path(node)
    store[qn] = (
        module_qn + cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(mod_parts)
        if mod_parts
        else module_qn
    )


def _module_path_segment(
    node: Node, include_impl_targets: bool, class_types: Sequence[str]
) -> str | None:
    # The qn segment an enclosing item contributes: a `mod` name, an `impl`
    # target when requested, or a class-like item's name when requested.
    if node.type == cs.TS_RS_MOD_ITEM:
        return _declared_name_text(node)
    if node.type == cs.TS_IMPL_ITEM:
        return (extract_impl_target(node) or None) if include_impl_targets else None
    if node.type in class_types:
        return _declared_name_text(node)
    return None


def _declared_name_text(node: Node) -> str | None:
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    if name_node is None or name_node.text is None:
        return None
    return name_node.text.decode(cs.RS_ENCODING_UTF8)


def build_module_path(
    node: Node,
    include_impl_targets: bool = False,
    include_classes: bool = False,
    class_node_types: Sequence[str] | None = None,
) -> list[str]:
    path_parts: list[str] = []
    current = node.parent

    class_types = (class_node_types or ()) if include_classes else ()
    while current and current.type != cs.TS_RS_SOURCE_FILE:
        segment = _module_path_segment(current, include_impl_targets, class_types)
        if segment is not None:
            path_parts.append(segment)
        current = current.parent

    path_parts.reverse()
    return path_parts


def in_attribute_arguments(token: Node) -> bool:
    """True when a token sits inside an attribute's `#[...]` arguments.

    tree-sitter-rust gives attribute arguments the same `token_tree` a macro
    body gets, so `skip(self)` in `#[instrument(skip(self))]` has the shape of
    a macro-internal call. Only the owner of the enclosing groups tells them
    apart (issue #2541).
    """
    group = token.parent
    while group is not None and group.type == cs.TS_RS_TOKEN_TREE:
        if _is_raw_attribute_group(group):
            return True
        group = group.parent
    return group is not None and group.type == cs.TS_RS_ATTRIBUTE


def _is_raw_attribute_group(group: Node) -> bool:
    # A macro body (`macro_rules!` arm, `quote!`) does not parse the attributes
    # it carries: `#[cfg(not(test))]` there is a `#` token then a `[...]` group.
    first = group.child(0)
    if first is None or first.type != cs.TS_RS_TOKEN_BRACKET_OPEN:
        return False
    marker = group.prev_sibling
    if marker is not None and marker.type == cs.TS_RS_TOKEN_BANG:
        marker = marker.prev_sibling
    return marker is not None and marker.type == cs.TS_RS_TOKEN_HASH
