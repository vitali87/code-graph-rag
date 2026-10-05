"""Resolution of runtime frames to graph nodes.

A traced frame is identified by ``(absolute path, co_qualname, first line)``;
graph callables are identified by qualified name. The mapping strips runtime
artifacts (``<locals>`` scopes, ``@line`` duplicate markers) and falls back to
line containment when names alone are ambiguous.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from .. import constants as cs
from ..utils import qn_markers
from .records import FramePoint

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


def _repo_root_posix(repo_root: Path) -> str:
    """The resolved repo root as a POSIX prefix ending in a separator."""
    return repo_root.resolve().as_posix().rstrip("/") + "/"


def _repo_relative(root_posix: str, frame_path: str) -> str | None:
    """The repo-relative POSIX path of an in-repo frame, or None if outside.

    Frame paths arrive either POSIX (the pprof/V8 converters emit forward
    slashes, since production build paths are POSIX) or in the host's native
    form (the in-process tracers emit ``co_filename`` with ``os.sep``). Both are
    normalised to POSIX before the containment check so a separator mismatch
    cannot read an in-repo frame as outside the repository on Windows.
    ``posixpath.normpath`` collapses any ``..`` first, so a frame that walks out
    of and back into the repo still resolves and one that walks out stays out.
    The returned path only ever keys ``_callables_by_path`` (a table of known
    in-repo node paths), so a stray relative fragment fails to match rather than
    escaping anywhere. Case-insensitive drives and symlink aliases are left as
    the identity match the graph itself uses, since the indexer keys nodes by
    the same lexical relative paths without resolving either.
    """
    frame_posix = posixpath.normpath(Path(frame_path).as_posix())
    if not frame_posix.startswith(root_posix):
        return None
    return frame_posix[len(root_posix) :]


def _portable_posix(frame_path: str) -> str:
    """A frame path in POSIX form, whichever OS recorded it.

    ``Path.as_posix`` converts only the running host's separator, so a
    Windows traceback read on Linux kept its backslashes and never matched a
    graph path. No real source path contains a backslash on POSIX, so
    treating one as a separator loses nothing.
    """
    return posixpath.normpath(
        frame_path.replace(cs.TRACE_WINDOWS_PATH_SEPARATOR, cs.SEPARATOR_SLASH)
    )


def _dir_prefix(path: str) -> str:
    return path.rstrip(cs.SEPARATOR_SLASH) + cs.SEPARATOR_SLASH


def _recorded_on_windows(posix_path: str) -> bool:
    """Whether a POSIX-form path is rooted at a Windows drive or share."""
    return PureWindowsPath(posix_path).is_absolute()


def _prefix_key(posix_path: str) -> str:
    """How a POSIX-form path compares as a prefix on the OS that recorded it.

    Windows names one file whatever the case of its path, and a drive letter
    is written either way (``sys.path`` can hold ``c:\\``), so a Windows path
    compares without case; a POSIX path keeps it.
    """
    if _recorded_on_windows(posix_path):
        return posix_path.casefold()
    return posix_path


def path_spellings(graph_paths: Iterable[str]) -> dict[str, str]:
    """Each graph path keyed without case, for frames recorded on Windows.

    A Windows frame can spell an in-repo directory unlike the indexed path
    (``SHOP\\cli.py`` for ``shop/cli.py``: the script typed on the command
    line, a tool that normalises case) and still name the same file. Two
    graph paths differing only in case, which a POSIX checkout can hold, give
    no one answer, so that key is left out and only an exact spelling matches.
    """
    found: dict[str, set[str]] = {}
    for path in graph_paths:
        found.setdefault(path.casefold(), set()).add(path)
    return {key: next(iter(paths)) for key, paths in found.items() if len(paths) == 1}


def _below_dir(posix_path: str, dir_prefix: str) -> str | None:
    """``posix_path`` relative to ``dir_prefix``, or None when not under it."""
    if not _prefix_key(posix_path).startswith(_prefix_key(dir_prefix)):
        return None
    # Sliced by separators, not characters: case folding may change a
    # string's length but never its separators.
    depth = dir_prefix.count(cs.SEPARATOR_SLASH)
    return posix_path.split(cs.SEPARATOR_SLASH, depth)[depth]


def is_absolute_on_any_os(frame_path: str) -> bool:
    """Whether a recorded path is absolute on the OS that recorded it.

    The host's own ``Path.is_absolute`` reads ``C:\\app\\x.py`` as relative
    on Linux and ``/app/x.py`` as relative on Windows, and a relative frame is
    joined to the checkout root, which buries the real root inside the path.
    """
    return PurePosixPath(frame_path).is_absolute() or (
        PureWindowsPath(frame_path).is_absolute()
    )


@dataclass(frozen=True, slots=True)
class PathRebase:
    """Moves frames recorded under another checkout root onto this one.

    A traceback or trace names files by the root of the machine that ran the
    code (a CI runner's workspace, a container's ``/app``, a Windows
    profile), while graph paths are relative to the indexed checkout. Each
    rule maps a recorded prefix to a directory of this checkout; the longest
    matching prefix wins. A frame already under the checkout is never
    rebased: a recorded root can be an ancestor of the checkout, and
    re-rooting such a frame would move it to a path that does not exist.
    A frame recorded on Windows also takes the graph's spelling of its
    in-repo path (``spellings``), since there one file answers to any case.
    """

    local_root: str
    rules: tuple[tuple[str, str], ...] = ()
    spellings: Mapping[str, str] = field(
        default_factory=dict, repr=False, compare=False
    )

    @classmethod
    def from_prefix_map(
        cls,
        repo_root: Path,
        prefix_map: Mapping[str, str],
        spellings: Mapping[str, str] | None = None,
    ) -> PathRebase:
        local_root = _repo_root_posix(repo_root)
        rules = [
            (
                _dir_prefix(_portable_posix(recorded)),
                # normpath lets ``..`` walk out of the checkout, where the
                # containment check then reports the frame as outside it.
                _dir_prefix(
                    posixpath.normpath(
                        posixpath.join(local_root, _portable_posix(target))
                    )
                ),
            )
            for recorded, target in prefix_map.items()
            if recorded
        ]
        rules.sort(key=lambda rule: len(rule[0]), reverse=True)
        return cls(local_root=local_root, rules=tuple(rules), spellings=spellings or {})

    @classmethod
    def from_recorded_root(
        cls,
        repo_root: Path,
        recorded_root: str,
        spellings: Mapping[str, str] | None = None,
    ) -> PathRebase:
        """Rebase from the root a trace header says it was recorded under.

        A relative header root names no machine's checkout, so it is not a
        prefix anything can be stripped by.
        """
        if not is_absolute_on_any_os(recorded_root):
            return cls(
                local_root=_repo_root_posix(repo_root), spellings=spellings or {}
            )
        return cls.from_prefix_map(
            repo_root, {recorded_root: cs.PATH_CURRENT_DIR}, spellings
        )

    def matches(self, frame: FramePoint) -> bool:
        """Whether a rule, rather than the checkout root, anchors the frame."""
        path = _portable_posix(frame.path)
        return _below_dir(path, self.local_root) is None and any(
            _below_dir(path, recorded) is not None for recorded, _ in self.rules
        )

    def apply(self, frame: FramePoint) -> FramePoint:
        path = _portable_posix(frame.path)
        windows = _recorded_on_windows(path)
        # A Windows frame already under the checkout still goes through the
        # loop: its in-repo part may be spelled unlike the indexed path.
        if path.startswith(self.local_root) and not windows:
            return frame
        # The checkout root first: a frame under it in another case (Windows)
        # is re-cased onto it rather than moved by an ancestor's rule.
        for recorded, local in ((self.local_root, self.local_root), *self.rules):
            if (rest := _below_dir(path, recorded)) is not None:
                if windows:
                    rest = self._indexed_spelling(local, rest)
                return FramePoint(
                    path=local + rest,
                    qualname=frame.qualname,
                    line=frame.line,
                )
        return frame

    def _indexed_spelling(self, local: str, rest: str) -> str:
        """``rest`` as the graph spells it under ``local``.

        Only the part the recording machine wrote is re-spelled: ``local`` is
        this checkout's own directory, so a mapped target keeps its case.
        """
        base = local.removeprefix(self.local_root)
        indexed = self.spellings.get((base + rest).casefold())
        if indexed is None or not indexed.startswith(base):
            return rest
        return indexed[len(base) :]


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
    # The frame has no node of its own (a lambda body, an anonymous function
    # or class, a closure) and was folded into the node whose span holds it,
    # so a call from that node into this frame happens inside one node
    # (issue #2709). Not part of the frame's identity.
    folded: bool = field(default=False, compare=False)


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
    return qn_markers.strip_dup_marker(qualified_name)


class FrameResolver:
    """Maps runtime frame identities of one project to graph nodes."""

    def __init__(self, repo_root: Path, nodes: list[CallableNode]) -> None:
        self._root_posix = _repo_root_posix(repo_root)
        self._callables_by_path: dict[str, list[CallableNode]] = {}
        self._modules_by_path: dict[str, CallableNode] = {}
        for node in nodes:
            if node.label == cs.NodeLabel.MODULE:
                self._modules_by_path[node.path] = node
            else:
                self._callables_by_path.setdefault(node.path, []).append(node)
        self.path_spellings = path_spellings(node.path for node in nodes)

    def resolve(
        self, frame: FramePoint, stats: ResolutionStats
    ) -> ResolvedFrame | None:
        rel_path = _repo_relative(self._root_posix, frame.path)
        if rel_path is None:
            stats.record(cs.TraceUnresolvedReason.OUTSIDE_REPO)
            return None

        parts = _runtime_name_parts(frame.qualname)
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

        by_name = _named_by(candidates, parts)
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
        return ResolvedFrame(
            label=chosen.label,
            qualified_name=chosen.qualified_name,
            folded=not by_name,
        )

    @staticmethod
    def _innermost_span_containing_line(
        candidates: list[CallableNode], line: int
    ) -> CallableNode | None:
        return _innermost_span_containing_line(candidates, line)

    def infer_recorded_root(self, frames: Iterable[FramePoint]) -> str | None:
        """The root another checkout recorded these frames under, or None.

        Each frame votes for every prefix whose removal leaves a whole graph
        path (never a bare basename: ``app/x.py`` and ``lib/x.py`` both end in
        ``x.py``) AND a node there that the frame's own name picks out. The
        name is the second witness: without it a different project's
        ``utils.py`` would be bound by line span to whatever function of ours
        covers that line. The prefix most frames agree on wins; unrelated
        prefixes tied for the lead are indistinguishable, so none is chosen.
        One frame naming both ``pkg/x.py`` and ``x.py`` ties two NESTED
        prefixes, and the shorter keeps the longer, more specific graph path.

        Installed code never votes: a ``site-packages`` copy of our package is
        not the checkout, however well its names match. And when any frame is
        already under the checkout, the traceback came from here, so every
        other frame is genuinely outside it and nothing is inferred.
        """
        # Votes are keyed as the recording OS compares paths: `C:\Work` and
        # `c:\work` are one Windows root, and counted apart they would tie.
        # The first spelling seen names the root.
        votes: Counter[str] = Counter()
        spelled: dict[str, str] = {}
        for frame in frames:
            if frame.path.startswith(cs.TRACE_SYNTHETIC_PREFIX):
                continue
            if _repo_relative(self._root_posix, frame.path) is not None:
                return None
            for prefix in self._root_votes(frame):
                key = _prefix_key(prefix)
                spelled.setdefault(key, prefix)
                votes[key] += 1
        if not votes:
            return None
        top = max(votes.values())
        leaders = sorted((p for p, n in votes.items() if n == top), key=len)
        if all(prefix.startswith(leaders[0]) for prefix in leaders):
            return spelled[leaders[0]]
        return None

    def _root_votes(self, frame: FramePoint) -> set[str]:
        """The roots whose removal leaves a graph path naming ``frame``."""
        path = _portable_posix(frame.path)
        parts = path.split(cs.SEPARATOR_SLASH)
        if not cs.TRACE_INSTALLED_DIR_NAMES.isdisjoint(parts):
            return set()
        windows = _recorded_on_windows(path)
        return {
            _dir_prefix(cs.SEPARATOR_SLASH.join(parts[:cut]))
            for cut in range(1, len(parts))
            if self._names_frame(
                cs.SEPARATOR_SLASH.join(parts[cut:]), frame.qualname, windows
            )
        }

    def _names_frame(self, rel_path: str, qualname: str, windows: bool) -> bool:
        """Whether a node at ``rel_path`` matches the frame by name alone."""
        if windows:
            rel_path = self.path_spellings.get(rel_path.casefold(), rel_path)
        parts = _runtime_name_parts(qualname)
        if parts == [cs.TRACE_QUALNAME_MODULE]:
            return rel_path in self._modules_by_path
        if any(p.startswith(cs.TRACE_SYNTHETIC_PREFIX) for p in parts):
            return False
        return bool(_named_by(self._callables_by_path.get(rel_path, []), parts))


def _runtime_name_parts(qualname: str) -> list[str]:
    """A runtime qualname's scope chain without its ``<locals>`` hops."""
    return [
        p for p in qualname.split(cs.SEPARATOR_DOT) if p != cs.TRACE_QUALNAME_LOCALS
    ]


