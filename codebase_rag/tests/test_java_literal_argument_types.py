# Issue #1344: a literal argument contributed no type, so a call whose
# arguments are all literals fell back to arity-only matching and bound to
# whichever same-arity overload was declared first.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships


def _targets(project: Path, mock_ingestor: MagicMock, source: str) -> set[str]:
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    ).run()
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}


_SOURCE = """
class Factory {{
    public void take(String text) {{ }}
    public void take(int count) {{ }}
    public void take(boolean flag) {{ }}
}}
public class Main {{
    public static void main(String[] args) {{
        Factory factory = new Factory();
        factory.take({literal});
    }}
}}
"""


def test_string_literal_selects_the_string_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    targets = _targets(
        temp_repo / "proj", mock_ingestor, _SOURCE.format(literal='"text"')
    )
    assert "proj.src.Main.Factory.take(String)" in targets


def test_integer_literal_selects_the_int_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    targets = _targets(temp_repo / "proj", mock_ingestor, _SOURCE.format(literal="42"))
    assert "proj.src.Main.Factory.take(int)" in targets


def test_boolean_literal_selects_the_boolean_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    targets = _targets(
        temp_repo / "proj", mock_ingestor, _SOURCE.format(literal="true")
    )
    assert "proj.src.Main.Factory.take(boolean)" in targets


_BOXED = """
class Wrong {{
    public void only() {{ }}
}}
class Factory {{
    public void take(String text) {{ }}
    public void take({param} value) {{ }}
}}
public class Main {{
    public static void main(String[] args) {{
        Factory factory = new Factory();
        factory.take({literal});
    }}
}}
"""


def test_int_literal_binds_a_boxed_integer_parameter(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Boxing is invisible at the call site, so an exact simple-name comparison
    # would reject the only applicable overload and fall back to arity, which
    # picks take(String).
    targets = _targets(
        temp_repo / "proj", mock_ingestor, _BOXED.format(param="Integer", literal="42")
    )
    assert "proj.src.Main.Factory.take(Integer)" in targets


def test_int_literal_binds_a_widened_long_parameter(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    targets = _targets(
        temp_repo / "proj", mock_ingestor, _BOXED.format(param="long", literal="42")
    )
    assert "proj.src.Main.Factory.take(long)" in targets


def test_null_literal_stays_a_wildcard(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `null` is compatible with every reference parameter, so typing it would
    # exclude candidates the language allows.
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        _BOXED.format(param="Integer", literal="null"),
    )
    assert any(t.startswith("proj.src.Main.Factory.take(") for t in targets)


def test_int_literal_reaches_object_after_boxing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Boxing followed by a widening reference conversion is legal (JLS 5.3),
    # so take(Object) is applicable and take(String) is not.
    targets = _targets(
        temp_repo / "proj", mock_ingestor, _BOXED.format(param="Object", literal="42")
    )
    assert "proj.src.Main.Factory.take(Object)" in targets


def test_int_literal_reaches_number_after_boxing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    targets = _targets(
        temp_repo / "proj", mock_ingestor, _BOXED.format(param="Number", literal="42")
    )
    assert "proj.src.Main.Factory.take(Number)" in targets


_SPECIFICITY = """
class Factory {{
    public void take(Object value) {{ }}
    public void take({param} value) {{ }}
}}
public class Main {{
    public static void main(String[] args) {{
        Factory factory = new Factory();
        factory.take({literal});
    }}
}}
"""


def test_the_most_specific_applicable_overload_wins(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Object is applicable to everything, so preferring the first applicable
    # candidate would always pick it; the language picks the most specific.
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        _SPECIFICITY.format(param="Integer", literal="42"),
    )
    assert "proj.src.Main.Factory.take(Integer)" in targets
    assert "proj.src.Main.Factory.take(Object)" not in targets


def test_widening_a_primitive_beats_boxing_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # JLS phase 1 considers widening primitive conversion before boxing.
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        """
class Factory {
    public void take(Integer boxed) { }
    public void take(long widened) { }
}
public class Main {
    public static void main(String[] args) {
        Factory factory = new Factory();
        factory.take(42);
    }
}
""",
    )
    assert "proj.src.Main.Factory.take(long)" in targets


def test_char_literal_selects_the_char_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    targets = _targets(
        temp_repo / "proj", mock_ingestor, _BOXED.format(param="char", literal="'x'")
    )
    assert "proj.src.Main.Factory.take(char)" in targets


def test_float_suffixed_literal_selects_the_float_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The suffix decides: 1.5f is a float, 1.5 a double.
    targets = _targets(
        temp_repo / "proj", mock_ingestor, _BOXED.format(param="float", literal="1.5f")
    )
    assert "proj.src.Main.Factory.take(float)" in targets


def test_unsuffixed_floating_literal_is_a_double(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    targets = _targets(
        temp_repo / "proj", mock_ingestor, _BOXED.format(param="double", literal="1.5")
    )
    assert "proj.src.Main.Factory.take(double)" in targets


def test_overload_rank_rejects_an_arity_mismatch() -> None:
    from codebase_rag.parsers.java.method_resolver import _overload_rank

    assert _overload_rank("C.take(int,int)", ("int",)) is None


def test_overload_rank_treats_an_unknown_argument_as_a_wildcard() -> None:
    # An argument whose type could not be inferred must not exclude a
    # candidate, or an unrelated expression would silently narrow the pick.
    from codebase_rag.parsers.java.method_resolver import _overload_rank

    assert _overload_rank("C.take(String,int)", (None, "int")) == 0


def test_overload_rank_rejects_an_unreachable_parameter_type() -> None:
    from codebase_rag.parsers.java.method_resolver import _overload_rank

    assert _overload_rank("C.take(Widget)", ("int",)) is None


def test_hex_floating_literal_is_a_double(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        _BOXED.format(param="double", literal="0x1.0p0"),
    )
    assert "proj.src.Main.Factory.take(double)" in targets


def test_hex_floating_literal_honours_the_float_suffix(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        _BOXED.format(param="float", literal="0x1.0p0f"),
    )
    assert "proj.src.Main.Factory.take(float)" in targets


def test_the_most_specific_overload_wins_in_either_declaration_order(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The specific overload declared FIRST must win too, so the ranking is not
    # accidentally reproducing declaration order.
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        """
class Factory {
    public void take(Integer boxed) { }
    public void take(Object any) { }
}
public class Main {
    public static void main(String[] args) {
        Factory factory = new Factory();
        factory.take(42);
    }
}
""",
    )
    assert "proj.src.Main.Factory.take(Integer)" in targets
    assert "proj.src.Main.Factory.take(Object)" not in targets
