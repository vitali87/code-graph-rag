"""Resolution of runtime frames to graph nodes.

A traced frame is identified by ``(absolute path, co_qualname, first line)``;
graph callables are identified by qualified name. The mapping strips runtime
artifacts (``<locals>`` scopes, ``@line`` duplicate markers) and falls back to
line containment when names alone are ambiguous.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .. import constants as cs

if TYPE_CHECKING:
    from .records import FramePoint


@dataclass(frozen=True, slots=True)
class CallableNode:
    """A graph node a traced frame may resolve to."""

    label: str
    qualified_name: str
    path: str
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True, slots=True)
class ResolvedFrame:
    label: str
    qualified_name: str


@dataclass(slots=True)
class ResolutionStats:
    """Counts of unresolvable frames, keyed by reason."""

    unresolved: dict[str, int] = field(default_factory=dict)

    def record(self, reason: cs.TraceUnresolvedReason) -> None:
        self.unresolved[reason.value] = self.unresolved.get(reason.value, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.unresolved.values())


def _natural_qualified_name(qualified_name: str) -> str:
    """Strip the duplicate-definition marker (``qn@line`` or ``qn@line_col``)."""
    base, _, suffix = qualified_name.rpartition(cs.DUP_QN_MARKER)
    if not base:
        return qualified_name
    plain = suffix.replace(cs.DUP_QN_COLUMN_MARKER, "")
    return base if plain.isdigit() else qualified_name


class FrameResolver:
    """Maps runtime frame identities of one project to graph nodes."""

    def __init__(self, repo_root: Path, nodes: list[CallableNode]) -> None:
        self._repo_root = repo_root.resolve()
        self._root_prefix = str(self._repo_root) + os.sep
        self._callables_by_path: dict[str, list[CallableNode]] = {}
        self._modules_by_path: dict[str, CallableNode] = {}
        for node in nodes:
            if node.label == cs.NodeLabel.MODULE:
                self._modules_by_path[node.path] = node
            else:
                self._callables_by_path.setdefault(node.path, []).append(node)

    def resolve(
        self, frame: FramePoint, stats: ResolutionStats
    ) -> ResolvedFrame | None:
        if not frame.path.startswith(self._root_prefix):
            stats.record(cs.TraceUnresolvedReason.OUTSIDE_REPO)
            return None
        rel_path = Path(frame.path).relative_to(self._repo_root).as_posix()

        parts = [
            p
            for p in frame.qualname.split(cs.SEPARATOR_DOT)
            if p != cs.TRACE_QUALNAME_LOCALS
        ]
        if parts == [cs.TRACE_QUALNAME_MODULE]:
            module = self._modules_by_path.get(rel_path)
            if module is None:
                stats.record(cs.TraceUnresolvedReason.UNKNOWN_PATH)
                return None
            return ResolvedFrame(
                label=module.label, qualified_name=module.qualified_name
            )
        if any(p.startswith(cs.TRACE_SYNTHETIC_PREFIX) for p in parts):
            stats.record(cs.TraceUnresolvedReason.SYNTHETIC)
            return None

        candidates = self._callables_by_path.get(rel_path)
        if not candidates:
            stats.record(cs.TraceUnresolvedReason.UNKNOWN_PATH)
            return None

        suffix = cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)
        by_name = [
            n
            for n in candidates
            if _natural_qualified_name(n.qualified_name).endswith(suffix)
        ]
        # Prefer a name match whose span contains the runtime line; among name
        # matches without span data, take the first by qualified name so
        # resolution stays deterministic. Only when no candidate matches by
        # name does the line span alone decide.
        chosen = (
            self._innermost_span_containing_line(by_name, frame.line)
            or (min(by_name, key=lambda n: n.qualified_name) if by_name else None)
            or self._innermost_span_containing_line(candidates, frame.line)
        )
        if chosen is None:
            stats.record(cs.TraceUnresolvedReason.NO_MATCH)
            return None
        return ResolvedFrame(label=chosen.label, qualified_name=chosen.qualified_name)

    @staticmethod
    def _innermost_span_containing_line(
        candidates: list[CallableNode], line: int
    ) -> CallableNode | None:
        return _innermost_span_containing_line(candidates, line)


def _innermost_span_containing_line(
    candidates: list[CallableNode], line: int
) -> CallableNode | None:
    containing = [
        n
        for n in candidates
        if n.start_line is not None
        and n.end_line is not None
        and n.start_line <= line <= n.end_line
    ]
    if not containing:
        return None
    # The innermost span wins: a nested function's span sits inside
    # its parent's, and the runtime line points at the inner def.
    return max(containing, key=lambda n: n.start_line or 0)


def _signatureless(qualified_name: str) -> str:
    """Strip a Java/C#-style trailing parameter signature, e.g. ``bar(String)``."""
    if not qualified_name.endswith(")"):
        return qualified_name
    head, sep, _ = qualified_name.rpartition("(")
    return head if sep else qualified_name


