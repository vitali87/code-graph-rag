"""`DartFunctionIngest`: a free function's return type, and the `main` a
library declares as the program's entry."""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.parsers.dart.function_ingest import DartFunctionIngest
from codebase_rag.parsers.definition_facts import DefinitionFacts
from codebase_rag.tests.collaborator_support import nodes_of, parse

_DART = cs.SupportedLanguage.DART


def test_only_a_library_level_main_is_the_programs_entry() -> None:
    root = parse(
        _DART,
        "void main() {}\nvoid other() {}\nclass C {\n  void main() {}\n}\n",
    )
    top_main, other, method_main = nodes_of(root, "function_signature")
    ingest = DartFunctionIngest(DefinitionFacts())
    assert ingest.is_program_main(top_main)
    assert not ingest.is_program_main(other)
    assert not ingest.is_program_main(method_main)


def test_a_declared_return_type_is_recorded_under_the_functions_qn() -> None:
    root = parse(_DART, "Point make() => Point();\nvoid run() {}\nmake2() => 1;\n")
    make, run, untyped = nodes_of(root, "function_signature")
    facts = DefinitionFacts()
    ingest = DartFunctionIngest(facts)
    for node, qn in ((make, "dp.a.make"), (run, "dp.a.run"), (untyped, "dp.a.make2")):
        ingest.record_return_type(node, qn)
    # `void` and an omitted type name no class a receiver could be typed by.
    assert facts.method_return_types == {"dp.a.make": "Point"}
