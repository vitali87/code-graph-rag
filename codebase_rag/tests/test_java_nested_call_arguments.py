# Issue #1348: a nested call argument was typed by NAME only, so an overloaded
# inner call contributed the first declaration's return type and the outer call
# bound the wrong overload.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships

_SOURCE = """
class Wrong {{ }}
class Right {{ }}
class Factory {{
    public Wrong make(int n) {{ return new Wrong(); }}
    public Right make(String s) {{ return new Right(); }}
    public void take(Wrong wrong) {{ }}
    public void take(Right right) {{ }}
}}
public class Main {{
    public static void main(String[] args) {{
        Factory factory = new Factory();
        {call}
    }}
}}
"""


def _targets(project: Path, mock_ingestor: MagicMock, source: str) -> set[str]:
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    ).run()
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}


def test_nested_call_is_typed_from_its_selected_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        _SOURCE.format(call='factory.take(factory.make("x"));'),
    )
    assert "proj.src.Main.Factory.make(String)" in targets
    assert "proj.src.Main.Factory.take(Right)" in targets
    assert "proj.src.Main.Factory.take(Wrong)" not in targets


def test_the_other_nested_overload_selects_the_other_target(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The mirror case: the ranking must follow the argument, not the
    # declaration order of `make`.
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        _SOURCE.format(call="factory.take(factory.make(1));"),
    )
    assert "proj.src.Main.Factory.take(Wrong)" in targets
    assert "proj.src.Main.Factory.take(Right)" not in targets


def test_nested_call_on_a_field_receiver(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The receiver is a field rather than a local, so it resolves through the
    # field-type lookup instead of the caller's variable map.
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        """
class Wrong { }
class Right { }
class Factory {
    public Wrong make(int n) { return new Wrong(); }
    public Right make(String s) { return new Right(); }
    public void take(Wrong wrong) { }
    public void take(Right right) { }
}
public class Main {
    private Factory factory = new Factory();
    public void run() {
        this.factory.take(this.factory.make("x"));
    }
}
""",
    )
    assert "proj.src.Main.Factory.take(Right)" in targets
    assert "proj.src.Main.Factory.take(Wrong)" not in targets


def test_a_deeply_nested_argument_chain_terminates(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Argument typing calls resolution and resolution types arguments, so a
    # call nested inside its own argument list must not recurse without a
    # brake; three levels is enough to exercise the descent.
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        """
class Factory {
    public String echo(String s) { return s; }
    public void take(String s) { }
}
public class Main {
    public static void main(String[] args) {
        Factory factory = new Factory();
        factory.take(factory.echo(factory.echo(factory.echo("x"))));
    }
}
""",
    )
    assert "proj.src.Main.Factory.take(String)" in targets
    assert "proj.src.Main.Factory.echo(String)" in targets


def test_field_access_argument_uses_the_active_methods_locals(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `holder` is declared in TWO methods with different types. The base of a
    # field-access argument must resolve through the CALLER's locals, not a
    # module-wide name lookup that can land on the other method's variable.
    targets = _targets(
        temp_repo / "proj",
        mock_ingestor,
        """
class Wrong { }
class Right { }
class WrongHolder { public Wrong target = new Wrong(); }
class RightHolder { public Right target = new Right(); }
class Factory {
    public void take(Wrong wrong) { }
    public void take(Right right) { }
}
public class Main {
    public void run() {
        RightHolder holder = new RightHolder();
        Factory factory = new Factory();
        factory.take(holder.target);
    }
    public void other() {
        WrongHolder holder = new WrongHolder();
        System.out.println(holder);
    }
}
""",
    )
    assert "proj.src.Main.Factory.take(Right)" in targets
    assert "proj.src.Main.Factory.take(Wrong)" not in targets
