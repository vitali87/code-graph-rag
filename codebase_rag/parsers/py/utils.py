from __future__ import annotations

from typing import TYPE_CHECKING

from ...constants import SEPARATOR_DOT
from ...types_defs import FunctionRegistryTrieProtocol

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor


def resolve_class_name(
    class_name: str,
    module_qn: str,
    import_processor: ImportProcessor,
    function_registry: FunctionRegistryTrieProtocol,
    require_registered: bool = False,
) -> str | None:
    if module_qn in import_processor.import_mapping:
        import_map = import_processor.import_mapping[module_qn]
        if class_name in import_map:
            mapped = import_map[class_name]
            # (H) C++ include entries map header STEMS to MODULE qns; when the
            # (H) stem coincides with a class name (Directive.h defining class
            # (H) Directive, the dominant C++ layout) the map answer is a module,
            # (H) not a class. Callers that need a real registered node (call
            # (H) attribution in Pass 3) must fall through to the registry-backed
            # (H) steps below (issue #652: 11k phantom callers on souffle).
            if not require_registered or function_registry.get(mapped) is not None:
                return mapped

    same_module_qn = f"{module_qn}.{class_name}"
    if same_module_qn in function_registry:
        return same_module_qn

    module_parts = module_qn.split(SEPARATOR_DOT)
    for i in range(len(module_parts) - 1, 0, -1):
        parent_module = SEPARATOR_DOT.join(module_parts[:i])
        potential_qn = f"{parent_module}.{class_name}"
        if potential_qn in function_registry:
            return potential_qn

    matches = function_registry.find_ending_with(class_name)
    # (H) Among same-named candidates in different files (gson's per-factory nested
    # (H) `Adapter`), prefer one nested in the CURRENT module: a sibling/enclosing
    # (H) nested class shadows a same-named class elsewhere, so `class Sub extends
    # (H) Adapter` binds to its own file's Adapter, not another file's that merely
    # (H) sorts first. Fall back to the first full-segment match otherwise.
    module_prefix = f"{module_qn}{SEPARATOR_DOT}"
    same_module = [
        match
        for match in matches
        if match.startswith(module_prefix) and class_name in match.split(SEPARATOR_DOT)
    ]
    if same_module:
        return str(min(same_module, key=len))

    for match in matches:
        match_parts = match.split(SEPARATOR_DOT)
        if class_name in match_parts:
            return str(match)

    return None


def external_stdlib_base_method_names(parent_qns: list[str]) -> frozenset[str]:
    # (H) Method names defined by any EXTERNAL stdlib base among a class's parents
    # (H) (`textwrap.TextWrapper` -> its full attribute set). A subclass method with
    # (H) one of these names overrides the stdlib base and is invoked by the base's
    # (H) machinery (click's `_wrap_chunks` via textwrap's `wrap()`), so callers mark
    # (H) it as an external-override reachability root. Only stdlib modules are
    # (H) imported (sys.stdlib_module_names gate): importing them is side-effect-safe
    # (H) and requires no third-party environment.
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
        except Exception:
            # (H) Broad on purpose: importing a stdlib module executes its
            # (H) module-level code, which can raise arbitrary platform-specific
            # (H) errors; the parser must degrade to "no external base info"
            # (H) rather than crash the indexing run.
            continue
        if isinstance(base, type):
            names.update(dir(base))
    return frozenset(names)
