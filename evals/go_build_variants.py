"""Differential oracle for Go names each build declares again (PR #3360).

Generates small multi-file Go packages whose interfaces, types and methods
are declared per build (file-name suffixes and `//go:build` lines), with
local and imported embeds and callers in build-specific and common files.
The real go/types, run once per build (each GOOS, and Linux with every tag
the cases use), is the ground truth for what implements what and what each
call reaches in each build; cgr's graph is then checked against it:

- no IMPLEMENTS or OVERRIDES edge that no build supports;
- no exact (unlabelled, `exact` or `overload`) edge that fails in a build
  holding both its ends (for a call: a build compiling the caller);
- nothing a build proves missing (a heuristic edge is fine), except for an
  interface past cgr's cap on method-set alternatives (`Wide<n>`, n > 6),
  whose method set is unknown by design: those are counted apart;
- an OVERRIDES edge between two interface methods joins two declarations
  one interface's method set holds in some build, and the declarations
  each interface's set holds of one method (its own, and the nearest one
  down each embed), read from the files go/types compiled, are joined;
- a rename either refuses and changes nothing, or keeps `go vet` (which
  type-checks every package and test file) passing on every build that
  passed; either way it leaves
  byte-identical the same-named Python, JavaScript, external test package
  (`p_test`) and sibling package files it places beside the target. A
  `Wide<n>` interface's methods and its candidate implementers' are among
  the targets: past the cap cgr cannot tell its implementers, so renaming
  `WN` must refuse.

Some cases add an interface embedding n tagged interfaces (`Wide<n>`, 2**n
method sets); a fourth go/types build sets every such tag. Some add a
diamond of embeds 8 to 12 levels deep (`DA<k>` and `DB<k>` each embedding
both of the level below), one method set per build at every level. Some
add a chain of 400 or 600 embeds (`L0` embeds `L1`, ...), one method set
per build at every link. Some add interfaces no type implements (`O<k>`),
each embedding up to two before it and declaring `OM` again or not, one of
them declared per build, with functions converting each to one embedding
it: a rename of `OM` that misses a declaration breaks them.

Run: `python -m evals.go_build_variants --cases 400 --seed 7`.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
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
# cgr's cap on an interface's method-set alternatives: past it the set is
# unknown and links nothing.
_ALTERNATIVES_CAP = 64
_WIDE_TAG = re.compile(r"\bc\d{3}w\d+\b")
_WIDE_NAME = re.compile(r"(?:Wide|CallWide)(\d+)")
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


def generate_case(
    rng: random.Random,
    case: int,
    wide: float = 0.0,
    deep: float = 0.0,
    long: float = 0.0,
    only: float = 0.0,
) -> dict[str, str]:
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
    files.update(_extra_files(rng, root, case, (wide, deep, long, only)))
    return files


def _extra_files(
    rng: random.Random, root: str, case: int, shares: tuple[float, float, float, float]
) -> dict[str, str]:
    # Each kind of extra a case adds, by the share of cases adding it: a
    # `Wide<n>` interface, a diamond, a chain, interfaces no type implements.
    wide, deep, long, only = shares
    files: dict[str, str] = {}
    if wide and rng.random() < wide:
        files.update(_wide_files(root, case, rng.choice((4, 5, 6, 7, 8))))
    if deep and rng.random() < deep:
        files.update(_deep_files(root, case, rng.choice((8, 10, 12))))
    if long and rng.random() < long:
        files.update(_long_files(root, case, rng.choice((400, 600))))
    if only and rng.random() < only:
        files.update(_only_files(root, case, rng))
    return files


def _only_files(root: str, case: int, rng: random.Random) -> dict[str, str]:
    """Interfaces alone, no type implementing any: O0 to O<n>, each embedding
    up to two before it and declaring `OM` again half the time (always when
    it embeds none), so each asks for `OM` alone. O0 may be declared once
    per build, or the last one per build with different embeds. A function
    converts each interface to one embedding it in every build, which
    compiles only while every declaration of `OM` keeps one name, and each
    one declaring `OM` in every build calls it."""
    c, count = case, rng.randint(3, 6)
    last = count - 1
    embeds = {
        k: sorted(rng.sample(range(k), rng.randint(0, min(2, k)))) for k in range(count)
    }
    declares = {k: not embeds[k] or rng.random() < 0.5 for k in range(count)}
    split = {"none": None, "first": 0, "last": last}[
        rng.choice(("none", "first", "last"))
    ]
    other = sorted(rng.sample(range(last), rng.randint(0, min(2, last))))
    other_declares = not other or rng.random() < 0.5
    if split == 0:
        other, other_declares = [], True

    def body(bases: list[int], declared: bool) -> str:
        return "".join(f"\tO{j}\n" for j in bases) + (
            f"\tOM_{c}()\n" if declared else ""
        )

    below: dict[int, set[int]] = {}
    for k in range(count):
        below[k] = set(embeds[k]).union(*(below[j] for j in embeds[k]))
    calls = dict(declares)
    if split == last:
        below[last] &= set(other).union(*(below[j] for j in other))
        calls[last] = declares[last] and other_declares
    lines = [
        f"type O{k} interface {{\n{body(embeds[k], declares[k])}}}\n"
        for k in range(count)
        if k != split
    ]
    for k in range(count):
        if below[k]:
            j = rng.choice(sorted(below[k]))
            lines.append(f"func OC{k}_{j}(x O{j}) O{k} {{\n\treturn x\n}}\n")
        if calls[k]:
            lines.append(f"func OUse{k}(x O{k}) {{\n\tx.OM_{c}()\n}}\n")
    files = {f"{root}/p/only.go": "package p\n\n" + "\n".join(lines)}
    if split is not None:
        files[f"{root}/p/o{split}_linux.go"] = (
            f"package p\n\ntype O{split} interface {{\n"
            f"{body(embeds[split], declares[split])}}}\n"
        )
        files[f"{root}/p/o{split}_other.go"] = (
            f"//go:build !linux\n\npackage p\n\ntype O{split} interface {{\n"
            f"{body(other, other_declares)}}}\n"
        )
    return files


def _long_files(root: str, case: int, links: int) -> dict[str, str]:
    """A chain `links` long: L0 embeds L1, which embeds L2, and so on; the
    last asks for `LM() int` on Linux and `LM() string` elsewhere, so every
    link has one method set per build. TL is each on Linux, UL elsewhere.
    A walk taking a Python frame per embed overflowed the stack."""
    c, last = case, links - 1
    chain = "".join(f"type L{k} interface {{\n\tL{k + 1}\n}}\n\n" for k in range(last))
    return {
        f"{root}/p/long.go": f"package p\n\n{chain}",
        f"{root}/p/llast_linux.go": (
            f"package p\n\ntype L{last} interface {{\n\tLM_{c}() int\n}}\n"
        ),
        f"{root}/p/llast_other.go": (
            f"//go:build !linux\n\npackage p\n\n"
            f"type L{last} interface {{\n\tLM_{c}() string\n}}\n"
        ),
        f"{root}/p/tl_linux.go": (
            f"package p\n\ntype TL struct{{}}\n\nfunc (TL) LM_{c}() int {{ return 0 }}\n"
        ),
        f"{root}/p/ul_other.go": (
            f"//go:build !linux\n\npackage p\n\ntype UL struct{{}}\n\n"
            f'func (UL) LM_{c}() string {{ return "" }}\n'
        ),
        f"{root}/p/calllong.go": (
            f"package p\n\nfunc CallLong{links}(l L0) {{\n\tl.LM_{c}()\n}}\n"
        ),
    }


def _deep_files(root: str, case: int, levels: int) -> dict[str, str]:
    """A diamond `levels` deep: DA<k> and DB<k> each embed DA<k-1> and
    DB<k-1>. DA0 asks for `DM0() int` on Linux and `DM0() string` elsewhere,
    so every level has one method set per build: TD is each on Linux, UD
    (declared off Linux) each elsewhere. A walk that does not reuse what it
    found walks DA0 2**levels times."""
    c = case
    body = [f"package p\n\ntype DB0 interface {{\n\tDN0_{c}()\n}}\n"]
    for k in range(1, levels):
        for name, method in (("DA", "DM"), ("DB", "DN")):
            body.append(
                f"type {name}{k} interface {{\n\tDA{k - 1}\n\tDB{k - 1}\n"
                f"\t{method}{k}_{c}()\n}}\n"
            )
    rest = "".join(
        f"func ({{owner}}) {method}{k}_{c}() {{{{}}}}\n"
        for k in range(levels)
        for method in ("DM", "DN")
        if (method, k) != ("DM", 0)
    )
    top = levels - 1
    return {
        f"{root}/p/da0_linux.go": (
            f"package p\n\ntype DA0 interface {{\n\tDM0_{c}() int\n}}\n"
        ),
        f"{root}/p/da0_other.go": (
            f"//go:build !linux\n\npackage p\n\n"
            f"type DA0 interface {{\n\tDM0_{c}() string\n}}\n"
        ),
        f"{root}/p/deep.go": "\n".join(body),
        f"{root}/p/td.go": (
            f"package p\n\ntype TD struct{{}}\n\n"
            f"func (TD) DM0_{c}() int {{ return 0 }}\n" + rest.format(owner="TD")
        ),
        f"{root}/p/ud_other.go": (
            f"//go:build !linux\n\npackage p\n\ntype UD struct{{}}\n\n"
            f'func (UD) DM0_{c}() string {{ return "" }}\n' + rest.format(owner="UD")
        ),
        f"{root}/p/calldeep.go": (
            f"package p\n\nfunc CallDeep{levels}(a DA{top}) {{\n\ta.DM{top}_{c}()\n}}\n"
        ),
    }


def _wide_files(root: str, case: int, count: int) -> dict[str, str]:
    """`Wide<count>` embeds W1..W<count>, each asking for WA<k> under its own
    tag and WB<k> without it: 2**count method sets. TW is one in the build
    without tags, AW in the build with every tag, UW, with WN alone, in
    none."""
    files: dict[str, str] = {}
    for index in range(1, count + 1):
        tag = f"c{case:03d}w{index}"
        for header, method in ((tag, "WA"), (f"!{tag}", "WB")):
            files[f"{root}/p/w{index}{method[1].lower()}.go"] = (
                f"//go:build {header}\n\npackage p\n\n"
                f"type W{index} interface {{\n\t{method}{index}_{case}()\n}}\n"
            )
    embeds = "".join(f"\tW{index}\n" for index in range(1, count + 1))
    files[f"{root}/p/wide.go"] = (
        f"package p\n\ntype Wide{count} interface {{\n{embeds}\tWN_{case}()\n}}\n"
    )
    for name, prefix in (("TW", "WB"), ("AW", "WA"), ("UW", "")):
        methods = "".join(
            f"func ({name}) {prefix}{index}_{case}() {{}}\n"
            for index in range(1, count + 1)
            if prefix
        )
        files[f"{root}/p/{name.lower()}.go"] = (
            f"package p\n\ntype {name} struct{{}}\n\n{methods}"
            f"func ({name}) WN_{case}() {{}}\n"
        )
    files[f"{root}/p/callwide.go"] = (
        f"package p\n\nfunc CallWide{count}(w Wide{count}) {{\n\tw.WN_{case}()\n}}\n"
    )
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


def write_repo(
    root: Path,
    cases: int,
    seed: int,
    wide: float = 0.0,
    deep: float = 0.0,
    long: float = 0.0,
    only: float = 0.0,
) -> dict[int, dict[str, str]]:
    rng = random.Random(seed)  # noqa: S311 - a reproducible generator, not a secret
    generated = {
        case: generate_case(rng, case, wide, deep, long, only) for case in range(cases)
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "go.mod").write_text(f"module {PROJECT}\n\ngo 1.22\n", encoding="utf-8")
    for files in generated.values():
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return generated


# --- ground truth ------------------------------------------------------------


class Build(NamedTuple):
    """One build the ground truth reads: a GOOS and the tags it sets."""

    name: str
    goos: str
    tags: str = ""


def builds_for(generated: dict[int, dict[str, str]]) -> tuple[Build, ...]:
    # Each GOOS without tags, and Linux with every tag a case uses.
    tags = sorted(
        {
            tag
            for files in generated.values()
            for text in files.values()
            for tag in _WIDE_TAG.findall(text)
        }
    )
    base = tuple(Build(goos, goos) for goos in GOOS)
    return base + ((Build("linux-tags", "linux", ",".join(tags)),) if tags else ())


class BuildFacts(NamedTuple):
    """What go/types proved for one build, by (file, line) of declarations."""

    checked: frozenset[str]
    implements: set[tuple[tuple[str, int], tuple[str, int]]]
    # (caller file, call line) -> (target file, target line).
    calls: dict[tuple[str, int], tuple[str, int]]


@contextmanager
def _environment(**values: str) -> Generator[None]:
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _build_env(build: Build) -> dict[str, str]:
    env = {"GOOS": build.goos}
    if build.tags:
        env["GOFLAGS"] = f"-tags={build.tags}"
    return env


def ground_truth(root: Path, builds: tuple[Build, ...]) -> dict[str, BuildFacts]:
    from codebase_rag.parsers.go_frontend import frontend

    go = shutil.which("go")
    if go is None:
        raise RuntimeError("go toolchain not available")
    binary = frontend._build_tool(go)
    if binary is None:
        raise RuntimeError("go/types helper did not build")
    truth: dict[str, BuildFacts] = {}
    for build in builds:
        with _environment(**_build_env(build)):
            facts = frontend._run_tool_once(binary, root, cs.IGNORE_PATTERNS)
        if facts is None:
            raise RuntimeError(f"go/types helper failed for {build.name}")
        truth[build.name] = BuildFacts(
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


def build_status(root: Path, builds: tuple[Build, ...]) -> dict[str, bool]:
    """Whether `go vet` passes, per build: it type-checks every package and
    every test file, which is what a rename can break (a `go build` of
    these libraries added a second compile and nothing more)."""
    go = shutil.which("go")
    assert go is not None
    status: dict[str, bool] = {}
    for build in builds:
        env = {
            **os.environ,
            **_build_env(build),
            "GOARCH": "amd64",
            "CGO_ENABLED": "0",
        }
        status[build.name] = (
            subprocess.run(
                [go, "vet", "./..."],
                cwd=root,
                capture_output=True,
                check=False,
                env=env,
            ).returncode
            == 0
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
        with _environment(GOOS=goos) if goos else _host():
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
def _host() -> Generator[None]:
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
    # The joins the interface method groups of every build ask for.
    interface_pairs_supported: int = 0
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
    # What a build proves through an interface past the alternatives cap:
    # cgr knows nothing of its method set by design, so links nothing.
    unknown_past_cap: list[str] = field(default_factory=list)
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


def _past_cap(item: str) -> bool:
    return any(
        2 ** int(count) > _ALTERNATIVES_CAP for count in _WIDE_NAME.findall(item)
    )


def _add_missing(findings: Findings, missing: list[str], items: list[str]) -> None:
    for item in items:
        (findings.unknown_past_cap if _past_cap(item) else missing).append(item)


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


def compare(
    store: _StatefulIngestor,
    truth: dict[str, BuildFacts],
    interfaces: dict[str, InterfaceTruth] | None = None,
) -> Findings:
    """`interfaces` holds what `interface_truth` reads per build."""
    graph = _graph(store)
    known = _Truth(graph, truth)
    findings = Findings()
    _compare_pairs(graph, known, findings)
    _compare_interface_overrides(graph, interfaces or {}, findings)
    _compare_calls(graph, known, findings)
    return findings


_GO_EMBED = re.compile(r"^\t(\w+(?:\.\w+)?)$", re.MULTILINE)


class InterfaceTruth(NamedTuple):
    """One build's interface method sets, read from the files it compiles
    (as go/types listed them)."""

    # Per interface and method, its own declaration and the nearest one
    # down each embed, where that makes two or more, as method qns. Go
    # requires them identical: they are one method there.
    groups: list[tuple[str, ...]]
    # Each interface -> the interfaces embedding it.
    embedders: dict[str, list[str]]
    # Every interface method the build declares.
    declared: frozenset[str]

    def together(self, first: str, second: str) -> bool:
        """Whether one interface's method set holds both declarations."""
        if (
            _method_name(first) != _method_name(second)
            or not {
                first,
                second,
            }
            <= self.declared
        ):
            return False
        return bool(self._above(_owner(first)) & self._above(_owner(second)))

    def _above(self, interface: str) -> set[str]:
        found, pending = {interface}, [interface]
        while pending:
            for embedder in self.embedders.get(pending.pop(), ()):
                if embedder not in found:
                    found.add(embedder)
                    pending.append(embedder)
        return found


