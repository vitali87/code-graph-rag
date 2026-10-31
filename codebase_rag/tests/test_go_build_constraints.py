"""Which Go files one build compiles together (issue #2483, PR #3360)."""

from __future__ import annotations

import random
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

from codebase_rag.parsers.go import interfaces as go_ifaces
from codebase_rag.parsers.go.builds import (
    ALWAYS,
    NEVER,
    UNKNOWN,
    Expr,
    GoBuilds,
    all_of,
    any_of,
    decide,
    header_constraint,
    name_constraint,
    negate,
)


@pytest.mark.parametrize(
    ("first", "second", "together"),
    [
        ("i_linux.go", "i_windows.go", False),
        ("i_linux.go", "i_amd64.go", True),
        ("i_linux_amd64.go", "i_linux_arm64.go", False),
        ("i_windows_amd64.go", "i_amd64.go", True),
        ("i_linux_test.go", "i_windows.go", False),
        # GOOS=android builds `_linux` files too.
        ("i_android.go", "i_linux.go", True),
        # Only a trailing GOOS or GOARCH counts, and not without a `_`.
        ("linux.go", "windows.go", True),
        ("i_linux_extra.go", "i_windows.go", True),
    ],
)
def test_a_file_name_constrains_its_build(
    first: str, second: str, together: bool
) -> None:
    assert decide((name_constraint(first), name_constraint(second))) is together


@pytest.mark.parametrize(
    ("first", "second", "together"),
    [
        ("//go:build linux\n", "//go:build !linux\n", False),
        ("//go:build linux || darwin\n", "//go:build windows\n", False),
        ("//go:build unix\n", "//go:build windows\n", False),
        ("//go:build unix\n", "//go:build darwin\n", True),
        ("//go:build linux && !(arm64 || cgo)\n", "//go:build arm64\n", False),
        # A tag no GOOS or GOARCH fixes may be either.
        ("//go:build foo\n", "//go:build !foo\n", False),
        ("//go:build foo\n", "//go:build bar\n", True),
        ("//go:build go1.21\n", "//go:build !go1.22\n", True),
        # The legacy form, used only without a //go:build line.
        ("// +build linux,amd64 darwin\n\n", "// +build windows\n\n", False),
        ("// +build linux\n\n", "//go:build linux\n", True),
    ],
)
def test_a_build_line_constrains_its_build(
    first: str, second: str, together: bool
) -> None:
    assert decide((header_constraint(first), header_constraint(second))) is together


@pytest.mark.parametrize(
    "header",
    [
        # Not a constraint: no blank line ends the legacy block, the line
        # follows the package clause, or it is no directive.
        "// +build linux\npackage p\n",
        "package p\n\n//go:build linux\n",
        "//go:buildlinux\n\npackage p\n",
    ],
)
def test_what_is_no_constraint_holds_in_every_build(header: str) -> None:
    assert header_constraint(header) == ALWAYS


