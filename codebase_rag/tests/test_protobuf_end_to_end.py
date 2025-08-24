from pathlib import Path

import pytest

import codec.schema_pb2 as pb
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.protobuf_service import ProtobufFileIngestor

COMPREHENSIVE_PROJECT_FIXTURE: dict[str, str] = {
    "pyproject.toml": """
[project]
name = "golden_project"
dependencies = ["fastapi>=0.100.0"]
""",
    "README.md": "# Golden Project Docs",
    "my_app/__init__.py": "# Makes my_app a Python package",
    "my_app/db/__init__.py": "",
    "my_app/db/base.py": """
class BaseRepo:
    def connect(self):
        pass
""",
    "my_app/services/__init__.py": "",
    "my_app/services/user_service.py": """
from my_app.db.base import BaseRepo
from my_app.utils import log_execution

@log_execution
class UserService(BaseRepo):
    def get_user(self, user_id: int) -> dict:
        self.connect()
        return {"user_id": user_id}
""",
    "my_app/utils.py": """
import functools
def log_execution(func): return func
""",
    "my_app/cpp/calculator.cpp": """
namespace math { class Calculator { public: int add(int a, int b); }; }
""",
}


def test_comprehensive_pipeline_produces_valid_artifact(tmp_path: Path) -> None:
    """
    Tests that the GraphUpdater -> ProtobufFileIngestor pipeline can
    successfully run on a comprehensive project and produce a valid,
    non-empty, and deserializable binary artifact.
    """
    project_dir = tmp_path / "golden_project"
    for file_path, content in COMPREHENSIVE_PROJECT_FIXTURE.items():
        full_path = project_dir / Path(file_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
    output_file = tmp_path / "graph_output.proto.bin"

    ingestor = ProtobufFileIngestor(str(output_file))
    parsers, queries = load_parsers()
    updater = GraphUpdater(ingestor, project_dir, parsers, queries)

    updater.run()

    assert output_file.exists(), "The protobuf output file was not created."
    assert output_file.stat().st_size > 100, (
        "The protobuf file is suspiciously small or empty."
    )

    deserialized_index = pb.GraphCodeIndex()
    try:
        with open(output_file, "rb") as f:
            deserialized_index.ParseFromString(f.read())
    except Exception as e:
        pytest.fail(
            f"The output file is not a valid Protobuf message. Deserialization failed with: {e}"
        )

    assert len(deserialized_index.nodes) > 5, (
        "The serialized graph contains too few nodes."
    )
    assert len(deserialized_index.relationships) > 5, (
        "The serialized graph contains too few relationships."
    )

    print(
        "\n✅ Pipeline Integrity Test Passed: Successfully generated a valid and well-formed protobuf file."
    )