def interface_truth(root: Path, checked: frozenset[str]) -> InterfaceTruth:
    declared: dict[tuple[str, str], tuple[str, list[str], list[str]]] = {}
    for rel in sorted(checked):
        if not (root / rel).is_file():
            continue
        text = (root / rel).read_text(encoding="utf-8")
        qn = f"{PROJECT}.{rel.removesuffix(cs.EXT_GO).replace('/', '.')}"
        for name, body in _GO_INTERFACE.findall(text):
            declared[(_directory(rel), name)] = (
                f"{qn}.{name}",
                _GO_EMBED.findall(body),
                _GO_INTERFACE_METHOD.findall(body),
            )
    embeds = {
        key: [
            found for text in written if (found := _embedded(key[0], text)) in declared
        ]
        for key, (_, written, _) in declared.items()
    }
    embedders: dict[str, list[str]] = {}
    for key, bases in embeds.items():
        for base in bases:
            embedders.setdefault(declared[base][0], []).append(declared[key][0])
    nearest: dict[tuple[str, str], dict[str, set[str]]] = {}
    groups: list[tuple[str, ...]] = []
    for key in TopologicalSorter(embeds).static_order():
        qn, _, methods = declared[key]
        merged: dict[str, set[str]] = {}
        for base in embeds[key]:
            for name, found in nearest[base].items():
                merged.setdefault(name, set()).update(found)
        own = {name: {f"{qn}.{name}"} for name in methods}
        groups.extend(
            tuple(sorted(found | own.get(name, set())))
            for name, found in sorted(merged.items())
            if len(found | own.get(name, set())) > 1
        )
        nearest[key] = {**merged, **own}
    return InterfaceTruth(
        groups,
        embedders,
        frozenset(
            f"{qn}.{name}" for qn, _, methods in declared.values() for name in methods
        ),
    )


