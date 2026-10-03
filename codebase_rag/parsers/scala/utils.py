from __future__ import annotations

from collections.abc import Container
from itertools import pairwise

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import (
    ScalaBinding,
    ScalaClassBases,
    ScalaImportTarget,
    ScalaPackageBlock,
    ScalaPackageIndex,
    ScalaPackageScan,
)
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


def scala_enclosing_packages_at(node: Node) -> tuple[str, ...]:
    """The packages whose members an import at `node` names relatively,
    innermost first.

    A bodiless clause opens its package for the later siblings in its scope,
    while `package p { ... }` opens `p` inside its own body only. So two
    sibling blocks of one file each resolve against their own package, which
    the file-level clauses alone cannot say.
    """
    path: list[Node] = []
    current: Node | None = node
    while current is not None:
        path.append(current)
        current = current.parent
    path.reverse()
    opened: list[str] = []
    package = ""
    for container, step in pairwise(path):
        for sibling in container.named_children:
            if sibling.id == step.id:
                break
            if (
                sibling.type == cs.TS_SCALA_PACKAGE_CLAUSE
                and sibling.child_by_field_name(cs.FIELD_BODY) is None
                and (name := _dotted_name(sibling.child_by_field_name(cs.FIELD_NAME)))
            ):
                package = _joined(package, name)
                opened.append(package)
        if step.type == cs.TS_SCALA_PACKAGE_CLAUSE and (
            name := _dotted_name(step.child_by_field_name(cs.FIELD_NAME))
        ):
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


def _package_block(clause: Node) -> ScalaPackageBlock | None:
    body = clause.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return None
    return ScalaPackageBlock(
        start=body.start_byte,
        end=body.end_byte,
        enclosing=scala_enclosing_packages_at(body),
    )


def _package_blocks(root: Node) -> tuple[ScalaPackageBlock, ...]:
    blocks: list[ScalaPackageBlock] = []
    stack = [root]
    while stack:
        node = stack.pop()
        # Package clauses sit only at the top level and in package bodies.
        for child in node.named_children:
            if child.type == cs.TS_SCALA_PACKAGE_CLAUSE and (
                block := _package_block(child)
            ):
                blocks.append(block)
                body = child.child_by_field_name(cs.FIELD_BODY)
                if body is not None:
                    stack.append(body)
    return tuple(blocks)


def scala_import_block(import_node: Node) -> ScalaPackageBlock | None:
    """The package block an import is visible in; None for the whole file."""
    current = import_node.parent
    while current is not None:
        if current.type == cs.TS_SCALA_PACKAGE_CLAUSE and (
            block := _package_block(current)
        ):
            return block
        current = current.parent
    return None


def _written_base(node: Node) -> str | None:
    # The base as written, qualifier kept and type arguments dropped: the
    # spelling the class-ingest pass binds (`foo.Service[Int]` -> foo.Service).
    match node.type:
        case cs.TS_SCALA_TYPE_IDENTIFIER | cs.TS_SCALA_STABLE_TYPE_IDENTIFIER:
            return safe_decode_text(node)
        case cs.TS_SCALA_GENERIC_TYPE:
            return _written_base(node.child_by_field_name(cs.FIELD_TYPE) or node)
    return None


def _class_bases(root: Node) -> dict[str, ScalaClassBases]:
    # Keyed the way the definition pass names a class under its module: the
    # enclosing classes, objects, traits and defs; package blocks add nothing.
    found: dict[str, ScalaClassBases] = {}
    stack: list[tuple[Node, tuple[str, ...]]] = [(root, ())]
    while stack:
        node, path = stack.pop()
        for child in node.named_children:
            child_path = path
            if child.type in cs.SCALA_QN_SCOPE_TYPES and (
                name := safe_decode_text(child.child_by_field_name(cs.FIELD_NAME))
            ):
                child_path = (*path, name)
                clause = next(
                    (c for c in child.children if c.type == cs.TS_EXTENDS_CLAUSE),
                    None,
                )
                bases = (
                    tuple(b for c in clause.children if (b := _written_base(c)))
                    if clause is not None
                    else ()
                )
                if bases:
                    found.setdefault(
                        cs.SEPARATOR_DOT.join(child_path),
                        ScalaClassBases(child.start_byte, bases),
                    )
            stack.append((child, child_path))
    return found


def scan_scala_packages(root: Node) -> ScalaPackageScan:
    """The packages a Scala file declares, opens and the names it mentions."""
    found: dict[str, set[str]] = {}
    _collect_packages(root, "", found)
    return ScalaPackageScan(
        packages={package: frozenset(names) for package, names in found.items()},
        enclosing=_enclosing_packages(root),
        mentions=_mentions(root),
        blocks=_package_blocks(root),
        class_bases=_class_bases(root),
    )


