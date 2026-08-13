# String-keyed dispatch registries (issue #913): a handler registered under a
# string key by a `@flow`/`@task` registrar decorator EXPOSES
# `resource::DISPATCH::<key>`; a producer scheduling work with a recognised
# dispatch keyword emits a WRITES_TO sink on the same node, so both sides meet
# without a resolution pass. A produced `name/deployment` key resolves to the
# bare registered `name` when no exact registration exists. A module-level
# `{str: func}` dict is deliberately NOT a registry (issue #1241): it is an
# ordinary lookup table and cannot be tied to a DISPATCH key by key text alone.
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


def test_dict_is_never_a_dispatch_registry(tmp_path: Path) -> None:
    # Issue #1241: a `{str: func}` dict is an ordinary lookup table, never a
    # dispatch registry. It is indistinguishable from click's stream tables,
    # command maps or plugin registries, and a DISPATCH resource keyed only by a
    # bare string cannot be tied back to a specific dict. Even when an unrelated
    # producer dispatches a colliding key, the dict must NOT expose a handler --
    # only the producer's own WRITES_TO edge is emitted. Only explicit
    # `@flow`/`@task` decorators register handlers.
    files = {
        "_compat.py": (
            "def get_binary_stdin():\n    return 1\n\n"
            "def get_binary_stdout():\n    return 2\n\n"
            "binary_streams = {\n"
            '    "stdin": get_binary_stdin,\n'
            '    "stdout": get_binary_stdout,\n'
            "}\n"
        ),
        # An unrelated producer that happens to dispatch a colliding key must
        # not turn the lookup table above into a registry (the cross-module
        # collision that key-text corroboration would wrongly join).
        "producer.py": (
            'def schedule(client):\n    client.deploy(workflow_name="stdin")\n'
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    # The producer still writes to its own dispatch node...
    assert (
        f"{project}.producer.schedule",
        WRITES_TO,
        "resource::DISPATCH::stdin",
    ) in rels, rels
    # ...but the dict never exposes any handler.
    assert not any(EXPOSES == r for _a, r, _b in rels), rels


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


def test_escaped_key_joins_plain_spelling(tmp_path: Path) -> None:
    # `"run-things\x2fdev"` and `"run-things/dev"` are the same runtime
    # string; the resource identity must use the decoded value.
    files = {
        "producer.py": (
            "def schedule(client):\n"
            '    client.deploy(workflow_name="run-things\\x2fdev")\n'
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    assert (
        f"{project}.producer.schedule",
        WRITES_TO,
        "resource::DISPATCH::run-things/dev",
    ) in rels, rels


def test_reprocessed_module_drops_stale_facts(tmp_path: Path) -> None:
    # Watch-mode re-parse of a module must replace its recorded facts: a
    # removed registration may not replay from stale state at finalize.
    from codebase_rag import constants as cs2
    from codebase_rag.capture import ALL_ENABLED
    from codebase_rag.parsers.dispatch_registry import DispatchRegistryProcessor

    parsers, _ = load_parsers()

    class _Registry:
        def get(self, qn: str):  # noqa: ANN201
            from codebase_rag.types_defs import NodeType

            return NodeType.FUNCTION if qn.endswith(".run_things") else None

    ingestor = MagicMock()
    processor = DispatchRegistryProcessor(
        ingestor=ingestor,
        selection=ALL_ENABLED,
        function_registry=_Registry(),
    )
    with_flow = parsers["python"].parse(
        b'from prefect import flow\n\n@flow(name="run-things")\ndef run_things():\n    return 1\n'
    )
    without_flow = parsers["python"].parse(b"def run_things():\n    return 1\n")
    processor.process_file(
        with_flow.root_node, "proj.flows", cs2.SupportedLanguage.PYTHON
    )
    processor.process_file(
        without_flow.root_node, "proj.flows", cs2.SupportedLanguage.PYTHON
    )
    ingestor.reset_mock()
    producer = parsers["python"].parse(
        b'def schedule(client):\n    client.deploy(workflow_name="run-things/dev")\n'
    )
    processor.process_file(
        producer.root_node, "proj.producer", cs2.SupportedLanguage.PYTHON
    )
    processor.finalize()
    resolves = [
        c
        for c in ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == RESOLVES_TO
    ]
    assert not resolves, resolves


def test_finalize_seeds_registrations_from_database(tmp_path: Path) -> None:
    # Incremental runs reprocess only changed files: a registration living in
    # an unchanged file must still anchor suffix resolution, seeded from the
    # live graph.
    from codebase_rag import constants as cs2
    from codebase_rag.capture import ALL_ENABLED
    from codebase_rag.parsers.dispatch_registry import DispatchRegistryProcessor

    parsers, _ = load_parsers()

    class _QueryIngestor(MagicMock):
        def fetch_all(self, query, params=None):  # noqa: ANN001, ANN201
            return [{"name": "run-things"}]

        def execute_write(self, query, params=None):  # noqa: ANN001, ANN201
            return None

    class _Registry:
        def get(self, qn: str):  # noqa: ANN201
            from codebase_rag.types_defs import NodeType

            return NodeType.FUNCTION if qn.endswith(".schedule") else None

    ingestor = _QueryIngestor()
    processor = DispatchRegistryProcessor(
        ingestor=ingestor,
        selection=ALL_ENABLED,
        function_registry=_Registry(),
    )
    producer = parsers["python"].parse(
        b'def schedule(client):\n    client.deploy(workflow_name="run-things/dev")\n'
    )
    processor.process_file(
        producer.root_node, "proj.producer", cs2.SupportedLanguage.PYTHON
    )
    processor.finalize()
    resolves = [
        c
        for c in ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == RESOLVES_TO
    ]
    assert resolves, ingestor.ensure_relationship_batch.call_args_list


def test_registration_seed_tolerates_unreadable_graph(tmp_path: Path) -> None:
    # The suffix-resolution pass seeds registered keys from the live graph. A
    # read failure (the graph briefly down mid-rebuild) must degrade to no seeds
    # rather than aborting finalize -- the rebuild has to complete regardless.
    from codebase_rag import constants as cs2
    from codebase_rag.capture import ALL_ENABLED
    from codebase_rag.parsers.dispatch_registry import DispatchRegistryProcessor
    from codebase_rag.types_defs import NodeType

    parsers, _ = load_parsers()

    class _DownIngestor(MagicMock):
        def fetch_all(self, query, params=None):  # noqa: ANN001, ANN201
            raise RuntimeError("graph down")

        def execute_write(self, query, params=None):  # noqa: ANN001, ANN201
            return None

    class _Registry:
        def get(self, qn: str):  # noqa: ANN201
            return NodeType.FUNCTION if qn.endswith(".schedule") else None

    processor = DispatchRegistryProcessor(
        ingestor=_DownIngestor(),
        selection=ALL_ENABLED,
        function_registry=_Registry(),
    )
    producer = parsers["python"].parse(
        b'def schedule(client):\n    client.deploy(workflow_name="run-things/dev")\n'
    )
    processor.process_file(
        producer.root_node, "proj.producer", cs2.SupportedLanguage.PYTHON
    )
    processor.finalize()  # must not raise


def test_resolves_only_capture_still_links_suffix(tmp_path: Path) -> None:
    # With only RESOLVES_TO enabled, the suffix link (and its endpoint
    # nodes) must still emit; the audit inside _run guards the structure.
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
    rels = _run(
        tmp_path,
        files,
        capture=resolve_capture(["io", "-exposes", "-writes_to"]),
    )
    assert (
        "resource::DISPATCH::run-things/dev",
        RESOLVES_TO,
        "resource::DISPATCH::run-things",
    ) in rels, rels