def _named_by(candidates: list[CallableNode], parts: list[str]) -> list[CallableNode]:
    suffix = cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)
    return [
        n
        for n in candidates
        if _natural_qualified_name(n.qualified_name).endswith(suffix)
    ]


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
        self._root_posix = _repo_root_posix(repo_root)
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
        rel_path = _repo_relative(self._root_posix, frame.path)
        if rel_path is None:
            stats.record(cs.TraceUnresolvedReason.OUTSIDE_REPO)
            return None

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
        return ResolvedFrame(
            label=chosen.label,
            qualified_name=chosen.qualified_name,
            folded=not by_name,
        )


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
        self._root_posix = _repo_root_posix(repo_root)
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
            # A closure is named by its own position, never a node of its
            # own: it folds into the function that defines it.
            resolved = self._resolve_position(
                closure.group("path"), int(closure.group("start")), stats
            )
            return None if resolved is None else replace(resolved, folded=True)
        if frame.path:
            if frame.qualname == cs.TRACE_XDEBUG_MAIN:
                return self._resolve_module(frame.path, stats)
            return self._resolve_position(frame.path, frame.line, stats)
        return self._resolve_by_name_tail(frame.qualname, stats)

    def _relative(self, path: str) -> str | None:
        return _repo_relative(self._root_posix, path)

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
# A C# local function compiles to ``<EnclosingHost>g__LocalName|N_M``; unlike a
# lambda it keeps a source name, so it resolves to the node the static tier
# nests under its host: a method (``Run`` -> ``Run.Local``) or an accessor body
# (``get_X``/``set_X``, which is not its own scope, so the local nests at class
# level). A constructor host (``<.ctor>``) carries a dot that the CLR-name
# splitter consumes before this pattern is reached, so ctor-hosted locals stay
# unresolved rather than mis-resolved.
_DOTNET_LOCAL_FUNCTION = re.compile(r"^<(?P<host>\w+)>g__(?P<local>\w+)\|\w+$")
_DOTNET_DISPLAY_CLASS = re.compile(r"^<>c(__DisplayClass\w*)?$")
_DOTNET_ACCESSOR = re.compile(r"^(?:get|set)_(\w+)$")


