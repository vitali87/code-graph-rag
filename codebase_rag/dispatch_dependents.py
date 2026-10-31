"""Which callers a changed Python class hierarchy rebinds (issue #3338).

A fresh index fans a call out to every dispatch target it can reach: a
``self.M()`` in a base class reaches each subclass override, and a call on
a Protocol-typed receiver reaches every structural conformer. An
incremental sync that adds the subclass or the conformer in a new file
never re-parsed the unchanged callers, because no edge points at the new
file and the callers record no unresolved reference. The updater therefore
asks this module, from the changed files' new bytes and the old graph's
hierarchy and method sets, which CALLS targets the dispatch set gained or
lost, and re-parses the files holding edges into them.

Only Python: both fan-outs are Python-only paths in the call resolver
(``self_dispatch_targets`` and ``protocol_dispatch_targets``), gated on
the caller's language. The computation mirrors the resolver's tests --
a Protocol is a class whose direct base is named ``Protocol``, a
conformer defines every method the Protocol declares (or is the unique
``Xxx`` implementer of ``XxxProtocol``) -- without reimplementing
resolution: it names targets, and the re-parse re-derives the edges.

The dependency set is the UNION of the old and the new dispatch state:
a target a class used to reach needs its callers re-parsed just as much
as a target it newly reaches, or the old edge survives as an orphan.
"""

from __future__ import annotations

import ast
from collections.abc import Collection
from dataclasses import dataclass, field

from . import constants as cs
from .utils import qn_markers

_PROTOCOL_LEAF = cs.PY_PROTOCOL


@dataclass(frozen=True)
class ImportBinding:
    """One import statement, as a base name in the file can spell it."""

    level: int
    module: str
    name: str
    asname: str | None
    is_from: bool


@dataclass(frozen=True)
class ClassScan:
    """A class definition as written: its methods and its bases."""

    path: tuple[str, ...]
    methods: frozenset[str] = frozenset()
    bases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScannedFile:
    """The classes and imports a changed file's new bytes define."""

    key: str
    module_qn: str
    is_package_init: bool
    classes: dict[tuple[str, ...], ClassScan] = field(default_factory=dict)
    imports: tuple[ImportBinding, ...] = ()


@dataclass(frozen=True)
class ClassChange:
    """A class whose dispatch-relevant shape moved between two syncs."""

    qn: str
    old_methods: frozenset[str] = frozenset()
    new_methods: frozenset[str] = frozenset()
    old_bases: tuple[str, ...] = ()
    new_bases: tuple[str, ...] = ()
    present: bool = True


def _dotted_name(node: ast.expr) -> str | None:
    # A base as written: `Transformer`, `base.Transformer`, `Generic[T]`
    # (the subscript's value), never a call or an expression.
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return None if parent is None else f"{parent}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return _dotted_name(node.value)
    return None


def _collect_classes(
    statements: list[ast.stmt],
    prefix: tuple[str, ...],
    classes: dict[tuple[str, ...], ClassScan],
    imports: list[ImportBinding],
) -> None:
    # Classes nested in functions are not walked: their qualified names
    # are function-scoped, and no dispatch target is keyed on them. A
    # class under `if`/`try`/`with` is still a module-level definition.
    for stmt in statements:
        if isinstance(stmt, ast.ClassDef):
            path = (*prefix, stmt.name)
            methods = frozenset(
                child.name
                for child in stmt.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            bases = tuple(
                name for base in stmt.bases if (name := _dotted_name(base)) is not None
            )
            classes[path] = ClassScan(path, methods, bases)
            _collect_classes(stmt.body, path, classes, imports)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                imports.append(ImportBinding(0, alias.name, "", alias.asname, False))
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                imports.append(
                    ImportBinding(
                        stmt.level, stmt.module or "", alias.name, alias.asname, True
                    )
                )
        elif isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            _collect_classes(stmt.body, prefix, classes, imports)
            _collect_classes(stmt.orelse, prefix, classes, imports)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            _collect_classes(stmt.body, prefix, classes, imports)
        elif isinstance(stmt, (ast.Try, ast.TryStar)):
            _collect_classes(stmt.body, prefix, classes, imports)
            for handler in stmt.handlers:
                _collect_classes(handler.body, prefix, classes, imports)
            _collect_classes(stmt.orelse, prefix, classes, imports)
            _collect_classes(stmt.finalbody, prefix, classes, imports)


def scan_python_classes(
    data: bytes,
) -> tuple[dict[tuple[str, ...], ClassScan], tuple[ImportBinding, ...]]:
    """The classes and imports `data` defines, or nothing when it does not
    parse: an unparsable file is the definition pass's problem, and a
    failed scan must never invent dispatch dependents for it."""
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError, RecursionError):
        return {}, ()
    classes: dict[tuple[str, ...], ClassScan] = {}
    imports: list[ImportBinding] = []
    _collect_classes(tree.body, (), classes, imports)
    return classes, tuple(imports)