class JsFrameResolver:
    """Maps V8 profile frames of one project to graph nodes.

    V8 reports bare function names, never dotted scope chains, so the
    suffix-matching the Python resolver uses cannot work. Resolution anchors
    on the repo-relative path, narrows to nodes whose final qualified-name
    part equals the frame's name, and picks the innermost span containing
    the declaration line (the converter already made V8's 0-based lines
    1-based, aligning them with node start lines). Anonymous frames resolve
    by span alone; module toplevels map to the file's Module node.
    """

    def __init__(self, repo_root: Path, nodes: list[CallableNode]) -> None:
        self._repo_root = repo_root.resolve()
        self._root_prefix = str(self._repo_root) + os.sep
        self._callables_by_path: dict[str, list[CallableNode]] = {}
        self._modules_by_path: dict[str, CallableNode] = {}
        for node in nodes:
            if node.label == cs.NodeLabel.MODULE:
                self._modules_by_path[node.path] = node
            else:
                self._callables_by_path.setdefault(node.path, []).append(node)

    def resolve(
        self, frame: FramePoint, stats: ResolutionStats
    ) -> ResolvedFrame | None:
        if not frame.path.startswith(self._root_prefix):
            stats.record(cs.TraceUnresolvedReason.OUTSIDE_REPO)
            return None
        rel_path = Path(frame.path).relative_to(self._repo_root).as_posix()

        if frame.qualname == cs.TRACE_QUALNAME_MODULE:
            module = self._modules_by_path.get(rel_path)
            if module is None:
                stats.record(cs.TraceUnresolvedReason.UNKNOWN_PATH)
                return None
            return ResolvedFrame(
                label=module.label, qualified_name=module.qualified_name
            )

        candidates = self._callables_by_path.get(rel_path)
        if not candidates:
            stats.record(cs.TraceUnresolvedReason.UNKNOWN_PATH)
            return None

        by_name: list[CallableNode] = []
        if not frame.qualname.startswith(cs.TRACE_SYNTHETIC_PREFIX):
            by_name = [
                n
                for n in candidates
                if _natural_qualified_name(n.qualified_name).rsplit(
                    cs.SEPARATOR_DOT, 1
                )[-1]
                == frame.qualname
            ]
        chosen = (
            _innermost_span_containing_line(by_name, frame.line)
            or (min(by_name, key=lambda n: n.qualified_name) if by_name else None)
            or _innermost_span_containing_line(candidates, frame.line)
        )
        if chosen is None:
            stats.record(cs.TraceUnresolvedReason.NO_MATCH)
            return None
        return ResolvedFrame(label=chosen.label, qualified_name=chosen.qualified_name)


_DOTNET_ARITY = re.compile(r"`\d+")
_PHP_CLOSURE = re.compile(r"^\{closure:(?P<path>.+):(?P<start>\d+)-\d+\}$")