def _owner_chain(owner: str) -> tuple[list[str], str | None]:
    """The declaring type chain and any state-machine source method.

    Splits nested types on ``+``, dropping display classes (lambda hosts) and
    unwrapping an async state machine (``<RunAsync>d__3``) to the method name it
    stands for, which supersedes the frame's own method segment.
    """
    chain: list[str] = []
    state_machine_method: str | None = None
    for part in owner.split(cs.TRACE_DOTNET_NESTED_MARKER):
        machine = _DOTNET_STATE_MACHINE.match(part)
        if machine:
            state_machine_method = machine.group(1)
            continue
        if _DOTNET_DISPLAY_CLASS.match(part):
            continue
        chain.append(part)
    return chain, state_machine_method


def _dotnet_lambda_body(name: str) -> bool:
    """Whether a CLR frame is a lambda body, which demangles to its host.

    The body is a ``<Host>b__N_M`` method, usually on a display class
    (``<>c``, ``<>c__DisplayClass0_0``); a local function keeps a source name
    of its own and is not one.
    """
    owner, _, method = name.rpartition(cs.SEPARATOR_DOT)
    return bool(_DOTNET_LAMBDA_BODY.match(method)) or any(
        _DOTNET_DISPLAY_CLASS.match(part)
        for part in owner.split(cs.TRACE_DOTNET_NESTED_MARKER)
    )


