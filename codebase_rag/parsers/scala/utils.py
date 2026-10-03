from __future__ import annotations

from collections.abc import Container

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import ScalaImportTarget, ScalaPackageIndex, ScalaPackageScan
from ..utils import safe_decode_text

# Subtrees whose identifiers name packages or import paths rather than use
# anything, so they are no evidence that a file uses an imported member. The
# package NAME only: a `package a { ... }` clause holds its whole body.
_NOT_MENTIONS = frozenset(
    {cs.TS_SCALA_IMPORT_DECLARATION, cs.TS_SCALA_PACKAGE_IDENTIFIER}
)
_MENTION_TYPES = frozenset({cs.TS_SCALA_IDENTIFIER, cs.TS_SCALA_TYPE_IDENTIFIER})


def _dotted_name(node: Node | None) -> str | None:
    # A package_identifier is loose identifiers and dots; joining the
    # identifiers drops whatever whitespace or comments sit between them.
    if node is None:
        return None
    parts = [
        text
        for child in node.children
        if child.type == cs.TS_SCALA_IDENTIFIER and (text := safe_decode_text(child))
    ]
    return cs.SEPARATOR_DOT.join(parts) if parts else None


def _joined(prefix: str, name: str) -> str:
    return f"{prefix}{cs.SEPARATOR_DOT}{name}" if prefix else name


def _member_name(node: Node) -> str | None:
    if node.type in cs.SCALA_NAMED_PACKAGE_MEMBERS:
        return safe_decode_text(node.child_by_field_name(cs.FIELD_NAME))
    if node.type in cs.SCALA_BINDING_DEFINITIONS:
        pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
        if pattern is not None and pattern.type == cs.TS_SCALA_IDENTIFIER:
            return safe_decode_text(pattern)
    return None


def _collect_packages(
    container: Node, package: str, found: dict[str, set[str]]
) -> None:
    for child in container.named_children:
        if child.type == cs.TS_SCALA_PACKAGE_CLAUSE:
            name = _dotted_name(child.child_by_field_name(cs.FIELD_NAME))
            if name is None:
                continue
            body = child.child_by_field_name(cs.FIELD_BODY)
            if body is None:
                # A bodiless clause opens its package for the REST of the
                # enclosing scope: every later sibling is declared in it.
                package = _joined(package, name)
                found.setdefault(package, set())
            else:
                found.setdefault(_joined(package, name), set())
                _collect_packages(body, _joined(package, name), found)
        elif child.type == cs.TS_SCALA_PACKAGE_OBJECT:
            name = safe_decode_text(child.child_by_field_name(cs.FIELD_NAME))
            body = child.child_by_field_name(cs.FIELD_BODY)
            if name and body is not None:
                members = found.setdefault(_joined(package, name), set())
                members.update(
                    member
                    for grandchild in body.named_children
                    if (member := _member_name(grandchild))
                )
        elif member := _member_name(child):
            found.setdefault(package, set()).add(member)


def _enclosing_packages(root: Node) -> tuple[str, ...]:
    # Each bodiless top-level clause opens the members of the package it
    # completes; `package com.acme.app` opens com.acme.app alone, while
    # `package com.acme` then `package app` opens com.acme.app and com.acme.
    opened: list[str] = []
    package = ""
    for child in root.named_children:
        if child.type != cs.TS_SCALA_PACKAGE_CLAUSE:
            continue
        if child.child_by_field_name(cs.FIELD_BODY) is not None:
            continue
        if name := _dotted_name(child.child_by_field_name(cs.FIELD_NAME)):
            package = _joined(package, name)
            opened.append(package)
    return tuple(reversed(opened))


def _mentions(root: Node) -> frozenset[str]:
    names: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _NOT_MENTIONS:
            continue
        if node.type in _MENTION_TYPES:
            if text := safe_decode_text(node):
                names.add(text)
            continue
        stack.extend(node.children)
    return frozenset(names)


def scan_scala_packages(root: Node) -> ScalaPackageScan:
    """The packages a Scala file declares, opens and the names it mentions."""
    found: dict[str, set[str]] = {}
    _collect_packages(root, "", found)
    return ScalaPackageScan(
        packages={package: frozenset(names) for package, names in found.items()},
        enclosing=_enclosing_packages(root),
        mentions=_mentions(root),
    )


def scala_import_candidates(path: str, enclosing: tuple[str, ...]) -> list[str]:
    """The absolute paths a written import path may mean, in lookup order.

    Scala resolves an import's first segment against the enclosing packages
    before the root, which is why `_root_` exists to force the root.
    """
    root_prefix = f"{cs.SCALA_ROOT_PACKAGE}{cs.SEPARATOR_DOT}"
    if path.startswith(root_prefix):
        return [path[len(root_prefix) :]]
    return [*(_joined(package, path) for package in enclosing), path]


def _type_name(type_node: Node | None) -> str | None:
    if type_node is None:
        return None
    match type_node.type:
        case cs.TS_SCALA_TYPE_IDENTIFIER | cs.TS_SCALA_STABLE_TYPE_IDENTIFIER:
            return safe_decode_text(type_node)
        case cs.TS_SCALA_GENERIC_TYPE:
            # `Box[Int]` is a Box; the argument is not the type.
            return _type_name(type_node.child_by_field_name(cs.FIELD_TYPE))
        case cs.TS_SCALA_LAZY_PARAMETER_TYPE:
            # A by-name `=> Cart` parameter evaluates to a Cart.
            return _type_name(type_node.child_by_field_name(cs.FIELD_TYPE))
        case cs.TS_SCALA_COMPOUND_TYPE:
            # `new A with B` builds an A first; its mixins come after.
            return _type_name(type_node.child_by_field_name(cs.TS_SCALA_FIELD_BASE))
    return None


