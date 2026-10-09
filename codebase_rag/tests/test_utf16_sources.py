"""UTF-16 and UTF-32 sources with a BOM are parsed as the text they hold.

Only Python sources declaring an encoding were transcoded before parsing.
Any other UTF-16 file reached tree-sitter as NUL-interleaved bytes read as
UTF-8, which yields no declarations: a UTF-16 `.cs`, `.java` or `.cpp` was
indexed as an empty module, silently (issue #3153).
"""

from __future__ import annotations

import codecs
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_nodes,
    get_relationships,
)
from codebase_rag.utils.source_encoding import grammar_bytes
from codebase_rag.utils.source_extraction import extract_source_lines

_CS = (
    "namespace Acme\n{\n    public static class Util\n    {\n"
    "        public static int Helper() => 1;\n"
    "        public static int Use() => Helper();\n    }\n}\n"
)
_ENCODINGS = {
    "utf16le": lambda text: text.encode("utf-16"),
    "utf16be": lambda text: codecs.BOM_UTF16_BE + text.encode("utf-16-be"),
    "utf32": lambda text: text.encode("utf-32"),
}


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> MagicMock:
    root = tmp_path_factory.mktemp("u16") / "u16mix"
    (root / "p").mkdir(parents=True)
    (root / "Plain.cs").write_text(_CS, encoding="utf-8")
    for name, encode in _ENCODINGS.items():
        (root / f"{name}.cs").write_bytes(encode(_CS.replace("Util", f"W{name}")))
    (root / "p" / "Wide.java").write_bytes(
        "package p;\n\npublic class Wide {\n    static int helper() { return 1; }\n}\n".encode(
            "utf-16"
        )
    )
    (root / "p" / "Narrow.java").write_text(
        "package p;\n\npublic class Narrow {\n"
        "    static int run() { return Wide.helper(); }\n}\n",
        encoding="utf-8",
    )
    (root / "wide.cpp").write_bytes(
        "int twice(int x) { return 2 * x; }\nint use() { return twice(3); }\n".encode(
            "utf-16"
        )
    )
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="c_sharp")
    return mock


def _short(qn: object) -> str:
    return str(qn).split(".", 1)[1]


def _calls(indexed: MagicMock) -> set[tuple[str, str]]:
    return {
        (_short(c.args[0][2]), _short(c.args[2][2]))
        for c in get_relationships(indexed, cs.RelationshipType.CALLS)
    }


@pytest.mark.parametrize("encoding", sorted(_ENCODINGS))
def test_a_wide_csharp_file_indexes_like_its_utf8_copy(
    indexed: MagicMock, encoding: str
) -> None:
    cls = f"{encoding}.Acme.W{encoding}"
    methods = {
        _short(c.args[1][cs.KEY_QUALIFIED_NAME]) for c in get_nodes(indexed, "Method")
    }
    assert {f"{cls}.Helper", f"{cls}.Use"} <= methods, methods
    assert (f"{cls}.Use", f"{cls}.Helper") in _calls(indexed)
    lines = {
        _short(c.args[1][cs.KEY_QUALIFIED_NAME]): c.args[1].get(cs.KEY_START_LINE)
        for c in get_nodes(indexed, "Method")
    }
    assert lines[f"{cls}.Helper"] == lines["Plain.Acme.Util.Helper"] == 5


def test_wide_java_and_cpp_files_index_too(indexed: MagicMock) -> None:
    calls = _calls(indexed)
    assert ("p.Narrow.Narrow.run()", "p.Wide.Wide.helper()") in calls, calls
    assert ("wide.use", "wide.twice") in calls, calls


def test_a_snippet_of_a_wide_file_reads_its_text(tmp_path: Path) -> None:
    path = tmp_path / "Wide.cs"
    path.write_bytes(_CS.encode("utf-16"))
    assert (
        extract_source_lines(path, 5, 6)
        == "public static int Helper() => 1;\n        public static int Use() => Helper();"
    )


def test_other_files_reach_the_grammar_unchanged(tmp_path: Path) -> None:
    # Negatives: UTF-8 with or without a BOM is already what the grammar
    # reads (the same object comes back), and a malformed wide file falls back
    # to the raw bytes rather than failing the run.
    plain = _CS.encode("utf-8")
    bom = codecs.BOM_UTF8 + plain
    for source in (plain, bom):
        assert grammar_bytes(source, cs.SupportedLanguage.CSHARP, tmp_path) is source
    truncated = _CS.encode("utf-16")[:-1]
    assert grammar_bytes(truncated, cs.SupportedLanguage.CSHARP, tmp_path) is truncated