def _embedded(directory: str, text: str) -> tuple[str, str]:
    # `I` in the interface's own package; `p.I` in the case's package `p`.
    qualifier, dot, name = text.rpartition(".")
    return (f"{_directory(directory)}/{qualifier}" if dot else directory, name)


def _compare_interface_overrides(
    graph: _Graph, interfaces: dict[str, InterfaceTruth], findings: Findings
) -> None:
    # An OVERRIDES edge between two interface methods must join two
    # declarations one interface's method set holds in some build, and the
    # declarations each groups (`InterfaceTruth.groups`) must be joined,
    # through such edges, in the graph.
    groups = {group for found in interfaces.values() for group in found.groups}
    findings.interface_pairs_supported += sum(len(group) - 1 for group in groups)
    joined: dict[str, str] = {}
    for pair, labels in sorted(_labels(graph, "OVERRIDES").items()):
        if not _between_interfaces(graph, pair):
            continue
        if not any(found.together(*pair) for found in interfaces.values()):
            findings.false_overrides.append(f"{pair} {labels} (interfaces)")
        _join(joined, *pair)
    findings.missing_overrides.extend(
        f"{group} (interfaces, not joined)"
        for group in sorted(groups)
        if len({_root(joined, qn) for qn in group}) > 1
    )


def _between_interfaces(graph: _Graph, pair: tuple[str, str]) -> bool:
    return all(
        graph.nodes.get(_owner(qn), ("", 0, 0, ""))[3] == "Interface" for qn in pair
    )


