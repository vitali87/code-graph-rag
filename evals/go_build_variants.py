"""Differential oracle for Go names each build declares again (PR #3360).

Generates small multi-file Go packages whose interfaces, types and methods
are declared per build (file-name suffixes and `//go:build` lines), with
local and imported embeds and callers in build-specific and common files.
The real go/types, run once per GOOS, is the ground truth for what
implements what and what each call reaches in each build; cgr's graph is
then checked against it:

- no IMPLEMENTS or OVERRIDES edge that no build supports;
- no exact (unlabelled, `exact` or `overload`) edge that fails in a build
  holding both its ends (for a call: a build compiling the caller);
- nothing a build proves missing (a heuristic edge is fine);
- a rename keeps `go build` and `go vet` passing on every GOOS that passed.

Run: `python -m evals.go_build_variants --cases 400 --seed 7`.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NamedTuple, cast
from unittest.mock import MagicMock

import typer

from codebase_rag import constants as cs
from codebase_rag.graph_query import QueryFn
from evals.cgr_graph import _StatefulIngestor

if TYPE_CHECKING:
    from codebase_rag.tests.test_rename_op import RecordedGraph

PROJECT = "oracle"
GOOS = ("linux", "windows", "darwin")
_ALL = frozenset(GOOS)
# cgr's modes: names only, and the go/types helper reading the host's build
# (Linux here), Windows' and Darwin's.
FRONTENDS = ("treesitter", "gotypes", "gotypes-windows", "gotypes-darwin")
_NOT_HEURISTIC = {None, "None", cs.EdgeResolution.EXACT, cs.EdgeResolution.OVERLOAD}


class _Part(NamedTuple):
    suffix: str
    header: str
    goos: frozenset[str]


_PARTITIONS: dict[str, tuple[_Part, ...]] = {
    "single": (_Part("", "", _ALL),),
    "suffix": (
        _Part("_linux", "", frozenset({"linux"})),
        _Part("_windows", "", frozenset({"windows"})),
        _Part("_darwin", "", frozenset({"darwin"})),
    ),
    "tag": (
        _Part("_a", "//go:build linux\n\n", frozenset({"linux"})),
        _Part("_b", "//go:build !linux\n\n", frozenset({"windows", "darwin"})),
    ),
    "mixed": (
        _Part("_windows", "", frozenset({"windows"})),
        _Part("_rest", "//go:build !windows\n\n", frozenset({"linux", "darwin"})),
    ),
}
_VARIANT_STYLES = ("suffix", "tag", "mixed")
_RESULTS = ("int", "string")
_RETURNS = {"int": "0", "string": '""'}


# --- generation --------------------------------------------------------------


@dataclass
class _Interface:
    name: str
    style: str
    # Per part of the style's partition: method base name -> result type.
    parts: list[dict[str, str]]

    def methods_in(self, goos: str) -> dict[str, str]:
        for part, methods in zip(_PARTITIONS[self.style], self.parts, strict=True):
            if goos in part.goos:
                return methods
        return {}


def _interface(rng: random.Random, name: str, pool: tuple[str, ...]) -> _Interface:
    style = rng.choice(("single", *_VARIANT_STYLES))
    count = len(_PARTITIONS[style])

    def one() -> dict[str, str]:
        chosen = [m for m in pool if rng.random() < 0.6] or [rng.choice(pool)]
        return {m: rng.choice(_RESULTS) for m in chosen}

    if rng.random() < 0.35:
        same = one()
        return _Interface(name, style, [dict(same) for _ in range(count)])
    return _Interface(name, style, [one() for _ in range(count)])


def _method(type_name: str, name: str, result: str, case: int) -> str:
    if not result:
        return f"func ({type_name}) {name}_{case}() {{}}\n"
    return (
        f"func ({type_name}) {name}_{case}() {result} {{ return {_RETURNS[result]} }}\n"
    )


def generate_case(rng: random.Random, case: int) -> dict[str, str]:
    """One case's files, by repo-relative path: package `p` declaring I (and
    maybe K) per build, J embedding them (in `p`, or in `q` through the
    import), types implementing some of them in some builds, and callers."""
    root = f"c{case:03d}"
    files: dict[str, str] = {}
    interfaces = [_interface(rng, "I", ("A", "B"))]
    if rng.random() < 0.5:
        interfaces.append(_interface(rng, "K", ("C", "D")))
    for iface in interfaces:
        for part, methods in zip(_PARTITIONS[iface.style], iface.parts, strict=True):
            body = "".join(
                f"\t{m}_{case}() {result}\n".replace(" \n", "\n")
                for m, result in methods.items()
            )
            files[f"{root}/p/{iface.name.lower()}{part.suffix}.go"] = (
                f"{part.header}package p\n\ntype {iface.name} interface {{\n{body}}}\n"
            )
    local = rng.random() < 0.5
    pkg = "p" if local else "q"
    qualify = "" if local else "p."
    head = f"package {pkg}\n\n" + ("" if local else f'import "{PROJECT}/{root}/p"\n\n')
    embeds = "".join(f"\t{qualify}{iface.name}\n" for iface in interfaces)
    files[f"{root}/{pkg}/j.go"] = (
        f"{head}type J interface {{\n{embeds}\tN_{case}()\n}}\n"
    )
    for index in range(1, rng.randint(2, 4) + 1):
        files.update(_type_files(rng, root, pkg, index, case))
    files.update(_caller_files(rng, root, pkg, interfaces, case))
    return files


def _type_files(
    rng: random.Random, root: str, pkg: str, index: int, case: int
) -> dict[str, str]:
    files: dict[str, str] = {}
    name = f"T{index}"
    style = "single" if rng.random() < 0.6 else rng.choice(_VARIANT_STYLES)
    for part in _PARTITIONS[style]:
        files[f"{root}/{pkg}/t{index}{part.suffix}.go"] = (
            f"{part.header}package {pkg}\n\ntype {name} struct{{}}\n"
        )
    common: list[str] = []
    for method in ("A", "B", "C", "D", "N"):
        roll = rng.random()
        result = "" if method == "N" else rng.choice(_RESULTS)
        if roll < 0.35:
            continue
        if roll < 0.75:
            common.append(_method(name, method, result, case))
            continue
        files.update(_split_method_files(rng, f"{root}/{pkg}", index, method, case))
    if common:
        files[f"{root}/{pkg}/t{index}m.go"] = f"package {pkg}\n\n" + "\n".join(common)
    return files


def _split_method_files(
    rng: random.Random, directory: str, index: int, method: str, case: int
) -> dict[str, str]:
    # The method declared again in some files of one partition, each with a
    # result of its own.
    pkg = directory.rpartition("/")[2]
    split = _PARTITIONS[rng.choice(_VARIANT_STYLES)]
    chosen = [part for part in split if rng.random() < 0.7] or [split[0]]
    files: dict[str, str] = {}
    for part in chosen:
        own = "" if method == "N" else rng.choice(_RESULTS)
        files[f"{directory}/t{index}{method.lower()}{part.suffix}.go"] = (
            f"{part.header}package {pkg}\n\n{_method(f'T{index}', method, own, case)}"
        )
    return files


def _caller_files(
    rng: random.Random,
    root: str,
    pkg: str,
    interfaces: list[_Interface],
    case: int,
) -> dict[str, str]:
    # Each caller calls one method every build compiling its file has.
    files: dict[str, str] = {}
    qualify = "" if pkg == "p" else "p."
    first = interfaces[0]
    for index in range(rng.randint(2, 4)):
        part = rng.choice(
            (_PARTITIONS["single"][0], *_PARTITIONS[rng.choice(_VARIANT_STYLES)])
        )
        through_i = rng.random() < 0.3
        if through_i:
            sets = [set(first.methods_in(goos)) for goos in part.goos]
            receiver, kind = "i", f"{qualify}I"
        else:
            sets = [
                {"N"}.union(*(iface.methods_in(goos) for iface in interfaces))
                for goos in part.goos
            ]
            receiver, kind = "j", "J"
        shared = sorted(set.intersection(*sets))
        if not shared:
            continue
        method = rng.choice(shared)
        imports = f'import "{PROJECT}/{root}/p"\n\n' if "." in kind else ""
        files[f"{root}/{pkg}/call{index}{part.suffix}.go"] = (
            f"{part.header}package {pkg}\n\n{imports}"
            + f"func Call{index}({receiver} {kind}) {{\n\t{receiver}.{method}_{case}()\n}}\n"
        )
    return files


def write_repo(root: Path, cases: int, seed: int) -> dict[int, dict[str, str]]:
    rng = random.Random(seed)  # noqa: S311 - a reproducible generator, not a secret
    generated = {case: generate_case(rng, case) for case in range(cases)}
    root.mkdir(parents=True, exist_ok=True)
    (root / "go.mod").write_text(f"module {PROJECT}\n\ngo 1.22\n", encoding="utf-8")
    for files in generated.values():
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return generated


# --- ground truth ------------------------------------------------------------


class BuildFacts(NamedTuple):
    """What go/types proved for one GOOS, by (file, line) of declarations."""

    checked: frozenset[str]
    implements: set[tuple[tuple[str, int], tuple[str, int]]]
    # (caller file, call line) -> (target file, target line).
    calls: dict[tuple[str, int], tuple[str, int]]


@contextmanager
def _goos(value: str) -> Iterator[None]:
    old = os.environ.get("GOOS")
    os.environ["GOOS"] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("GOOS", None)
        else:
            os.environ["GOOS"] = old


def ground_truth(root: Path) -> dict[str, BuildFacts]:
    from codebase_rag.parsers.go_frontend import frontend

    go = shutil.which("go")
    if go is None:
        raise RuntimeError("go toolchain not available")
    binary = frontend._build_tool(go)
    if binary is None:
        raise RuntimeError("go/types helper did not build")
    truth: dict[str, BuildFacts] = {}
    for goos in GOOS:
        with _goos(goos):
            facts = frontend._run_tool_once(binary, root, cs.IGNORE_PATTERNS)
        if facts is None:
            raise RuntimeError(f"go/types helper failed for GOOS={goos}")
        truth[goos] = BuildFacts(
            facts.checked_files,
            {
                ((p.impl_file, p.impl_line), (p.iface_file, p.iface_line))
                for p in facts.implements
            },
            {
                (key[0], key[1]): (site.target_file, site.target_line)
                for key, site in facts.call_sites.items()
            },
        )
    return truth


def build_status(root: Path) -> dict[str, bool]:
    """Whether `go build` and `go vet` pass, per GOOS."""
    go = shutil.which("go")
    assert go is not None
    status: dict[str, bool] = {}
    for goos in GOOS:
        env = {**os.environ, "GOOS": goos, "GOARCH": "amd64", "CGO_ENABLED": "0"}
        status[goos] = all(
            subprocess.run(
                [go, command, "./..."],
                cwd=root,
                capture_output=True,
                check=False,
                env=env,
            ).returncode
            == 0
            for command in ("build", "vet")
        )
    return status


# --- cgr's graph -------------------------------------------------------------


def index(root: Path, frontend: str) -> _StatefulIngestor:
    from codebase_rag import graph_updater as gu
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    # `gotypes-windows`: go/types reads the Windows build, as on a Windows
    # host or under GOOS=windows.
    mode, _, goos = frontend.partition("-")
    previous = gu.settings.GO_FRONTEND
    gu.settings.GO_FRONTEND = (
        cs.GoFrontend.GOTYPES if mode == "gotypes" else cs.GoFrontend.TREESITTER
    )
    try:
        parsers, queries = load_parsers()
        store = _StatefulIngestor()
        with _goos(goos) if goos else _host():
            GraphUpdater(
                ingestor=store,
                repo_path=root,
                parsers=parsers,
                queries=queries,
                project_name=PROJECT,
            ).run(force=True)
    finally:
        gu.settings.GO_FRONTEND = previous
    return store


@contextmanager
def _host() -> Iterator[None]:
    yield


@dataclass
class _Graph:
    # qn -> (path, start line, end line, label)
    nodes: dict[str, tuple[str, int, int, str]]
    at: dict[tuple[str, int], str]
    # (source, rel, target) -> resolution labels, one per site.
    edges: dict[tuple[str, str, str], list[str | None]]


def _graph(store: _StatefulIngestor) -> _Graph:
    nodes: dict[str, tuple[str, int, int, str]] = {}
    at: dict[tuple[str, int], str] = {}
    for (label, qn), props in store.nodes.items():
        if label not in ("Class", "Type", "Interface", "Method", "Function"):
            continue
        path, start = props.get(cs.KEY_PATH), props.get("start_line")
        if not isinstance(path, str) or not isinstance(start, int):
            continue
        end = props.get("end_line")
        nodes[str(qn)] = (path, start, end if isinstance(end, int) else start, label)
        at[(path, start)] = str(qn)
    edges: dict[tuple[str, str, str], list[str | None]] = {}
    for edge in store.keyed_edges:
        if edge[2] not in ("IMPLEMENTS", "OVERRIDES", "CALLS"):
            continue
        props = store.edge_props.get(edge, {})
        resolution = props.get(cs.KEY_RESOLUTION)
        edges.setdefault((str(edge[1]), str(edge[2]), str(edge[4])), []).append(
            None if resolution is None else str(resolution)
        )
    return _Graph(nodes, at, edges)


# --- comparison --------------------------------------------------------------


@dataclass
class Findings:
    pairs_supported: int = 0
    # IMPLEMENTS edges carrying no heuristic label: go/types' word.
    proven_implements: int = 0
    false_implements: list[str] = field(default_factory=list)
    partial_exact_implements: list[str] = field(default_factory=list)
    missing_implements: list[str] = field(default_factory=list)
    false_overrides: list[str] = field(default_factory=list)
    partial_exact_overrides: list[str] = field(default_factory=list)
    missing_overrides: list[str] = field(default_factory=list)
    partial_exact_calls: list[str] = field(default_factory=list)
    missing_calls: list[str] = field(default_factory=list)
    unsupported_heuristic_calls: list[str] = field(default_factory=list)
    calls_checked: int = 0

    def failures(self) -> int:
        return sum(
            len(found)
            for found in (
                self.false_implements,
                self.partial_exact_implements,
                self.missing_implements,
                self.false_overrides,
                self.partial_exact_overrides,
                self.missing_overrides,
                self.partial_exact_calls,
                self.missing_calls,
            )
        )


def _receiver(qn: str) -> str:
    # `oracle.c001.q.t1m.T1.A_1@7` -> `T1`.
    return qn.rpartition(cs.SEPARATOR_DOT)[0].rpartition(cs.SEPARATOR_DOT)[2]


def _method_name(qn: str) -> str:
    return qn.rpartition(cs.SEPARATOR_DOT)[2].partition("@")[0]


def _directory(path: str) -> str:
    return path.rpartition("/")[0]


class _Truth:
    """The ground truth in graph qns."""

    def __init__(self, graph: _Graph, truth: dict[str, BuildFacts]) -> None:
        self.graph = graph
        self.truth = truth
        self.implements = {
            goos: {
                (graph.at[impl], graph.at[iface])
                for impl, iface in facts.implements
                if impl in graph.at and iface in graph.at
            }
            for goos, facts in truth.items()
        }
        self.methods = [qn for qn, node in graph.nodes.items() if node[3] == "Method"]

    def built(self, goos: str, qn: str) -> bool:
        return self.graph.nodes[qn][0] in self.truth[goos].checked

    def implementers(self, goos: str, interface_qn: str) -> set[str]:
        return {t for t, i in self.implements[goos] if i == interface_qn}

    def declarations(self, goos: str, type_qn: str, name: str) -> set[str]:
        # The declarations of `name` the type has in `goos`: by receiver name
        # and package directory, in a file that build compiles, whichever
        # variant of the type cgr filed it under.
        directory = _directory(self.graph.nodes[type_qn][0])
        receiver = type_qn.rpartition(cs.SEPARATOR_DOT)[2]
        return {
            qn
            for qn in self.methods
            if _method_name(qn) == name
            and _receiver(qn) == receiver
            and _directory(self.graph.nodes[qn][0]) == directory
            and self.built(goos, qn)
            and self.graph.nodes[_owner(qn)][3] != "Interface"
        }

    def overrides(self, goos: str) -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        for type_qn, interface_qn in self.implements[goos]:
            for method_qn in self.methods:
                if _owner(method_qn) != interface_qn:
                    continue
                found.update(
                    (declaration, method_qn)
                    for declaration in self.declarations(
                        goos, type_qn, _method_name(method_qn)
                    )
                )
        return found

    def possible(self, goos: str, caller: str) -> set[str] | None:
        """What `caller`'s one call may run in `goos`: its static callee and,
        through an interface, each implementation."""
        path, start, end, _ = self.graph.nodes[caller]
        target = next(
            (
                site
                for (file, line), site in self.truth[goos].calls.items()
                if file == path and start <= line <= end
            ),
            None,
        )
        if target is None or target not in self.graph.at:
            return None
        callee = self.graph.at[target]
        found = {callee}
        owner = _owner(callee)
        if self.graph.nodes.get(owner, ("", 0, 0, ""))[3] == "Interface":
            for type_qn in self.implementers(goos, owner):
                found |= self.declarations(goos, type_qn, _method_name(callee))
        return found


def _owner(qn: str) -> str:
    return qn.rpartition(cs.SEPARATOR_DOT)[0]


def compare(store: _StatefulIngestor, truth: dict[str, BuildFacts]) -> Findings:
    graph = _graph(store)
    known = _Truth(graph, truth)
    findings = Findings()
    _compare_pairs(graph, known, findings)
    _compare_calls(graph, known, findings)
    return findings


def _labels(graph: _Graph, rel: str) -> dict[tuple[str, str], list[str | None]]:
    return {
        (source, target): labels
        for (source, kind, target), labels in graph.edges.items()
        if kind == rel and source.startswith(f"{PROJECT}.")
    }


def _compare_pairs(graph: _Graph, known: _Truth, findings: Findings) -> None:
    for rel, per_build, false, partial, missing in (
        (
            "IMPLEMENTS",
            known.implements,
            findings.false_implements,
            findings.partial_exact_implements,
            findings.missing_implements,
        ),
        (
            "OVERRIDES",
            {goos: known.overrides(goos) for goos in GOOS},
            findings.false_overrides,
            findings.partial_exact_overrides,
            findings.missing_overrides,
        ),
    ):
        emitted = _labels(graph, rel)
        supported = set().union(*per_build.values())
        findings.pairs_supported += len(supported)
        if rel == "IMPLEMENTS":
            findings.proven_implements += sum(
                any(label in _NOT_HEURISTIC for label in labels)
                for labels in emitted.values()
            )
        for pair, labels in emitted.items():
            if pair not in supported:
                false.append(f"{pair} {labels}")
            elif any(label in _NOT_HEURISTIC for label in labels) and any(
                known.built(goos, pair[0])
                and known.built(goos, pair[1])
                and pair not in per_build[goos]
                for goos in GOOS
            ):
                partial.append(f"{pair} {labels}")
        missing.extend(str(pair) for pair in sorted(supported - emitted.keys()))


def _compare_calls(graph: _Graph, known: _Truth, findings: Findings) -> None:
    calls = _labels(graph, "CALLS")
    for caller in _callers(graph):
        possible = {
            goos: known.possible(goos, caller)
            for goos in GOOS
            if known.built(goos, caller)
        }
        findings.calls_checked += 1
        reached = {
            target: labels for (src, target), labels in calls.items() if src == caller
        }
        for target, labels in reached.items():
            _check_call(findings, caller, target, labels, possible)
        findings.missing_calls.extend(_missing(caller, reached, possible))


def _callers(graph: _Graph) -> list[str]:
    return sorted(
        qn
        for qn, node in graph.nodes.items()
        if node[3] == "Function" and _method_name(qn).startswith("Call")
    )


def _missing(
    caller: str,
    reached: dict[str, list[str | None]],
    possible: dict[str, set[str] | None],
) -> list[str]:
    return [
        f"{goos}: {caller} -> {target}"
        for goos, found in possible.items()
        for target in sorted(found or ())
        if target not in reached
    ]


def _check_call(
    findings: Findings,
    caller: str,
    target: str,
    labels: list[str | None],
    possible: dict[str, set[str] | None],
) -> None:
    # A heuristic edge no build supports, or a non-heuristic one some build
    # compiling the caller does not.
    exact = any(label in _NOT_HEURISTIC for label in labels)
    if not exact and not any(target in (found or ()) for found in possible.values()):
        findings.unsupported_heuristic_calls.append(f"{caller} -> {target}")
    if exact and any(
        found is not None and target not in found for found in possible.values()
    ):
        findings.partial_exact_calls.append(f"{caller} -> {target} {labels}")


# --- renames -----------------------------------------------------------------


@dataclass
class RenameResult:
    case: int
    target: str
    applied: bool
    broke: list[str]
    # Whether any GOOS built and vetted before the rename.
    built: bool = True


def _declared_name(root: Path, file: str, line: int) -> str:
    # `type T1 struct{}` -> `T1`.
    text = (root / file).read_text(encoding="utf-8").splitlines()[line - 1]
    return text.split()[1]


def _assertions(root: Path) -> dict[str, str]:
    """`var _ X = T{}` for each pair go/types proves, in a file only the
    GOOS proving it compiles: a rename that leaves the pair half-renamed
    breaks that build."""
    files: dict[str, str] = {}
    for goos, facts in ground_truth(root).items():
        by_package: dict[str, set[str]] = {}
        imports: dict[str, set[str]] = {}
        for (impl_file, impl_line), (iface_file, iface_line) in facts.implements:
            package = impl_file.rpartition("/")[0]
            iface = _declared_name(root, iface_file, iface_line)
            iface_package = iface_file.rpartition("/")[0]
            if iface_package != package:
                imports.setdefault(package, set()).add(iface_package)
                iface = f"{iface_package.rpartition('/')[2]}.{iface}"
            by_package.setdefault(package, set()).add(
                f"var _ {iface} = {_declared_name(root, impl_file, impl_line)}{{}}\n"
            )
        for package, lines in by_package.items():
            name = package.rpartition("/")[2]
            imported = "".join(
                f'import "{PROJECT}/{path}"\n'
                for path in sorted(imports.get(package, ()))
            )
            files[f"{package}/zz_assert_{goos}.go"] = (
                f"package {name}\n\n{imported}\n" + "".join(sorted(lines))
            )
    return files


def _rename_targets(graph: RecordedGraph, case: int) -> list[str]:
    # An implementer's own method, or an interface's, by name.
    return sorted(
        qn
        for qn, props in graph.nodes.items()
        if props.get(cs.KEY_LABEL) == "Method"
        and qn.startswith(f"{PROJECT}.c{case:03d}.")
        and _method_name(qn).rpartition("_")[0] in ("A", "B", "C", "D", "N")
    )


def rename_check(
    files: dict[str, str], case: int, workdir: Path, rng: random.Random
) -> RenameResult | None:
    """Rename one method of the case, in a module of the case alone with an
    assertion per pair each build proves, and report each GOOS whose build
    and vet passed before and fail after."""
    from codebase_rag.editing.rename import RenameRefused, rename
    from codebase_rag.tests.conftest import _MockIngestor, create_and_run_updater
    from codebase_rag.tests.test_rename_op import RecordedGraph

    root = workdir / f"rename{case:03d}" / PROJECT
    root.mkdir(parents=True)
    (root / "go.mod").write_text(f"module {PROJECT}\n\ngo 1.22\n", encoding="utf-8")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for rel, text in _assertions(root).items():
        (root / rel).write_text(text, encoding="utf-8")
    before = build_status(root)
    ingestor = _MockIngestor()
    create_and_run_updater(root, cast(MagicMock, ingestor))
    graph = RecordedGraph(cast(MagicMock, ingestor), PROJECT)
    graph.root_path = str(root.resolve())
    targets = _rename_targets(graph, case)
    if not targets:
        return None
    target = rng.choice(targets)

    query: QueryFn = lambda cypher, params: graph.fetch_all(  # noqa: E731
        cypher, dict(params) if params is not None else None
    )

    try:
        report = rename(
            root, query, PROJECT, target, f"Renamed_{case}", allow_heuristic=True
        )
        applied = report.applied
    except RenameRefused:
        applied = False
    after = build_status(root)
    return RenameResult(
        case,
        target,
        applied,
        [goos for goos in GOOS if before[goos] and not after[goos]],
        any(before.values()),
    )


# --- driver ------------------------------------------------------------------


@dataclass
class Report:
    cases: int
    findings: dict[str, Findings]
    renames: list[RenameResult]

    def summary(self) -> dict[str, object]:
        return {
            "cases": self.cases,
            **{
                frontend: {
                    "failures": found.failures(),
                    "supported_pairs": found.pairs_supported,
                    "proven_implements": found.proven_implements,
                    "calls_checked": found.calls_checked,
                    "false_implements": len(found.false_implements),
                    "partial_exact_implements": len(found.partial_exact_implements),
                    "missing_implements": len(found.missing_implements),
                    "false_overrides": len(found.false_overrides),
                    "partial_exact_overrides": len(found.partial_exact_overrides),
                    "missing_overrides": len(found.missing_overrides),
                    "partial_exact_calls": len(found.partial_exact_calls),
                    "missing_calls": len(found.missing_calls),
                    "unsupported_heuristic_calls": len(
                        found.unsupported_heuristic_calls
                    ),
                }
                for frontend, found in self.findings.items()
            },
            "renames": len(self.renames),
            "renames_applied": sum(r.applied for r in self.renames),
            "renames_breaking_a_build": [
                (r.case, r.target, r.broke) for r in self.renames if r.broke
            ],
            "renames_never_built": [r.case for r in self.renames if not r.built],
        }


def run(
    cases: int,
    seed: int,
    workdir: Path,
    frontends: tuple[str, ...] = FRONTENDS,
    renames: int = 0,
) -> Report:
    root = workdir / PROJECT
    generated = write_repo(root, cases, seed)
    truth = ground_truth(root)
    findings = {
        frontend: compare(index(root, frontend), truth) for frontend in frontends
    }
    rng = random.Random(seed)  # noqa: S311 - a reproducible generator, not a secret
    picked = rng.sample(sorted(generated), min(renames, cases))
    results = [
        found
        for case in picked
        if (found := rename_check(generated[case], case, workdir, rng)) is not None
    ]
    return Report(cases, findings, results)


def main(
    cases: Annotated[int, typer.Option(help="Generated cases.")] = 400,
    seed: Annotated[int, typer.Option(help="Generator seed.")] = 7,
    renames: Annotated[int, typer.Option(help="Cases to rename and rebuild.")] = 30,
    details: Annotated[bool, typer.Option(help="Print every finding.")] = False,
) -> None:
    from loguru import logger

    logger.remove()
    with tempfile.TemporaryDirectory() as tmp:
        report = run(cases, seed, Path(tmp), renames=renames)
    typer.echo(json.dumps(report.summary(), indent=2, default=str))
    if details:
        for frontend, found in report.findings.items():
            typer.echo(f"== {frontend}")
            _echo_lists(found)


def _echo_lists(found: Findings) -> None:
    for name, value in vars(found).items():
        if isinstance(value, list) and value:
            typer.echo(f"-- {name}")
            typer.echo("".join(f"   {line}\n" for line in value[:50]), nl=False)


if __name__ == "__main__":
    typer.run(main)
