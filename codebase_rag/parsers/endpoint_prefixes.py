"""Router and blueprint mount-prefix resolution (issue #877).

Route decorators only carry the path suffix; the mount prefix lives on the
router object (``APIRouter(prefix='/users')``, ``Blueprint(...,
url_prefix='/payments')``) and on the mount call (``include_router(...,
prefix='/api/v2')``, ``register_blueprint(..., url_prefix=...)``), often in a
different module. This walker collects router definitions, mounts and imports
from every Python module AST so endpoint emission can compose each handler's
full template. A non-literal prefix anywhere in the chain yields an explicit
unknown-lead marker instead of a silently wrong template.

Ceilings (each falls back to today's bare template, never a wrong one):
aliased factory imports (``APIRouter as AR``), ``app.mount``,
``add_url_rule``, routers reassigned through intermediate variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .. import constants as cs

if TYPE_CHECKING:
    from tree_sitter import Node

# The template segment marking an unresolvable mount prefix; matching treats
# the rest of the template as a path tail.
UNKNOWN_LEAD_SEGMENT = "**"
UNKNOWN_LEAD = f"/{UNKNOWN_LEAD_SEGMENT}"

# Incremental runs re-parse only changed files; the unchanged modules that
# hold the mounts come back from the graph so cross-module prefixes keep
# resolving.
CYPHER_PROJECT_PY_MODULES = (
    "MATCH (m:Module) WHERE m.qualified_name STARTS WITH $project_prefix "
    "AND m.path ENDS WITH '.py' "
    "RETURN m.qualified_name AS qualified_name, m.path AS path"
)


class _Kind(Enum):
    APP = "app"
    ROUTER = "router"
    BLUEPRINT = "blueprint"


_FACTORY_KINDS = {
    "FastAPI": _Kind.APP,
    "Flask": _Kind.APP,
    "APIRouter": _Kind.ROUTER,
    "Blueprint": _Kind.BLUEPRINT,
}
_PREFIX_KEYWORDS = ("prefix", "url_prefix")
_MOUNT_METHODS = frozenset({"include_router", "register_blueprint"})

_ImportBinding = tuple[str, str]  # (module_qn, attr); attr '' = the module


@dataclass(frozen=True)
class _Router:
    kind: _Kind
    prefix: str | None  # own literal prefix; None = non-literal (unknown)


@dataclass(frozen=True)
class _Mount:
    parent: tuple[str, str] | None  # (module_qn, var); None = unresolvable
    prefix: str | None  # literal mount prefix; None = non-literal
    given: bool  # whether the mount call passed a prefix at all


@dataclass(frozen=True)
class _RawMount:
    module_qn: str
    parent_text: str
    child_text: str
    prefix: str | None
    given: bool


def _decode(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode(cs.ENCODING_UTF8)


def _string_value(node: Node | None) -> str | None:
    # A plain literal only: an f-string or any other expression is unknown,
    # never a guessed prefix.
    if node is None or node.type != cs.TS_PY_STRING:
        return None
    if any(c.type == cs.TS_PY_INTERPOLATION for c in node.named_children):
        return None
    return "".join(
        _decode(c) or ""
        for c in node.named_children
        if c.type == cs.TS_PY_STRING_CONTENT
    )


def _keyword_argument(call: Node) -> tuple[Node | None, bool]:
    # (value node, present) for the first prefix-style keyword argument.
    args = call.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
    if args is None:
        return None, False
    for child in args.named_children:
        if child.type != cs.TS_PY_KEYWORD_ARGUMENT:
            continue
        name = _decode(child.child_by_field_name(cs.TS_FIELD_NAME))
        if name in _PREFIX_KEYWORDS:
            return child.child_by_field_name(cs.FIELD_VALUE), True
    return None, False


def _first_positional(call: Node) -> Node | None:
    args = call.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
    if args is None:
        return None
    for child in args.named_children:
        if child.type not in (cs.TS_PY_KEYWORD_ARGUMENT, cs.TS_COMMENT):
            return child
    return None


def _import_targets(module_qn: str, node: Node) -> dict[str, str]:
    # local name -> dotted target. Relative imports resolve against the
    # importing module's own qn, so they come back fully qualified; absolute
    # ones resolve later by suffix match.
    out: dict[str, str] = {}
    if node.type == cs.TS_PY_IMPORT_STATEMENT:
        for child in node.named_children:
            if child.type == cs.TS_PY_DOTTED_NAME:
                dotted = _decode(child)
                if dotted:
                    # `import a.b` binds only the head name `a`.
                    head = dotted.split(cs.SEPARATOR_DOT)[0]
                    out[head] = head
            elif child.type == cs.TS_ALIASED_IMPORT:
                dotted = _decode(child.child_by_field_name(cs.TS_FIELD_NAME))
                alias = _decode(child.child_by_field_name(cs.FIELD_ALIAS))
                if dotted and alias:
                    out[alias] = dotted
        return out
    if node.type != cs.TS_PY_IMPORT_FROM_STATEMENT:
        return out
    base_node = node.child_by_field_name(cs.FIELD_MODULE_NAME)
    base = _decode(base_node)
    if base is None:
        return out
    if base.startswith(cs.SEPARATOR_DOT):
        level = len(base) - len(base.lstrip(cs.SEPARATOR_DOT))
        rest = base[level:]
        parts = module_qn.split(cs.SEPARATOR_DOT)[:-level]
        base = cs.SEPARATOR_DOT.join(parts + ([rest] if rest else []))
    for child in node.named_children:
        if child is base_node:
            continue
        if child.type == cs.TS_PY_DOTTED_NAME:
            name = _decode(child)
            if name and cs.SEPARATOR_DOT not in name:
                out[name] = f"{base}{cs.SEPARATOR_DOT}{name}"
        elif child.type == cs.TS_ALIASED_IMPORT:
            name = _decode(child.child_by_field_name(cs.TS_FIELD_NAME))
            alias = _decode(child.child_by_field_name(cs.FIELD_ALIAS))
            if name and alias:
                out[alias] = f"{base}{cs.SEPARATOR_DOT}{name}"
    return out


def _resolve_module(modules: set[str], importer_qn: str, dotted: str) -> str | None:
    if dotted in modules:
        return dotted
    project = importer_qn.split(cs.SEPARATOR_DOT, maxsplit=1)[0]
    segments = dotted.split(cs.SEPARATOR_DOT)
    candidates: list[str] = [
        qn
        for qn in modules
        if qn.split(cs.SEPARATOR_DOT)[0] == project
        and qn.split(cs.SEPARATOR_DOT)[-len(segments) :] == segments
    ]
    return sorted(candidates, key=len)[0] if candidates else None


class RouterRegistry:
    """Resolves a decorator receiver to its full mount prefixes."""

    def __init__(
        self,
        routers: dict[tuple[str, str], _Router],
        mounts: dict[tuple[str, str], list[_Mount]],
        imports: dict[str, dict[str, _ImportBinding | None]],
    ) -> None:
        self._routers = routers
        self._mounts = mounts
        self._imports = imports

    def resolve_var(self, module_qn: str, text: str) -> tuple[str, str] | None:
        """The router key a variable reference addresses, if any."""
        if cs.SEPARATOR_DOT not in text:
            if (module_qn, text) in self._routers:
                return (module_qn, text)
            imported = self._imports.get(module_qn, {}).get(text)
            if imported is not None:
                imported_module, attr = imported
                if attr and (imported_module, attr) in self._routers:
                    return (imported_module, attr)
            return None
        head, _, rest = text.partition(cs.SEPARATOR_DOT)
        if cs.SEPARATOR_DOT in rest:
            return None
        imported = self._imports.get(module_qn, {}).get(head)
        if imported is None or imported[1]:
            return None
        key = (imported[0], rest)
        return key if key in self._routers else None

    def mount_prefixes(self, module_qn: str, receiver: str) -> list[str] | None:
        """Full mount prefixes for a decorator receiver, or None if unknown.

        Each entry is ready to prepend to the decorator path; an
        unresolvable prefix in the chain yields an ``/**`` lead.
        """
        key = self.resolve_var(module_qn, receiver)
        if key is None:
            return None
        rendered: list[str] = []
        for text, unknown in self._full(key, frozenset()):
            prefix = (UNKNOWN_LEAD if unknown else "") + text
            if prefix not in rendered:
                rendered.append(prefix)
        return rendered

    def _full(
        self, key: tuple[str, str], visiting: frozenset[tuple[str, str]]
    ) -> list[tuple[str, bool]]:
        router = self._routers[key]
        if router.kind is _Kind.APP:
            return [("", False)]
        mounts = self._mounts.get(key, [])
        if not mounts:
            # Unmounted (or mounted by an un-indexed app): its own prefix is
            # still the best-known template lead.
            own = router.prefix
            return [(own, False)] if own is not None else [("", True)]
        results: list[tuple[str, bool]] = []
        for mount in mounts:
            seg_text, seg_unknown = self._segment(router, mount)
            if (
                mount.parent is None
                or mount.parent in visiting
                or mount.parent not in self._routers
            ):
                bases = [("", True)]
            else:
                bases = self._full(mount.parent, visiting | {key})
            for base_text, base_unknown in bases:
                if seg_unknown:
                    # The unknown part swallows everything above it; the
                    # known tail below it survives behind the marker.
                    results.append((seg_text, True))
                else:
                    results.append((base_text + seg_text, base_unknown))
        return results

    @staticmethod
    def _segment(router: _Router, mount: _Mount) -> tuple[str, bool]:
        # (known tail text, unknown lead). Flask semantics: a register-time
        # url_prefix replaces the blueprint's own; FastAPI include prefixes
        # concatenate, so a dynamic include keeps the router's own prefix as
        # the known tail.
        if router.kind is _Kind.BLUEPRINT:
            chosen = mount.prefix if mount.given else router.prefix
            return ("", True) if chosen is None else (chosen, False)
        include_prefix = (mount.prefix if mount.given else "") or ""
        include_unknown = mount.given and mount.prefix is None
        if router.prefix is None:
            return ("", True)
        if include_unknown:
            return (router.prefix, True)
        return (include_prefix + router.prefix, False)


def build_router_registry(module_asts: dict[str, Node]) -> RouterRegistry:
    """Collect router definitions, mounts and imports from module ASTs."""
    routers: dict[tuple[str, str], _Router] = {}
    raw_mounts: list[_RawMount] = []
    raw_imports: dict[str, dict[str, str]] = {}

    for module_qn, root in module_asts.items():
        module_imports: dict[str, str] = {}
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in (
                cs.TS_PY_IMPORT_STATEMENT,
                cs.TS_PY_IMPORT_FROM_STATEMENT,
            ):
                module_imports.update(_import_targets(module_qn, node))
            elif node.type == cs.TS_PY_ASSIGNMENT:
                left = node.child_by_field_name(cs.FIELD_LEFT)
                right = node.child_by_field_name(cs.FIELD_RIGHT)
                if (
                    left is not None
                    and left.type == cs.TS_PY_IDENTIFIER
                    and right is not None
                    and right.type == cs.TS_PY_CALL
                ):
                    callee = _decode(right.child_by_field_name(cs.TS_FIELD_FUNCTION))
                    kind = _FACTORY_KINDS.get(
                        (callee or "").split(cs.SEPARATOR_DOT)[-1]
                    )
                    if kind is not None:
                        value, given = _keyword_argument(right)
                        routers[(module_qn, _decode(left) or "")] = _Router(
                            kind, _string_value(value) if given else ""
                        )
            elif node.type == cs.TS_PY_CALL:
                fn = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
                if fn is not None and fn.type == cs.TS_PY_ATTRIBUTE:
                    method = _decode(fn.child_by_field_name(cs.TS_ATTRIBUTE))
                    parent_text = _decode(fn.child_by_field_name(cs.FIELD_OBJECT))
                    child_text = _decode(_first_positional(node))
                    if (
                        method in _MOUNT_METHODS
                        and parent_text is not None
                        and child_text is not None
                    ):
                        value, given = _keyword_argument(node)
                        raw_mounts.append(
                            _RawMount(
                                module_qn,
                                parent_text,
                                child_text,
                                _string_value(value) if given else None,
                                given,
                            )
                        )
            stack.extend(node.named_children)
        raw_imports[module_qn] = module_imports

    modules = set(module_asts)
    imports: dict[str, dict[str, _ImportBinding | None]] = {}
    for module_qn, module_imports in raw_imports.items():
        resolved: dict[str, _ImportBinding | None] = {}
        for local, dotted in module_imports.items():
            target = _resolve_module(modules, module_qn, dotted)
            if target is not None:
                resolved[local] = (target, "")
                continue
            head, _, attr = dotted.rpartition(cs.SEPARATOR_DOT)
            target = _resolve_module(modules, module_qn, head) if head else None
            resolved[local] = (target, attr) if target is not None else None
        imports[module_qn] = resolved

    registry = RouterRegistry(routers, {}, imports)
    mounts: dict[tuple[str, str], list[_Mount]] = registry._mounts
    for raw in raw_mounts:
        child_key = registry.resolve_var(raw.module_qn, raw.child_text)
        if child_key is None:
            continue
        parent_key = registry.resolve_var(raw.module_qn, raw.parent_text)
        mounts.setdefault(child_key, []).append(
            _Mount(parent_key, raw.prefix, raw.given)
        )
    return registry