def scala_blocks_at(
    blocks: tuple[ScalaPackageBlock, ...], position: int
) -> list[ScalaPackageBlock]:
    """The package blocks holding byte `position`, innermost first."""
    return sorted(
        (block for block in blocks if block.start <= position < block.end),
        key=lambda block: block.end - block.start,
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


def _scoped(
    name: str | None, type_name: str | None, scope: Node | None
) -> ScalaBinding | None:
    if not name or scope is None:
        return None
    return ScalaBinding(name, type_name, scope.start_byte, scope.end_byte)


def _ancestor(node: Node, node_type: str) -> Node | None:
    current = node.parent
    while current is not None and current.type != node_type:
        current = current.parent
    return current


def _binding(node: Node) -> ScalaBinding | None:
    match node.type:
        case cs.TS_SCALA_PARAMETER | cs.TS_SCALA_BINDING:
            # A def's parameters are visible in its whole definition, a
            # lambda's typed ones in the lambda: the owner of the list.
            owner = node.parent.parent if node.parent is not None else None
            return _scoped(
                safe_decode_text(node.child_by_field_name(cs.FIELD_NAME)),
                _type_name(node.child_by_field_name(cs.FIELD_TYPE)),
                owner,
            )
        case cs.TS_SCALA_LAMBDA_EXPRESSION:
            params = node.child_by_field_name(cs.FIELD_PARAMETERS)
            if params is not None and params.type == cs.TS_SCALA_IDENTIFIER:
                return _scoped(safe_decode_text(params), None, node)
        case cs.TS_SCALA_VAL_DEFINITION | cs.TS_SCALA_VAR_DEFINITION:
            pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
            if pattern is not None and pattern.type == cs.TS_SCALA_IDENTIFIER:
                declared = _type_name(node.child_by_field_name(cs.FIELD_TYPE))
                return _scoped(
                    safe_decode_text(pattern),
                    declared
                    or _value_type_name(node.child_by_field_name(cs.FIELD_VALUE)),
                    node.parent,
                )
        case cs.TS_SCALA_TYPED_PATTERN:
            pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
            if pattern is not None and pattern.type == cs.TS_SCALA_IDENTIFIER:
                return _scoped(
                    safe_decode_text(pattern),
                    _type_name(node.child_by_field_name(cs.FIELD_TYPE)),
                    _ancestor(node, cs.TS_SCALA_CASE_CLAUSE),
                )
        case cs.TS_SCALA_CASE_CLAUSE:
            pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
            if pattern is not None and pattern.type == cs.TS_SCALA_IDENTIFIER:
                return _scoped(safe_decode_text(pattern), None, node)
        case cs.TS_SCALA_ENUMERATOR:
            first = node.named_children[0] if node.named_children else None
            if first is not None and first.type == cs.TS_SCALA_IDENTIFIER:
                return _scoped(
                    safe_decode_text(first),
                    None,
                    _ancestor(node, cs.TS_SCALA_FOR_EXPRESSION),
                )
    return None


def scala_bindings(caller_node: Node) -> dict[str, list[ScalaBinding]]:
    """Every name a caller binds, by name, with its type and scope.

    Types come from parameters (`c: Cart`), annotated vals (`val c: Cart =
    ...`), constructions (`val c = new Cart()`, `val i = Item("x")`) and typed
    patterns. Untyped binders (`c => ...`, `for (c <- xs)`) are kept too: they
    still hide an outer `c`, and are never mistaken for an object of that name.
    """
    bindings: dict[str, list[ScalaBinding]] = {}
    stack = [caller_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if (binding := _binding(node)) is not None:
            bindings.setdefault(binding.name, []).append(binding)
    return bindings


def scala_binding_at(
    bindings: dict[str, list[ScalaBinding]], name: str, position: int
) -> ScalaBinding | None:
    """The binding of `name` in effect at byte `position`, if any.

    The innermost scope holding the position wins. Two bindings of the name
    in that one scope with different types leave it untyped: a guess would
    let the wrong class's member answer.
    """
    visible = [
        binding
        for binding in bindings.get(name, ())
        if binding.scope_start <= position < binding.scope_end
    ]
    if not visible:
        return None
    width = min(binding.scope_end - binding.scope_start for binding in visible)
    innermost = [
        binding
        for binding in visible
        if binding.scope_end - binding.scope_start == width
    ]
    types = {binding.type_name for binding in innermost}
    return innermost[0]._replace(type_name=types.pop() if len(types) == 1 else None)


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


def bind_scala_import(
    mapping: dict[str, str],
    local_name: str,
    path: str,
    target: ScalaImportTarget | None,
) -> None:
    """Bind one written import into an import map, resolved when it can be."""
    is_wildcard = local_name.startswith(cs.SCALA_WILDCARD_PREFIX)
    if target is None or (target.member_qn is None and not is_wildcard):
        # Not the project's, or `import a.b` binding the package itself,
        # which names no single qn.
        mapping[local_name] = path
    elif target.member_qn is None:
        # A package's members are spread over the files declaring it; each
        # one's own wildcard lets the resolver find them.
        for module_qn in sorted(target.declaring):
            mapping[f"{cs.SCALA_WILDCARD_PREFIX}{module_qn}"] = module_qn
    elif is_wildcard:
        mapping[f"{cs.SCALA_WILDCARD_PREFIX}{target.member_qn}"] = target.member_qn
    else:
        mapping[local_name] = target.member_qn