class PhpFrameResolver:
    """Maps Xdebug frames of one project to graph nodes.

    PHP qualified names are path-derived (the namespace declaration is
    ignored), so resolution is span-first: frames carrying a file position
    (call sites, recovered defining positions, closure names embedding
    their file and lines) resolve to the innermost containing node. Leaf
    callees whose defining file could not be recovered fall back to the
    runtime name's ``Class.method`` tail, which drops the namespace exactly
    as the graph does.
    """

    def __init__(self, repo_root: Path, nodes: list[CallableNode]) -> None:
        self._repo_root = repo_root.resolve()
        self._root_prefix = str(self._repo_root) + os.sep
        self._callables_by_path: dict[str, list[CallableNode]] = {}
        self._modules_by_path: dict[str, CallableNode] = {}
        for node in nodes:
            if node.label == cs.NodeLabel.MODULE:
                self._modules_by_path[node.path] = node
            else:
                self._callables_by_path.setdefault(node.path, []).append(node)

    def resolve(
        self, frame: FramePoint, stats: ResolutionStats
    ) -> ResolvedFrame | None:
        closure = _PHP_CLOSURE.match(frame.qualname)
        if closure:
            return self._resolve_position(
                closure.group("path"), int(closure.group("start")), stats
            )
        if frame.path:
            if frame.qualname == cs.TRACE_XDEBUG_MAIN:
                return self._resolve_module(frame.path, stats)
            return self._resolve_position(frame.path, frame.line, stats)
        return self._resolve_by_name_tail(frame.qualname, stats)

    def _relative(self, path: str) -> str | None:
        if not path.startswith(self._root_prefix):
            return None
        return Path(path).relative_to(self._repo_root).as_posix()

    def _resolve_module(
        self, path: str, stats: ResolutionStats
    ) -> ResolvedFrame | None:
        rel_path = self._relative(path)
        if rel_path is None:
            stats.record(cs.TraceUnresolvedReason.OUTSIDE_REPO)
            return None
        module = self._modules_by_path.get(rel_path)
        if module is None:
            stats.record(cs.TraceUnresolvedReason.UNKNOWN_PATH)
            return None
        return ResolvedFrame(label=module.label, qualified_name=module.qualified_name)

    def _resolve_position(
        self, path: str, line: int, stats: ResolutionStats
    ) -> ResolvedFrame | None:
        rel_path = self._relative(path)
        if rel_path is None:
            stats.record(cs.TraceUnresolvedReason.OUTSIDE_REPO)
            return None
        candidates = self._callables_by_path.get(rel_path)
        if not candidates:
            stats.record(cs.TraceUnresolvedReason.UNKNOWN_PATH)
            return None
        chosen = _innermost_span_containing_line(candidates, line)
        if chosen is None:
            stats.record(cs.TraceUnresolvedReason.NO_MATCH)
            return None
        return ResolvedFrame(label=chosen.label, qualified_name=chosen.qualified_name)

    def _resolve_by_name_tail(
        self, qualname: str, stats: ResolutionStats
    ) -> ResolvedFrame | None:
        for separator in (
            cs.TRACE_PHP_INSTANCE_SEPARATOR,
            cs.TRACE_PHP_STATIC_SEPARATOR,
        ):
            if separator in qualname:
                owner, _, method = qualname.partition(separator)
                owner = owner.rsplit(cs.TRACE_PHP_NAMESPACE_SEPARATOR, 1)[-1]
                tail = f"{cs.SEPARATOR_DOT}{owner}{cs.SEPARATOR_DOT}{method}"
                break
        else:
            plain = qualname.rsplit(cs.TRACE_PHP_NAMESPACE_SEPARATOR, 1)[-1]
            tail = f"{cs.SEPARATOR_DOT}{plain}"
        matches = [
            node
            for nodes in self._callables_by_path.values()
            for node in nodes
            if _natural_qualified_name(node.qualified_name).endswith(tail)
        ]
        if not matches:
            stats.record(cs.TraceUnresolvedReason.NO_MATCH)
            return None
        if len(matches) > 1:
            # The tail dropped the namespace, so several unrelated classes
            # can collide; guessing would attach the edge (and its static
            # classification) to the wrong declaration.
            stats.record(cs.TraceUnresolvedReason.AMBIGUOUS)
            return None
        chosen = matches[0]
        return ResolvedFrame(label=chosen.label, qualified_name=chosen.qualified_name)


_DOTNET_STATE_MACHINE = re.compile(r"^<(\w+)>d__\d+$")
_DOTNET_LAMBDA_BODY = re.compile(r"^<(\w+)>b__\w+$")
_DOTNET_DISPLAY_CLASS = re.compile(r"^<>c(__DisplayClass\w*)?$")
_DOTNET_ACCESSOR = re.compile(r"^(?:get|set)_(\w+)$")


