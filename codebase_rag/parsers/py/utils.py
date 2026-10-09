from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from functools import lru_cache
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...constants import SEPARATOR_DOT
from ...types_defs import FunctionRegistryTrieProtocol, NodeType
from ..utils import follow_reexports

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor

_LITERAL_RECEIVER_START = re.compile(cs.PY_LITERAL_RECEIVER_RE)


def python_literal_type(
    node: Node, types: Mapping[str, str] = cs.PY_LITERAL_BUILTIN_TYPES
) -> str | None:
    """The builtin a literal node evaluates to, or None for any other node.

    A string is `bytes` when its prefix says so (`b"x"`, `rb'x'`); a
    concatenation takes its first part's. Parentheses and a sign on a number
    keep its type (`(1)`, `-1`, `(-1)`; bot review on PR #2912).
    """
    if node.type == cs.TS_PY_PARENTHESIZED_EXPRESSION:
        inner = node.named_children
        return python_literal_type(inner[0], types) if len(inner) == 1 else None
    if node.type == cs.TS_PY_UNARY_OPERATOR:
        operand = node.child_by_field_name(cs.TS_FIELD_ARGUMENT)
        builtin = None if operand is None else python_literal_type(operand, types)
        return builtin if builtin in (cs.PY_TYPE_INT, cs.PY_TYPE_FLOAT) else None
    builtin = types.get(node.type)
    if builtin != cs.PY_TYPE_STR:
        return builtin
    first = (
        node.named_children[0]
        if node.type == cs.TS_PY_CONCATENATED_STRING and node.named_children
        else node
    )
    start = first.children[0] if first.children else None
    prefix = (
        start.text.decode(cs.ENCODING_UTF8, errors="replace")
        if start is not None and start.type == cs.TS_PY_STRING_START and start.text
        else ""
    )
    return (
        cs.PY_TYPE_BYTES
        if cs.PY_BYTES_PREFIX_CHAR in prefix.lower()
        else cs.PY_TYPE_STR
    )


@lru_cache(maxsize=4096)
def python_literal_text_type(text: str) -> str | None:
    """The builtin a receiver written as a literal evaluates to.

    A call reaches resolution as text (`"-".join`, `{}.get`), so the
    receiver is parsed back; only text that can open a literal is.
    """
    if not _LITERAL_RECEIVER_START.match(text):
        return None
    # Local import: parser_loader pulls in the language grammars.
    from ...parser_loader import load_parsers

    parsers, _ = load_parsers()
    parser = parsers.get(cs.SupportedLanguage.PYTHON)
    if parser is None:
        return None
    root = parser.parse(text.encode(cs.ENCODING_UTF8)).root_node
    if root.has_error or len(root.named_children) != 1:
        return None
    statement = root.named_children[0]
    if (
        statement.type != cs.TS_PY_EXPRESSION_STATEMENT
        or len(statement.named_children) != 1
    ):
        return None
    return python_literal_type(
        statement.named_children[0], cs.PY_RECEIVER_LITERAL_TYPES
    )


def resolve_dotted_class(
    path: str,
    module_qn: str,
    import_processor: ImportProcessor,
    function_registry: FunctionRegistryTrieProtocol,
    own_class_rebinds: bool = False,
) -> str | None:
    """The indexed class a dotted path names from `module_qn`, else None.

    `pkg.Client`, `pkg._client.Client` and `Outer.Inner` start with a name
    the module binds (an import, or a class of its own); the rest is looked
    up under what that name refers to, following the package's re-exports
    (`pkg/__init__.py`'s `from ._client import Client`). A path into a module
    outside the project (`pd.DataFrame`) names no indexed class.
    `own_class_rebinds`: the module's own class of that name is defined
    after the import and rebinds it, so only the class is looked in.
    """
    head, _, rest = path.partition(SEPARATOR_DOT)
    if not rest:
        return None
    import_mapping = import_processor.import_mapping
    own_class = f"{module_qn}{SEPARATOR_DOT}{head}"
    bases = (
        [own_class]
        if own_class_rebinds
        else _dotted_head_bases(head, module_qn, import_mapping.get(module_qn, {}))
    )
    for base in bases:
        qn = follow_reexports(
            f"{base}{SEPARATOR_DOT}{rest}", import_mapping, function_registry
        )
        if function_registry.get(qn) == NodeType.CLASS:
            return qn
    return None


def _dotted_head_bases(
    head: str, module_qn: str, import_map: dict[str, str]
) -> Iterator[str]:
    """What the first name of a dotted path can refer to, most specific first.

    `import pkg._client` binds `pkg` but is recorded as `pkg ->
    <project>.pkg._client`, so besides the recorded target the package
    itself is tried: the target cut after its `pkg` segment.
    """
    if target := import_map.get(head):
        yield target
        parts = target.split(SEPARATOR_DOT)
        for end in range(len(parts) - 1, 0, -1):
            if parts[end - 1] == head:
                yield SEPARATOR_DOT.join(parts[:end])
    yield f"{module_qn}{SEPARATOR_DOT}{head}"


def resolve_class_name(
    class_name: str,
    module_qn: str,
    import_processor: ImportProcessor,
    function_registry: FunctionRegistryTrieProtocol,
    require_registered: bool = False,
    kinds: frozenset[NodeType] | None = None,
) -> str | None:
    """The class qn `class_name` names from `module_qn`: import map first, then
    the module and its enclosing packages, then the registry's name search.

    `kinds` limits the two registry tiers to those node kinds. The name
    search matches any registered node whose last segment is the name, so
    without it a same-named method or property answers for a type.
    """
    # `is not None`, not truthiness: an import-map entry can be the empty
    # string (a relative JS specifier that climbs to the root), and the
    # original returned it as the answer rather than falling through.
    mapped = _import_mapped_class(
        class_name, module_qn, import_processor, function_registry, require_registered
    )
    if mapped is not None:
        return mapped
    return _class_in_module_or_enclosing_package(
        class_name, module_qn, function_registry, kinds
    ) or _class_by_simple_name(class_name, module_qn, function_registry, kinds)


def _is_wanted_kind(
    qualified_name: str,
    function_registry: FunctionRegistryTrieProtocol,
    kinds: frozenset[NodeType] | None,
) -> bool:
    if kinds is None:
        return qualified_name in function_registry
    return function_registry.get(qualified_name) in kinds


def _import_mapped_class(
    class_name: str,
    module_qn: str,
    import_processor: ImportProcessor,
    function_registry: FunctionRegistryTrieProtocol,
    require_registered: bool,
) -> str | None:
    import_map = import_processor.import_mapping.get(module_qn)
    if not import_map or class_name not in import_map:
        return None
    mapped = import_map[class_name]
    # C++ include entries map header STEMS to MODULE qns; when the
    # stem coincides with a class name (Directive.h defining class
    # Directive, the dominant C++ layout) the map answer is a module,
    # not a class. Callers that need a real registered node (call
    # attribution in Pass 3) must fall through to the registry-backed
    # steps below (issue #652: 11k phantom callers on souffle).
    if require_registered and function_registry.get(mapped) is None:
        return None
    return mapped


def _class_in_module_or_enclosing_package(
    class_name: str,
    module_qn: str,
    function_registry: FunctionRegistryTrieProtocol,
    kinds: frozenset[NodeType] | None = None,
) -> str | None:
    same_module_qn = f"{module_qn}.{class_name}"
    if _is_wanted_kind(same_module_qn, function_registry, kinds):
        return same_module_qn
    module_parts = module_qn.split(SEPARATOR_DOT)
    for i in range(len(module_parts) - 1, 0, -1):
        parent_module = SEPARATOR_DOT.join(module_parts[:i])
        potential_qn = f"{parent_module}.{class_name}"
        if _is_wanted_kind(potential_qn, function_registry, kinds):
            return potential_qn
    return None


def _class_by_simple_name(
    class_name: str,
    module_qn: str,
    function_registry: FunctionRegistryTrieProtocol,
    kinds: frozenset[NodeType] | None = None,
) -> str | None:
    matches = [
        match
        for match in function_registry.find_ending_with(class_name)
        if kinds is None or function_registry.get(match) in kinds
    ]
    # Among same-named candidates in different files (gson's per-factory nested
    # `Adapter`), prefer one nested in the CURRENT module: a sibling/enclosing
    # nested class shadows a same-named class elsewhere, so `class Sub extends
    # Adapter` binds to its own file's Adapter, not another file's that merely
    # sorts first. Fall back to the first full-segment match otherwise. A
    # dotted name (`Outer.Inner`, a nested type through its outer) matches on
    # its whole segment sequence; `class_name in parts` could never match it
    # and left every such base unresolved (CodeRabbit, #1770).
    wanted = class_name.split(SEPARATOR_DOT)
    module_prefix = f"{module_qn}{SEPARATOR_DOT}"
    same_module = [
        match
        for match in matches
        if match.startswith(module_prefix) and _ends_with_segments(match, wanted)
    ]
    if same_module:
        return str(min(same_module, key=len))
    for match in matches:
        if _ends_with_segments(match, wanted):
            return str(match)
    return None


def _ends_with_segments(qualified_name: str, wanted: list[str]) -> bool:
    """Whether the last `len(wanted)` dotted segments of `qualified_name` are
    exactly `wanted`: a full-segment match, one segment or several."""
    parts = qualified_name.split(SEPARATOR_DOT)
    return len(parts) >= len(wanted) and parts[-len(wanted) :] == wanted


def external_stdlib_base_method_names(parent_qns: list[str]) -> frozenset[str]:
    # Method names defined by any EXTERNAL stdlib base among a class's parents
    # (`textwrap.TextWrapper` -> its full attribute set). A subclass method with
    # one of these names overrides the stdlib base and is invoked by the base's
    # machinery (click's `_wrap_chunks` via textwrap's `wrap()`), so callers mark
    # it as an external-override reachability root. Only stdlib modules are
    # imported (sys.stdlib_module_names gate): importing them is side-effect-safe
    # and requires no third-party environment.
    import importlib
    import sys

    names: set[str] = set()
    for parent_qn in parent_qns:
        module_path, _, class_name = parent_qn.rpartition(SEPARATOR_DOT)
        if not module_path or not class_name:
            continue
        top_module = module_path.split(SEPARATOR_DOT, 1)[0]
        if top_module not in sys.stdlib_module_names:
            continue
        try:
            module = importlib.import_module(module_path)
            base = getattr(module, class_name, None)
        except Exception:  # noqa: S112
            # Broad on purpose: importing a stdlib module executes its
            # module-level code, which can raise arbitrary platform-specific
            # errors; the parser must degrade to "no external base info"
            # rather than crash the indexing run.
            continue
        if isinstance(base, type):
            names.update(dir(base))
    return frozenset(names)
