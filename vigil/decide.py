from typing import NamedTuple

from . import constants as vc
from .types_defs import Comparison, GraphDelta, Item, SuiteRun


class Findings(NamedTuple):
    item: Item
    experiment: vc.Experiment
    untestable: str | None = None
    error: str | None = None
    frozen: list[str] = []
    baseline_tests: SuiteRun | None = None
    candidate_tests: SuiteRun | None = None
    new_failures: list[str] = []
    tool_regression: str | None = None
    deltas: list[GraphDelta] = []
    comparison: Comparison | None = None
    widened: str | None = None
    reach: str | None = None


class Decision(NamedTuple):
    verdict: vc.Verdict
    headline: str
    evidence: list[str]


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def bench_line(c: Comparison) -> str:
    base = sorted(c.baseline_ms)[len(c.baseline_ms) // 2]
    return (
        f"{c.corpus} build {base:.0f} ms, {_pct(c.relative)} "
        f"({c.wins} faster / {c.losses} slower of {len(c.baseline_ms)} pairs, "
        f"p={c.p_value:.3f}): {c.speed}; peak RSS x{c.rss_ratio:.2f}"
    )


def delta_line(d: GraphDelta) -> str:
    return (
        f"{d.corpus} graph {d.baseline_digest} -> {d.candidate_digest}: "
        f"nodes +{d.nodes_added}/-{d.nodes_removed}, "
        f"edges +{d.rels_added}/-{d.rels_removed}"
    )


def tests_line(base: SuiteRun, cand: SuiteRun, new: list[str]) -> str:
    line = f"{cand.selected} test files, {cand.passed} passed, {len(new)} new failures"
    if base.failed:
        line += f" ({len(base.failed)} already failing on base)"
    return line


def evidence(f: Findings) -> list[str]:
    lines: list[str] = []
    if f.reach:
        lines.append(f.reach)
    if f.baseline_tests is not None and f.candidate_tests is not None:
        lines.append(tests_line(f.baseline_tests, f.candidate_tests, f.new_failures))
        lines += [f"failing: {t}" for t in f.new_failures[: vc.FAILURE_SAMPLE]]
    if f.tool_regression:
        lines.append(f.tool_regression)
    for d in f.deltas:
        lines.append(delta_line(d))
        lines += d.examples
    if f.comparison is not None:
        lines.append(bench_line(f.comparison))
    return lines


def _breakage(f: Findings) -> str | None:
    if f.new_failures:
        return f"breaks {len(f.new_failures)} tests"
    if f.tool_regression:
        return f"regresses {f.item.package}'s own gate"
    return None


def decide_upgrade(f: Findings) -> Decision:
    """Verdict for moving a dependency to its newest release.

    What decides urgency is `in_range`: when cgr's published range already
    admits the release, every fresh install gets it today, so a break or a
    changed graph there is something to act on, not a future upgrade.
    Outside the range the same result only says the cap still holds.
    """
    it = f.item
    move = f"{it.locked} -> {it.latest}"
    ev = evidence(f)
    if f.untestable:
        return Decision(vc.Verdict.WATCHING, f"{move}: not run, {f.untestable}", ev)
    if f.error:
        return Decision(
            vc.Verdict.WATCHING,
            f"{move}: the resolver cannot take it yet",
            [*ev, *f.error.splitlines()[-3:]],
        )
    broken = _breakage(f)
    if broken:
        if it.in_range:
            return Decision(
                vc.Verdict.ACT,
                f"{move}: fresh installs resolve {it.latest} today and it {broken}",
                ev,
            )
        return Decision(
            vc.Verdict.WATCHING,
            f"{move}: still {broken}; `{it.requirement}` keeps it out",
            ev,
        )
    if f.deltas:
        changed = ", ".join(d.corpus for d in f.deltas)
        if it.in_range:
            return Decision(
                vc.Verdict.ACT,
                f"{move}: fresh installs build a different graph ({changed})",
                ev,
            )
        return Decision(
            vc.Verdict.WATCHING, f"{move}: changes the graph ({changed})", ev
        )
    c = f.comparison
    if c is not None and c.speed == vc.Speed.SLOWER:
        verdict = vc.Verdict.ACT if it.in_range else vc.Verdict.WATCHING
        return Decision(verdict, f"{move}: builds {_pct(c.relative)} slower", ev)
    faster = (
        f", builds {_pct(c.relative)}"
        if c is not None and c.speed == vc.Speed.FASTER
        else ""
    )
    if f.widened:
        return Decision(
            vc.Verdict.READY,
            f"{move}: `{it.requirement}` can widen to `{f.widened}`, green, graphs identical{faster}",
            ev,
        )
    return Decision(
        vc.Verdict.READY, f"{move}: lock bump green, graphs identical{faster}", ev
    )


def decide_adopt(f: Findings, idea: str) -> Decision:
    """Verdict for a code change an agent made to use something new.

    The bar is the autoresearch one: the graph must come out byte-identical
    and the speedup must clear the noise floor, or the attempt is dropped
    with its reason, however plausible the idea.
    """
    ev = evidence(f)
    if f.frozen:
        return Decision(
            vc.Verdict.DISCARDED,
            f"{idea}: touched frozen paths {', '.join(f.frozen)}",
            ev,
        )
    broken = _breakage(f)
    if broken:
        return Decision(vc.Verdict.DISCARDED, f"{idea}: {broken}", ev)
    if f.deltas:
        return Decision(vc.Verdict.DISCARDED, f"{idea}: changes the graph", ev)
    c = f.comparison
    if c is None:
        return Decision(vc.Verdict.DISCARDED, f"{idea}: not measured", ev)
    if c.rss_ratio > 1 + vc.RSS_GUARD_FRACTION:
        return Decision(
            vc.Verdict.DISCARDED,
            f"{idea}: peak RSS x{c.rss_ratio:.2f}, over the guard",
            ev,
        )
    if c.speed == vc.Speed.FASTER:
        return Decision(
            vc.Verdict.READY, f"{idea}: builds {_pct(c.relative)}, graphs identical", ev
        )
    return Decision(vc.Verdict.DISCARDED, f"{idea}: {c.speed}", ev)
