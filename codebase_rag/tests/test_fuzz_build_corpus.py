"""`fuzz/build_corpus.py` writes the seed corpora the fuzz harnesses start
from. The committed corpus is only reproducible if the builder still writes
exactly those bytes, and its harness cross-check only protects the seeds'
file indexes if it still fires when the harness fixture drifts."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parser_loader import load_parsers
from fuzz import build_corpus as builder

COMMITTED = Path(builder.__file__).resolve().parent / "corpus"


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "corpus"
    monkeypatch.setattr(builder, "CORPUS", out)
    return out


def test_main_rebuilds_the_committed_seeds_byte_for_byte(
    corpus: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if missing := set(LANGUAGE_SPECS) - set(load_parsers()[0]):
        # The Python selector byte is Python's place among the installed
        # grammars, and the committed seeds were built with all of them.
        pytest.skip(f"{sorted(missing)} parsers not available")

    builder.main()

    parse = len(builder.SOURCES) + len(builder.EDGE_CASES)
    parse += len(builder.BYTE_EDGE_CASES)
    assert capsys.readouterr().out.split() == [
        "fuzz_parse_source:",
        str(parse),
        "seeds",
        "fuzz_shell_command:",
        "15",
        "seeds",
        "fuzz_incremental_update:",
        "9",
        "seeds",
    ]
    written = sorted(p.relative_to(corpus) for p in corpus.rglob("*.bin"))
    assert len(written) == parse + 15 + 9
    stale = [
        str(rel)
        for rel in written
        if not (COMMITTED / rel).is_file()
        or (COMMITTED / rel).read_bytes() != (corpus / rel).read_bytes()
    ]
    assert stale == [], "regenerate with `uv run python fuzz/build_corpus.py`"


def test_each_language_seed_ends_with_its_selector(corpus: Path) -> None:
    builder.build_parse_corpus()
    for index, name in enumerate(sorted(builder.SOURCES)):
        seed = (corpus / "fuzz_parse_source" / f"{name}.bin").read_bytes()
        # The harness reads the selector off the BACK of the buffer.
        assert seed[-1] == index, name
        assert seed[:-1].decode() == builder.SOURCES[name]


def test_the_invalid_utf8_seeds_select_the_python_grammar(corpus: Path) -> None:
    builder.build_parse_corpus()
    languages = tuple(sorted(load_parsers()[0]))
    python = languages.index(cs.SupportedLanguage.PYTHON)
    assert builder._python_index() == python
    for name, raw in builder.BYTE_EDGE_CASES.items():
        seed = (corpus / "fuzz_parse_source" / f"edge_{name}.bin").read_bytes()
        assert seed == raw + bytes([python]), name


def test_shell_seeds_lead_with_an_ascii_string_spec(corpus: Path) -> None:
    assert builder.build_shell_corpus() == 15
    seed = (corpus / "fuzz_shell_command" / "safe_git.bin").read_bytes()
    assert seed == b"\x01git status --short"


def test_an_edit_plan_is_written_in_reverse_consumption_order(corpus: Path) -> None:
    assert builder.build_incremental_corpus() == 9
    seed = (corpus / "fuzz_incremental_update" / "multi_edit.bin").read_bytes()
    util = builder.EDITABLE.index("pkg/util.py")
    app = builder.EDITABLE.index("pkg/app.py")
    main_py = builder.EDITABLE.index("main.py")
    # shape 1, three edits (written as 3 - 1), then (file, kind) per edit.
    consumed = [1, 2, util, 0, app, 4, main_py, 6]
    assert seed == b"\x01blob" + bytes(reversed(consumed))


def test_the_editable_list_matches_the_harness_fixture() -> None:
    builder._check_editable_matches_harness()


def test_a_drifted_harness_fixture_stops_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fuzz_incremental_update.py").write_text(
        'FIXTURE = {\n    "main.py": "",\n    "pkg/new.py": "",\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(builder, "__file__", str(tmp_path / "build_corpus.py"))
    with pytest.raises(SystemExit, match="no longer matches EDITABLE"):
        builder._check_editable_matches_harness()
