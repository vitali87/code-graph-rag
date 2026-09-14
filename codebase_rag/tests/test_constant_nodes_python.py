"""The Python module-level constant enumerator (issue #1806).

Python has no `const` keyword, so `python_declared_constants` is a naming
heuristic (`UPPER_CASE`) widened by an explicit `Final` annotation. Both halves
need their own negative: a test that only checks `MAX = 1` is picked is
satisfied equally by an enumerator that takes EVERY module-level assignment,
which is why the lowercase, dunder, tuple, augmented, nested and class-level
cases are each pinned separately.
"""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.constant_nodes import (
    DeclaredConstant,
    declared_constants,
    python_declared_constants,
)


def _constants(source: str) -> list[DeclaredConstant]:
    parsers, _queries = load_parsers()
    parser = parsers[cs.SupportedLanguage.PYTHON]
    tree = parser.parse(source.encode())
    return python_declared_constants(tree.root_node)


def _names(source: str) -> list[str]:
    return [c.name for c in _constants(source)]


def test_an_upper_case_assignment_is_a_constant() -> None:
    assert _names("MAX = 1\nMAX_SIZE = 2\nX = 3\n") == ["MAX", "MAX_SIZE", "X"]


def test_a_lowercase_assignment_is_not_a_constant() -> None:
    """The half the UPPER_CASE regex exists for.

    Without it every module-level binding becomes a Constant: the issue
    measured 190 lowercase incidental assignments on this repo against 5,489
    real ones, and `logger = get_logger()` is not a constant.
    """
    assert _names("logger = object()\nmaxSize = 3\nConfig = object()\n") == []


def test_a_final_annotation_makes_a_lowercase_name_a_constant() -> None:
    """`Final` is the language saying so explicitly, so case does not decide."""
    assert _names("from typing import Final\n\ntimeout: Final = 30\n") == ["timeout"]
    assert _names("import typing\n\nlimit: typing.Final = 5\n") == ["limit"]
    assert _names("from typing import Final\n\nsize: Final[int] = 5\n") == ["size"]


def test_a_final_subscript_unwraps_to_its_argument() -> None:
    """`x: Final[int]` declares an `int`, not a `Final`.

    Recording the wrapper would send OF_TYPE looking for a class called
    `Final`, and a bare `Final` names no type at all.
    """
    (subscripted,) = _constants("from typing import Final\n\nsize: Final[int] = 5\n")
    assert subscripted.type_name == "int"

    (bare,) = _constants("from typing import Final\n\nsize: Final = 5\n")
    assert bare.type_name is None

    (qualified,) = _constants("import typing\n\nn: typing.Final[Widget] = w\n")
    assert qualified.type_name == "Widget"


def test_a_plain_annotation_is_kept_as_written() -> None:
    (constant,) = _constants("MAX: int = 1\n")
    assert constant.type_name == "int"
    (generic,) = _constants("NAMES: list[str] = []\n")
    assert generic.type_name == "list[str]"
    (unannotated,) = _constants("MAX = 1\n")
    assert unannotated.type_name is None


def test_a_dunder_is_not_a_constant() -> None:
    """`__all__` is a re-export list and `__version__` is machinery; both are
    UPPER_CASE-adjacent and would otherwise be picked."""
    assert _names('__all__ = ["a"]\n__version__ = "1.0"\nREAL = 1\n') == ["REAL"]


def test_a_tuple_target_is_skipped() -> None:
    """No single target owns the right-hand side of `A, B = 1, 2`."""
    assert _names("A, B = 1, 2\n[C, D] = [3, 4]\nREAL = 5\n") == ["REAL"]


def test_an_attribute_target_is_skipped() -> None:
    """`obj.MAX = 1` binds no module-level name."""
    assert _names("import obj\n\nobj.MAX = 1\nREAL = 2\n") == ["REAL"]


def test_a_chained_assignment_records_every_name_with_the_real_value() -> None:
    """`MAX = MIN = 0` declares two constants, both bound to `0`.

    The grammar nests a second `assignment` as the first one's `right`, so
    reading `right` whole gave `MAX` the source text `MIN = 0` as its value
    and dropped `MIN` entirely (found in local review). A malformed literal
    that reads as a valid one is the failure direction the value cap exists
    to avoid, so the value must be the innermost right-hand side.
    """
    constants = {c.name: c.value for c in _constants("MAX = MIN = 0\n")}
    assert constants == {"MAX": "0", "MIN": "0"}
    # Chains are not limited to two, and the filters still apply to every
    # name in the chain, not just the first.
    assert _names("A = B = C = 1\n") == ["A", "B", "C"]
    assert _names("lower = OTHER = 2\n") == ["OTHER"]
    assert _names("__all__ = OTHER = []\n") == ["OTHER"]


def test_an_augmented_assignment_is_skipped() -> None:
    """`X += 1` mutates an existing binding; it does not declare one."""
    assert _names("X = 1\nX += 1\n") == ["X"]
    assert _names("X += 1\n") == []


def test_a_constant_inside_a_function_is_skipped() -> None:
    """Module scope only: a local is not a module constant, and its qualified
    name would collide with a real module-level one of the same name."""
    assert _names("def f():\n    MAX = 1\n    return MAX\n\nOUTER = 2\n") == ["OUTER"]


