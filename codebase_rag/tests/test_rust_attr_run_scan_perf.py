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


def test_unbroken_attribute_run_scans_linearly() -> None:
    small = _scan_time(_attr_run(1000))
    large = _scan_time(_attr_run(4000))
    # Quadratic behaviour makes 4x the input ~16x the time; linear stays
    # around 4x. The bound leaves generous headroom for loaded runners.
    assert large < max(small * 10, 0.05), (small, large)
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
