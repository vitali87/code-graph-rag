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

            if parent_method_qn in function_registry:
                ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
                    cs.RelationshipType.OVERRIDES,
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, parent_method_qn),
                )
                logger.debug(
                    logs.CLASS_METHOD_OVERRIDE.format(
                        method_qn=method_qn, parent_method_qn=parent_method_qn
                    )
                )
                return

        if current_class in class_inheritance:
            for parent_class_qn in class_inheritance[current_class]:
                if parent_class_qn not in visited:
                    visited.add(parent_class_qn)
                    queue.append(parent_class_qn)
