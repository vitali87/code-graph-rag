"""Which of a C# type name's arity twins a reference means (issue #2579).

`PB` and `PB<TResult>` are two types: CLR `PB` and ``PB`1``. A name lookup
that ignores type arguments finds one of them, so every reference that has
one in hand (a construction, a static call, a base, a parameter type) is
moved to the declaration its written arity names. Twins in one file carry
the arity in their qualified names (`N.PB`, `N.PB`1`); twins in two files
(`PB.cs`, `PB.TResult.cs`) share their declared form (`N.PB`) and are told
apart by the arity each declares.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import NamedTuple

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol, NodeType
from ...utils import qn_markers
from . import utils as csharp_utils

TYPE_DECLS = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)


class CSharpArityIndex(NamedTuple):
    registry: FunctionRegistryTrieProtocol
    # Only generic types are recorded: an absent qn declares no parameters.
    generic_arity: Mapping[str, int]
    class_namespaced: Mapping[str, str]
    namespaced_qns: Mapping[str, set[str]]
    partial_groups: dict[str, list[str]]

    def arity(self, type_qn: str) -> int:
        return self.generic_arity.get(type_qn, 0)

    def is_type(self, qn: str) -> bool:
        return self.registry.get(qn) in TYPE_DECLS

    def twin(self, type_qn: str, arity: int) -> str:
        """The type `type_qn`'s name denotes at `arity`.

        `type_qn` itself when it already has that arity, or when no single
        declaration of the name has it: an unknown twin is no reason to drop
        what the name lookup found.
        """
        if self.arity(type_qn) == arity:
            return type_qn
        sibling = qn_markers.with_leaf_arity(type_qn, arity)
        if sibling != type_qn and self.is_type(sibling):
            return sibling
        declared = self.class_namespaced.get(type_qn)
        if declared is None:
            return type_qn
        carriers = {
            qn
            for qn in self.namespaced_qns.get(declared, ())
            if self.is_type(qn) and self.arity(qn) == arity
        }
        return (
            csharp_utils.unique_carrier(
                _nearest(carriers, type_qn), self.partial_groups
            )
            or type_qn
        )

    def has_twin(self, type_qn: str) -> bool:
        """Whether a declaration of `type_qn`'s name has another arity."""
        declared = self.class_namespaced.get(type_qn)
        if declared is None:
            return False
        own = self.arity(type_qn)
        return any(
            self.arity(qn) != own
            for qn in self.namespaced_qns.get(declared, ())
            if self.is_type(qn)
        )


def spellings(simple_name: str, arity: int | None) -> list[str]:
    """The registered simple names a written `simple_name` of `arity` can
    carry: its CLR spelling first, for a twin declared beside another arity,
    then the plain one, which every other declaration keeps."""
    if not arity:
        return [simple_name]
    return [
        f"{simple_name}{cs.CSHARP_GENERIC_ARITY_MARKER}{arity}",
        simple_name,
    ]


def _nearest(carriers: set[str], anchor: str) -> set[str]:
    # Two projects may each declare the name; the one sharing the longest
    # qualified-name prefix with what the lookup found is the same project.
    if len(carriers) < 2:
        return carriers
    anchor_parts = anchor.split(cs.SEPARATOR_DOT)
    shared = {qn: _shared_prefix(anchor_parts, qn) for qn in carriers}
    best = max(shared.values())
    return {qn for qn, depth in shared.items() if depth == best}


def _shared_prefix(anchor_parts: Iterable[str], qn: str) -> int:
    depth = 0
    for mine, theirs in zip(anchor_parts, qn.split(cs.SEPARATOR_DOT), strict=False):
        if mine != theirs:
            break
        depth += 1
    return depth
