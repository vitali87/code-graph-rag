# Covers the Ruby structure oracle harness (evals/oracles/ruby_oracle):
# Prism, Ruby's official parser, is authoritative ground truth, and cgr's Ruby
# nodes are graded against it on (kind, file, start_line).
#
# Ruby reaches the graph through the ast-grep structural tier (".rb" maps to
# ast_grep_id "ruby"), which is a weaker tier than the tree-sitter one; grading
# it against a real parser is the point of issue #1190's Ruby gap.
from __future__ import annotations

from pathlib import Path

import pytest

from evals.oracles import (
    ruby_oracle_skip_reason,
    run_ruby_oracle,
)

# Every construct here is one the oracle must find. Kept small so the labels
# stay reviewable, and deliberately mixed so a parser that only handles the
# common form is visibly wrong:
#   - a bare top-level method
#   - a class with an instance method and a `self.` class method
#   - a module with a nested class, so qualification has to nest
#   - a singleton method defined on an object
RUBY_SRC = """\
def free_fn(a)
  a + 1
end

class Greeter
  def hello(name)
    "hi #{name}"
  end

  def self.build
    new
  end
end

module Outer
  class Inner
    def deep
      42
    end
  end
end
"""


def _require_ruby() -> None:
    reason = ruby_oracle_skip_reason()
    if reason is not None:
        pytest.skip(reason)


def test_oracle_finds_every_ruby_definition(tmp_path: Path) -> None:
    """The oracle must see all five definitions, not just the easy ones.

    A parser that handled only `def` at top level would find two of these and
    still return a non-empty payload, so asserting "some nodes came back" would
    pass against a substantially broken oracle.
    """
    _require_ruby()
    project = tmp_path / "ruby_oracle_test"
    project.mkdir()
    (project / "m.rb").write_text(RUBY_SRC, encoding="utf-8")

    oracle = run_ruby_oracle(project)
    names = {node.name for node in oracle.nodes.values()}

    assert {"free_fn", "hello", "build", "deep"} <= names, names


def test_oracle_reports_classes(tmp_path: Path) -> None:
    _require_ruby()
    project = tmp_path / "ruby_oracle_test"
    project.mkdir()
    (project / "m.rb").write_text(RUBY_SRC, encoding="utf-8")

    oracle = run_ruby_oracle(project)
    names = {node.name for node in oracle.nodes.values()}

    assert {"Greeter", "Inner"} <= names, names


def test_modules_are_excluded_but_their_contents_are_not(tmp_path: Path) -> None:
    """A Ruby module emits no node, yet what it contains still does.

    cgr has no Module label, so grading modules against one would report a
    recall miss no implementation could fix; calling a module a Class instead
    would put a falsehood in ground truth and would score a future Module label
    as a regression. Excluding it keeps the gap visible and honest.

    The paired positive assertion is what makes this meaningful: asserting only
    that "Outer" is absent would pass just as well if the module body never
    parsed at all.
    """
    _require_ruby()
    project = tmp_path / "ruby_oracle_test"
    project.mkdir()
    (project / "m.rb").write_text(RUBY_SRC, encoding="utf-8")

    oracle = run_ruby_oracle(project)
    names = {node.name for node in oracle.nodes.values()}

    assert "Outer" not in names, names
    # The class and method nested inside that module are real definitions cgr
    # does emit, so they must survive the module's exclusion.
    assert "Inner" in names, names
    assert "deep" in names, names


def test_def_directly_inside_a_module_is_a_method(tmp_path: Path) -> None:
    """Excluding the module must not demote its methods to top-level Functions."""
    _require_ruby()
    project = tmp_path / "ruby_module_method"
    project.mkdir()
    (project / "helpers.rb").write_text(
        "module Helpers\n  def helper\n    1\n  end\nend\n", encoding="utf-8"
    )

    oracle = run_ruby_oracle(project)
    kinds = {node.name: key.kind for key, node in oracle.nodes.items()}

    assert kinds.get("helper") == "Method", kinds


def test_oracle_line_numbers_are_one_based(tmp_path: Path) -> None:
    """Off-by-one here would misalign every node against cgr's spans."""
    _require_ruby()
    project = tmp_path / "ruby_oracle_test"
    project.mkdir()
    (project / "m.rb").write_text(RUBY_SRC, encoding="utf-8")

    oracle = run_ruby_oracle(project)
    # nodes is keyed by NodeKey(kind, file, start_line); the line lives on the
    # key rather than on the DefNode value.
    by_name = {node.name: key for key, node in oracle.nodes.items()}

    # "def free_fn" is the first line of the fixture.
    assert by_name["free_fn"].start_line == 1, by_name["free_fn"]
    # "class Greeter" is the fifth.
    assert by_name["Greeter"].start_line == 5, by_name["Greeter"]


def test_multibyte_characters_do_not_shift_line_numbers(tmp_path: Path) -> None:
    """Prism reports BYTE offsets; a JS string is indexed in UTF-16 units.

    A comment or identifier outside ASCII makes those two disagree, and every
    span after it drifts by the difference. Ruby source routinely carries
    non-ASCII in comments and string literals, so this is not an edge case.

    Both ends are asserted, and against a file whose length is known: an
    end_line past the end of the file is the symptom that shows up first,
    because the error accumulates across the whole document.
    """
    _require_ruby()
    project = tmp_path / "ruby_multibyte"
    project.mkdir()
    # 10 lines. The first carries multibyte characters, so a UTF-16 index
    # under-counts the bytes Prism reports for everything below it.
    source = (
        "# コメント: a multibyte comment\n"
        "def first_fn\n"
        "  1\n"
        "end\n"
        "\n"
        "class Café\n"
        "  def método\n"
        "    2\n"
        "  end\n"
        "end\n"
    )
    (project / "mb.rb").write_text(source, encoding="utf-8")

    oracle = run_ruby_oracle(project)
    spans = {
        node.name: (key.start_line, node.end_line) for key, node in oracle.nodes.items()
    }

    assert spans["first_fn"] == (2, 4), spans
    assert spans["Café"] == (6, 10), spans
    assert spans["método"] == (7, 9), spans


def test_oracle_on_an_empty_project_returns_no_nodes(tmp_path: Path) -> None:
    _require_ruby()
    project = tmp_path / "empty_ruby"
    project.mkdir()
    (project / "blank.rb").write_text("# just a comment\n", encoding="utf-8")

    oracle = run_ruby_oracle(project)

    assert not oracle.nodes


def test_syntax_error_does_not_crash_the_oracle(tmp_path: Path) -> None:
    """A malformed file must not abort a whole-corpus run.

    Prism is error-tolerant and still returns a tree, so the oracle should
    report whatever it recovered rather than raising.
    """
    _require_ruby()
    project = tmp_path / "broken_ruby"
    project.mkdir()
    (project / "ok.rb").write_text("def fine\n  1\nend\n", encoding="utf-8")
    (project / "bad.rb").write_text("def oops\n  unterminated(\n", encoding="utf-8")

    oracle = run_ruby_oracle(project)
    names = {node.name for node in oracle.nodes.values()}

    # The healthy file's definition still lands.
    assert "fine" in names, names