def _package_qn(module_qn: str, is_package_init: bool, project_name: str) -> str:
    if is_package_init:
        return module_qn
    head, sep, _tail = module_qn.rpartition(cs.SEPARATOR_DOT)
    return head if sep else project_name


def _resolve_module(
    spelling: str, level: int, module_qn: str, is_package_init: bool, project_name: str
) -> str | None:
    # The module qualified name an import spelling names, or None when a
    # relative import climbs above the project root.
    if level == 0:
        return (
            f"{project_name}{cs.SEPARATOR_DOT}{spelling}" if spelling else project_name
        )
    package = _package_qn(module_qn, is_package_init, project_name)
    for _ in range(level - 1):
        head, sep, _tail = package.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return None
        package = head
    return f"{package}{cs.SEPARATOR_DOT}{spelling}" if spelling else package


def _bound_name(binding: ImportBinding) -> str:
    if binding.asname:
        return binding.asname
    if binding.is_from:
        return binding.name
    return binding.module.split(cs.SEPARATOR_DOT, 1)[0]


def _candidate_base_qns(
    written: str,
    scanned: ScannedFile,
    project_name: str,
    known_classes: set[str],
    by_simple_name: dict[str, set[str]],
) -> set[str]:
    # The project classes a written base can name. A `Protocol` base is
    # not one: Protocol conformance is judged by method sets, not by the
    # subclass path, and the Protocol stub itself is outside the project.
    leaf = written.rsplit(cs.SEPARATOR_DOT, 1)[-1]
    if leaf == _PROTOCOL_LEAF:
        return set()
    first, dot, rest = written.partition(cs.SEPARATOR_DOT)
    if dot:
        # A dotted base: resolve it through the file's imports, or as a
        # project-qualified name. An import-resolved candidate is
        # trusted even when the old graph holds no class of that name
        # yet: the base may be defined by another file this run adds,
        # and a candidate with no node and no edge simply matches
        # nothing. Only the same-name fallback is held to the classes
        # known to exist, so a common leaf cannot fan out to every
        # same-named class in the project.
        for binding in scanned.imports:
            if first != _bound_name(binding) or binding.name == "*":
                continue
            if binding.is_from:
                resolved = _resolve_module(
                    binding.module,
                    binding.level,
                    scanned.module_qn,
                    scanned.is_package_init,
                    project_name,
                )
                if resolved is None:
                    continue
                # `resolved` is already project-qualified.
                return {
                    f"{resolved}{cs.SEPARATOR_DOT}{binding.name}"
                    f"{cs.SEPARATOR_DOT}{rest}"
                }
            if binding.asname:
                return {
                    f"{project_name}{cs.SEPARATOR_DOT}{binding.module}"
                    f"{cs.SEPARATOR_DOT}{rest}"
                }
            # `import a.b`: the bound name is the top module, and the
            # written name already spells the path from it.
            return {f"{project_name}{cs.SEPARATOR_DOT}{written}"}
        qualified = f"{project_name}{cs.SEPARATOR_DOT}{written}"
        return {qualified} | (set(by_simple_name.get(leaf, ())) & known_classes)
    # A bare name: a class in the same module wins over an import, which
    # wins over the only same-named class in the project; several
    # same-named classes with no import evidence all count, which can only
    # over-queue a re-parse, never miss a dispatch target. The
    # same-module candidate is held to the classes known to exist: the
    # module is the file itself, so a base it defines is in the old
    # graph or in this run's scan either way. An import-resolved
    # candidate is trusted as written instead: the base may live in an
    # unchanged file with no hierarchy row yet (a first subclass), and
    # a candidate with no node matches nothing.
    same_module = {
        candidate
        for path in scanned.classes
        for candidate in [
            f"{scanned.module_qn}{cs.SEPARATOR_DOT}"
            f"{cs.SEPARATOR_DOT.join(path[:-1])}{cs.SEPARATOR_DOT}{written}"
            if len(path) > 1
            else f"{scanned.module_qn}{cs.SEPARATOR_DOT}{written}"
        ]
    } & known_classes
    if same_module:
        return same_module
    for binding in scanned.imports:
        if not binding.is_from or _bound_name(binding) != written:
            continue
        resolved = _resolve_module(
            binding.module,
            binding.level,
            scanned.module_qn,
            scanned.is_package_init,
            project_name,
        )
        if resolved is None:
            continue
        return {f"{resolved}{cs.SEPARATOR_DOT}{binding.name}"}
    return set(by_simple_name.get(written, ())) & known_classes