def test_a_class_level_constant_is_skipped() -> None:
    """A class member is already a `Field` (#1805). Emitting a Constant for it
    too would put two labels on one qualified name."""
    assert _names("class C:\n    MAX = 1\n\nOUTER = 2\n") == ["OUTER"]


def test_no_nesting_shape_leaks_a_constant_into_module_scope() -> None:
    """Every awkward nesting, not just the two obvious ones.

    A peer session hit a scope walk that skipped nested functions but not
    nested CLASS bodies, so one shape leaked. This enumerator descends no
    further than the module node's direct children, so the class of defect
    cannot arise -- but "cannot arise" is a claim about the code, and this
    is the check of it. Each case carries `OUTER` as a live control, so a
    silently empty result fails here rather than reading as a pass.
    """
    cases = {
        "class inside a function": "def f():\n    class C:\n        MAX = 1\n\nOUTER = 2\n",
        "function inside a class": "class C:\n    def m(self):\n        MAX = 1\n\nOUTER = 2\n",
        "class inside a class": "class C:\n    class D:\n        MAX = 1\n\nOUTER = 2\n",
        "async function": "async def f():\n    MAX = 1\n\nOUTER = 2\n",
        "decorated class": "@dec\nclass C:\n    MAX = 1\n\nOUTER = 2\n",
        "module-level with": "with ctx():\n    MAX = 1\n\nOUTER = 2\n",
        "module-level for": "for i in y:\n    MAX = 1\n\nOUTER = 2\n",
    }
    for label, source in cases.items():
        assert _names(source) == ["OUTER"], f"{label} leaked a nested MAX"
    # The control is only meaningful if the same declaration IS picked up at
    # module scope: otherwise every case above passes by finding nothing.
    assert _names("MAX = 1\n") == ["MAX"]


def test_a_statement_nested_in_an_if_or_try_is_skipped() -> None:
    """Direct children of the module node only.

    A conditionally bound name has no single value, position or type, so
    picking one branch would be a silent choice. Documented in the docstring
    as a deliberate limit, not an oversight.
    """
    source = (
        "import sys\n"
        "\n"
        "if sys.version_info >= (3, 12):\n"
        "    MODE = 'new'\n"
        "else:\n"
        "    MODE = 'old'\n"
        "\n"
        "try:\n"
        "    BACKEND = 'fast'\n"
        "except ImportError:\n"
        "    BACKEND = 'slow'\n"
        "\n"
        "TOP = 1\n"
    )
    assert _names(source) == ["TOP"]


def test_the_value_text_is_captured_as_written() -> None:
    assert _constants("MAX = 1\n")[0].value == "1"
    assert _constants("NAME = 'widget'\n")[0].value == "'widget'"
    assert _constants("SIZES = [1, 2, 3]\n")[0].value == "[1, 2, 3]"
    assert _constants("TOTAL = 2 + 3\n")[0].value == "2 + 3"
    # Annotated assignments carry a value too.
    assert _constants("MAX: int = 7\n")[0].value == "7"


def test_a_value_over_the_cap_is_absent_rather_than_truncated() -> None:
    """A generated lookup table can be megabytes on one line, and a truncated
    literal reads as a complete one -- wrong in the reassuring direction."""
    under = "x" * (cs.CONSTANT_VALUE_MAX_CHARS - 2)
    (small,) = _constants(f"TABLE = '{under}'\n")
    assert small.value is not None
    assert len(small.value) == cs.CONSTANT_VALUE_MAX_CHARS

    over = "x" * cs.CONSTANT_VALUE_MAX_CHARS
    (big,) = _constants(f"TABLE = '{over}'\n")
    assert big.value is None, "an over-cap value must be absent, not truncated"


def test_a_declaration_without_a_value_carries_none() -> None:
    """`MAX: int` is a bare annotation: a declared type and no right-hand side."""
    constants = _constants("MAX: int\n")
    # tree-sitter does not model a bare annotation as an `assignment` with a
    # `right`; whichever way it parses, no value may be invented.
    assert all(c.value is None for c in constants), constants


def test_positions_are_the_names_own() -> None:
    """1-based line, 0-based column, at the NAME rather than the statement."""
    (constant,) = _constants("\n\nMAX = 1\n")
    assert (constant.start_line, constant.start_col) == (3, 0)
    (annotated,) = _constants("# lead\nMAX_SIZE: int = 2\n")
    assert (annotated.start_line, annotated.start_col) == (2, 0)


def test_the_dispatch_covers_python_and_nothing_else_yet() -> None:
    """A language with no enumerator returns `[]` meaning NOT COVERED.

    The positive half matters: without it the empty list below is equally
    explained by a dispatch that covers nothing at all.
    """
    parsers, _queries = load_parsers()
    tree = parsers[cs.SupportedLanguage.PYTHON].parse(b"MAX = 1\n")
    root = tree.root_node
    assert [c.name for c in declared_constants(root, cs.SupportedLanguage.PYTHON)] == [
        "MAX"
    ]
    for language in (
        cs.SupportedLanguage.JAVA,
        cs.SupportedLanguage.GO,
        cs.SupportedLanguage.RUST,
        None,
    ):
        assert declared_constants(root, language) == [], language
