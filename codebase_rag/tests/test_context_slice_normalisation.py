# context(target, budget_tokens) (issue #1536): the excerpt helpers on their
# own -- line-ending normalisation, the doc lookup key and a target missing
# from the graph. Split from test_context_slice.py, which drives the whole
# slice over an indexed fixture.

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs


# Excerpts are read from disk and trimmed with .strip("\n") / .rstrip("\n"),
# which leaves the b"\r" of a CRLF file attached to every line. The slice then
# reported `def scale(a: int) -> int:\r`, so every Windows unit job failed here
# while Linux and macOS stayed green -- no fixture in this suite used CRLF.
# Drive the helpers directly on both endings; the LF case is the control that
# proves the assertion is not vacuous.
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_excerpts_carry_no_carriage_return(tmp_path: Path, newline: str) -> None:
    from codebase_rag.context_slice import _header, _line, _lines

    body = newline.join(["def scale(a: int) -> int:", "    return a * 2", ""])
    (tmp_path / "mod.py").write_bytes(body.encode(cs.ENCODING_UTF8))

    one = _line(tmp_path, "mod.py", 1)
    many = _lines(tmp_path, "mod.py", 1, 2)
    head = _header(tmp_path, "mod.py", 1, 2)

    # Positive equality, not `"\r" not in ...`: the weak form is satisfied by
    # any implementation that removes the CR by any means, including the
    # swapped ordering `.replace("\r", "\n").replace("\r\n", "\n")` -- which
    # doubles every line break and is exactly the mistake `_normalise` had to
    # avoid. Pin the whole text so a wrong fold is visible.
    assert one == "def scale(a: int) -> int:", repr(one)
    assert many == "def scale(a: int) -> int:\n    return a * 2", repr(many)
    # `_header` returns the definition's first line, identical under either
    # ending once the fold is correct. Pin it exactly, for the same reason as
    # the two above: a presence check passes for any CR removal at all.
    assert head == "def scale(a: int) -> int:", repr(head)


# The disk path above is the FALLBACK. `_target_piece` prefers
# `definition["source"]`, which `graph_query.definition` fills by calling
# `extract_source_lines` itself -- the same disk reader, but reached without
# going through `_lines`. Normalising only `_lines` therefore left the PREFERRED
# path broken, and CI kept failing on Windows while this suite was green.
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_graph_stored_source_is_normalised(newline: str) -> None:
    from codebase_rag.context_slice import _target_piece

    stored = newline.join(["def scale(a: int) -> int:", "    return a * 2", ""])
    piece = _target_piece(
        {  # type: ignore[arg-type]
            "qualified_name": "p.m.scale",
            "path": "m.py",
            "start_line": 1,
            "end_line": 2,
            "source": stored,
        },
        None,
    )

    assert piece.source == "def scale(a: int) -> int:\n    return a * 2", repr(
        piece.source
    )


# A lone CR (old-Mac endings, and what a truncated CRLF write leaves behind) is
# the only input that separates a correct fold from `.replace("\r", "")`. Both
# satisfy every CRLF assertion above, because on CRLF input they are identical;
# they differ only here, where dropping the CR joins two lines into one. Without
# this case the second `.replace` in `_normalise` is untested.
def test_a_lone_carriage_return_becomes_a_newline(tmp_path: Path) -> None:
    from codebase_rag.context_slice import _lines

    (tmp_path / "mac.py").write_bytes(b"def scale(a: int) -> int:\r    return a * 2\r")

    assert (
        _lines(tmp_path, "mac.py", 1, 2)
        == "def scale(a: int) -> int:\n    return a * 2"
    )


# `_test_pieces` is the third `_normalise` call site and the only one no CRLF
# test reached: the fixture writes via `Path.write_text`, which emits LF, so
# reverting that one call left the suite green. Drive the contract it depends
# on -- `graph_query.definition` reading a CRLF file off disk -- so the piece
# built from it is pinned to the folded text.
def test_definition_source_from_a_crlf_file_is_normalised(tmp_path: Path) -> None:
    from codebase_rag.context_slice import _normalise
    from codebase_rag.utils.source_extraction import extract_source_lines

    (tmp_path / "t.py").write_bytes(b"def test_run():\r\n    assert run() == 6\r\n")
    raw = extract_source_lines(tmp_path / "t.py", 1, 2)

    # The reader deliberately preserves the file's bytes; the fold is the
    # caller's job, which is precisely why every consumer needs `_normalise`.
    assert "\r" in (raw or ""), repr(raw)
    assert (
        _normalise(raw or "").rstrip("\n") == "def test_run():\n    assert run() == 6"
    ), repr(raw)