def _root(joined: dict[str, str], qn: str) -> str:
    while joined.get(qn, qn) != qn:
        qn = joined[qn]
    return qn


def _join(joined: dict[str, str], first: str, second: str) -> None:
    joined[_root(joined, first)] = _root(joined, second)


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
            {build: known.overrides(build) for build in known.truth},
            findings.false_overrides,
            findings.partial_exact_overrides,
            findings.missing_overrides,
        ),
    ):
        emitted = {
            pair: labels
            for pair, labels in _labels(graph, rel).items()
            if not _between_interfaces(graph, pair)
        }
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
                for goos in known.truth
            ):
                partial.append(f"{pair} {labels}")
        _add_missing(
            findings,
            missing,
            [str(pair) for pair in sorted(supported - emitted.keys())],
        )


def _compare_calls(graph: _Graph, known: _Truth, findings: Findings) -> None:
    calls = _labels(graph, "CALLS")
    for caller in _callers(graph):
        possible = {
            goos: known.possible(goos, caller)
            for goos in known.truth
            if known.built(goos, caller)
        }
        findings.calls_checked += 1
        reached = {
            target: labels for (src, target), labels in calls.items() if src == caller
        }
        for target, labels in reached.items():
            _check_call(findings, caller, target, labels, possible)
        _add_missing(
            findings, findings.missing_calls, _missing(caller, reached, possible)
        )


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
    # Whether any build built and vetted before the rename.
    built: bool = True
    # Neighbour files (other languages, other packages) the rename changed.
    touched: list[str] = field(default_factory=list)
    # Whether it refused, and any file it changed although it did.
    refused: bool = False
    changed_on_refusal: list[str] = field(default_factory=list)


