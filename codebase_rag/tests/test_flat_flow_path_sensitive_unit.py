from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.flow_access import FlowKind
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

# Fast unit coverage for the Go/Java path-sensitive flat walk (issue #714): runs the
# real GraphUpdater against a one-file project with a recording in-memory ingestor
# (no Memgraph), so it is collected under `pytest -m "not integration"` and its
# coverage counts toward SonarCloud. The integration suite
# (test_flat_path_sensitive_flow_e2e.py) asserts the same behavior end to end.

_LANG_BY_FILE = {"main.go": "flow_go", "App.java": "flow_java"}


class _RecordingIngestor:
    # Structural IngestorProtocol stand-in recording FLOWS_TO relationships;
    # query-side methods are no-ops (a fresh project needs no rehydration).
    def __init__(self) -> None:
        self.rels: list[
            tuple[
                tuple[str, str, PropertyValue],
                str,
                tuple[str, str, PropertyValue],
                PropertyDict,
            ]
        ] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        pass

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec, rel_type, to_spec, properties or {}))

    def flush_all(self) -> None:
        pass

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        pass

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []


def _resource_flows(code: str, filename: str, tmp_path: Path) -> list[tuple[str, str]]:
    project = tmp_path / _LANG_BY_FILE[filename]
    project.mkdir()
    (project / filename).write_text(code, encoding="utf-8")
    parsers, queries = load_parsers()
    ingestor = _RecordingIngestor()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
        skip_embeddings=True,
    ).run()
    out: list[tuple[str, str]] = []
    for from_spec, rel_type, to_spec, props in ingestor.rels:
        if (
            rel_type == cs.RelationshipType.FLOWS_TO
            and props.get("kind") == FlowKind.RESOURCE.value
        ):
            out.append((str(from_spec[2]), str(to_spec[2])))
    return out


def test_go_kill_on_one_branch_taint_survives(tmp_path: Path) -> None:
    flows = _resource_flows(
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func f(cond bool) {\n"
        '\tsecret := os.Getenv("SECRET")\n'
        "\tif cond {\n"
        '\t\tsecret = "redacted"\n'
        "\t}\n"
        "\tfmt.Println(secret)\n"
        "}\n",
        "main.go",
        tmp_path,
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_go_kill_on_all_branches_no_flow(tmp_path: Path) -> None:
    flows = _resource_flows(
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func f(cond bool) {\n"
        '\tsecret := os.Getenv("SECRET")\n'
        "\tif cond {\n"
        '\t\tsecret = "a"\n'
        "\t} else {\n"
        '\t\tsecret = "b"\n'
        "\t}\n"
        "\tfmt.Println(secret)\n"
        "}\n",
        "main.go",
        tmp_path,
    )
    assert flows == []


def test_go_branch_local_shadow_does_not_leak(tmp_path: Path) -> None:
    flows = _resource_flows(
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func f(cond bool) {\n"
        "\tif cond {\n"
        "\t\tos := load()\n"
        "\t\t_ = os\n"
        "\t}\n"
        '\tfmt.Println(os.Getenv("SECRET"))\n'
        "}\n",
        "main.go",
        tmp_path,
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_go_if_initializer_shadow_does_not_leak(tmp_path: Path) -> None:
    flows = _resource_flows(
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func f() {\n"
        "\tif os := load(); os != nil {\n"
        "\t\t_ = os\n"
        "\t}\n"
        '\tfmt.Println(os.Getenv("SECRET"))\n'
        "}\n",
        "main.go",
        tmp_path,
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_java_kill_on_one_branch_taint_survives(tmp_path: Path) -> None:
    flows = _resource_flows(
        "class App {\n"
        "    void f(boolean cond) {\n"
        '        String s = System.getenv("SECRET");\n'
        "        if (cond) {\n"
        '            s = "redacted";\n'
        "        }\n"
        "        System.out.println(s);\n"
        "    }\n"
        "}\n",
        "App.java",
        tmp_path,
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_java_kill_on_all_branches_no_flow(tmp_path: Path) -> None:
    flows = _resource_flows(
        "class App {\n"
        "    void f(boolean cond) {\n"
        '        String s = System.getenv("SECRET");\n'
        "        if (cond) {\n"
        '            s = "a";\n'
        "        } else {\n"
        '            s = "b";\n'
        "        }\n"
        "        System.out.println(s);\n"
        "    }\n"
        "}\n",
        "App.java",
        tmp_path,
    )
    assert flows == []
