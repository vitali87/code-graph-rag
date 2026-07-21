from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.flow_access import FlowKind
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

# Fast unit coverage for the Rust macro-sink / C++ stream-sink FLOWS_TO walk
# (issue #714): drives the real GraphUpdater against a one-file project with a
# recording in-memory ingestor (no Memgraph), so it is collected under
# `pytest -m "not integration"` and its coverage counts toward SonarCloud. The
# integration suite (test_rust_cpp_flow_e2e.py) asserts the same behavior e2e.

_LANG_BY_FILE = {"main.rs": "flow_rs", "main.cpp": "flow_cpp"}


class _RecordingIngestor:
    # Structural IngestorProtocol stand-in that records FLOWS_TO relationships; the
    # query-side methods are no-ops (a single fresh project needs no rehydration).
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


def test_rust_env_var_to_println(tmp_path: Path) -> None:
    flows = _resource_flows(
        "fn boot() {\n"
        '    let secret = std::env::var("SECRET").unwrap();\n'
        '    println!("{}", secret);\n'
        "}\n",
        "main.rs",
        tmp_path,
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_rust_inlined_env_in_println(tmp_path: Path) -> None:
    flows = _resource_flows(
        'fn boot() {\n    println!("{}", std::env::var("SECRET").unwrap());\n}\n',
        "main.rs",
        tmp_path,
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_rust_tainted_path_name_no_over_taint(tmp_path: Path) -> None:
    flows = _resource_flows(
        "fn boot() {\n"
        '    let env = std::env::var("SECRET").unwrap();\n'
        '    println!("{}", std::env::var("CLEAN").unwrap());\n'
        "}\n",
        "main.rs",
        tmp_path,
    )
    assert ("resource::ENV::CLEAN", "resource::STDOUT::<dynamic>") in flows
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") not in flows


def test_rust_inline_format_capture_to_println(tmp_path: Path) -> None:
    flows = _resource_flows(
        "fn boot() {\n"
        '    let secret = std::env::var("SECRET").unwrap();\n'
        '    println!("{secret}");\n'
        "}\n",
        "main.rs",
        tmp_path,
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_rust_untainted_println_no_flow(tmp_path: Path) -> None:
    flows = _resource_flows(
        'fn boot() {\n    println!("hello");\n}\n',
        "main.rs",
        tmp_path,
    )
    assert flows == []


def test_cpp_getenv_to_cout(tmp_path: Path) -> None:
    flows = _resource_flows(
        "#include <iostream>\n"
        "#include <cstdlib>\n"
        "void boot() {\n"
        '    const char* secret = getenv("SECRET");\n'
        "    std::cout << secret;\n"
        "}\n",
        "main.cpp",
        tmp_path,
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_cpp_inlined_getenv_in_cout(tmp_path: Path) -> None:
    flows = _resource_flows(
        "#include <iostream>\n"
        "#include <cstdlib>\n"
        "void boot() {\n"
        '    std::cout << getenv("SECRET");\n'
        "}\n",
        "main.cpp",
        tmp_path,
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_cpp_untainted_cout_no_flow(tmp_path: Path) -> None:
    flows = _resource_flows(
        '#include <iostream>\nvoid boot() {\n    std::cout << "hello";\n}\n',
        "main.cpp",
        tmp_path,
    )
    assert flows == []
