from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships


def _call_targets(mock_ingestor: MagicMock) -> set[str]:
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}


def _run(project_path: Path, mock_ingestor: MagicMock) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    ).run()


def test_mixed_field_access_then_method_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(
        """
class Engine { public void start() { System.out.println("started"); } }
class Car { public Engine engine = new Engine(); }
public class Main {
    public static void main(String[] args) {
        Car obj = new Car();
        obj.engine.start();
    }
}
""",
        encoding="utf-8",
    )
    _run(project, mock_ingestor)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith(".Engine.start()") for t in targets), (
        f"obj.engine.start() should resolve to Engine.start(); got {sorted(targets)}"
    )


def test_multilevel_field_access_then_method_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(
        """
class City { public void ping() { System.out.println("ping"); } }
class Address { public City city = new City(); }
class User { public Address address = new Address(); }
public class Main {
    public static void main(String[] args) {
        User obj = new User();
        obj.address.city.ping();
    }
}
""",
        encoding="utf-8",
    )
    _run(project, mock_ingestor)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith(".City.ping()") for t in targets), (
        f"obj.address.city.ping() should resolve to City.ping(); got {sorted(targets)}"
    )


def test_nested_field_access_type_inference_via_var(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(
        """
class City { public void ping() { System.out.println("ping"); } }
class Address { public City city = new City(); }
class User { public Address address = new Address(); }
public class Main {
    public static void main(String[] args) {
        User obj = new User();
        var c = obj.address.city;
        c.ping();
    }
}
""",
        encoding="utf-8",
    )
    _run(project, mock_ingestor)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith(".City.ping()") for t in targets), (
        f"var c = obj.address.city; c.ping() should resolve to City.ping(); "
        f"got {sorted(targets)}"
    )


def test_this_rooted_nested_field_access_via_var(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(
        """
class City { public void ping() { System.out.println("ping"); } }
class Address { public City city = new City(); }
public class Container {
    public Address address = new Address();
    public void run() {
        var c = this.address.city;
        c.ping();
    }
}
""",
        encoding="utf-8",
    )
    _run(project, mock_ingestor)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith(".City.ping()") for t in targets), (
        f"var c = this.address.city; c.ping() should resolve to City.ping(); "
        f"got {sorted(targets)}"
    )


def test_super_rooted_nested_field_access_via_var(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(
        """
class City { public void ping() { System.out.println("ping"); } }
class Address { public City city = new City(); }
class Base { public Address address = new Address(); }
public class Derived extends Base {
    public void run() {
        var c = super.address.city;
        c.ping();
    }
}
""",
        encoding="utf-8",
    )
    _run(project, mock_ingestor)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith(".City.ping()") for t in targets), (
        f"var c = super.address.city; c.ping() should resolve to City.ping(); "
        f"got {sorted(targets)}"
    )
