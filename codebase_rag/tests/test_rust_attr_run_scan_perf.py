"""The Rust declaration-scan patterns must stay linear on an unbroken run of
attribute lines: the newline-crossing attribute group let every line of the
run restart a match attempt that consumed the rest of the run (issue #1089)."""

import time

from codebase_rag.parsers.import_processor import (
    _RS_ITEM_DECL_PATTERN,
    _RS_MOD_DECL_PATTERN,
    _RS_MOD_REDIRECT_PATTERN,
    _rs_entry_decls_of,
)


def _attr_run(n: int) -> str:
    return "#[allow(dead_code)]\n" * n


def _scan_time(source: str) -> float:
    start = time.perf_counter()
    _RS_MOD_DECL_PATTERN.findall(source)
    _RS_ITEM_DECL_PATTERN.findall(source)
    list(_RS_MOD_REDIRECT_PATTERN.finditer(source))
    return time.perf_counter() - start


def _best_scan_time(source: str, repeats: int = 5) -> float:
    # Take the FASTEST run rather than one sample. Scheduler noise only ever
    # ADDS time, so the minimum is the closest estimate of the real cost, and
    # one clean run among several is far likelier than a single clean sample
    # (issue #1382: a loaded macOS runner inflated one sample ~6x and failed).
    return min(_scan_time(source) for _ in range(repeats))


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
    # A deliberate second bound, and NOT the kind just removed. The ratio cannot
    # see a uniform slowdown: if some change made every scan 100x slower, 4x
    # input still costs 4x time and the ratio stays healthy while the scan is
    # unusable. This catches that. It survives where the old absolute bound did
    # not because of the margin: `large` measures ~17ms, so the ceiling sits
    # ~120x above it, against the ~6x inflation a loaded runner produced in
    # issue #1382.
    assert large < 2.0, large


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