def _demangle_clr_name(name: str) -> str | None:
    """CLR runtime names as dotted source names, or None for pure synthetics.

    ``Ns.Worker+<RunAsync>d__3.MoveNext`` names the compiler's async state
    machine; the source declaration is ``Ns.Worker.RunAsync``. Display
    classes host lambda bodies whose best source anchor is the enclosing
    method. Constructors (``..ctor``) take the class's own name, matching
    how the static tier names them; type initialisers have no source
    declaration at all.
    """
    # dotnet-trace keeps CLR generic arity markers (``Dictionary`2``,
    # ``Method`1``); the graph stores the source spelling, so strip them.
    name = _DOTNET_ARITY.sub("", name)
    if name.endswith(cs.TRACE_DOTNET_CCTOR):
        return None
    constructor = name.endswith(cs.TRACE_DOTNET_CTOR)
    if constructor:
        owner = name[: -len(cs.TRACE_DOTNET_CTOR)]
        method = ""
    else:
        owner, separator, method = name.rpartition(cs.SEPARATOR_DOT)
        if not separator:
            return None
    chain: list[str] = []
    for part in owner.split(cs.TRACE_DOTNET_NESTED_MARKER):
        machine = _DOTNET_STATE_MACHINE.match(part)
        if machine:
            method = machine.group(1)
            continue
        if _DOTNET_DISPLAY_CLASS.match(part):
            continue
        chain.append(part)
    if not chain:
        return None
    if constructor:
        method = chain[-1].rsplit(cs.SEPARATOR_DOT, 1)[-1]
    lambda_body = _DOTNET_LAMBDA_BODY.match(method)
    if lambda_body:
        method = lambda_body.group(1)
    if not method or "<" in method or any("<" in part for part in chain):
        return None
    return cs.SEPARATOR_DOT.join([*chain, method])


class DotnetFrameResolver:
    """Maps sampled .NET frames of one project to graph nodes.

    dotnet-trace frames carry no file paths or lines, but C# qualified names
    embed the declared namespace, so a demangled ``Namespace.Class.Method``
    joins as a qualified-name suffix with the parameter signature stripped
    (runtime argument types are CLR names that never match the graph's
    source-text spellings, so overloads collapse onto one deterministic
    node). Property accessors (``get_X``/``set_X``) fall back to the
    property node when no literal match exists.
    """

    def __init__(self, nodes: list[CallableNode]) -> None:
        self._nodes = [n for n in nodes if n.label != cs.NodeLabel.MODULE]

    def resolve(
        self, frame: FramePoint, stats: ResolutionStats
    ) -> ResolvedFrame | None:
        demangled = _demangle_clr_name(frame.qualname)
        if demangled is None:
            stats.record(cs.TraceUnresolvedReason.SYNTHETIC)
            return None
        chosen = self._match(demangled)
        if chosen is None:
            head, _, leaf = demangled.rpartition(cs.SEPARATOR_DOT)
            accessor = _DOTNET_ACCESSOR.match(leaf)
            if head and accessor:
                chosen = self._match(f"{head}{cs.SEPARATOR_DOT}{accessor.group(1)}")
        if chosen is None:
            stats.record(cs.TraceUnresolvedReason.NO_MATCH)
            return None
        return ResolvedFrame(label=chosen.label, qualified_name=chosen.qualified_name)

    def _match(self, demangled: str) -> CallableNode | None:
        suffix = cs.SEPARATOR_DOT + demangled
        matches = [
            n
            for n in self._nodes
            if _signatureless(_natural_qualified_name(n.qualified_name)).endswith(
                suffix
            )
        ]
        if not matches:
            return None
        return min(matches, key=lambda n: n.qualified_name)