# The test above composes `extract_source_lines` and `_normalise` by hand, which
# proves the pair folds correctly but never enters `_test_pieces` -- so deleting
# that call site's `_normalise` left the file green. Drive the function itself,
# with the graph reads stubbed, so the third call site is actually pinned.
def test_test_pieces_normalises_the_source_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag import context_slice as cs_mod
    from codebase_rag.utils.source_extraction import extract_source_lines

    (tmp_path / "test_app.py").write_bytes(
        b"def test_run():\r\n    assert run() == 6\r\n"
    )

    class _Reach:
        @staticmethod
        def build(_fetch: object, _project: str) -> _Reach:
            return _Reach()

        @staticmethod
        def tests_reaching(_qn: str) -> list[dict[str, object]]:
            return [
                {
                    "qualified_name": "p.test_app.test_run",
                    "path": "test_app.py",
                    "depth": 0,
                    "through": "p.app.run",
                }
            ]

    def _definition(
        _fetch: object, _project: str, _qn: str, _root: Path | None
    ) -> dict[str, object]:
        return {
            "qualified_name": "p.test_app.test_run",
            "path": "test_app.py",
            "start_line": 1,
            "end_line": 2,
            "source": extract_source_lines(tmp_path / "test_app.py", 1, 2),
        }

    monkeypatch.setattr(cs_mod, "ReachIndex", _Reach)
    monkeypatch.setattr(cs_mod.graph_query, "definition", _definition)

    pieces = cs_mod._test_pieces(lambda *a, **k: [], "p", "p.app.run", tmp_path)

    assert len(pieces) == 1, repr(pieces)
    assert pieces[0].source == "def test_run():\n    assert run() == 6", repr(
        pieces[0].source
    )


# The indexer writes absolute_path through `cached_resolve_posix`, so the lookup
# key must be POSIX too. Building it with `str(...resolve())` matched on Linux and
# macOS and emitted backslashes on Windows, so the doc query found nothing there,
# no CONTEXT_WHY_DOC candidate was built, and the grouping raised KeyError.
#
# This bug CANNOT be caught behaviourally on POSIX. `WindowsPath` cannot be
# instantiated here, and for any `PosixPath` `str(p.resolve())` and
# `p.resolve().as_posix()` are equal by construction -- so no input makes the two
# implementations disagree, and any behavioural test is satisfied by both. Two
# attempts at one (comparing the key against the helper; patching the helper the
# old code never calls) both passed against the reverted implementation.
#
# So assert the SOURCE instead: `_doc_pieces` must build its key with the same
# helper the writer uses. A static check is weaker than a behavioural one, but it
# is the only form that can fail on the machines this suite actually runs on, and
# it fails immediately if someone reintroduces a hand-built key.
def test_doc_lookup_key_is_built_with_the_writers_helper() -> None:
    import inspect

    from codebase_rag import context_slice as cs_mod

    source = inspect.getsource(cs_mod._doc_pieces)

    # Match on the two facts rather than on an exact line, so a behaviour-
    # preserving refactor (renaming the local, inlining the call) does not fail
    # this. Both halves are needed: the first alone passes if a stray
    # `str(...resolve())` is reintroduced alongside the helper, the second alone
    # passes if the key is built some third way that is also wrong on Windows.
    assert "cached_resolve_posix(repo_root / path)" in source, (
        "_doc_pieces must build its lookup key with cached_resolve_posix, the "
        "helper the indexer writes absolute_path with"
    )
    assert "str((repo_root / path).resolve())" not in source, (
        "_doc_pieces must not hand-build the lookup key: str(...resolve()) is "
        "backslash-separated on Windows and matches no stored absolute_path"
    )


# `graph_query.definition` returns a row with `start_line=None` when the symbol
# is not in the graph (graph_query.py:213). `_target_piece` defaulted that to 1
# while its sibling `_test_pieces` defaulted to 0, so the same missing value
# produced an empty excerpt in one place and LINE 1 OF THE FILE in the other --
# an unrelated import presented as the definition's body, with no error. The
# sentinel one field up from the one the fix was about, unpinned because the
# fix was about the other one.
def test_a_target_missing_from_the_graph_yields_no_excerpt(tmp_path: Path) -> None:
    from codebase_rag.context_slice import _target_piece

    (tmp_path / "m.py").write_text("import os\ndef real():\n    return 1\n")

    piece = _target_piece(
        {  # type: ignore[arg-type]
            "label": None,
            "qualified_name": "p.m.missing",
            "path": "m.py",
            "start_line": None,
            "end_line": None,
            "name": None,
            "docstring": None,
            "source": None,
            "found": False,
        },
        tmp_path,
    )

    assert piece.source == "", repr(piece.source)
    assert "import os" not in piece.source, repr(piece.source)
