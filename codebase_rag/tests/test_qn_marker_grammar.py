"""One grammar for the duplicate-qn marker, read in three ways (issue #2114).

`function_registry` appends `@<line>`, optionally `_<col>`, to a qn that is
already registered. Readers used to spell that grammar themselves; each now
calls `qn_markers`, and this table pins what every entry point answers, so
a fifth spelling, or a change to one of these, fails here rather than in
whichever reader drifted.
"""

from __future__ import annotations

import pytest

from codebase_rag.utils import qn_markers

# (input, end-anchored strip, strip from every segment, line of the trailing marker)
TABLE = [
    ("Box@12", "Box", "Box", 12),
    ("Box@12_5", "Box", "Box", 12),
    ("@event", "@event", "@event", None),
    ("@event@12", "@event", "@event", 12),
    ("@event@12_4", "@event", "@event", 12),
    ("@lock", "@lock", "@lock", None),
    # A marker on an INNER segment: a duplicate-suffixed outer type with a
    # nested type. Only the whole-path form strips it.
    ("proj.Lib@7.Helper", "proj.Lib@7.Helper", "proj.Lib.Helper", None),
    ("proj.Lib@7.Helper@12", "proj.Lib@7.Helper", "proj.Lib.Helper", 12),
    ("proj.@lock.Inner", "proj.@lock.Inner", "proj.@lock.Inner", None),
    ("plain", "plain", "plain", None),
]


@pytest.mark.parametrize(("name", "end", "every", "line"), TABLE)
def test_every_entry_point_reads_the_same_grammar(
    name: str, end: str, every: str, line: int | None
) -> None:
    assert qn_markers.strip_dup_marker(name) == end
    assert qn_markers.strip_all_markers(name) == every
    assert qn_markers.marker_line(name) == line


@pytest.mark.parametrize(("name", "end", "every", "line"), TABLE)
def test_the_readers_agree_with_the_table(
    name: str, end: str, every: str, line: int | None
) -> None:
    from codebase_rag.duplicates import _qn_normalized
    from codebase_rag.trace.resolution import _natural_qualified_name

    assert _natural_qualified_name(name) == end
    assert _qn_normalized(name) == every