def _local_function_target(host: str, local: str) -> str:
    """The dotted name of a C# local function under its host.

    A plain method host keeps its name (``Run`` -> ``Run.Local``). An accessor
    body (``get_X``/``set_X``) has no scope of its own, so the static tier nests
    its local directly under the class; the host is dropped (``Local``).
    """
    if _DOTNET_ACCESSOR.match(host):
        return local
    return f"{host}{cs.SEPARATOR_DOT}{local}"


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
    chain, state_machine_method = _owner_chain(owner)
    if state_machine_method is not None:
        method = state_machine_method
    if not chain:
        return None
    if constructor:
        method = chain[-1].rsplit(cs.SEPARATOR_DOT, 1)[-1]
    lambda_body = _DOTNET_LAMBDA_BODY.match(method)
    if lambda_body:
        method = lambda_body.group(1)
    local_function = _DOTNET_LOCAL_FUNCTION.match(method)
    if local_function:
        method = _local_function_target(
            local_function.group("host"), local_function.group("local")
        )
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
        return ResolvedFrame(
            label=chosen.label,
            qualified_name=chosen.qualified_name,
            folded=_dotnet_lambda_body(frame.qualname),
        )

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
        return ResolvedFrame(
            label=chosen.label,
            qualified_name=chosen.qualified_name,
            folded=not by_name,
        )

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
