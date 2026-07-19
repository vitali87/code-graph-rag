from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_nodes, run_updater


def _function_qns(ingestor: MagicMock) -> set[str]:
    return {c.args[1]["qualified_name"] for c in get_nodes(ingestor, "Function")}


def test_rust_src_bin_binaries_are_ingested(temp_repo: Path) -> None:
    # (H) Cargo's standard multi-binary layout puts additional binaries under
    # (H) src/bin/; the generic build-output ignore for `bin` directories must
    # (H) not swallow it (mini-redis's entire cli.rs/server.rs were invisible).
    root = temp_repo / "crate"
    (root / "src" / "bin").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "crate"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (root / "src" / "lib.rs").write_text(
        "pub fn shared() -> u32 { 1 }\n", encoding="utf-8"
    )
    (root / "src" / "bin" / "cli.rs").write_text(
        "fn main() { crate_lib_helper(); }\nfn crate_lib_helper() {}\n",
        encoding="utf-8",
    )

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing="rust")

    qns = _function_qns(ingestor)
    assert any(qn.endswith("src.bin.cli.main") for qn in qns), qns
    assert any(qn.endswith("src.bin.cli.crate_lib_helper") for qn in qns), qns


def test_non_src_bin_directories_stay_ignored(temp_repo: Path) -> None:
    # (H) Build-output bin/ trees (dotnet's <proj>/bin/Debug, a repo-root bin/)
    # (H) keep the existing ignore.
    root = temp_repo / "app"
    (root / "bin").mkdir(parents=True)
    (root / "proj" / "bin" / "Debug").mkdir(parents=True)
    (root / "bin" / "tool.rs").write_text("fn ignored_root() {}\n", encoding="utf-8")
    (root / "proj" / "bin" / "Debug" / "gen.rs").write_text(
        "fn ignored_output() {}\n", encoding="utf-8"
    )
    (root / "lib.rs").write_text("pub fn kept() {}\n", encoding="utf-8")

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing="rust")

    qns = _function_qns(ingestor)
    assert any(qn.endswith("lib.kept") for qn in qns), qns
    assert not any("ignored_root" in qn for qn in qns), qns
    assert not any("ignored_output" in qn for qn in qns), qns