def _simple_name(qualified_name: str) -> str:
    return qn_markers.strip_dup_marker(qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1])


def _spelling_key(qualified_name: str, project_name: str) -> str:
    # A base as the persisted hierarchy spells it, without the project
    # prefix: the graph spells an external base `foo.Bar`, while a scan
    # spells its project-prefixed guess `proj.foo.Bar`. The two spell
    # the same base, and comparing them raw invents a base change.
    prefix = project_name + cs.SEPARATOR_DOT
    return (
        qualified_name[len(prefix) :]
        if qualified_name.startswith(prefix)
        else qualified_name
    )


def build_class_changes(
    scanned_files: list[ScannedFile],
    old_classes: dict[str, str],
    old_methods: dict[str, set[str]],
    hierarchy: dict[str, list[str]],
    project_name: str,
    changed_keys: Collection[str] = (),
    graph_class_qns: Collection[str] = (),
) -> list[ClassChange]:
    """The classes whose methods or bases moved, old state against new.

    `old_classes` maps each class the graph held for a changed or deleted
    file to that file's key; `old_methods` maps a class qualified name to
    the method names the graph held for it; `hierarchy` is the old
    child-to-bases map. `changed_keys` names the files whose old classes
    may count as removed: a class the graph holds for any other file is
    untouched by this run, whatever the scan says. `graph_class_qns`
    names every class and interface node the graph holds: a resolved
    base counts only when it names a class in the graph, in this run's
    scan, in the hierarchy, or under the spelling the persisted
    hierarchy already uses for it, so an import-resolved guess for an
    external base cannot invent a change for a comment-only edit, while
    a real base in an unchanged file is still detected. A class present
    in neither old nor new state, or identical in both, is not a change:
    most edits are not dispatch changes, and this list is what keeps
    them from re-parsing callers.
    """
    scanned_qns: dict[str, tuple[ScannedFile, ClassScan]] = {}
    for scanned in scanned_files:
        for path, scan in scanned.classes.items():
            qn = f"{scanned.module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(path)}"
            scanned_qns[qn] = (scanned, scan)
    known_classes = (
        set(old_classes)
        | set(scanned_qns)
        | set(hierarchy)
        | {base for bases in hierarchy.values() for base in bases}
    )
    by_simple_name: dict[str, set[str]] = {}
    for qn in known_classes:
        by_simple_name.setdefault(_simple_name(qn), set()).add(qn)
    existing_classes = known_classes | set(graph_class_qns)
    # A base spelling is graph-backed when the graph, this run's scan,
    # or the persisted hierarchy names a class of exactly that
    # spelling. Only then can a spelling difference hide an identity
    # change. A base the hierarchy merely mentions as a value is an
    # unresolved external guess, not a class, so a comment-only edit
    # on an external base still reports no change.
    graph_backed_qns = set(graph_class_qns) | set(scanned_qns) | set(hierarchy)
    changes: list[ClassChange] = []
    for qn, (scanned, scan) in scanned_qns.items():
        old_base_keys = {
            _spelling_key(base, project_name) for base in hierarchy.get(qn, ())
        }
        new_bases = tuple(
            sorted(
                {
                    candidate
                    for written in scan.bases
                    for candidate in _candidate_base_qns(
                        written, scanned, project_name, known_classes, by_simple_name
                    )
                    if candidate in existing_classes
                    or _spelling_key(candidate, project_name) in old_base_keys
                }
            )
        )
        old_method_set = frozenset(old_methods.get(qn, ()))
        new_method_set = scan.methods
        old_base_set = tuple(sorted(hierarchy.get(qn, ())))
        # Only bases whose spelling actually changed can hide an
        # identity change: an unchanged graph-backed base shared by
        # both sides must not defeat the reset for the whole tuple
        # (mixed local + external inheritance, comment-only edit).
        # The length check keeps a count change from collapsing into
        # a spelling match when the key sets deduplicate.
        differing_bases = set(new_bases) ^ set(old_base_set)
        if (
            len(new_bases) == len(old_base_set)
            and not any(base in graph_backed_qns for base in differing_bases)
            and {_spelling_key(base, project_name) for base in new_bases}
            == {_spelling_key(base, project_name) for base in old_base_set}
        ):
            # Same bases, different spellings, neither side a class the
            # graph, this run's scan, or the hierarchy names: keep the
            # persisted spelling, which is the one the graph will hold
            # after the re-parse, so the merged state matches it.
            new_bases = old_base_set
        if qn not in old_classes:
            if new_method_set or new_bases:
                changes.append(
                    ClassChange(qn, frozenset(), new_method_set, (), new_bases)
                )
            continue
        if new_method_set != old_method_set or new_bases != old_base_set:
            changes.append(
                ClassChange(qn, old_method_set, new_method_set, old_base_set, new_bases)
            )
    seen = set(scanned_qns)
    changed = set(changed_keys)
    for qn, path in old_classes.items():
        if qn in seen or path not in changed:
            continue
        # A class the new bytes no longer define, in a file this run
        # changed or deleted: its old dispatch targets are the ones
        # whose callers must be re-parsed, so the removal prunes
        # rather than orphans their edges.
        changes.append(
            ClassChange(
                qn,
                frozenset(old_methods.get(qn, ())),
                frozenset(),
                tuple(sorted(hierarchy.get(qn, ()))),
                (),
                present=False,
            )
        )
    return changes