def test_variants_are_the_declarations_no_build_compiles_together(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.go").write_text("//go:build linux\n\npackage p\n", encoding="utf-8")
    (tmp_path / "b.go").write_text("//go:build !linux\n\npackage p\n", encoding="utf-8")
    (tmp_path / "c.go").write_text("package p\n", encoding="utf-8")
    builds = GoBuilds(tmp_path)
    found = builds.variants([("p.b.I", "b.go"), ("p.a.I", "a.go")], "c.go")
    assert found == ("p.a.I", "p.b.I")
    assert builds.variants([("p.a.I", "a.go")], "x_windows.go") == ()
    assert builds.variants([("p.a.I", "a.go"), ("p.c.I", "c.go")], "c.go") is None
    assert builds.covered("c.go", ["a.go", "b.go"])
    assert not builds.covered("c.go", ["a.go"])


def test_a_changed_file_is_read_again(tmp_path: Path) -> None:
    path = tmp_path / "a.go"
    path.write_text("//go:build linux\n\npackage p\n", encoding="utf-8")
    builds = GoBuilds(tmp_path)
    assert not builds.together(("a.go", "x_windows.go"))
    path.write_text("//go:build windows\n\npackage p\n\n// grown\n", encoding="utf-8")
    assert builds.together(("a.go", "x_windows.go"))


@pytest.mark.parametrize(
    "header",
    [
        "//go:build " + "(" * 300 + "linux" + ")" * 300 + "\n",
        "//go:build " + "!" * 5000 + "linux\n",
        "//go:build " + " && ".join(["linux"] * 900) + "\n",
        "//go:build ((linux)\n",
        "//go:build linux))\n",
        "//go:build ) linux (\n",
        "//go:build linux && || windows\n",
        "//go:build linux $ windows\n",
        "//go:build \x00\n",
        "//go:build linux &&\n",
        "// +build ,,, !\n\n",
    ],
)
def test_a_constraint_this_cannot_read_is_unknown(tmp_path: Path, header: str) -> None:
    # Malformed, or nested past what this reads: never an exception, and
    # neither true nor false in any build, so the file is neither known to
    # build with another nor known apart from it.
    assert header_constraint(header) == UNKNOWN
    (tmp_path / "a.go").write_text(f"{header}\npackage p\n", encoding="utf-8")
    builds = GoBuilds(tmp_path)
    for other in ("b_windows.go", "b_linux.go"):
        assert not builds.together(("a.go", other))
        assert not builds.apart(("a.go", other))


def test_a_head_cut_off_before_its_package_clause_is_unknown(tmp_path: Path) -> None:
    # The constraint may lie past the bytes read.
    filler = "// filler\n" * 8000
    (tmp_path / "a.go").write_text(
        f"{filler}//go:build linux\n\npackage p\n", encoding="utf-8"
    )
    builds = GoBuilds(tmp_path)
    assert builds.constraint("a.go") == UNKNOWN
    assert not builds.apart(("a.go", "b_windows.go"))


def test_more_tags_than_this_enumerates_is_undecided() -> None:
    # Each tag both asked for and ruled out: only enumerating tells.
    chain = " && ".join(f"(t{index} || !t{index + 1})" for index in range(12))
    assert decide((header_constraint(f"//go:build {chain}\n"),)) is None


def test_a_tag_only_asked_for_or_only_ruled_out_is_never_enumerated() -> None:
    # The build go/types read rules out the tag of each `!t<k>` file it
    # type-checked: dozens, in a repository of many tags. Set each tag only
    # asked for and unset each only ruled out, and no other part fails for
    # it, so what rests on them is still decided.
    ruled_out = [header_constraint(f"//go:build !t{k}\n") for k in range(40)]
    asked = [header_constraint(f"//go:build u{k}\n") for k in range(40)]
    linux, windows = name_constraint("x_linux.go"), name_constraint("y_windows.go")
    assert decide((*ruled_out, *asked, linux)) is True
    assert decide((*ruled_out, *asked, linux, windows)) is False
    assert decide((*ruled_out, header_constraint("//go:build t3\n"))) is False


def test_a_constraint_nested_within_the_limit_is_read() -> None:
    nested = "//go:build " + "(" * 50 + "linux" + ")" * 50 + "\n"
    assert decide((header_constraint(nested), name_constraint("x_windows.go"))) is False


# Where `go/build` reads a constraint (#3360 review): past blank lines and
# line and block comments, never inside a block comment, and a `// +build`
# line only before the first line that is no line comment. Each verdict
# below is `go list`'s under GOOS=windows: True where the file builds there.
_HEADERS = {
    "block": ("/* license */\n\n//go:build linux\n\npackage p\n", False),
    "multi-line-block": (
        "/*\n * Copyright 2026\n */\n//go:build linux\n\npackage p\n",
        False,
    ),
    "line-then-block": (
        "// Copyright\n/* more */\n\n//go:build linux\npackage p\n",
        False,
    ),
    "build-text-in-a-block": (
        "/* //go:build windows */\n//go:build linux\n\npackage p\n",
        False,
    ),
    "build-line-inside-a-block": (
        "/*\n//go:build linux\n*/\n\npackage p\n",
        True,
    ),
    "block-on-the-code-line": ("//go:build linux\n/* x */ package p\n", False),
    "plus-build-after-a-block": (
        "/* x */\n\n// +build linux\n\npackage p\n",
        True,
    ),
    "plus-build-before-a-block": (
        "// +build linux\n\n/* x */\npackage p\n",
        False,
    ),
}


@pytest.mark.parametrize("header", sorted(_HEADERS))
def test_a_constraint_is_read_where_go_reads_it(header: str) -> None:
    text, on_windows = _HEADERS[header]
    windows = name_constraint("x_windows.go")
    assert decide((header_constraint(text), windows)) is on_windows


def test_a_head_ending_inside_a_block_comment_is_unknown() -> None:
    assert header_constraint("/* unterminated\n//go:build linux\n") == UNKNOWN


def test_nested_conjunctions_stay_as_large_as_their_distinct_parts() -> None:
    # An interface's builds join its own file's and each embed's, level by
    # level: nested, a part shared by two embeds was repeated once per path
    # to it, doubling per level of a diamond.
    tags = [header_constraint(f"//go:build t{k}\n") for k in range(40)]
    left = right = ALWAYS
    for tag in tags:
        left, right = all_of((tag, left, right)), all_of((tag, right, left))
    assert left.op == "and"
    assert len(left.parts) == 40


def _walk_without_memo(
    interface: str,
    own: dict[str, go_ifaces.GoMethodSet],
    embeds: dict[str, tuple[go_ifaces.GoEmbedAlternatives, ...] | None],
    files: dict[str, Expr],
    seen: frozenset[str] = frozenset(),
) -> tuple[go_ifaces.GoBuildSet, ...] | None:
    # The walk as it was before its memo: each embed walked again per path.
    written = embeds.get(interface)
    if written is None:
        return None
    sets = [go_ifaces.GoBuildSet(dict(own.get(interface, {})), files[interface])]
    resolved = True
    for alternatives in written:
        options: list[go_ifaces.GoBuildSet] = []
        known = True
        for base, builds in alternatives:
            if base in seen | {interface}:
                continue
            inherited = _walk_without_memo(base, own, embeds, files, seen | {interface})
            if inherited is None:
                known = False
                continue
            options.extend(
                go_ifaces.GoBuildSet(found.methods, all_of((found.builds, builds)))
                for found in inherited
            )
        if not known:
            resolved = False
        elif options:
            sets = go_ifaces._combined(sets, options)
    return tuple(sets) if resolved else None


def _random_hierarchy(
    seed: int,
) -> tuple[
    dict[str, go_ifaces.GoMethodSet],
    dict[str, tuple[go_ifaces.GoEmbedAlternatives, ...] | None],
    dict[str, Expr],
]:
    rng = random.Random(seed)  # noqa: S311 - a reproducible case, not a secret
    constraints = [
        ALWAYS,
        name_constraint("x_linux.go"),
        name_constraint("x_windows.go"),
        header_constraint("//go:build !linux\n"),
    ]
    names = [f"I{k}" for k in range(9)]
    own: dict[str, go_ifaces.GoMethodSet] = {}
    embeds: dict[str, tuple[go_ifaces.GoEmbedAlternatives, ...] | None] = {}
    files: dict[str, Expr] = {}
    for index, name in enumerate(names):
        own[name] = {
            go_ifaces.method_key(f"M{rng.randint(0, 5)}", "p"): (
                go_ifaces.GoSignature((), (rng.choice(("int", "string")),)),
            )
        }
        files[name] = rng.choice(constraints)
        if index and rng.random() < 0.1:
            embeds[name] = None
            continue
        lower = names[:index]
        embeds[name] = tuple(
            tuple(
                (base, rng.choice(constraints))
                for base in rng.sample(lower, rng.randint(1, min(2, len(lower))))
            )
            for _ in range(rng.randint(0, min(3, len(lower))))
        )
    return own, embeds, files


@pytest.mark.parametrize("seed", range(40))
def test_the_memo_gives_what_the_walk_without_it_gives(seed: int) -> None:
    # The same method sets, unknown ones and varying ones, interface by
    # interface, on small hierarchies with per-build embeds and files.
    own, embeds, files = _random_hierarchy(seed)
    full = go_ifaces.full_method_sets(own, embeds, files)
    for interface in embeds:
        try:
            expected: tuple[go_ifaces.GoBuildSet, ...] | None | str = (
                _walk_without_memo(interface, own, embeds, files)
            )
        except go_ifaces._VaryingError:
            expected = "varying"
        found = "varying" if interface in full.varying else full.sets[interface]
        assert found == expected, interface


def _random_chain(
    seed: int, links: int
) -> tuple[
    dict[str, go_ifaces.GoMethodSet],
    dict[str, tuple[go_ifaces.GoEmbedAlternatives, ...] | None],
    dict[str, Expr],
]:
    # I0 embeds I1, ..., each link's embed under a random constraint (one
    # meaning, so the walk without a memo stays linear), each file under
    # one, a method of its own now and then.
    rng = random.Random(seed)  # noqa: S311 - a reproducible case, not a secret
    constraints = [
        ALWAYS,
        name_constraint("x_linux.go"),
        header_constraint("//go:build !linux\n"),
    ]
    own: dict[str, go_ifaces.GoMethodSet] = {}
    embeds: dict[str, tuple[go_ifaces.GoEmbedAlternatives, ...] | None] = {}
    files: dict[str, Expr] = {}
    for k in range(links):
        name = f"I{k}"
        own[name] = (
            {go_ifaces.method_key(f"M{k}", "p"): (go_ifaces.GoSignature((), ()),)}
            if rng.random() < 0.3
            else {}
        )
        files[name] = rng.choice(constraints)
        embeds[name] = (
            () if k == links - 1 else (((f"I{k + 1}", rng.choice(constraints)),),)
        )
    return own, embeds, files


@pytest.mark.parametrize("seed", range(10))
def test_the_memo_gives_what_the_walk_without_it_gives_on_a_chain(seed: int) -> None:
    # Long enough to need many steps, short enough for the reference's own
    # recursion.
    own, embeds, files = _random_chain(seed, 150)
    full = go_ifaces.full_method_sets(own, embeds, files)
    for interface in embeds:
        try:
            expected: tuple[go_ifaces.GoBuildSet, ...] | None | str = (
                _walk_without_memo(interface, own, embeds, files)
            )
        except go_ifaces._VaryingError:
            expected = "varying"
        found = "varying" if interface in full.varying else full.sets[interface]
        assert found == expected, interface


def _cpu_seconds() -> float:
    # CPU time, not wall clock, for the bounds below: each catches a walk
    # gone quadratic, and a busy `pytest -n auto` runner deschedules this
    # process for seconds without the walk doing any more work (a walk of
    # a few tenths of a second read 5.04 s on main's coverage run). As in
    # test_rust_attr_run_scan_perf.py (#1473).
    return time.process_time()


@pytest.mark.parametrize("tail", ["known", "unresolved"])
def test_a_chain_far_past_the_recursion_limit_is_walked(tail: str) -> None:
    # Three Python frames per embed overflowed at a few hundred links.
    links = 5000
    own: dict[str, go_ifaces.GoMethodSet] = {
        f"I{links - 1}": {
            go_ifaces.method_key("M", "p"): (go_ifaces.GoSignature((), ()),)
        }
    }
    embeds: dict[str, tuple[go_ifaces.GoEmbedAlternatives, ...] | None] = {
        f"I{k}": ((((f"I{k + 1}", ALWAYS),),) if k < links - 1 else ())
        for k in range(links)
    }
    if tail == "unresolved":
        embeds[f"I{links - 1}"] = None
    started = _cpu_seconds()
    full = go_ifaces.full_method_sets(own, embeds, {})
    assert _cpu_seconds() - started < 10
    top = full.sets["I0"]
    if tail == "unresolved":
        assert top is None
    else:
        assert top is not None
        assert [set(found.methods) for found in top] == [set(own[f"I{links - 1}"])]


def test_merged_builds_along_a_chain_stay_small() -> None:
    # A method set each build reaches through its own declaration of an
    # embed merges `B && linux || B && !linux`, which is B: the builds an
    # interface inherits through a chain of such embeds doubled per link.
    linux = name_constraint("x_linux.go")
    builds = header_constraint("//go:build cgo\n")
    started = _cpu_seconds()
    for _ in range(5000):
        builds = any_of((all_of((builds, linux)), all_of((builds, negate(linux)))))
    assert _cpu_seconds() - started < 5
    assert builds == header_constraint("//go:build cgo\n")


def test_a_constraint_past_the_size_or_depth_cap_is_unknown() -> None:
    # An expression nested deeper than the decision's walk and Python's
    # hashing recurse through is unknown, and decided as such, at once.
    nested = header_constraint("//go:build t0\n")
    started = _cpu_seconds()
    for k in range(1, 5000):
        tag = header_constraint(f"//go:build t{k % 7}\n")
        nested = any_of(
            (all_of((nested, tag)), header_constraint(f"//go:build u{k}\n"))
        )
    assert _cpu_seconds() - started < 5
    assert nested == UNKNOWN
    assert decide((nested,)) is None


def test_negations_cancel_and_complements_decide() -> None:
    linux = name_constraint("x_linux.go")
    negated = linux
    for _ in range(5001):
        negated = negate(negated)
    assert negated == negate(linux)
    assert any_of((linux, negate(linux))) == ALWAYS
    assert all_of((linux, negate(linux))) == NEVER


# --- an interface method's declarations down its embeds (PR #3360) ----------


def _links(
    own: dict[str, dict[str, str]],
    bases: dict[str, list[str]],
    files: dict[str, Expr] | None = None,
) -> set[tuple[str, str]]:
    # Exported names only: each method's key is its name.
    return set(
        go_ifaces.embedded_method_links(
            {
                interface: {go_ifaces.method_key(name, "p"): qn}
                for interface, methods in own.items()
                for name, qn in methods.items()
            },
            bases,
            files or {},
        )
    )


@pytest.mark.parametrize("declaring", ["every", "ends"])
def test_a_chain_of_redeclarations_links_each_link_once(declaring: str) -> None:
    # I0 embeds I1, ..., each declaring M again, or only the two ends: one
    # pair per link, or one for the chain, in a step per link.
    links = 10_000
    own = {
        f"I{k}": {"M": f"I{k}.M"}
        for k in range(links)
        if declaring == "every" or k in (0, links - 1)
    }
    bases = {f"I{k}": [f"I{k + 1}"] for k in range(links - 1)}
    started = _cpu_seconds()
    found = _links(own, bases)
    assert _cpu_seconds() - started < 5
    if declaring == "every":
        assert found == {(f"I{k}.M", f"I{k + 1}.M") for k in range(links - 1)}
    else:
        assert found == {("I0.M", f"I{links - 1}.M")}


def test_embeds_bringing_one_method_together_are_linked_once() -> None:
    # D embeds B and C. B declares M again over A; C only embeds A; E
    # declares M beside F, which does too. The pair B.M -> A.M is not also
    # made the other way round where D meets A.M through C.
    own = {
        "A": {"M": "A.M"},
        "B": {"M": "B.M"},
        "E": {"M": "E.M"},
        "F": {"M": "F.M"},
    }
    bases = {"B": ["A"], "C": ["A"], "D": ["B", "C"], "G": ["E", "F"]}
    assert _links(own, bases) == {("B.M", "A.M"), ("E.M", "F.M")}


def test_unexported_methods_of_two_packages_are_two_methods() -> None:
    own = {
        "p.A": {go_ifaces.method_key("m", "p"): "p.A.m"},
        "q.B": {go_ifaces.method_key("m", "q"): "q.B.m"},
    }
    assert go_ifaces.embedded_method_links(own, {"q.B": ["p.A"]}, {}) == []


def test_an_embed_cycle_ends() -> None:
    # Go rejects it; the walk skips the embed that closes it (B's, as it
    # starts from A).
    own = {"A": {"M": "A.M"}, "B": {"M": "B.M"}}
    assert _links(own, {"A": ["B"], "B": ["A"]}) == {("A.M", "B.M")}


def test_two_builds_declarations_are_paired_only_where_one_build_holds_both() -> None:
    # J embeds I, which i_linux.go and i_windows.go each declare with M: no
    # build holds both, so they are not paired. B (in a file every build
    # compiles) declares M over I: it is either one's. W, in a Windows
    # file, declares M over I too: only Windows' I is W's.
    linux = name_constraint("i_linux.go")
    windows = name_constraint("i_windows.go")
    own = {"IL": {"M": "IL.M"}, "IW": {"M": "IW.M"}, "B": {"M": "B.M"}}
    own["W"] = {"M": "W.M"}
    bases = {"J": ["IL", "IW"], "B": ["IL", "IW"], "W": ["IL", "IW"]}
    files = {"IL": linux, "IW": windows, "W": windows}
    assert _links(own, bases, files) == {
        ("B.M", "IL.M"),
        ("B.M", "IW.M"),
        ("W.M", "IW.M"),
    }


def _cascade(count: int) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    # Greptile's fixture (PR #3360): each `A<i>` declares M; `C0` embeds A0,
    # and each `C<i>` embeds `C<i-1>` and `A<i>`, so `C<i>` holds i + 1
    # declarations of M.
    own = {f"A{index}": {"M": f"A{index}.M"} for index in range(count)}
    bases = {"C0": ["A0"]}
    bases.update(
        {f"C{index}": [f"C{index - 1}", f"A{index}"] for index in range(1, count)}
    )
    return own, bases


@pytest.mark.parametrize("builds", ["one", "alternating"])
def test_a_cascade_of_gathered_declarations_is_joined_in_linear_work(
    monkeypatch: pytest.MonkeyPatch, builds: str
) -> None:
    # Every combining interface checked every pair gathered below it again:
    # n**3/6 checks and n**2/2 links (a clique). Now one link per
    # declaration, and the decisions grow as the cascade does. `alternating`
    # declares every other A for Linux only and the rest for Windows, so
    # half the gathered declarations never share a build.
    decide = go_ifaces.decide
    decided: list[int] = []

    def counted(exprs: Iterable[Expr]) -> bool | None:
        decided.append(1)
        return decide(exprs)

    monkeypatch.setattr(go_ifaces, "decide", counted)
    files = (
        {
            f"A{index}": name_constraint("a_linux.go" if index % 2 else "a_windows.go")
            for index in range(4000)
        }
        if builds == "alternating"
        else {}
    )
    work: dict[int, tuple[int, int]] = {}
    for count in (1000, 2000):
        decided.clear()
        own, bases = _cascade(count)
        links = _links(own, bases, files)
        work[count] = (len(decided), len(links))
    assert work[2000][1] == (2000 - 1 if builds == "one" else 2000 - 2)
    assert work[2000][0] <= 3 * max(work[1000][0], 1)
    own, bases = _cascade(4000)
    started = _cpu_seconds()
    _links(own, bases, files)
    assert _cpu_seconds() - started < 5
