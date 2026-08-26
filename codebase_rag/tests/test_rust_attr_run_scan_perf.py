"""The Rust declaration-scan patterns must stay linear on an unbroken run of
attribute lines: the newline-crossing attribute group let every line of the
run restart a match attempt that consumed the rest of the run (issue #1089)."""

import os
import time

import pytest

from codebase_rag.parsers.import_processor import (
    _RS_ITEM_DECL_PATTERN,
    _RS_MOD_DECL_PATTERN,
    _RS_MOD_REDIRECT_PATTERN,
    _rs_entry_decls_of,
)


def _attr_run(n: int) -> str:
    return "#[allow(dead_code)]\n" * n


def _scan_time(source: str) -> float:
    # CPU time, not WALL clock. The distinction is the whole fix for #1473:
    # perf_counter includes time this process was descheduled, so a busy
    # runner inflates a sample without the scanner doing any more work.
    # process_time counts only cycles actually spent here, so contention
    # cannot manufacture a slowdown that never happened.
    #
    # Measured on this repo, ratio of 8000-line to 2000-line input across
    # five rounds, with every core saturated by competing processes:
    #
    #     wall clock: spread 0.13 idle -> 0.30 under load
    #     CPU time:   spread 0.07 idle -> 0.10 under load
    #
    # Wall clock more than doubles its spread under exactly the condition
    # that made this test flaky under `pytest -n auto`; CPU time barely
    # moves.
    start = time.process_time()
    _RS_MOD_DECL_PATTERN.findall(source)
    _RS_ITEM_DECL_PATTERN.findall(source)
    list(_RS_MOD_REDIRECT_PATTERN.finditer(source))
    return time.process_time() - start


def _best_scan_time(source: str, repeats: int = 5) -> float:
    # Take the FASTEST run rather than one sample. Noise only ever ADDS time,
    # so the minimum is the closest estimate of the real cost, and one clean
    # run among several is far likelier than a single clean sample (issue
    # #1382: a loaded macOS runner inflated one sample ~6x and failed).
    #
    # Kept alongside the switch to CPU time rather than replaced by it: the
    # two address different noise. Best-of-N handles a single bad sample;
    # CPU time handles sustained contention, which best-of-N cannot, because
    # under sustained load EVERY sample is delayed and the minimum is still
    # not an uncontended measurement (issue #1473).
    return min(_scan_time(source) for _ in range(repeats))


# Skipped under xdist (issue #1473). A timing assertion cannot be made
# reliable on a shared parallel runner, and this file is the evidence: it has
# now been hardened FOUR times -- best-of-N sampling (#1089),
# ratio-not-absolute (#1382), CPU time instead of wall clock, and an
# xdist_group marker -- and each round reduced the flake rate without
# reaching zero.
#
# The marker was the instructive failure. `xdist_group` groups marked tests
# with EACH OTHER; it does not reserve a worker. Probed directly, two
# workers, three runs: the grouped test landed on gw0 every time and an
# unmarked test landed on gw0 with it every time. The integration suite uses
# the same marker correctly, for the opposite purpose -- serialising its own
# tests onto one worker so they do not race a shared container.
#
# So there is no marker that buys isolation, and `re` exposes no step counter
# to measure work deterministically (match COUNTS are identical for the
# linear and quadratic shapes; the cost is backtracking). What remains is to
# stop asserting timing where the measurement is not trustworthy, and keep
# asserting it where it is.
#
# CPU time is retained because it is strictly better than wall clock when the
# test does run -- measured spread under full load, 0.30 wall vs 0.10 CPU.
@pytest.mark.skipif(
    os.environ.get("PYTEST_XDIST_WORKER") is not None,
    reason=(
        "timing assertion is not trustworthy on a shared xdist worker; "
        "run this file serially to exercise it (issue #1473)"
    ),
)
def test_unbroken_attribute_run_scans_linearly() -> None:
    # Compare a RATIO, never an absolute duration: the machine's speed cancels
    # out, which an absolute ceiling cannot do. The previous absolute floor was
    # the branch that actually fired, and 50ms of wall clock on a shared runner
    # says nothing about complexity.
    small = _best_scan_time(_attr_run(2000))
    large = _best_scan_time(_attr_run(8000))
    # 4x the input costs ~4x linear and ~16x quadratic. Measured: linear sits
    # at 3.6-4.9x and the #1089 pattern this guards against measures 16.7x, so
    # the bound separates them with room on both sides.
    assert large < small * 10, (small, large)
    # No absolute ceiling here on purpose. One used to sit below this line to
    # catch a UNIFORM slowdown, which the ratio cannot see: if every scan got
    # 100x slower, 4x input would still cost 4x time. It was removed anyway,
    # because a sustained-contention runner can delay EVERY sample, so even a
    # best-of-N minimum is not an uncontended measurement, and no wall-clock
    # number distinguishes "the scanner regressed" from "the runner is busy".
    # That coverage belongs in a controlled benchmark environment rather than a
    # unit test on shared CI (issue #1382).


def test_attribute_block_above_declaration_still_redirects() -> None:
    decls = _rs_entry_decls_of(
        '#[allow(dead_code)]\n#[path = "alt.rs"]\n\npub mod sub;\n'
    )
    assert decls.redirects == {"sub": "alt.rs"}


def test_same_line_attribute_still_redirects() -> None:
    decls = _rs_entry_decls_of('#[path = "alt.rs"] mod sub;\n')
    assert decls.redirects == {"sub": "alt.rs"}


def test_cfg_twin_disagreement_stays_ambiguous() -> None:
    decls = _rs_entry_decls_of(
        '#[cfg(unix)]\n#[path = "unix.rs"]\nmod platform;\n'
        '#[cfg(windows)]\n#[path = "windows.rs"]\nmod platform;\n'
    )
    assert "platform" not in decls.redirects


def test_long_generated_attribute_run_before_item_still_matches() -> None:
    source = _attr_run(300) + "pub mod tail;\n"
    decls = _rs_entry_decls_of(source)
    assert "tail" in decls.mods
