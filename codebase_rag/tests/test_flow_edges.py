from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.flow_access import FlowKind

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])

# (H) One FLOWS_TO edge as (from_qn, to_qn, properties).
FlowEdge = tuple[str, str, dict[str, str]]


def _run_flow(
    tmp_path: Path,
    files: dict[str, str],
    capture: CaptureSelection = _CAPTURE_IO,
) -> list[FlowEdge]:
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run()
    edges: list[FlowEdge] = []
    for c in mock.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) != FLOWS_TO:
            continue
        props = c.kwargs.get("properties")
        if props is None and len(c.args) > 3:
            props = c.args[3]
        edges.append((c.args[0][2], c.args[2][2], dict(props or {})))
    return edges


def _node_qns(mock: MagicMock) -> set[str]:
    return {
        c.args[1].get(cs.KEY_QUALIFIED_NAME)
        for c in mock.ensure_node_batch.call_args_list
        if len(c.args) >= 2
    }


def _has(edges: list[FlowEdge], frm: str, to: str, **props: str) -> bool:
    return any(
        a.endswith(frm)
        and b.endswith(to)
        and all(p.get(k) == v for k, v in props.items())
        for a, b, p in edges
    )


def test_resource_to_resource_env_to_stdout(tmp_path: Path) -> None:
    files = {"m.py": "import os\n\ndef leak():\n    x = os.getenv('K')\n    print(x)\n"}
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_tainted_positional_arg_flows_to_callee(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import os\n\n"
            "def helper(v):\n    pass\n\n"
            "def caller():\n    t = os.getenv('K')\n    helper(t)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.caller", "m.helper", via="arg:0", kind=FlowKind.ARG.value)


def test_tainted_keyword_arg_flows_to_callee(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import os\n\n"
            "def helper(v):\n    pass\n\n"
            "def caller():\n    t = os.getenv('K')\n    helper(v=t)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.caller", "m.helper", via="kw:v", kind=FlowKind.ARG.value)


def test_return_value_flows_from_callee_to_caller(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import os\n\n"
            "def build():\n    return os.getenv('K')\n\n"
            "def caller():\n    v = build()\n    print(v)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.build", "m.caller", via="return", kind=FlowKind.RETURN.value)


def test_direct_return_of_tainted_callee_emits_return_edge(tmp_path: Path) -> None:
    # (H) `return inner()` consumes inner's tainted return just as `v = inner()`
    # (H) does, so the callee->caller return edge must fire at the direct-return
    # (H) site too, not only at assignment sites.
    files = {
        "m.py": (
            "import os\n\n"
            "def inner():\n    return os.getenv('K')\n\n"
            "def outer():\n    return inner()\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.inner", "m.outer", via="return", kind=FlowKind.RETURN.value)


def test_return_parenthesized_tainted_value_emits_flow(tmp_path: Path) -> None:
    # (H) A returned value wrapped in parentheses is still the same tainted value;
    # (H) the return edge and downstream resource flow must survive the wrapper.
    files = {
        "m.py": (
            "import os\n\n"
            "def build():\n    return (os.getenv('K'))\n\n"
            "def caller():\n    v = build()\n    print(v)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.build", "m.caller", via="return", kind=FlowKind.RETURN.value)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_return_tuple_with_tainted_element_emits_flow(tmp_path: Path) -> None:
    # (H) `return a, b` wraps the values in an expression_list; a tainted element
    # (H) must still be seen as a returned tainted value.
    files = {
        "m.py": (
            "import os\n\n"
            "def build():\n    t = os.getenv('K')\n    return t, 1\n\n"
            "def caller():\n    v = build()\n    print(v)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(edges, "m.build", "m.caller", via="return", kind=FlowKind.RETURN.value)


def test_return_taint_reaches_resource_sink(tmp_path: Path) -> None:
    # (H) A value returned from a tainted callee carries its source resource, so a
    # (H) later sink emits the full resource->resource flow, not just the return edge.
    files = {
        "m.py": (
            "import os\n\n"
            "def build():\n    return os.getenv('K')\n\n"
            "def caller():\n    v = build()\n    print(v)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_taint_propagates_through_plain_assignment(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import os\n\n"
            "def leak():\n    a = os.getenv('K')\n    b = a\n    print(b)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges,
        "resource::ENV::K",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_untainted_arg_emits_no_flow(tmp_path: Path) -> None:
    # (H) Co-occurrence of a read source and an unrelated call is not flow.
    files = {
        "m.py": (
            "import os\n\n"
            "def helper(v):\n    pass\n\n"
            "def caller():\n    u = 1\n    helper(u)\n    os.getenv('K')\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not any(b.endswith("m.helper") for _, b, _ in edges)


def test_overwrite_with_literal_kills_taint(tmp_path: Path) -> None:
    # (H) Reassigning a tainted local to a safe literal kills its taint; the later
    # (H) sink must not emit a resource->resource flow (no stale-taint false positive).
    files = {
        "m.py": (
            "import os\n\n"
            "def leak():\n    x = os.getenv('K')\n    x = 'safe'\n    print(x)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not any(
        a.endswith("resource::ENV::K") and b.endswith("resource::STDOUT::<dynamic>")
        for a, b, _ in edges
    )


def test_overwrite_with_untainted_name_kills_taint(tmp_path: Path) -> None:
    # (H) Reassigning a tainted local from an untainted variable also kills taint.
    files = {
        "m.py": (
            "import os\n\n"
            "def leak():\n    x = os.getenv('K')\n    y = 1\n    x = y\n    print(x)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert not any(
        a.endswith("resource::ENV::K") and b.endswith("resource::STDOUT::<dynamic>")
        for a, b, _ in edges
    )


def test_default_capture_emits_no_flow(tmp_path: Path) -> None:
    files = {"m.py": "import os\n\ndef leak():\n    x = os.getenv('K')\n    print(x)\n"}
    edges = _run_flow(tmp_path, files, capture=resolve_capture([]))
    assert edges == []


def test_flow_only_capture_still_ensures_resource_nodes(tmp_path: Path) -> None:
    # (H) FLOWS_TO enabled, READS_FROM/WRITES_TO dropped: the resource endpoints of a
    # (H) FLOWS_TO edge must still be ensured so no edge dangles to a missing node.
    capture = resolve_capture(
        [cs.CAPTURE_TOKEN_NONE, f"{cs.CAPTURE_ADD_PREFIX}{FLOWS_TO}"]
    )
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    (tmp_path / "m.py").write_text(
        "import os\n\ndef leak():\n    x = os.getenv('K')\n    print(x)\n",
        encoding="utf-8",
    )
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run()
    node_qns = _node_qns(mock)
    assert "resource::ENV::K" in node_qns
    assert "resource::STDOUT::<dynamic>" in node_qns


def test_multiple_returns_distinct_sources_all_flow(tmp_path: Path) -> None:
    # (H) A callee returning DIFFERENT tainted sources on different branches must
    # (H) carry ALL origins to the caller's sink, not just the first-seen one.
    files = {
        "m.py": (
            "import os\n\n"
            "def build(flag):\n"
            "    if flag:\n"
            "        return os.getenv('A')\n"
            "    return os.getenv('B')\n\n"
            "def caller(flag):\n"
            "    v = build(flag)\n"
            "    print(v)\n"
        )
    }
    edges = _run_flow(tmp_path, files)
    assert _has(
        edges, "resource::ENV::A", "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )
    assert _has(
        edges, "resource::ENV::B", "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )
