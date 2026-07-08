from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs
from ...types_defs import NodeType

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import FunctionRegistryTrieProtocol


def process_all_method_overrides(
    function_registry: FunctionRegistryTrieProtocol,
    class_inheritance: dict[str, list[str]],
    ingestor: IngestorProtocol,
) -> None:
    logger.info(logs.CLASS_PASS_4)

    for method_qn in function_registry.keys():
        if (
            function_registry[method_qn] == NodeType.METHOD
            and cs.SEPARATOR_DOT in method_qn
        ):
            parts = method_qn.rsplit(cs.SEPARATOR_DOT, 1)
            if len(parts) == 2:
                class_qn, method_name = parts
                check_method_overrides(
                    method_qn,
                    method_name,
                    class_qn,
                    function_registry,
                    class_inheritance,
                    ingestor,
                )


def _signature_arity(method_name: str) -> int | None:
    # (H) Number of top-level parameters in a signatured method name
    # (H) (`readField(A,JsonReader,BoundField)` -> 3, `create()` -> 0); None when the
    # (H) name carries no signature (Python/JS methods). Commas inside generics
    # (H) (`Map<K, V>`) are nested, so only depth-0 commas separate parameters.
    open_idx = method_name.find(cs.CHAR_PAREN_OPEN)
    if open_idx < 0:
        return None
    inner = method_name[open_idx + 1 : method_name.rfind(cs.CHAR_PAREN_CLOSE)]
    if not inner.strip():
        return 0
    depth = 0
    count = 1
    for ch in inner:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def _find_override_by_arity(
    parent_class: str,
    method_name: str,
    function_registry: FunctionRegistryTrieProtocol,
) -> str | None:
    # (H) Override matching by exact signature fails when a subclass renames a generic
    # (H) type parameter (base `readField(A,...)` vs override `readField(T,...)`), which
    # (H) is a distinct qn. Java overriding is by name + erased parameter types, so fall
    # (H) back to a UNIQUE parent method with the same simple name and arity; ambiguous
    # (H) overloads (>1 candidate) are left unmatched rather than guessed.
    arity = _signature_arity(method_name)
    if arity is None:
        return None
    base_name = method_name.split(cs.CHAR_PAREN_OPEN, 1)[0]
    prefix = f"{parent_class}{cs.SEPARATOR_DOT}"
    matches: list[str] = []
    for qn, node_type in function_registry.find_with_prefix(parent_class):
        if node_type != NodeType.METHOD or not qn.startswith(prefix):
            continue
        leaf = qn[len(prefix) :]
        if cs.SEPARATOR_DOT in leaf.split(cs.CHAR_PAREN_OPEN, 1)[0]:
            continue  # (H) a method of a nested class, not directly on parent_class
        if leaf.split(cs.CHAR_PAREN_OPEN, 1)[0] == base_name and (
            _signature_arity(leaf) == arity
        ):
            matches.append(qn)
    return matches[0] if len(matches) == 1 else None


def check_method_overrides(
    method_qn: str,
    method_name: str,
    class_qn: str,
    function_registry: FunctionRegistryTrieProtocol,
    class_inheritance: dict[str, list[str]],
    ingestor: IngestorProtocol,
) -> None:
    if class_qn not in class_inheritance:
        return

    queue = deque([class_qn])
    visited = {class_qn}

    while queue:
        current_class = queue.popleft()

        if current_class != class_qn:
            parent_method_qn = f"{current_class}.{method_name}"
            if parent_method_qn not in function_registry:
                # (H) Fall back to name+arity so a generic type-var rename in the
                # (H) override signature still matches the base method.
                parent_method_qn = (
                    _find_override_by_arity(
                        current_class, method_name, function_registry
                    )
                    or parent_method_qn
                )

            if parent_method_qn in function_registry:
                ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
                    cs.RelationshipType.OVERRIDES,
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, parent_method_qn),
                )
                logger.debug(
                    logs.CLASS_METHOD_OVERRIDE,
                    method_qn=method_qn,
                    parent_method_qn=parent_method_qn,
                )
                return

        if current_class in class_inheritance:
            for parent_class_qn in class_inheritance[current_class]:
                if parent_class_qn not in visited:
                    visited.add(parent_class_qn)
                    queue.append(parent_class_qn)
