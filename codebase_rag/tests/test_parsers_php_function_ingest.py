"""`PhpFunctionIngest` names the scope a PHP anonymous class gives a closure
written in its methods, and where the nested-qn walk resumes above it."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parsers.php.function_ingest import PhpFunctionIngest
from codebase_rag.tests.collaborator_support import first, index, parse
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor

_ANONYMOUS = (
    "<?php\nfunction anon() {\n"
    "    $o = new class {\n"
    "        public function run() { $f = function () { return 1; }; }\n"
    "    };\n}\n"
)


def test_an_anonymous_class_is_named_under_the_callable_it_is_written_in() -> None:
    anonymous = first(parse(cs.SupportedLanguage.PHP, _ANONYMOUS), "anonymous_class")
    assert PhpFunctionIngest().anonymous_class_name(anonymous) == "anon.anonymous_2_13"


def test_the_walk_resumes_above_the_callable_the_name_spells() -> None:
    anonymous = first(parse(cs.SupportedLanguage.PHP, _ANONYMOUS), "anonymous_class")
    stop = PhpFunctionIngest().next_path_ancestor(anonymous)
    # Above `anon`, which the class's name already spells.
    assert stop is not None
    assert stop.type == "program"


def test_any_other_node_resumes_at_its_parent() -> None:
    method = first(
        parse(cs.SupportedLanguage.PHP, _ANONYMOUS), cs.TS_PHP_METHOD_DECLARATION
    )
    assert PhpFunctionIngest().next_path_ancestor(method) == method.parent


def test_a_named_class_gives_no_anonymous_name() -> None:
    named = first(
        parse(cs.SupportedLanguage.PHP, "<?php\nclass Box {}\n"), "class_declaration"
    )
    assert PhpFunctionIngest().anonymous_class_name(named) is None


def test_the_closure_sits_under_the_anonymous_classs_own_methods(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    index(temp_repo / "proj", mock_ingestor, {"src/use.php": _ANONYMOUS})
    methods = {
        str(n.properties[cs.KEY_QUALIFIED_NAME])
        for n in get_nodes(mock_ingestor, cs.NodeLabel.METHOD)
    }
    functions = {
        str(n.properties[cs.KEY_QUALIFIED_NAME])
        for n in get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
    }
    assert "proj.src.use.anon.anonymous_2_13.run" in methods
    assert "proj.src.use.anon.anonymous_2_13.run.anonymous_3_37" in functions
