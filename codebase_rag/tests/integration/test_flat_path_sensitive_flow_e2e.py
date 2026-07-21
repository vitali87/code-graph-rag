from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.flow_access import FlowKind

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_FLOWS = cs.RelationshipType.FLOWS_TO.value


def _build(
    ingestor: MemgraphIngestor, tmp_path: Path, filename: str, code: str
) -> None:
    project = tmp_path / "flat_ps"
    project.mkdir()
    (project / filename).write_text(code, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()


def _flows(ingestor: MemgraphIngestor) -> list[dict[str, str | None]]:
    rows = ingestor.fetch_all(
        f"MATCH (a)-[r:{_FLOWS}]->(b) "
        "RETURN a.qualified_name AS frm, b.qualified_name AS to, "
        "r.kind AS kind, r.via AS via"
    )
    return [{k: None if v is None else str(v) for k, v in row.items()} for row in rows]


def _resource_flow(flows: list[dict[str, str | None]], frm: str, to: str) -> bool:
    return any(
        (f["frm"] or "").endswith(frm)
        and (f["to"] or "").endswith(to)
        and f["kind"] == FlowKind.RESOURCE.value
        for f in flows
    )


def _any_resource(flows: list[dict[str, str | None]]) -> bool:
    return any(f["kind"] == FlowKind.RESOURCE.value for f in flows)


# Go


def test_go_kill_on_one_branch_taint_survives(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # secret is killed only inside the if; on the fall-through path it is still the
    # env source, so a MAY analysis keeps ENV -> STDOUT. The flat walk over-kills.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.go",
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func boot(cond bool) {\n"
        '\tsecret := os.Getenv("SECRET")\n'
        "\tif cond {\n"
        '\t\tsecret = "redacted"\n'
        "\t}\n"
        "\tfmt.Println(secret)\n"
        "}\n",
    )
    assert _resource_flow(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_go_kill_on_all_branches_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # secret is reassigned to a clean value on BOTH the then and else paths, so no
    # tainted path reaches the sink -- MAY still yields no flow.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.go",
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func boot(cond bool) {\n"
        '\tsecret := os.Getenv("SECRET")\n'
        "\tif cond {\n"
        '\t\tsecret = "a"\n'
        "\t} else {\n"
        '\t\tsecret = "b"\n'
        "\t}\n"
        "\tfmt.Println(secret)\n"
        "}\n",
    )
    assert not _any_resource(_flows(memgraph_ingestor))


def test_go_branch_local_shadow_does_not_leak(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # `os := load()` is scoped to the if block; after the block the imported package
    # `os` is back in scope, so os.Getenv is the real env source. The flat walk grows
    # its shadow set function-wide and wrongly suppresses the later read.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.go",
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func boot(cond bool) {\n"
        "\tif cond {\n"
        "\t\tos := load()\n"
        "\t\t_ = os\n"
        "\t}\n"
        '\tfmt.Println(os.Getenv("SECRET"))\n'
        "}\n",
    )
    assert _resource_flow(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_go_if_initializer_shadow_does_not_leak(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A Go `if` initializer (`if os := load(); ...`) is scoped to the whole if
    # statement and must not leak past it, so os.Getenv AFTER the if is the real
    # env source. The shadow set must be restored to its pre-if state on exit.
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.go",
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func boot() {\n"
        "\tif os := load(); os != nil {\n"
        "\t\t_ = os\n"
        "\t}\n"
        '\tfmt.Println(os.Getenv("SECRET"))\n'
        "}\n",
    )
    assert _resource_flow(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


# Java


def test_java_kill_on_one_branch_taint_survives(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Java parity: the reassignment inside the if is one path only; the else path
    # still carries the env source, so MAY keeps ENV -> STDOUT.
    _build(
        memgraph_ingestor,
        tmp_path,
        "App.java",
        "class App {\n"
        "    void leak(boolean cond) {\n"
        '        String s = System.getenv("SECRET");\n'
        "        if (cond) {\n"
        '            s = "redacted";\n'
        "        }\n"
        "        System.out.println(s);\n"
        "    }\n"
        "}\n",
    )
    assert _resource_flow(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_java_kill_on_all_branches_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Clean reassignment on both paths -> no tainted path reaches the sink.
    _build(
        memgraph_ingestor,
        tmp_path,
        "App.java",
        "class App {\n"
        "    void leak(boolean cond) {\n"
        '        String s = System.getenv("SECRET");\n'
        "        if (cond) {\n"
        '            s = "a";\n'
        "        } else {\n"
        '            s = "b";\n'
        "        }\n"
        "        System.out.println(s);\n"
        "    }\n"
        "}\n",
    )
    assert not _any_resource(_flows(memgraph_ingestor))