def _declared_name(root: Path, file: str, line: int) -> str:
    # `type T1 struct{}` -> `T1`.
    text = (root / file).read_text(encoding="utf-8").splitlines()[line - 1]
    return text.split()[1]


def _assertions(
    root: Path, builds: tuple[Build, ...], truth: dict[str, BuildFacts] | None = None
) -> dict[str, str]:
    """`var _ X = T{}` for each pair go/types proves, in a file only the
    build proving it compiles: a rename that leaves the pair half-renamed
    breaks that build. `truth`, the whole repository's, holds the case's
    pairs already (its files sit at the same paths, and no other case's
    tag changes them), so go/types runs again only without it."""
    files: dict[str, str] = {}
    tags = sorted({tag for build in builds for tag in build.tags.split(",") if tag})
    known = truth if truth is not None else ground_truth(root, builds)
    for build in builds:
        facts = known[build.name]
        own = set(build.tags.split(","))
        header = " && ".join(
            [build.goos, *(tag if tag in own else f"!{tag}" for tag in tags)]
        )
        by_package: dict[str, set[str]] = {}
        imports: dict[str, set[str]] = {}
        for (impl_file, impl_line), (iface_file, iface_line) in facts.implements:
            if not (root / impl_file).is_file() or not (root / iface_file).is_file():
                continue
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
            files[f"{package}/zz_assert_{build.name.replace('-', '_')}.go"] = (
                f"//go:build {header}\n\npackage {name}\n\n{imported}\n"
                + "".join(sorted(lines))
            )
    return files


