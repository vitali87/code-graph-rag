"""One undecodable byte must not delete a whole file's definitions.

`node.text` is a slice of the raw file bytes, which are never validated as
UTF-8. The name extractors in `language_spec` decoded them strictly, so a
single bad byte raised `UnicodeDecodeError`; the per-file handler in
`graph_updater` caught it, logged `INCREMENTAL_FILE_FAILED` and abandoned the
file. The result was total and silent: every definition in that file vanished
from the graph, including pure-ASCII ones nowhere near the bad byte, with
nothing in the graph to say the file had been skipped (issue #1797).

The bytes sit in the file CONTENT, not in a filename, so these fixtures are
portable to the Windows runners: NTFS restricts the characters a path may
contain, not the bytes a file may hold.

What each test pins:

* `test_a_bad_byte_does_not_drop_the_rest_of_the_file` is the regression. It
  asserts on the UNTOUCHED sibling function, which is the definition the
  defect actually lost -- asserting on the damaged name instead would pass
  against a fix that dropped the file and reported nothing.
* `test_the_damaged_name_survives_as_a_replacement_char` pins the choice of
  `errors="replace"` over dropping the node, so a later switch to a silent
  skip is a red test rather than a quiet regression to invisible loss.
* `test_a_clean_file_is_unchanged_by_the_fix` is the control: it fails if the
  replacement decode has altered ordinary well-formed input.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
    get_relationships,
)
from codebase_rag.types_defs import NodeType, RelationshipType

# The bad bytes precede the dot, so they land inside the node
# `_generic_get_name` decodes AS A WHOLE. That is what makes the decode raise
# rather than merely truncate: with the bytes after the dot, tree-sitter
# splits them out of the identifier token and the decode quietly succeeds on
# a shortened name. Only this placement reproduces #1797's total loss.
_BAD = (
    b"function Greeter\xff\xff.greet(n) return 1 end\n"
    b"function other() return 2 end\n"
    b"function caller() return other() end\n"
)
_CLEAN = (
    b"function Greeter.greet(n) return 1 end\n"
    b"function other() return 2 end\n"
    b"function caller() return other() end\n"
)

_UNTOUCHED_SIBLING = "proj.a.other"


def _definitions(mock_ingestor: MagicMock) -> set[str]:
    """Every Function/Method qualified name written to the graph."""
    return get_node_names(mock_ingestor, NodeType.FUNCTION.value) | get_node_names(
        mock_ingestor, NodeType.METHOD.value
    )


def _index(tmp: Path, ingestor: MagicMock, payload: bytes) -> set[str]:
    project = tmp / "proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / "a.lua").write_bytes(payload)
    create_and_run_updater(project, ingestor, skip_if_missing=cs.SupportedLanguage.LUA)
    return _definitions(ingestor)


def test_a_bad_byte_does_not_drop_the_rest_of_the_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The regression. `other` is pure ASCII on its own line and has nothing to
    # do with the bad byte; before the fix it was lost with the whole file.
    assert _UNTOUCHED_SIBLING in _index(temp_repo, mock_ingestor, _BAD)


def test_the_damaged_name_survives_as_a_replacement_char(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The damaged definition is kept and visibly mangled, not silently
    # discarded: a reader sees U+FFFD and knows the source byte was invalid.
    damaged = {qn for qn in _index(temp_repo, mock_ingestor, _BAD) if "�" in qn}
    assert damaged, "the damaged definition was dropped instead of replaced"


def test_a_clean_file_is_unchanged_by_the_fix(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Control: well-formed input must decode exactly as before, with no
    # replacement characters anywhere.
    found = _index(temp_repo, mock_ingestor, _CLEAN)
    assert found == {
        "proj.a.Greeter.greet",
        _UNTOUCHED_SIBLING,
        "proj.a.caller",
    }


def test_the_call_pass_still_links_the_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The definition pass and the call pass decode names independently, so
    # fixing only `language_spec` left this pass raising and the file's CALLS
    # edges stranded. Without this, nothing in THIS module goes red for that
    # half of the fix -- only the conftest per-pass guard catches it, and a
    # guard living outside the test file is easy to silence by accident.
    _index(temp_repo, mock_ingestor, _BAD)
    edges = {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(mock_ingestor, RelationshipType.CALLS.value)
    }
    assert ("proj.a.caller", _UNTOUCHED_SIBLING) in edges
