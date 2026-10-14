"""Structural search and replace cover every language ast-grep parses.

The tools mapped twelve languages to ast-grep. Scala and Dart, and every
structural-tier language (Ruby, Kotlin, Swift, Elixir, Haskell, Solidity,
Bash, Nix), were refused by name ("Unknown or unsupported language 'ruby'")
and silently skipped without one, so a search over a Ruby file answered "No
structural matches" as if that were verified, although ast-grep parses all
of them and cgr's own findings rule packs use those grammars (issue #2783).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("ast_grep_py")

from codebase_rag.tools.ast_grep_service import AstGrepService

_DEPLOYER = "class Deployer\n  def run(cmd)\n    system(cmd)\n  end\nend\n"
_REPORT = (
    "object Report {\n  def show(msg: String): Unit = {\n    println(msg)\n  }\n}\n"
)

# One file per language with a pattern that matches in it: a call where the
# grammar parses a bare call as a pattern, else the enclosing declaration.
_SOURCES = {
    "scala": ("A.scala", "object A { def f() = log(1) }\n", "log($A)"),
    "dart": ("a.dart", "void main() { log(1); }\n", "void main() { $$$B }"),
    "ruby": ("a.rb", "log(1)\n", "log($A)"),
    "kotlin": ("A.kt", "fun main() { log(1) }\n", "log($A)"),
    "swift": ("a.swift", "log(1)\n", "log($A)"),
    "elixir": ("a.ex", "log(1)\n", "log($A)"),
    "haskell": ("A.hs", "main = log 1\n", "log $A"),
    "bash": ("a.sh", "log 1\n", "log $A"),
    "nix": ("a.nix", "{ x = log 1; }\n", "log $A"),
    "solidity": (
        "A.sol",
        "contract A { function f() public { log(1); } }\n",
        "contract $C { $$$B }",
    ),
}


def _repo(root: Path, files: dict[str, str]) -> AstGrepService:
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    return AstGrepService(str(root))


def test_the_issue_finds_ruby_and_scala_matches(tmp_path: Path) -> None:
    svc = _repo(tmp_path, {"deployer.rb": _DEPLOYER, "Report.scala": _REPORT})
    ruby = svc.search("system($A)", language="ruby")
    assert [(m["file"], m["line"], m["text"]) for m in ruby] == [
        ("deployer.rb", 3, "system(cmd)")
    ]
    unfiltered = svc.search("println($A)")
    assert [(m["file"], m["line"]) for m in unfiltered] == [("Report.scala", 3)]


@pytest.mark.parametrize("language", sorted(_SOURCES))
def test_each_language_is_searched_by_name(tmp_path: Path, language: str) -> None:
    name, source, pattern = _SOURCES[language]
    svc = _repo(tmp_path, {name: source, "decoy.py": "log(1)\n"})
    matches = svc.search(pattern, language=language)
    assert {m["file"] for m in matches} == {name}, matches


def test_a_ruby_rewrite_is_applied(tmp_path: Path) -> None:
    svc = _repo(tmp_path, {"deployer.rb": _DEPLOYER})
    changes = svc.replace(
        "system($A)", "safe_system($A)", language="ruby", dry_run=False
    )
    assert changes
    assert "safe_system(cmd)" in (tmp_path / "deployer.rb").read_text()


def test_what_ast_grep_cannot_parse_is_still_refused_or_skipped(
    tmp_path: Path,
) -> None:
    # Negatives: an unknown language is refused, and a file of a language
    # with no ast-grep grammar (SQL) is skipped by an unfiltered search.
    svc = _repo(tmp_path, {"q.sql": "select log(1);\n", "a.py": "log(1)\n"})
    with pytest.raises(ValueError, match="cobol"):
        svc.search("log($A)", language="cobol")
    assert {m["file"] for m in svc.search("log($A)")} == {"a.py"}
