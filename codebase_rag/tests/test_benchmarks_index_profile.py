"""The index profile summary reads a py-spy raw recording and charges each
sample to a stage, to cgr's innermost function and, for startup imports, to
the package imported. Pinned on a hand-written recording in py-spy's
collapsed format: one `;`-joined stack per line, then its sample count."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks import index_profile as profile

_ROOT = "python (pid 1);<module> (cli.py:1)"
_RUN = "run (codebase_rag/graph_updater.py:400)"
_IMPORT = "_find_and_load (<frozen importlib._bootstrap>:1360)"
_RECORDING = "\n".join(
    [
        f"{_ROOT};{_RUN};_process_files (codebase_rag/graph_updater.py:500);"
        "process_file (codebase_rag/parsers/definition_processor.py:481) 6",
        f"{_ROOT};{_RUN};_process_function_calls (codebase_rag/graph_updater.py:600);"
        "process_calls_in_file (codebase_rag/parsers/call_processor.py:441) 3",
        f"{_ROOT};{_IMPORT};<module> (codebase_rag/cli.py:1);{_IMPORT};"
        "<module> (pydantic/main.py:1) 1",
        f"{_ROOT};helper (tools/x.py:2) 2",
    ]
)


def test_a_sample_line_splits_into_its_frames_and_count(tmp_path: Path) -> None:
    path = tmp_path / "run.txt"
    path.write_text("proc;f (a.py:1);g (b.py:2) 7\n", encoding="utf-8")
    # The first frame names the process, not a function.
    assert list(profile._samples(str(path))) == [(["f (a.py:1)", "g (b.py:2)"], 7)]


@pytest.mark.parametrize(
    ("frames", "stage"),
    [
        (
            [_RUN, "_process_files (codebase_rag/graph_updater.py:500)"],
            "definitions pass",
        ),
        (["hash_file (codebase_rag/utils/fs.py:3)"], "file scan + hashing"),
        (["forward (torch/nn/module.py:9)"], "embeddings: model inference"),
        (
            ["flush (codebase_rag/services/graph_service.py:7)"],
            "graph store (ingest + SQLite)",
        ),
        ([_IMPORT], "imports / startup"),
        (["main (tools/x.py:1)"], "other"),
    ],
)
def test_each_stack_is_charged_to_its_stage(frames: list[str], stage: str) -> None:
    assert profile._stage(frames) == stage


def test_a_lazy_import_inside_a_pass_is_charged_to_the_pass() -> None:
    # Rules are tried in order, so the pass outranks the import it triggers.
    frames = ["_process_function_calls (codebase_rag/graph_updater.py:600)", _IMPORT]
    assert profile._stage(frames) == "calls pass"


def test_the_innermost_cgr_frame_names_the_function() -> None:
    frames = [
        _RUN,
        "process_file (/abs/checkout/codebase_rag/parsers/definition_processor.py:481)",
        "parse (tree_sitter/__init__.py:5)",
    ]
    assert (
        profile._own_function(frames) == "parsers/definition_processor.py:process_file"
    )
    assert profile._own_function(["parse (tree_sitter/__init__.py:5)"]) is None


def test_an_import_is_charged_to_the_outermost_foreign_package() -> None:
    frames = [
        _IMPORT,
        "<module> (codebase_rag/cli.py:1)",
        _IMPORT,
        "<module> (/venv/lib/python3.12/site-packages/qdrant_client/__init__.py:1)",
        _IMPORT,
        "<module> (/venv/lib/python3.12/site-packages/pydantic/main.py:1)",
    ]
    assert (
        profile._imported_package(["<module> (cli.py:1)", *frames]) == "qdrant_client"
    )


def test_an_import_of_cgr_alone_names_the_module_and_no_import_names_none() -> None:
    own = ["<module> (cli.py:1)", _IMPORT, "<module> (codebase_rag/cli.py:1)"]
    assert profile._imported_package(own) == "codebase_rag/cli.py"
    assert profile._imported_package(["<module> (cli.py:1)", _RUN]) is None


def test_main_prints_each_table_as_shares_of_all_samples(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "run.txt"
    path.write_text(_RECORDING + "\n", encoding="utf-8")

    profile.main(str(path))

    assert capsys.readouterr().out.splitlines() == [
        "12 samples",
        "-- stages",
        " 50.0%  definitions pass",
        " 25.0%  calls pass",
        " 16.7%  other",
        "  8.3%  imports / startup",
        "-- innermost cgr function",
        " 50.0%  parsers/definition_processor.py:process_file",
        " 25.0%  parsers/call_processor.py:process_calls_in_file",
        "  8.3%  cli.py:<module>",
        "-- imports by package",
        "  8.3%  pydantic",
    ]