class JvmFrameResolver:
    """Maps JVM runtime frames of one project to graph nodes.

    Runtime paths are package-derived (``com/example/Foo.java``) while node
    paths are repo-relative and may carry a build-tool source root
    (``src/main/java/com/example/Foo.java``), so paths join by suffix. Java
    node qualified names end in a raw-source parameter signature that JVM
    type descriptors cannot reproduce, so names match with the signature
    stripped and overloads are disambiguated by line span. Lambda bodies
    (``lambda$run$0``), Scala anonymous functions, and anonymous-class
    methods (``Client$1.run``) have no name-addressable node; they resolve
    purely by innermost containing span.
    """

    def __init__(self, nodes: list[CallableNode]) -> None:
        self._callables_by_path: dict[str, list[CallableNode]] = {}
        for node in nodes:
            if node.label != cs.NodeLabel.MODULE:
                self._callables_by_path.setdefault(node.path, []).append(node)
        self._paths_by_suffix: dict[str, list[str]] = {}

    def resolve(
        self, frame: FramePoint, stats: ResolutionStats
    ) -> ResolvedFrame | None:
        class_parts, method = self._split_qualname(frame.qualname)
        anonymous = any(part.isdigit() for part in class_parts)
        if method == cs.TRACE_JVM_STATIC_INITIALIZER or (
            # An anonymous class's constructor sits at the `new` expression
            # line; span resolution would fabricate a self-edge on the
            # enclosing method.
            anonymous and method == cs.TRACE_JVM_CONSTRUCTOR
        ):
            stats.record(cs.TraceUnresolvedReason.SYNTHETIC)
            return None

        candidates = self._candidates(frame.path)
        if not candidates:
            stats.record(cs.TraceUnresolvedReason.UNKNOWN_PATH)
            return None

        by_name: list[CallableNode] = []
        if not anonymous and self._name_addressable(method):
            if method == cs.TRACE_JVM_CONSTRUCTOR:
                method = class_parts[-1]
            suffix = cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join([*class_parts, method])
            by_name = [
                n
                for n in candidates
                if _signatureless(_natural_qualified_name(n.qualified_name)).endswith(
                    suffix
                )
            ]
        chosen = (
            _innermost_span_containing_line(by_name, frame.line)
            or (min(by_name, key=lambda n: n.qualified_name) if by_name else None)
            or _innermost_span_containing_line(candidates, frame.line)
        )
        if chosen is None:
            stats.record(cs.TraceUnresolvedReason.NO_MATCH)
            return None
        return ResolvedFrame(label=chosen.label, qualified_name=chosen.qualified_name)

    @staticmethod
    def _split_qualname(qualname: str) -> tuple[list[str], str]:
        """``Outer$Inner.bar`` becomes ``([Outer, Inner], bar)``.

        A trailing ``$`` (Scala object classes) produces an empty part that
        is dropped; anonymous-class ordinals stay so callers can detect them.
        """
        simple, _, method = qualname.rpartition(cs.SEPARATOR_DOT)
        parts = [part for part in simple.split(cs.TRACE_JVM_NESTED_MARKER) if part]
        return parts, method

    @staticmethod
    def _name_addressable(method: str) -> bool:
        """Whether the graph can hold a node under this dotted name.

        Compiler-generated lambda bodies exist only at runtime; the static
        tier names their code through the enclosing method, so only the line
        span can find them. The same goes for anonymous classes, handled by
        the caller since they are a class-chain property.
        """
        return not (
            method.startswith(cs.TRACE_JVM_LAMBDA_PREFIX)
            or method.startswith(cs.TRACE_JVM_ANONFUN_PREFIX)
        )

    def _candidates(self, frame_path: str) -> list[CallableNode]:
        paths = self._paths_by_suffix.get(frame_path)
        if paths is None:
            paths = self._resolve_paths(frame_path)
            self._paths_by_suffix[frame_path] = paths
        return [node for path in paths for node in self._callables_by_path[path]]

    def _resolve_paths(self, frame_path: str) -> list[str]:
        # An exact graph path is unambiguous. Otherwise the frame carries only a
        # source-relative suffix (the JVM records the source file, not its
        # root): match by suffix, but a suffix shared by two source roots cannot
        # be attributed to either -- merging their callables would let line-span
        # selection cross files and misattribute the call. A ceiling yields
        # nothing, never a wrong link (issue #1246).
        if frame_path in self._callables_by_path:
            return [frame_path]
        suffix = cs.SEPARATOR_SLASH + frame_path
        matches = [path for path in self._callables_by_path if path.endswith(suffix)]
        return matches if len(matches) == 1 else []
