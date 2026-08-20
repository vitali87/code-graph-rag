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
