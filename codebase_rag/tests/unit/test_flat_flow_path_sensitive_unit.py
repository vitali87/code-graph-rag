from __future__ import annotations

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.flow_access import FlowKind
from codebase_rag.parsers.flow_access.processor import FlowProcessor

# (H) Fast unit coverage for the Go/Java path-sensitive flat walk (issue #714): drives
# (H) FlowProcessor directly on a parsed snippet with recording fakes, no Memgraph. The
# (H) integration suite (test_flat_path_sensitive_flow_e2e.py) asserts the same behavior
# (H) end to end; these run under `pytest -m "not integration"` so the coverage counts.

_FUNC_TYPES = {"function_declaration", "method_declaration"}
_LANG_BY_FILE = {
    "main.go": cs.SupportedLanguage.GO,
    "App.java": cs.SupportedLanguage.JAVA,
}


_Spec = tuple[object, object, object]


class _RecordingIngestor:
    def __init__(self) -> None:
        self.rels: list[tuple[_Spec, object, _Spec, dict[str, object]]] = []

    def ensure_node_batch(self, label: object, properties: object) -> None:
        pass

    def ensure_relationship_batch(
        self,
        start: _Spec,
        rel_type: object,
        end: _Spec,
        properties: dict[str, object] | None = None,
    ) -> None:
        self.rels.append((start, rel_type, end, properties or {}))

    def flush_all(self) -> None:
        pass


class _NoResolver:
    def resolve_function_call(self, *args: object, **kwargs: object) -> None:
        return None


class _EmptyImports:
    def __init__(self) -> None:
        self.import_mapping: dict[str, dict[str, str]] = {}


def _first_function(node: Node) -> Node | None:
    if node.type in _FUNC_TYPES:
        return node
    for child in node.named_children:
        found = _first_function(child)
        if found is not None:
            return found
    return None


def _resource_flows(code: str, filename: str) -> list[tuple[str, str]]:
    language = _LANG_BY_FILE[filename]
    parsers, _ = load_parsers()
    tree = parsers[language.value].parse(code.encode("utf-8"))
    func = _first_function(tree.root_node)
    assert func is not None
    module_qn = "proj.mod"
    caller_qn = f"{module_qn}.f"
    ingestor = _RecordingIngestor()
    processor = FlowProcessor(
        ingestor,  # type: ignore[arg-type]
        _EmptyImports(),  # type: ignore[arg-type]
        _NoResolver(),  # type: ignore[arg-type]
        resolve_capture([cs.CaptureGroup.IO.value]),
    )
    processor.process_flow_for_caller(
        func,
        (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, caller_qn),
        caller_qn,
        module_qn,
        language,
        None,
    )
    processor.finalize()
    out: list[tuple[str, str]] = []
    for start, rel_type, end, props in ingestor.rels:
        if (
            rel_type == cs.RelationshipType.FLOWS_TO
            and props.get("kind") == FlowKind.RESOURCE.value
        ):
            out.append((str(start[2]), str(end[2])))
    return out


def test_go_kill_on_one_branch_taint_survives() -> None:
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
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_go_kill_on_all_branches_no_flow() -> None:
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
    )
    assert flows == []


def test_go_branch_local_shadow_does_not_leak() -> None:
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
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_go_if_initializer_shadow_does_not_leak() -> None:
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
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_java_kill_on_one_branch_taint_survives() -> None:
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
    )
    assert ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>") in flows


def test_java_kill_on_all_branches_no_flow() -> None:
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
    )
    assert flows == []