def scala_instance_type_name(instance_node: Node) -> str | None:
    """The class `new C(...)` constructs, or None for `new { ... }`."""
    for child in instance_node.named_children:
        if (name := _type_name(child)) is not None:
            return name
    return None


def _value_type_name(value: Node | None) -> str | None:
    # `new Cart()` and a case-class apply `Item("x")` name the type they
    # build; any other initializer's type is unknown without inference.
    if value is None:
        return None
    if value.type == cs.TS_SCALA_INSTANCE_EXPRESSION:
        return scala_instance_type_name(value)
    if value.type == cs.TS_SCALA_CALL_EXPRESSION:
        callee = value.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if callee is not None and callee.type == cs.TS_SCALA_IDENTIFIER:
            name = safe_decode_text(callee)
            if name and name[:1].isupper():
                return name
    return None


def _binding(node: Node) -> tuple[str, str | None] | None:
    if node.type == cs.TS_SCALA_PARAMETER:
        name = safe_decode_text(node.child_by_field_name(cs.FIELD_NAME))
        return (
            (name, _type_name(node.child_by_field_name(cs.FIELD_TYPE)))
            if name
            else None
        )
    if node.type in cs.SCALA_BINDING_DEFINITIONS:
        pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
        if pattern is None or pattern.type != cs.TS_SCALA_IDENTIFIER:
            return None
        name = safe_decode_text(pattern)
        if not name:
            return None
        declared = _type_name(node.child_by_field_name(cs.FIELD_TYPE))
        return name, declared or _value_type_name(
            node.child_by_field_name(cs.FIELD_VALUE)
        )
    return None


def scala_binding_types(caller_node: Node) -> dict[str, str | None]:
    """Every name a caller binds, with the one type it is known to hold.

    Types come from parameters (`c: Cart`), annotated vals (`val c: Cart =
    ...`) and constructions (`val c = new Cart()`, `val i = Item("x")`). A
    name bound to an unknown type, or to two different ones, maps to None:
    still a local, so never mistaken for an object of the same name, and
    never typed by a guess that would let the wrong class's member answer.
    """
    types: dict[str, str | None] = {}
    stack = [caller_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if (binding := _binding(node)) is None:
            continue
        name, type_name = binding
        types[name] = type_name if types.get(name, type_name) == type_name else None
    return types


def scala_selection(node: Node) -> tuple[str, str] | None:
    """`(receiver, member)` of a parameterless selection like `c.size`.

    None when the selection is the callee of a call (the call node names it),
    an assignment target, or has a receiver that is not a plain name.
    """
    parent = node.parent
    if parent is not None:
        if parent.type in (cs.TS_SCALA_CALL_EXPRESSION, cs.TS_SCALA_GENERIC_FUNCTION):
            callee = parent.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if callee is not None and callee.id == node.id:
                return None
        if parent.type == cs.TS_SCALA_ASSIGNMENT_EXPRESSION:
            left = parent.child_by_field_name(cs.FIELD_LEFT)
            if left is not None and left.id == node.id:
                return None
    receiver = node.child_by_field_name(cs.FIELD_VALUE)
    member = safe_decode_text(node.child_by_field_name(cs.FIELD_FIELD))
    if receiver is None or receiver.type != cs.TS_SCALA_IDENTIFIER or not member:
        return None
    receiver_name = safe_decode_text(receiver)
    return (receiver_name, member) if receiver_name else None


def scala_package_index(
    scans: dict[str, ScalaPackageScan], known_module_qns: Container[str]
) -> ScalaPackageIndex:
    """The packages the known modules declare, and who declares each name."""
    modules: dict[str, dict[str, frozenset[str]]] = {}
    owners: dict[str, dict[str, list[str]]] = {}
    for module_qn, scan in scans.items():
        if module_qn not in known_module_qns:
            continue
        for package, names in scan.packages.items():
            modules.setdefault(package, {})[module_qn] = names
            package_owners = owners.setdefault(package, {})
            for name in names:
                package_owners.setdefault(name, []).append(module_qn)
    return ScalaPackageIndex(
        modules=modules,
        owners={
            package: {name: tuple(qns) for name, qns in names.items()}
            for package, names in owners.items()
        },
    )


def scala_package_owner(
    package: str, name: str, index: ScalaPackageIndex
) -> str | None:
    """The module declaring `name` in `package`, when exactly one does.

    None when no module declares it (a library may share the package: split
    packages are legal) or several do (no single target to pick).
    """
    owners = index.owners.get(package, {}).get(name, ())
    return owners[0] if len(owners) == 1 else None


def _resolve_absolute(path: str, index: ScalaPackageIndex) -> ScalaImportTarget | None:
    segments = path.split(cs.SEPARATOR_DOT)
    # The longest declared package prefix owns the rest of the path: in
    # `a.b.C.m` with package `a.b` declared, C is its member and m is C's.
    for end in range(len(segments), 0, -1):
        package = cs.SEPARATOR_DOT.join(segments[:end])
        declaring = index.modules.get(package)
        if declaring is None:
            continue
        if end == len(segments):
            return ScalaImportTarget(member_qn=None, declaring=declaring)
        member = segments[end]
        owner = scala_package_owner(package, member, index)
        if owner is None:
            return None
        return ScalaImportTarget(
            member_qn=cs.SEPARATOR_DOT.join([owner, *segments[end:]]),
            declaring={owner: frozenset({member})},
        )
    return None


def resolve_scala_import(
    path: str, enclosing: tuple[str, ...], index: ScalaPackageIndex
) -> ScalaImportTarget | None:
    """The project member or package an import path names, if any."""
    for candidate in scala_import_candidates(path, enclosing):
        if (target := _resolve_absolute(candidate, index)) is not None:
            return target
    return None