def _owns(
    qualified_name: str, project_name: str, longer_project_prefixes: tuple[str, ...]
) -> bool:
    prefix = project_name + cs.SEPARATOR_DOT
    if qualified_name != project_name and not qualified_name.startswith(prefix):
        return False
    return not any(
        qualified_name == longer or qualified_name.startswith(longer + cs.SEPARATOR_DOT)
        for longer in longer_project_prefixes
    )


def _neighbourhood(
    root: str,
    hierarchy: dict[str, list[str]],
    project_name: str,
    longer_project_prefixes: tuple[str, ...],
) -> set[str]:
    # Every class in the root's hierarchy component, ancestors and
    # descendants both: a self-dispatch call in any of them can target
    # the root's method or an override of it, and the CALLS edge lands
    # on whichever class in the chain defines the method the call
    # resolved to. Third-party bases are never traversed through: a
    # project class extending `dict` does not make every `dict`
    # subclass a dispatch dependent.
    adjacency: dict[str, set[str]] = {}
    for child, bases in hierarchy.items():
        if not _owns(child, project_name, longer_project_prefixes):
            continue
        for base in bases:
            if not _owns(base, project_name, longer_project_prefixes):
                continue
            adjacency.setdefault(child, set()).add(base)
            adjacency.setdefault(base, set()).add(child)
    found = {root}
    stack = [root]
    while stack:
        current = stack.pop()
        for neighbour in adjacency.get(current, ()):
            if neighbour not in found:
                found.add(neighbour)
                stack.append(neighbour)
    return found


