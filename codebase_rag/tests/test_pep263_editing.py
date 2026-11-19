"""The editing and structural paths read a Python file as CPython does (#3334).

Since #2445 the indexer honours a PEP 263 declaration, but the editing paths
still read every file as UTF-8: `cgr rename` crashed with `UnicodeDecodeError`
on a Latin-1 source, MCP `surgical_replace_code` answered "not found", and
structural search/replace skipped the file silently. Every reader now decodes
with the declaration the indexer already honours — and writes an edit back in
the same encoding.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag.editing.rename import Renamer, RenameSite
from codebase_rag.tools.ast_grep_service import AstGrepService
from codebase_rag.tools.file_editor import FileEditor
from codebase_rag.utils.source_encoding import source_codec

CALC = (
    b"# -*- coding: latin-1 -*-\n"
    b'"""M\xf3dulo de c\xe1lculo."""\n'
    b"\n"
    b"\n"
    b"def precio(x):\n"
    b"    return x * 2\n"
    b"\n"
    b"\n"
    b"def total():\n"
    b'    return precio(3), "se\xf1or"\n'
)


def _calc_file(tmp_path: Path) -> Path:
    path = tmp_path / "calc.py"
    path.write_bytes(CALC)
    return path


class TestSourceCodec:
    def test_a_latin_1_declaration_names_the_codec(self, tmp_path):
        assert source_codec(_calc_file(tmp_path)) == "latin-1"

    def test_undeclared_is_utf8(self, tmp_path):
        plain = tmp_path / "plain.py"
        plain.write_bytes(b"x = 1\n")
        assert source_codec(plain) == "utf-8"


class TestFileEditorSurgicalReplace:
    def _editor(self, tmp_path: Path) -> FileEditor:
        return FileEditor(project_root=tmp_path)

    def test_a_latin_1_source_is_read_and_written_in_its_encoding(self, tmp_path):
        _calc_file(tmp_path)
        editor = self._editor(tmp_path)
        result = editor.apply_code_block(
            "calc.py",
            "def precio(x):\n    return x * 2",
            "def price(x):\n    return x * 2",
        )

        assert result.success, result.error_message
        on_disk = (tmp_path / "calc.py").read_bytes()
        # Written back as latin-1, not mojibake and not UTF-8.
        assert b"def price(x):" in on_disk

        assert b"\xf1" in on_disk  # the 'ñ' of 'señor' is still one latin-1 byte
        assert b"\xc3\xb1" not in on_disk  # no UTF-8 re-encoding of the rest
        assert source_codec(tmp_path / "calc.py") == "latin-1"

    def test_the_declaration_line_survives_the_edit(self, tmp_path):
        _calc_file(tmp_path)
        editor = self._editor(tmp_path)
        editor.apply_code_block(
            "calc.py",
            'def total():\n    return precio(3), "señor"',
            'def total():\n    return price(3), "señor"',
        )
        text = (tmp_path / "calc.py").read_bytes().decode("latin-1")
        assert text.startswith("# -*- coding: latin-1 -*-")
        assert "price(3)" in text and "precio(3)" not in text

    def test_unencodable_surgical_replace_preserves_original_file(self, tmp_path):
        _calc_file(tmp_path)
        editor = self._editor(tmp_path)
        original_bytes = (tmp_path / "calc.py").read_bytes()
        # \u2014 (em-dash) cannot be encoded in latin-1
        result = editor.apply_code_block(
            "calc.py", "def precio(x):", "def precio_\u2014(x):"
        )
        assert not result.success
        assert (tmp_path / "calc.py").read_bytes() == original_bytes
        assert not (tmp_path / ".calc.py.tmp").exists()


class TestRenamerRestorationChecks:
    def _renamer(self, tmp_path: Path) -> Renamer:
        renamer = object.__new__(Renamer)
        renamer.repo_root = tmp_path
        return renamer

    def test_all_rewrites_check_reads_a_latin_1_file(self, tmp_path):
        _calc_file(tmp_path)
        renamer = self._renamer(tmp_path)
        # Must not raise UnicodeDecodeError on the 0xf3 in the docstring.
        answer = renamer._all_rewrites_are_undone("calc.py", "precio", [(11, 0, 20)])
        # The old name is at the offset, but no __all__ block holds it.
        assert answer is False

    def test_import_check_reads_a_latin_1_file(self, tmp_path):
        _calc_file(tmp_path)
        renamer = self._renamer(tmp_path)
        site = RenameSite(
            kind="import", path="calc.py", line=10, col=4, owner="", resolution=None
        )
        answer = renamer._import_names_the_old_name(site, "precio")
        # No import on the line: the documented word-match fallback finds
        # the old name there. The point is that it read the file without
        # UnicodeDecodeError on the latin-1 bytes.
        assert answer is True

    def test_file_has_old_name_at_reads_a_latin_1_file(self, tmp_path):
        _calc_file(tmp_path)
        renamer = self._renamer(tmp_path)
        site = RenameSite(
            kind="call", path="calc.py", line=10, col=11, owner="", resolution=None
        )
        answer = renamer._file_has_old_name_at("calc.py", [site], b"precio")
        # 'precio(' is at the recorded column, so the old name is there.
        assert answer is True


class TestAstGrepReadsDeclaredEncoding:
    def test_read_decodes_a_latin_1_source(self, tmp_path):
        path = _calc_file(tmp_path)
        text = AstGrepService._read(path)
        assert text is not None
        assert "Módulo de cálculo" in text
        assert 'return precio(3), "señor"' in text

    def test_read_still_skips_undecodable_files(self, tmp_path):
        broken = tmp_path / "broken.py"
        broken.write_bytes(b"x = 1\n\xff\xfe garbage\n")
        assert AstGrepService._read(broken) is None

    def test_read_still_reads_plain_utf8(self, tmp_path):
        plain = tmp_path / "plain.py"
        plain.write_bytes(b"def f():\n    return 1\n")
        assert AstGrepService._read(plain) == "def f():\n    return 1\n"


class TestFileEditorFullEdit:
    def _editor(self, tmp_path: Path) -> FileEditor:
        return FileEditor(project_root=tmp_path)

    async def test_full_file_edit_preserves_latin_1_encoding(self, tmp_path):
        _calc_file(tmp_path)
        editor = self._editor(tmp_path)
        new_text = '# -*- coding: latin-1 -*-\n\nmsg = "caf\xe9"\n'
        result = await editor.edit_file("calc.py", new_text)
        assert result.success
        on_disk = (tmp_path / "calc.py").read_bytes()
        assert b'msg = "caf\xe9"' in on_disk
        assert b"\xc3\xa9" not in on_disk
        assert source_codec(tmp_path / "calc.py") == "latin-1"

    async def test_unencodable_full_edit_preserves_original_file(self, tmp_path):
        _calc_file(tmp_path)
        editor = self._editor(tmp_path)
        original_bytes = (tmp_path / "calc.py").read_bytes()
        # \u2014 cannot be encoded in latin-1
        result = await editor.edit_file(
            "calc.py", '# -*- coding: latin-1 -*-\n\nmsg = "\u2014"\n'
        )
        assert not result.success
        assert (tmp_path / "calc.py").read_bytes() == original_bytes
        assert not (tmp_path / ".calc.py.tmp").exists()


class TestAstGrepReplaceEncoding:
    def test_structural_replace_writes_latin_1_atomically(self, tmp_path):
        _calc_file(tmp_path)
        service = AstGrepService(project_root=tmp_path)
        changes = service.replace(
            pattern="precio($X)",
            rewrite="valor($X)",
            dry_run=False,
        )
        assert len(changes) == 1
        assert changes[0]["applied"] is True
        on_disk = (tmp_path / "calc.py").read_bytes()
        assert b"return valor(3)" in on_disk
        assert b"\xf1" in on_disk
        assert b"\xc3\xb1" not in on_disk
        assert not (tmp_path / ".calc.py.tmp").exists()

    def test_unencodable_structural_replace_preserves_original_file(self, tmp_path):
        _calc_file(tmp_path)
        service = AstGrepService(project_root=tmp_path)
        original_bytes = (tmp_path / "calc.py").read_bytes()
        # \u2014 cannot be encoded in latin-1
        changes = service.replace(
            pattern="precio($X)",
            rewrite="precio_\u2014($X)",
            dry_run=False,
        )
        assert len(changes) == 0
        assert (tmp_path / "calc.py").read_bytes() == original_bytes
        assert not (tmp_path / ".calc.py.tmp").exists()
