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


def test_dict_registry_exposes_producer_corroborated_entries(tmp_path: Path) -> None:
    # A `{str: func}` dict registers a handler ONLY for keys a producer
    # actually dispatches to (issue #1241): both keys here are produced.
    files = {
        "handlers.py": (
            "def plain(ctx):\n    return 1\n\n"
            "def with_factoid(ctx):\n    return 2\n\n"
            "handlers = {\n"
            '    "plain": plain,\n'
            '    "with_factoid": with_factoid,\n'
            "}\n"
        ),
        "producer.py": (
            "def schedule(client):\n"
            '    client.deploy(workflow_name="plain")\n'
            '    client.deploy(workflow_name="with_factoid")\n'
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
        "producer.py": (
            'def schedule(client):\n    client.deploy(workflow_name="plain")\n'
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
    # arbitrary config dicts out even when a producer names the key.
    files = {
        "config.py": (
            "def plain(ctx):\n    return 1\n\n"
            "settings = {\n"
            '    "plain": plain,\n'
            '    "retries": 3,\n'
            "}\n"
        ),
        "producer.py": (
            'def schedule(client):\n    client.deploy(workflow_name="plain")\n'
        ),
    }
    rels = _run(tmp_path, files)
    assert not any(EXPOSES == r for _a, r, _b in rels), rels


def test_dict_registry_without_producer_stays_out(tmp_path: Path) -> None:
    # Issue #1241: click's stream tables map string keys to module functions
    # but nothing dispatches to those keys -- they are plain lookup tables, not
    # workflow dispatch, so no DISPATCH resource or EXPOSES edge is emitted.
    files = {
        "_compat.py": (
            "def get_binary_stdin():\n    return 1\n\n"
            "def get_binary_stdout():\n    return 2\n\n"
            "def get_binary_stderr():\n    return 3\n\n"
            "binary_streams = {\n"
            '    "stdin": get_binary_stdin,\n'
            '    "stdout": get_binary_stdout,\n'
            '    "stderr": get_binary_stderr,\n'
            "}\n"
        ),
    }
    rels = _run(tmp_path, files)
    assert not any("resource::DISPATCH::" in b for _a, _r, b in rels), rels


def test_dict_registry_exposes_only_produced_keys(tmp_path: Path) -> None:
    # Selective corroboration (issue #1241): a producer dispatches to only one
    # of the dict's two keys, so only that entry registers; the unproduced key
    # stays out.
    files = {
        "handlers.py": (
            "def used(ctx):\n    return 1\n\n"
            "def unused(ctx):\n    return 2\n\n"
            "handlers = {\n"
            '    "used": used,\n'
            '    "unused": unused,\n'
            "}\n"
        ),
        "producer.py": (
            'def schedule(client):\n    client.deploy(workflow_name="used")\n'
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    assert (
        f"{project}.handlers.used",
        EXPOSES,
        "resource::DISPATCH::used",
    ) in rels, rels
    assert not any("resource::DISPATCH::unused" in b for _a, _r, b in rels), rels


def test_dict_registry_corroborated_by_deployment_suffix_producer(
    tmp_path: Path,
) -> None:
    # A producer dispatching `run-things/dev` corroborates the bare `run-things`
    # dict registration it resolves onto (issue #1241), mirroring the
    # deployment-suffix join.
    files = {
        "handlers.py": (
            "def run_things(ctx):\n    return 1\n\n"
            "handlers = {\n"
            '    "run-things": run_things,\n'
            "}\n"
        ),
        "producer.py": (
            'def schedule(client):\n    client.deploy(workflow_name="run-things/dev")\n'
        ),
    }
    rels = _run(tmp_path, files)
    project = tmp_path.name
    assert (
        f"{project}.handlers.run_things",
        EXPOSES,
        "resource::DISPATCH::run-things",
    ) in rels, rels


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
        "pkg/producer.py": (
            'def schedule(client):\n    client.deploy(workflow_name="execute_turn")\n'
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

    class _Imports:
        import_mapping: dict = {}

    ingestor = MagicMock()
    processor = DispatchRegistryProcessor(
        ingestor=ingestor,
        selection=ALL_ENABLED,
        function_registry=_Registry(),
        import_processor=_Imports(),
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

    class _Imports:
        import_mapping: dict = {}

    ingestor = _QueryIngestor()
    processor = DispatchRegistryProcessor(
        ingestor=ingestor,
        selection=ALL_ENABLED,
        function_registry=_Registry(),
        import_processor=_Imports(),
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


def test_dict_registry_corroborated_by_database_producer(tmp_path: Path) -> None:
    # Incremental runs reprocess only changed files: a dict registry in a
    # changed file whose only producer lives in an untouched file must still be
    # corroborated, seeded from the live graph's WRITES_TO edges (issue #1241).
    from codebase_rag import constants as cs2
    from codebase_rag.capture import ALL_ENABLED
    from codebase_rag.parsers.dispatch_registry import DispatchRegistryProcessor
    from codebase_rag.types_defs import NodeType

    parsers, _ = load_parsers()

    class _QueryIngestor(MagicMock):
        def fetch_all(self, query, params=None):  # noqa: ANN001, ANN201
            # WRITES_TO producers exist only in the DB; EXPOSES query is empty.
            return [{"name": "execute_turn"}] if "WRITES_TO" in query else []

        def execute_write(self, query, params=None):  # noqa: ANN001, ANN201
            return None

    class _Registry:
        def get(self, qn: str):  # noqa: ANN201
            return NodeType.FUNCTION if qn.endswith(".execute_turn") else None

    class _Imports:
        import_mapping: dict = {}

    ingestor = _QueryIngestor()
    processor = DispatchRegistryProcessor(
        ingestor=ingestor,
        selection=ALL_ENABLED,
        function_registry=_Registry(),
        import_processor=_Imports(),
    )
    registry = parsers["python"].parse(
        b'def execute_turn(ctx):\n    return 1\n\nhandlers = {\n    "execute_turn": execute_turn,\n}\n'
    )
    processor.process_file(
        registry.root_node, "proj.registry", cs2.SupportedLanguage.PYTHON
    )
    processor.finalize()
    exposes = [
        c
        for c in ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == EXPOSES
    ]
    assert any(c.args[2][2] == "resource::DISPATCH::execute_turn" for c in exposes), (
        exposes
    )


def test_dict_registry_corroboration_without_query_protocol(tmp_path: Path) -> None:
    # A non-QueryProtocol ingestor cannot seed producers from the graph, so
    # corroboration rests solely on producers found in the processed files
    # (issue #1241): the in-module producer is enough.
    from codebase_rag import constants as cs2
    from codebase_rag.capture import ALL_ENABLED
    from codebase_rag.parsers.dispatch_registry import DispatchRegistryProcessor
    from codebase_rag.types_defs import NodeType, PropertyDict

    parsers, _ = load_parsers()

    class _PlainIngestor:
        # Deliberately lacks fetch_all / execute_write -> not a QueryProtocol.
        def __init__(self) -> None:
            self.rels: list[tuple[object, object, object]] = []

        def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
            return None

        def ensure_relationship_batch(
            self, from_spec: object, rel_type: object, to_spec: object, **_: object
        ) -> None:
            self.rels.append((from_spec, rel_type, to_spec))

        def flush_all(self) -> None:
            return None

    class _Registry:
        def get(self, qn: str):  # noqa: ANN201
            return NodeType.FUNCTION if qn.endswith((".handle", ".schedule")) else None

    class _Imports:
        import_mapping: dict = {}

    ingestor = _PlainIngestor()
    processor = DispatchRegistryProcessor(
        ingestor=ingestor,
        selection=ALL_ENABLED,
        function_registry=_Registry(),
        import_processor=_Imports(),
    )
    registry = parsers["python"].parse(
        b'def handle(ctx):\n    return 1\n\nhandlers = {\n    "handle": handle,\n}\n'
    )
    producer = parsers["python"].parse(
        b'def schedule(client):\n    client.deploy(workflow_name="handle")\n'
    )
    processor.process_file(
        registry.root_node, "proj.registry", cs2.SupportedLanguage.PYTHON
    )
    processor.process_file(
        producer.root_node, "proj.producer", cs2.SupportedLanguage.PYTHON
    )
    processor.finalize()
    assert any(
        str(rel) == EXPOSES and to[2] == "resource::DISPATCH::handle"
        for _frm, rel, to in ingestor.rels
    ), ingestor.rels


def test_dict_corroboration_tolerates_unreadable_graph(tmp_path: Path) -> None:
    # A graph whose every read raises (briefly down mid-rebuild) must not abort
    # finalize: the DB producer seed degrades to nothing and the dict registry
    # simply goes uncorroborated (issue #1241).
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
            return NodeType.FUNCTION if qn.endswith(".handle") else None

    class _Imports:
        import_mapping: dict = {}

    processor = DispatchRegistryProcessor(
        ingestor=_DownIngestor(),
        selection=ALL_ENABLED,
        function_registry=_Registry(),
        import_processor=_Imports(),
    )
    registry = parsers["python"].parse(
        b'def handle(ctx):\n    return 1\n\nhandlers = {\n    "handle": handle,\n}\n'
    )
    processor.process_file(
        registry.root_node, "proj.registry", cs2.SupportedLanguage.PYTHON
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