def base_change_component_qns(
    changes: list[ClassChange],
    hierarchy: dict[str, list[str]],
    project_name: str,
    longer_project_prefixes: tuple[str, ...] = (),
) -> set[str]:
    """The classes in the hierarchy components of classes that changed
    bases, old and new state both: a caller of the updater, whose
    method sets are read filtered to the changed and Protocol names,
    needs these classes' whole method sets to learn a descendant
    override's name, which neither filter can name."""
    merged_hierarchy: dict[str, list[str]] = {
        child: list(bases) for child, bases in hierarchy.items()
    }
    for change in changes:
        if change.present:
            merged_hierarchy[change.qn] = list(change.new_bases)
        else:
            merged_hierarchy.pop(change.qn, None)
    found: set[str] = set()
    for change in changes:
        if set(change.old_bases) == set(change.new_bases):
            continue
        found.update(
            _neighbourhood(change.qn, hierarchy, project_name, longer_project_prefixes)
        )
        found.update(
            _neighbourhood(
                change.qn,
                merged_hierarchy,
                project_name,
                longer_project_prefixes,
            )
        )
    return found


def _effective_methods(
    class_qn: str,
    hierarchy: dict[str, list[str]],
    methods: dict[str, set[str]],
) -> set[str]:
    # A class's own methods plus every method its bases define, the set
    # the resolver's `_try_resolve_method` walks when it asks whether a
    # class implements a Protocol method.
    found: set[str] = set()
    stack = [class_qn]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        found.update(methods.get(current, ()))
        stack.extend(hierarchy.get(current, ()))
    return found


def _protocol_method_sets(
    protocols: dict[str, set[str]],
    hierarchy: dict[str, list[str]],
    override_methods: dict[str, set[str]] | None = None,
) -> dict[str, set[str]]:
    # Each Protocol's declared methods, its own plus those of the
    # Protocol bases it extends, as `_protocol_method_names` reads them.
    # `override_methods` carries the complete new method sets of the
    # classes this run changed; without it, the Protocol's own set as
    # the graph held it stands, because the class-methods read is
    # filtered to the method names under consideration and cannot
    # supply a Protocol's whole declaration.
    result: dict[str, set[str]] = {}
    for protocol_qn, own in protocols.items():
        own_methods = (
            override_methods.get(protocol_qn, own)
            if override_methods is not None
            else own
        )
        names = set(own_methods)
        stack = list(hierarchy.get(protocol_qn, ()))
        seen = {protocol_qn}
        while stack:
            current = stack.pop()
            if current in seen or current not in protocols:
                continue
            seen.add(current)
            base_own = (
                override_methods.get(current, protocols[current])
                if override_methods is not None
                else protocols[current]
            )
            names.update(base_own)
            stack.extend(hierarchy.get(current, ()))
        if names:
            result[protocol_qn] = names
    return result


def _method_owners(
    class_qn: str,
    method: str,
    hierarchy: dict[str, list[str]],
    methods: dict[str, set[str]],
) -> set[str]:
    # The classes on the inheritance chain that define `method` themselves:
    # the CALLS edge for an inherited method names the defining class,
    # the way `_try_resolve_method` walks the chain, never the conformer
    # that merely inherits it. Every definer counts, not just the first
    # the MRO would pick: a prefix that matches nothing only over-queues
    # a re-parse, while a missed owner loses the caller outright.
    found: set[str] = set()
    stack = [class_qn]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if method in methods.get(current, ()):
            found.add(current)
        stack.extend(hierarchy.get(current, ()))
    return found


def _descendant_methods(
    root: str,
    hierarchy: dict[str, list[str]],
    methods: dict[str, set[str]],
) -> set[str]:
    # The methods the strict descendants of `root` define: when `root`
    # changes bases, a descendant override can leave or join the
    # self-dispatch targets of a call in an ancestor even though the
    # changed class itself defines no method of that name.
    children: dict[str, set[str]] = {}
    for child, bases in hierarchy.items():
        for base in bases:
            children.setdefault(base, set()).add(child)
    found: set[str] = set()
    stack = [root]
    seen = {root}
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child in seen:
                continue
            seen.add(child)
            found.update(methods.get(child, ()))
            stack.append(child)
    return found


