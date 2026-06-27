from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.java.utils import extract_class_info
from codebase_rag.tests.conftest import get_relationships


def _call_targets(mock_ingestor: MagicMock) -> set[str]:
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}


def _class_node(java_source: str) -> Node:
    tree = Parser(Language(tsjava.language())).parse(java_source.encode())

    def walk(node: Node) -> Node | None:
        if node.type == "class_declaration":
            return node
        for child in node.children:
            if found := walk(child):
                return found
        return None

    found = walk(tree.root_node)
    assert found is not None
    return found


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


def test_inherited_field_chain_via_this(
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
        f"this.address.city (address inherited from Base) should resolve to "
        f"City.ping(); got {sorted(targets)}"
    )


def test_inherited_field_chain_via_object(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(
        """
class City { public void ping() { System.out.println("ping"); } }
class Address { public City city = new City(); }
class Base { public Address address = new Address(); }
class Derived extends Base {}
public class Main {
    public static void main(String[] args) {
        Derived obj = new Derived();
        obj.address.city.ping();
    }
}
""",
        encoding="utf-8",
    )
    _run(project, mock_ingestor)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith(".City.ping()") for t in targets), (
        f"obj.address.city (address inherited from Base) should resolve to "
        f"City.ping(); got {sorted(targets)}"
    )


def test_direct_this_field_chain_method_call_multiclass(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(
        """
class Aardvark { public void unused() {} }
class City { public void ping() { System.out.println("ping"); } }
class Address { public City city = new City(); }
public class Container {
    public Address address = new Address();
    public void run() {
        this.address.city.ping();
    }
}
""",
        encoding="utf-8",
    )
    _run(project, mock_ingestor)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith(".City.ping()") for t in targets), (
        f"direct this.address.city.ping() in a multi-class file should resolve to "
        f"City.ping(); got {sorted(targets)}"
    )


def test_direct_super_field_chain_method_call_multiclass(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(
        """
class Aardvark { public void unused() {} }
class Other {}
class Wrong extends Other { public void unused() {} }
class City { public void ping() { System.out.println("ping"); } }
class Address { public City city = new City(); }
class Base { public Address address = new Address(); }
public class Derived extends Base {
    public void run() {
        super.address.city.ping();
    }
}
""",
        encoding="utf-8",
    )
    _run(project, mock_ingestor)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith(".City.ping()") for t in targets), (
        f"direct super.address.city.ping() in a multi-class file should resolve to "
        f"City.ping(); got {sorted(targets)}"
    )


def test_scoped_superclass_extraction_keeps_actual_class() -> None:
    nested = extract_class_info(_class_node("class Child extends Outer.Base {}"))
    assert nested.get("superclass") == "Outer.Base", (
        f"scoped superclass should keep the full name, not the outer/package "
        f"segment; got {nested.get('superclass')!r}"
    )

    qualified = extract_class_info(_class_node("class Child extends pkg.Base {}"))
    assert qualified.get("superclass") == "pkg.Base", (
        f"package-qualified superclass should keep the full name; "
        f"got {qualified.get('superclass')!r}"
    )

    simple = extract_class_info(_class_node("class Child extends Base {}"))
    assert simple.get("superclass") == "Base"


def test_inherited_field_chain_via_nested_superclass(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.java").write_text(
        """
class City { public void ping() { System.out.println("ping"); } }
class Address { public City city = new City(); }
class Outer {
    static class Base { public Address address = new Address(); }
}
public class Child extends Outer.Base {
    public void run() {
        this.address.city.ping();
    }
}
""",
        encoding="utf-8",
    )
    _run(project, mock_ingestor)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith(".City.ping()") for t in targets), (
        f"this.address.city with a same-file nested superclass (Outer.Base) should "
        f"resolve to City.ping(); got {sorted(targets)}"
    )