_RENAMED = re.compile(r"(?:[ABCDN]|WN|W[AB]\d+|D[MN]\d+|LM|OM)_\d+")
# The methods a case renames half the time when it has them, as they are
# few beside the others: a `Wide<n>` interface's, and the interface-only
# hierarchy's.
_FOCUSED = re.compile(r"(?:WN|OM)_\d+")


def _rename_targets(graph: RecordedGraph, files: dict[str, str]) -> list[str]:
    # An implementer's own method, or an interface's, by name, in a file the
    # case generated: a `Wide<n>` interface's own `WN` and its candidate
    # implementers' among them, and those of the interfaces it embeds.
    return sorted(
        qn
        for qn, props in graph.nodes.items()
        if props.get(cs.KEY_LABEL) == "Method"
        and props.get(cs.KEY_PATH) in files
        and _RENAMED.fullmatch(_method_name(qn))
    )


def _pick(targets: list[str], rng: random.Random) -> str:
    focused = [qn for qn in targets if _FOCUSED.fullmatch(_method_name(qn))]
    if focused and rng.random() < 0.5:
        return rng.choice(focused)
    return rng.choice(targets)


_GO_TYPE = re.compile(r"^type (\w+) struct\{\}$", re.MULTILINE)
_GO_METHOD = re.compile(r"^func \((\w+)\) (\w+)\(", re.MULTILINE)
_GO_INTERFACE = re.compile(
    r"^type (\w+) interface \{\n(.*?)^\}", re.DOTALL | re.MULTILINE
)
_GO_INTERFACE_METHOD = re.compile(r"^\t(\w+)\(", re.MULTILINE)
_GO_HEADER = re.compile(r"\A(//go:build [^\n]*\n\n)?package \w+\n")


def _declared_methods(text: str) -> list[tuple[str, str]]:
    # (owner, method) for each method of a type or an interface.
    found = set(_GO_METHOD.findall(text))
    for owner, body in _GO_INTERFACE.findall(text):
        found |= {(owner, name) for name in _GO_INTERFACE_METHOD.findall(body)}
    return sorted(found)