def _named_implementer(
    protocol_qn: str,
    classes: set[str],
    protocols: set[str],
    by_simple: dict[str, list[str]],
) -> str | None:
    # The unique `Xxx` implementer of `XxxProtocol` among `classes`,
    # the mirror of `_protocol_impl_map` for one state of the graph:
    # uniqueness is judged per state, because adding a class can end
    # it, and the old state must keep naming its old implementer.
    simple = _simple_name(protocol_qn)
    if not simple.endswith(_PROTOCOL_LEAF):
        return None
    wanted = simple[: -len(_PROTOCOL_LEAF)]
    candidates = [
        qn for qn in by_simple.get(wanted, []) if qn in classes and qn not in protocols
    ]
    return candidates[0] if len(candidates) == 1 else None


def _conformers(
    required: set[str],
    classes: set[str],
    protocols: set[str],
    hierarchy: dict[str, list[str]],
    methods: dict[str, set[str]],
    named_impl: str | None,
) -> dict[str, set[str]]:
    # {class: methods of `required` it defines} for every class that
    # conforms: it defines them all, or it is the Protocol's unique
    # named implementer and defines at least one of them.
    result: dict[str, set[str]] = {}
    for candidate in classes:
        if candidate in protocols:
            continue
        effective = _effective_methods(candidate, hierarchy, methods)
        held = required & effective
        if held == required or (candidate == named_impl and held):
            result[candidate] = held
    return result


