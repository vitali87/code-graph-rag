"""`PhpCallResolution`: the class a `new C(...)` constructs and the method a
call on a receiver built by `new C` binds, named through PHP's namespace and
`use` rules, and the qualified name an anonymous class is registered under."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parsers.php.calls import PhpCallResolution, anonymous_class_qn
from codebase_rag.tests.collaborator_support import (
    edges,
    first,
    index,
    nodes_of,
    parse,
)
from evals.recorded_graph import RecordingIngestor

_PHP = cs.SupportedLanguage.PHP

_FILES = {
    "src/Box.php": (
        "<?php\nnamespace App\\Models;\n\nclass Box {\n"
        "    public function __construct() {}\n"
        "    public function open() { return 1; }\n}\n"
    ),
    # A same-named method elsewhere: the typed receiver must pick Box's.
    "src/Lid.php": (
        "<?php\nnamespace App\\Models;\n\nclass Lid {\n"
        "    public function open() { return 2; }\n}\n"
    ),
    "src/use.php": (
        "<?php\nnamespace App\\Http;\n\nuse App\\Models\\Box;\n\n"
        "function make() {\n    $b = new Box();\n    return $b->open();\n}\n\n"
        "function anon() {\n"
        "    $o = new class {\n        public function run() { return 1; }\n    };\n"
        "    return $o->run();\n}\n"
    ),
}


def _indexed(temp_repo: Path, mock_ingestor: RecordingIngestor) -> RecordingIngestor:
    updater = index(temp_repo / "proj", mock_ingestor, _FILES)
    calls = updater.factory.call_processor._calls_for(_PHP)
    assert isinstance(calls, PhpCallResolution)
    return mock_ingestor


def test_new_through_a_use_alias_instantiates_and_runs_the_constructor(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    graph = _indexed(temp_repo, mock_ingestor)
    assert ("proj.src.use.make", "proj.src.Box.Box") in edges(
        graph, cs.RelationshipType.INSTANTIATES
    )
    assert ("proj.src.use.make", "proj.src.Box.Box.__construct") in edges(
        graph, cs.RelationshipType.CALLS
    )


def test_a_receiver_built_by_new_binds_its_classs_method(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    calls = edges(_indexed(temp_repo, mock_ingestor), cs.RelationshipType.CALLS)
    assert ("proj.src.use.make", "proj.src.Box.Box.open") in calls
    assert ("proj.src.use.make", "proj.src.Lid.Lid.open") not in calls


def test_a_call_on_an_anonymous_class_binds_its_registered_method(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    calls = edges(_indexed(temp_repo, mock_ingestor), cs.RelationshipType.CALLS)
    assert ("proj.src.use.anon", "proj.src.use.anon.anonymous_11_13.run") in calls


def test_an_anonymous_class_qn_keeps_the_callables_around_it() -> None:
    root = parse(_PHP, _FILES["src/use.php"])
    anonymous = first(root, "anonymous_class")
    assert anonymous_class_qn(anonymous, "proj.src.use") == (
        "proj.src.use.anon.anonymous_11_13"
    )
    # A named class is the shared builder's to name.
    assert (
        anonymous_class_qn(
            first(parse(_PHP, _FILES["src/Box.php"]), "class_declaration"), "m"
        )
        is None
    )


def test_new_is_named_by_the_classs_simple_name(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "proj", mock_ingestor, {"src/a.php": "<?php\n"})
    calls = updater.factory.call_processor._calls_for(_PHP)
    root = parse(_PHP, "<?php\n$b = new \\App\\Models\\Box();\n$c = new Lid;\nf();\n")
    creations = [
        calls.node_call_name(node)
        for node in nodes_of(root, cs.TS_PHP_OBJECT_CREATION_EXPRESSION)
    ]
    assert creations == ["Box", "Lid"]
    # A plain call is the shared shapes' to name.
    assert calls.node_call_name(first(root, "function_call_expression")) is None
