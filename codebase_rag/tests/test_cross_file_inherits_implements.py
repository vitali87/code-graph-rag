# (H) A non-C++ base or interface that does not resolve at parse time used to
# (H) be anchored to the child's own module qn (`_resolve_to_qn` fallback), so
# (H) every cross-file or external base emitted an INHERITS/IMPLEMENTS edge to
# (H) a phantom the database drops (issue #652: 136 INHERITS + 81 IMPLEMENTS
# (H) across the fixture suite). Emission is now deferred until every class is
# (H) registered, re-resolved against the full registry, and skipped entirely
# (H) when the target resolves nowhere (java.lang.Exception, Rust Send/Sync).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

FLYABLE_JAVA = """
package animals;

public interface Flyable {
    void fly();
}
"""

DUCK_JAVA = """
package animals;

public class Duck extends Bird implements Flyable {
    public void fly() {}
}
"""

BIRD_JAVA = """
package animals;

public class Bird {
    protected String name;
}
"""

CUSTOM_EXCEPTION_JAVA = """
package errors;

public class CustomException extends Exception {
    public CustomException(String message) {
        super(message);
    }
}
"""

RUST_TRAITS = """
pub trait Processable: Send + Sync {
    fn process(&self) -> u32;
}

pub struct CustomError {
    code: u32,
}

impl std::fmt::Display for CustomError {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        write!(f, "{}", self.code)
    }
}

pub trait Countable {
    fn count(&self) -> u32;
}

impl Countable for CustomError {
    fn count(&self) -> u32 {
        self.code
    }
}
"""


def _node_keys(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0]), c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
    }


def test_java_cross_file_base_and_interface_resolve(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "Flyable.java").write_text(FLYABLE_JAVA)
    (temp_repo / "Duck.java").write_text(DUCK_JAVA)
    (temp_repo / "Bird.java").write_text(BIRD_JAVA)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="java")

    project = temp_repo.name
    inherits = {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.INHERITS.value)
    }
    assert (f"{project}.Duck.Duck", f"{project}.Bird.Bird") in inherits, inherits

    implements = {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(
            mock_ingestor, cs.RelationshipType.IMPLEMENTS.value
        )
    }
    assert (
        f"{project}.Duck.Duck",
        f"{project}.Flyable.Flyable",
    ) in implements, implements


def test_java_external_base_emits_no_phantom_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "CustomException.java").write_text(CUSTOM_EXCEPTION_JAVA)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="java")

    node_keys = _node_keys(mock_ingestor)
    inherits = get_relationships(mock_ingestor, cs.RelationshipType.INHERITS.value)
    for call in inherits:
        to_label, _, to_qn = call.args[2]
        assert (str(to_label), to_qn) in node_keys, call.args


def test_rust_external_traits_emit_no_phantom_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "lib.rs").write_text(RUST_TRAITS)
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    node_keys = _node_keys(mock_ingestor)
    for rel_type in (cs.RelationshipType.INHERITS, cs.RelationshipType.IMPLEMENTS):
        for call in get_relationships(mock_ingestor, rel_type.value):
            to_label, _, to_qn = call.args[2]
            assert (str(to_label), to_qn) in node_keys, call.args

    # (H) The first-party trait impl must survive the deferral.
    implements = {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(
            mock_ingestor, cs.RelationshipType.IMPLEMENTS.value
        )
    }
    assert (
        f"{project}.lib.CustomError",
        f"{project}.lib.Countable",
    ) in implements, implements
