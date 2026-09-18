import pytest

from codebase_rag.prompts import (
    _format_active_projects_block,
    build_rag_orchestrator_prompt,
)


@pytest.mark.parametrize(
    ("backend", "expected_engine"),
    [
        ("memgraph", "Memgraph"),
        ("neo4j", "Neo4j"),
    ],
)
def test_format_active_projects_block_engine_name(
    backend: str, expected_engine: str
) -> None:
    output = _format_active_projects_block(active_projects=None, backend=backend)
    assert (
        f"This {expected_engine} database may contain multiple indexed projects."
        in output
    )
    assert ("Memgraph" if expected_engine == "Neo4j" else "Neo4j") not in output


@pytest.mark.parametrize(
    ("backend", "expected_engine"),
    [
        ("memgraph", "Memgraph"),
        ("neo4j", "Neo4j"),
    ],
)
def test_orchestrator_prompt_reflects_configured_backend(
    backend: str, expected_engine: str
) -> None:
    # Use empty tool list or minimal mock
    prompt = build_rag_orchestrator_prompt(
        tools=[], active_projects=None, backend=backend
    )
    assert f"This {expected_engine} database may contain multiple" in prompt
