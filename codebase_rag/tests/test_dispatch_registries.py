# String-keyed dispatch registries (issue #913): a handler registered under a
# string key (module-level dict registry, or a `@flow`/`@task` registrar
# decorator) EXPOSES `resource::DISPATCH::<key>`; a producer scheduling work
# with a recognised dispatch keyword emits a WRITES_TO sink on the same node,
# so both sides meet without a resolution pass. A produced `name/deployment`
# key resolves to the bare registered `name` when no exact registration
# exists.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _audit_recorded_graph

EXPOSES = cs.RelationshipType.EXPOSES.value
WRITES_TO = cs.RelationshipType.WRITES_TO.value
RESOLVES_TO = cs.RelationshipType.RESOLVES_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run(
    tmp_path: Path,
    files: dict[str, str],
    capture: CaptureSelection = _CAPTURE_IO,
) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
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
    # Every fixture run must survive the structural audit: a dangling edge
    # is a defect even when no assertion looks at it (issue #652 gate).
    _audit_recorded_graph(mock)
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in (EXPOSES, WRITES_TO, RESOLVES_TO)
    }


def test_dict_registry_exposes_each_entry(tmp_path: Path) -> None:
    files = {
        "handlers.py": (
            "def plain(ctx):\n    return 1\n\n"
            "def with_factoid(ctx):\n    return 2\n\n"
            "handlers = {\n"
            '    "plain": plain,\n'
            '    "with_factoid": with_factoid,\n'
            "}\n"
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    assert (
        f"{project}.handlers.plain",
        EXPOSES,
        "resource::DISPATCH::plain",
    ) in rels, rels
    assert (
        f"{project}.handlers.with_factoid",
        EXPOSES,
        "resource::DISPATCH::with_factoid",
    ) in rels, rels


def test_annotated_dict_registry_exposes(tmp_path: Path) -> None:
    # The verified production shape carries a type annotation.
    files = {
        "registry.py": (
            "def plain(ctx):\n    return 1\n\n"
            "handlers: dict = {\n"
            '    "plain": plain,\n'
            "}\n"
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    assert (
        f"{project}.registry.plain",
        EXPOSES,
        "resource::DISPATCH::plain",
    ) in rels, rels


def test_mixed_dict_is_not_a_registry(tmp_path: Path) -> None:
    # The all-entries gate: one non-string key or non-function value keeps
    # arbitrary config dicts out entirely.
    files = {
        "config.py": (
            "def plain(ctx):\n    return 1\n\n"
            "settings = {\n"
            '    "plain": plain,\n'
            '    "retries": 3,\n'
            "}\n"
        ),
    }
    rels = _run(tmp_path, files)
    assert not any("resource::DISPATCH::" in b for _a, _r, b in rels), rels


def test_flow_decorator_with_name_exposes(tmp_path: Path) -> None:
    files = {
        "flows.py": (
            "from prefect import flow\n\n"
            '@flow(name="create-datasets")\n'
            "def create_datasets():\n    return 1\n"
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    assert (
        f"{project}.flows.create_datasets",
        EXPOSES,
        "resource::DISPATCH::create-datasets",
    ) in rels, rels


def test_bare_flow_decorator_uses_hyphenated_function_name(tmp_path: Path) -> None:
    # Prefect derives the flow name from the function name, dashes for
    # underscores.
    files = {
        "flows.py": (
            "from prefect import flow\n\n@flow\ndef my_debug_flow():\n    return 1\n"
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    assert (
        f"{project}.flows.my_debug_flow",
        EXPOSES,
        "resource::DISPATCH::my-debug-flow",
    ) in rels, rels


def test_unrelated_decorator_is_not_a_registrar(tmp_path: Path) -> None:
    files = {
        "app.py": (
            "def register(name):\n    return lambda f: f\n\n"
            '@register(name="not-dispatch")\n'
            "def helper():\n    return 1\n"
        ),
    }
    rels = _run(tmp_path, files)
    assert not any("resource::DISPATCH::" in b for _a, _r, b in rels), rels


def test_producer_keyword_literal_emits_sink(tmp_path: Path) -> None:
    files = {
        "producer.py": (
            'def schedule(client):\n    client.deploy(workflow_name="run-things/dev")\n'
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    assert (
        f"{project}.producer.schedule",
        WRITES_TO,
        "resource::DISPATCH::run-things/dev",
    ) in rels, rels


def test_producer_module_constant_resolves(tmp_path: Path) -> None:
    # The verified production shape passes a module-level string constant.
    files = {
        "producer.py": (
            'workflow_name = "run-things/dev"\n\n'
            "def schedule(client):\n"
            "    client.deploy(workflow_name=workflow_name)\n"
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    assert (
        f"{project}.producer.schedule",
        WRITES_TO,
        "resource::DISPATCH::run-things/dev",
    ) in rels, rels


def test_deployment_suffix_resolves_to_bare_registration(tmp_path: Path) -> None:
    # Producer schedules `x/dev`; only `x` is registered: the produced node
    # resolves onto the registered one (unique, no exact registration).
    files = {
        "flows.py": (
            "from prefect import flow\n\n"
            '@flow(name="run-things")\n'
            "def run_things():\n    return 1\n"
        ),
        "producer.py": (
            'def schedule(client):\n    client.deploy(workflow_name="run-things/dev")\n'
        ),
    }
    rels = _run(tmp_path, files)
    assert (
        "resource::DISPATCH::run-things/dev",
        RESOLVES_TO,
        "resource::DISPATCH::run-things",
    ) in rels, rels


def test_dynamic_producer_value_stays_out(tmp_path: Path) -> None:
    files = {
        "producer.py": (
            "def schedule(client, workflow):\n"
            '    client.deploy(workflow_name=f"{workflow}/dev")\n'
        ),
    }
    rels = _run(tmp_path, files)
    assert not any("resource::DISPATCH::" in b for _a, _r, b in rels), rels


def test_imported_handler_values_expose(tmp_path: Path) -> None:
    # The verified production registry imports its handlers from sibling
    # modules; values must resolve through the import map, not just the
    # registry module's own scope.
    files = {
        "pkg/__init__.py": "",
        "pkg/handlers.py": ("def execute_turn(ctx):\n    return 1\n"),
        "pkg/registry.py": (
            "from pkg.handlers import execute_turn\n\n"
            "handlers = {\n"
            '    "execute_turn": execute_turn,\n'
            "}\n"
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    assert (
        f"{project}.pkg.handlers.execute_turn",
        EXPOSES,
        "resource::DISPATCH::execute_turn",
    ) in rels, rels


def test_partial_capture_selections_never_dangle(tmp_path: Path) -> None:
    # Dropping either side's relationship must not leave the suffix
    # resolution with a dangling endpoint (the structural audit inside _run
    # is the assertion).
    files = {
        "flows.py": (
            "from prefect import flow\n\n"
            '@flow(name="run-things")\n'
            "def run_things():\n    return 1\n"
        ),
        "producer.py": (
            'def schedule(client):\n    client.deploy(workflow_name="run-things/dev")\n'
        ),
    }
    for i, tokens in enumerate((["io", "-writes_to"], ["io", "-exposes"])):
        _run(tmp_path / f"case{i}", files, capture=resolve_capture(tokens))