def neighbours(files: dict[str, str]) -> dict[str, str]:
    """What no rename of the case may touch. Beside each Go file declaring
    methods, a Python and a JavaScript file declaring same-named classes,
    methods and functions; and for each Go file declaring types or their
    methods, the same declarations, under the same name and constraint, in
    an external test package (`p_test`) and in a sibling package. A
    neighbour's method takes an `int`, so no interface of the case is
    implemented by a neighbour, nor one of them by a type of the case."""
    found: dict[str, str] = {}
    for rel, text in sorted(files.items()):
        directory, _, name = rel.rpartition("/")
        stem = name.removesuffix(".go")
        declared = _declared_methods(text)
        if declared:
            found[f"{directory}/{stem}_alt.py"] = "".join(
                f"class {owner}:\n    def {method}(self):\n        return 0\n\n\n"
                f"def {method}():\n    return {owner}().{method}()\n\n\n"
                for owner, method in declared
            )
            found[f"{directory}/{stem}_alt.js"] = "".join(
                f"class {owner} {{\n  {method}() {{\n    return 0;\n  }}\n}}\n"
                f"function {method}() {{\n  return new {owner}().{method}();\n}}\n"
                for owner, method in declared
            )
        mirrored = _mirrored(text)
        if mirrored is None:
            continue
        package = directory.rpartition("/")[2]
        header = _GO_HEADER.match(text)
        constraint = (header.group(1) or "") if header else ""
        found[f"{directory}/{stem}_test.go"] = (
            f"{constraint}package {package}_test\n\n{mirrored}"
        )
        found[f"{directory}sib/{stem}.go"] = (
            f"{constraint}package {package}sib\n\n{mirrored}"
        )
    return found


def _mirrored(text: str) -> str | None:
    types = _GO_TYPE.findall(text)
    methods = _GO_METHOD.findall(text)
    if not types and not methods:
        return None
    return "".join(f"type {name} struct{{}}\n\n" for name in types) + "".join(
        f"func ({owner}) {method}(int) {{}}\n" for owner, method in methods
    )


def probe_files(case: int) -> tuple[dict[str, str], dict[str, str]]:
    """A method `p`'s own tests declare once per GOOS and a common test
    calls, to rename, and an external test package's same-named type and
    method beside it, never to touch: (the first, the second)."""
    method = f"N_{case}"
    own = {
        f"c{case:03d}/p/zz_probe_test.go": (
            f"package p\n\ntype TP struct{{}}\n\nfunc probe() {{\n\tTP{{}}.{method}()\n}}\n"
        ),
        **{
            f"c{case:03d}/p/zz_probe_{goos}_test.go": (
                f"package p\n\nfunc (TP) {method}() {{}}\n"
            )
            for goos in GOOS
        },
    }
    beside = {
        f"c{case:03d}/p/zz_probe_ext_test.go": (
            f"package p_test\n\ntype TP struct{{}}\n\nfunc (TP) {method}(int) {{}}\n"
        )
    }
    return own, beside


def rename_check(
    files: dict[str, str],
    case: int,
    workdir: Path,
    rng: random.Random,
    truth: dict[str, BuildFacts] | None = None,
) -> RenameResult | None:
    """Rename one method of the case, in a module of the case alone with an
    assertion per pair each build proves, and report each build that built
    and vetted before and fails after, and each neighbour file (see
    `neighbours`) it changed."""
    from codebase_rag.editing.rename import RenameRefused, rename
    from codebase_rag.tests.conftest import _MockIngestor, create_and_run_updater
    from codebase_rag.tests.test_rename_op import RecordedGraph

    root = workdir / f"rename{case:03d}" / PROJECT
    root.mkdir(parents=True)
    (root / "go.mod").write_text(f"module {PROJECT}\n\ngo 1.22\n", encoding="utf-8")
    builds = builds_for({case: files})
    probe, probe_beside = probe_files(case)
    own = {**files, **probe}
    beside = {**neighbours(files), **probe_beside}
    for rel, text in {**own, **beside}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for rel, text in _assertions(root, builds, truth).items():
        (root / rel).write_text(text, encoding="utf-8")
    before = build_status(root, builds)
    ingestor = _MockIngestor()
    create_and_run_updater(root, cast(MagicMock, ingestor))
    graph = RecordedGraph(cast(MagicMock, ingestor), PROJECT)
    graph.root_path = str(root.resolve())
    targets = _rename_targets(graph, own)
    if not targets:
        return None
    target = _pick(targets, rng)
    written = {
        rel: (root / rel).read_text(encoding="utf-8")
        for rel in sorted(own.keys() | beside.keys() | _assertions_in(root))
    }

    query: QueryFn = lambda cypher, params: graph.fetch_all(  # noqa: E731
        cypher, dict(params) if params is not None else None
    )

    refused = False
    try:
        report = rename(
            root, query, PROJECT, target, f"Renamed_{case}", allow_heuristic=True
        )
        applied = report.applied
    except RenameRefused:
        applied, refused = False, True
    after = build_status(root, builds)
    return RenameResult(
        case,
        target,
        applied,
        [build for build, passed in before.items() if passed and not after[build]],
        any(before.values()),
        sorted(
            rel
            for rel, text in beside.items()
            if (root / rel).read_text(encoding="utf-8") != text
        ),
        refused,
        sorted(
            rel
            for rel, text in written.items()
            if refused and (root / rel).read_text(encoding="utf-8") != text
        ),
    )


def _assertions_in(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("zz_assert_*.go")}


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
                    "interface_pairs": found.interface_pairs_supported,
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
                    "unknown_past_cap": len(found.unknown_past_cap),
                }
                for frontend, found in self.findings.items()
            },
            "renames": len(self.renames),
            "renames_applied": sum(r.applied for r in self.renames),
            "renames_refused": [(r.case, r.target) for r in self.renames if r.refused],
            "renames_refused_but_changed": [
                (r.case, r.target, r.changed_on_refusal)
                for r in self.renames
                if r.changed_on_refusal
            ],
            "renames_breaking_a_build": [
                (r.case, r.target, r.broke) for r in self.renames if r.broke
            ],
            "renames_never_built": [r.case for r in self.renames if not r.built],
            "renames_touching_neighbours": [
                (r.case, r.target, r.touched) for r in self.renames if r.touched
            ],
        }


def run(
    cases: int,
    seed: int,
    workdir: Path,
    frontends: tuple[str, ...] = FRONTENDS,
    renames: int = 0,
    wide: float = 0.0,
    deep: float = 0.0,
    long: float = 0.0,
    only: float = 0.0,
) -> Report:
    root = workdir / PROJECT
    generated = write_repo(root, cases, seed, wide, deep, long, only)
    truth = ground_truth(root, builds_for(generated))
    interfaces = {
        build: interface_truth(root, facts.checked) for build, facts in truth.items()
    }
    findings = {
        frontend: compare(index(root, frontend), truth, interfaces)
        for frontend in frontends
    }
    rng = random.Random(seed)  # noqa: S311 - a reproducible generator, not a secret
    picked = rng.sample(sorted(generated), min(renames, cases))
    results = [
        found
        for case in picked
        if (found := rename_check(generated[case], case, workdir, rng, truth))
        is not None
    ]
    return Report(cases, findings, results)


def main(
    cases: Annotated[int, typer.Option(help="Generated cases.")] = 400,
    seed: Annotated[int, typer.Option(help="Generator seed.")] = 7,
    renames: Annotated[int, typer.Option(help="Cases to rename and rebuild.")] = 30,
    wide: Annotated[
        float, typer.Option(help="Share of cases adding a Wide<n> interface.")
    ] = 0.25,
    deep: Annotated[
        float, typer.Option(help="Share of cases adding a diamond of embeds.")
    ] = 0.25,
    long: Annotated[
        float, typer.Option(help="Share of cases adding a long chain of embeds.")
    ] = 0.05,
    only: Annotated[
        float,
        typer.Option(help="Share of cases adding interfaces no type implements."),
    ] = 0.1,
    details: Annotated[bool, typer.Option(help="Print every finding.")] = False,
) -> None:
    from loguru import logger

    logger.remove()
    with tempfile.TemporaryDirectory() as tmp:
        report = run(
            cases,
            seed,
            Path(tmp),
            renames=renames,
            wide=wide,
            deep=deep,
            long=long,
            only=only,
        )
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
