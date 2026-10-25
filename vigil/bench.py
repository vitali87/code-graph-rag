import math
import os
import statistics
import subprocess
from pathlib import Path

from . import constants as vc
from .measure import digest
from .types_defs import Comparison, GraphDelta, Measurement


def venv_python(worktree: Path) -> Path:
    return worktree / ".venv" / "bin" / "python"


def measure(
    worktree: Path, corpus: str, target: Path, dump: Path | None = None
) -> Measurement:
    """One build of `target` by the cgr checked out in `worktree`.

    The child runs that worktree's interpreter with the worktree first on
    the path, so the code measured is the candidate's, not the parent's.
    """
    cmd = [
        str(venv_python(worktree)),
        "-m",
        "vigil.measure",
        "--target",
        str(target),
        "--corpus",
        corpus,
    ]
    if dump is not None:
        cmd += ["--dump", str(dump)]
    env = {**os.environ, "PYTHONPATH": str(worktree)}
    done = subprocess.run(
        cmd,
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        encoding=vc.ENCODING,
        timeout=vc.MEASURE_TIMEOUT_S,
        check=True,
    )
    return Measurement.model_validate_json(done.stdout.strip().splitlines()[-1])


def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided sign test: how likely a split this lopsided is when
    each pair is a coin flip. Ties carry no direction and are dropped."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def compare(
    corpus: str,
    baseline: list[Measurement],
    candidate: list[Measurement],
) -> Comparison:
    """Judge paired runs: pair i was measured back to back.

    A candidate is faster or slower only when the pairs agree beyond chance
    (sign test) and the median paired change clears both noise floors. A
    win smaller than the jitter is labelled noise however consistent it
    looks, so a run of lucky timings can never be kept as an improvement.
    """
    base_ms = [m.build_ms for m in baseline]
    cand_ms = [m.build_ms for m in candidate]
    deltas = [c - b for b, c in zip(base_ms, cand_ms, strict=True)]
    wins = sum(d < 0 for d in deltas)
    losses = sum(d > 0 for d in deltas)
    median_delta = statistics.median(deltas)
    base_median = statistics.median(base_ms)
    floor = max(vc.NOISE_FLOOR_MS, vc.NOISE_FLOOR_FRACTION * base_median)
    p = sign_test_p(wins, losses)
    speed = vc.Speed.WITHIN_NOISE
    if p < vc.SIGN_TEST_ALPHA and abs(median_delta) > floor:
        speed = vc.Speed.FASTER if median_delta < 0 else vc.Speed.SLOWER
    rss_ratio = statistics.median(
        m.peak_rss_mib for m in candidate
    ) / statistics.median(m.peak_rss_mib for m in baseline)
    return Comparison(
        corpus=corpus,
        baseline_ms=base_ms,
        candidate_ms=cand_ms,
        median_delta_ms=round(median_delta, 1),
        relative=round(median_delta / base_median, 4),
        wins=wins,
        losses=losses,
        p_value=round(p, 4),
        speed=speed,
        rss_ratio=round(rss_ratio, 3),
    )


def interleaved(
    base: Path, cand: Path, corpus: str, target: Path, pairs: int = vc.BENCH_PAIRS
) -> Comparison:
    """Time baseline and candidate in alternating order, AB then BA.

    Alternating cancels drift on a shared machine (a neighbour's load, a
    thermal ramp) that would otherwise land on whichever side always runs
    second. Every measurement is a fresh interpreter.
    """
    baseline: list[Measurement] = []
    candidate: list[Measurement] = []
    for i in range(pairs):
        if i % 2 == 0:
            baseline.append(measure(base, corpus, target))
            candidate.append(measure(cand, corpus, target))
        else:
            candidate.append(measure(cand, corpus, target))
            baseline.append(measure(base, corpus, target))
    return compare(corpus, baseline, candidate)


def graph_delta(
    corpus: str, base_lines: list[str], cand_lines: list[str]
) -> GraphDelta:
    def split(lines: list[str]) -> tuple[set[str], set[str]]:
        cut = lines.index("--") if "--" in lines else len(lines)
        return set(lines[:cut]), set(lines[cut + 1 :])

    base_nodes, base_rels = split(base_lines)
    cand_nodes, cand_rels = split(cand_lines)
    added = sorted(cand_rels - base_rels)
    removed = sorted(base_rels - cand_rels)
    examples = [f"+ {r}" for r in added[: vc.DELTA_EXAMPLES]]
    examples += [f"- {r}" for r in removed[: vc.DELTA_EXAMPLES]]
    return GraphDelta(
        corpus=corpus,
        baseline_digest=digest(base_lines)[:12],
        candidate_digest=digest(cand_lines)[:12],
        nodes_added=len(cand_nodes - base_nodes),
        nodes_removed=len(base_nodes - cand_nodes),
        rels_added=len(added),
        rels_removed=len(removed),
        examples=examples,
    )