def dispatch_target_prefixes(
    changes: list[ClassChange],
    hierarchy: dict[str, list[str]],
    class_methods: dict[str, set[str]],
    protocols: dict[str, set[str]],
    project_name: str,
    longer_project_prefixes: tuple[str, ...] = (),
) -> tuple[set[str], set[str]]:
    """(`ClassQn.method` target prefixes, base class qns) whose CALLS
    edges the dispatch set gained or lost.

    `hierarchy` and `class_methods` are the OLD graph's; `protocols`
    maps each old Protocol to the methods it declares. The merged state
    overlays each change's new methods and bases, so a target is named
    whether the class newly reaches it or used to reach it.
    """
    if not changes:
        return set(), set()
    merged_hierarchy: dict[str, list[str]] = {
        child: list(bases) for child, bases in hierarchy.items()
    }
    merged_methods: dict[str, set[str]] = {
        qn: set(names) for qn, names in class_methods.items()
    }
    for change in changes:
        if change.present:
            merged_hierarchy[change.qn] = list(change.new_bases)
            merged_methods[change.qn] = set(change.new_methods)
        else:
            merged_hierarchy.pop(change.qn, None)
            merged_methods.pop(change.qn, None)
    all_classes = (
        {
            qn
            for qn in merged_hierarchy
            if _owns(qn, project_name, longer_project_prefixes)
        }
        | {
            qn
            for qn in merged_methods
            if _owns(qn, project_name, longer_project_prefixes)
        }
        | {change.qn for change in changes}
        # The candidate pool for named-implementer uniqueness must
        # cover every class either state holds, including a root base
        # that appears only as a hierarchy value in the old or the
        # merged hierarchy. The per-state sets below filter it; the
        # pool itself must not hide a class from either judgement.
        | {
            base
            for bases in (*hierarchy.values(), *merged_hierarchy.values())
            for base in bases
            if _owns(base, project_name, longer_project_prefixes)
        }
    )
    new_override_methods = {
        change.qn: set(change.new_methods) for change in changes if change.present
    }
    old_protocol_sets = _protocol_method_sets(protocols, hierarchy)
    new_protocol_sets = _protocol_method_sets(
        protocols, merged_hierarchy, new_override_methods
    )
    by_simple: dict[str, list[str]] = {}
    for qn in all_classes:
        by_simple.setdefault(_simple_name(qn), []).append(qn)
    # Each state names every class it holds, including a root base
    # that appears only as a hierarchy value (no methods entry of its
    # own, no bases of its own) and a modified class whose old shape
    # the filtered method read dropped: both still judge
    # named-implementer uniqueness, and omitting them lets a
    # same-named class look unique when it is not. A brand-new class
    # has an empty old shape and stays out of the old state; a removed
    # class stays out of the new state, even when it survives as a
    # base spelling on a surviving class's merged entry.
    old_state_classes = {
        qn
        for qn in set(class_methods)
        | set(hierarchy)
        | {base for bases in hierarchy.values() for base in bases}
        | {change.qn for change in changes if change.old_methods or change.old_bases}
        | {change.qn for change in changes if not change.present}
        if _owns(qn, project_name, longer_project_prefixes)
    }
    new_state_classes = {
        qn
        for qn in set(merged_methods)
        | set(merged_hierarchy)
        | {base for bases in merged_hierarchy.values() for base in bases}
        | {change.qn for change in changes if change.present}
        if _owns(qn, project_name, longer_project_prefixes)
    } - {change.qn for change in changes if not change.present}
    protocol_qns = set(protocols)
    prefixes: set[str] = set()
    base_qns: set[str] = set()
    for change in changes:
        method_names = change.old_methods | change.new_methods
        if set(change.old_bases) != set(change.new_bases):
            method_names |= _descendant_methods(change.qn, hierarchy, class_methods)
            method_names |= _descendant_methods(
                change.qn, merged_hierarchy, merged_methods
            )
        bases = {
            base
            for base in {*change.old_bases, *change.new_bases}
            if _owns(base, project_name, longer_project_prefixes)
        }
        for base in bases:
            base_qns.add(base)
            for neighbourhood in (
                _neighbourhood(base, hierarchy, project_name, longer_project_prefixes),
                _neighbourhood(
                    base, merged_hierarchy, project_name, longer_project_prefixes
                ),
            ):
                for owner in neighbourhood:
                    for method in method_names:
                        prefixes.add(f"{owner}{cs.SEPARATOR_DOT}{method}")
        for protocol_qn in protocols:
            old_required = old_protocol_sets.get(protocol_qn, set())
            new_required = new_protocol_sets.get(protocol_qn, set())
            if not old_required and not new_required:
                continue
            named_old = _named_implementer(
                protocol_qn, old_state_classes, protocol_qns, by_simple
            )
            named_new = _named_implementer(
                protocol_qn, new_state_classes, protocol_qns, by_simple
            )
            old_conformers = _conformers(
                old_required,
                old_state_classes,
                protocol_qns,
                hierarchy,
                class_methods,
                named_old,
            )
            new_conformers = _conformers(
                new_required,
                new_state_classes,
                protocol_qns,
                merged_hierarchy,
                merged_methods,
                named_new,
            )
            old_effective = _effective_methods(change.qn, hierarchy, class_methods)
            new_effective = _effective_methods(
                change.qn, merged_hierarchy, merged_methods
            )
            touched = (
                (old_required and old_required <= old_effective)
                or (new_required and new_required <= new_effective)
                or (
                    (change.qn == named_old and bool(old_required & old_effective))
                    or (change.qn == named_new and bool(new_required & new_effective))
                )
                # A method added to a parent can complete a child's
                # conformance without the child, or the parent, changing
                # its own conformance: the conformer set moving is
                # itself the dispatch change.
                or old_conformers != new_conformers
            )
            if not touched:
                continue
            for required, conformers, state_hierarchy, state_methods in (
                (old_required, old_conformers, hierarchy, class_methods),
                (new_required, new_conformers, merged_hierarchy, merged_methods),
            ):
                for method in required:
                    prefixes.add(f"{protocol_qn}{cs.SEPARATOR_DOT}{method}")
                    for conformer, held in conformers.items():
                        if method in held:
                            owners = _method_owners(
                                conformer, method, state_hierarchy, state_methods
                            )
                            # A held method with no definer on the chain
                            # can only mean the method set read was
                            # partial; keep the conformer name rather
                            # than name nothing at all.
                            for owner in owners or {conformer}:
                                prefixes.add(f"{owner}{cs.SEPARATOR_DOT}{method}")
    return prefixes, base_qns
